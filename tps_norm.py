"""识别 TrackMan TPS 的 Normalize 开关状态。

为什么要认这个开关：Normalize 打开后 TPS 显示的是**归一化到标准大气条件**的距离
（Carry / Total / Side 等），而设备 WS 推给我们的是**实测原始值**。同一个球员同一杆，
开与关能差 3~5%，不知道口径就会以为看板算错了。

两条检测路径，按可靠性排序：
1. **UIA + PrintWindow** —— 实时。WPF 的 Normalize 按钮没有 TogglePattern，
   只能读它的底色：激活时整块橙色 (236,105,26)。用 PrintWindow 抓窗口位图，
   TPS 被浏览器挡住、最小化到任务栏之外也照样能读。
2. **layout 文件兜底** —— 读 `*.layout` 里的 `<IsNormalizationEnabled>`。
   缺点是 TPS 只在保存布局时（退出 / 切工作区）才写盘，运行中切换不会立刻体现。
"""
import ctypes
import os
import re
import threading
import time
from ctypes import wintypes

TPS_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", ""), "TrackMan", "TrackMan Performance Studio")

# 激活态按钮底色（实测 TPS 10.4：纯 #EC691A）
ORANGE_MIN_R = 140
ORANGE_RG_GAP = 50
DARK_MAX_LUM = 90

_lock = threading.Lock()
_status = {
    "on": None,          # True / False / None(未知)
    "method": None,      # 'uia' | 'layout' | None
    "layout": None,      # 命中的 layout 文件名
    "detail": "",
    "at": 0.0,
}


# ---------------------------------------------------------------- Win32 准备
# 必须和 UIA 用同一套坐标（物理像素），否则 4K/150% 缩放下窗口 rect 和按钮 rect 会差 1.5 倍
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:                                # noqa: BLE001
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:                            # noqa: BLE001
        pass

_u32 = ctypes.windll.user32
_g32 = ctypes.windll.gdi32
_u32.GetWindowDC.restype = wintypes.HDC
_u32.GetWindowDC.argtypes = [wintypes.HWND]
_u32.PrintWindow.restype = wintypes.BOOL
_u32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
_u32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_g32.CreateCompatibleDC.restype = wintypes.HDC
_g32.CreateCompatibleDC.argtypes = [wintypes.HDC]
_g32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
_g32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
_g32.SelectObject.restype = wintypes.HGDIOBJ
_g32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
_g32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
_g32.DeleteDC.argtypes = [wintypes.HDC]
_u32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
_g32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT,
                           wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
_u32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_g32.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                        wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
_g32.BitBlt.restype = wintypes.BOOL

SRCCOPY = 0x00CC0020


class _BMPHDR(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


def _find_tps_hwnd():
    """按窗口标题找 TPS 主窗口。"""
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def cb(h, _):
        n = _u32.GetWindowTextLengthW(h)
        if not n:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        _u32.GetWindowTextW(h, buf, n + 1)
        if buf.value.strip() == "TrackMan Performance Studio":
            found.append(h)
        return True

    _u32.EnumWindows(cb, 0)
    return found[0] if found else None


def _grab_rect(hwnd, rect):
    """抓按钮那一小块：PrintWindow 整窗到内存位图，再 BitBlt 抠出按钮区域。

    为什么不直接 PrintWindow 到小位图：WPF 只认整窗 PrintWindow（传 bitmap DC 会返回 0）。
    为什么用 BitBlt 抠：GetDIBits 只能从行首开始整行取，没法指定列偏移。
    """
    wr = wintypes.RECT()
    _u32.GetWindowRect(hwnd, ctypes.byref(wr))
    W, H = wr.right - wr.left, wr.bottom - wr.top
    if W <= 0 or H <= 0:
        return None

    x0 = rect.left - wr.left
    y0 = rect.top - wr.top
    bw, bh = rect.right - rect.left, rect.bottom - rect.top
    if x0 < 0 or y0 < 0 or x0 + bw > W or y0 + bh > H or bw < 6 or bh < 6:
        return None                      # 坐标越界＝DPI 或窗口状态对不上，宁可判未知

    hdc = _u32.GetWindowDC(hwnd)
    mem = _g32.CreateCompatibleDC(hdc)
    bmp = _g32.CreateCompatibleBitmap(hdc, W, H)
    old = _g32.SelectObject(mem, bmp)
    sdc = sbm = sold = None
    try:
        if not _u32.PrintWindow(hwnd, mem, 2):   # PW_RENDERFULLCONTENT
            return None
        sdc = _g32.CreateCompatibleDC(hdc)
        sbm = _g32.CreateCompatibleBitmap(hdc, bw, bh)
        sold = _g32.SelectObject(sdc, sbm)
        if not _g32.BitBlt(sdc, 0, 0, bw, bh, mem, x0, y0, SRCCOPY):
            return None
        hdr = _BMPHDR()
        hdr.biSize = ctypes.sizeof(hdr)
        hdr.biWidth = bw
        hdr.biHeight = -bh          # 负高 = top-down
        hdr.biPlanes = 1
        hdr.biBitCount = 32
        hdr.biCompression = 0
        buf = ctypes.create_string_buffer(bw * bh * 4)
        got = _g32.GetDIBits(sdc, sbm, 0, bh, buf, ctypes.byref(hdr), 0)
        if got != bh:
            return None
        return buf.raw, bw, bh
    finally:
        if sdc:
            if sold:
                _g32.SelectObject(sdc, sold)
            if sbm:
                _g32.DeleteObject(sbm)
            _g32.DeleteDC(sdc)
        _g32.SelectObject(mem, old)
        _g32.DeleteObject(bmp)
        _g32.DeleteDC(mem)
        _u32.ReleaseDC(hwnd, hdc)


def _sample_button(hwnd, rect):
    """按钮内部取一圈像素，避开正中的白字。返回 [(r,g,b), ...]。"""
    got = _grab_rect(hwnd, rect)
    if not got:
        return None
    raw, bw, bh = got
    xs = [int(bw * f) for f in (0.10, 0.90, 0.10, 0.90, 0.50, 0.10, 0.90)]
    ys = [int(bh * f) for f in (0.10, 0.10, 0.90, 0.90, 0.04, 0.50, 0.50)]
    out = []
    for x, y in zip(xs, ys):
        if 0 <= x < bw and 0 <= y < bh:
            off = (y * bw + x) * 4
            b, g, r = raw[off], raw[off + 1], raw[off + 2]
            out.append((r, g, b))
    return out or None


def _is_orange(rgb):
    r, g, b = rgb
    return r >= ORANGE_MIN_R and (r - g) >= ORANGE_RG_GAP and (g - b) > -30


def _is_dark(rgb):
    r, g, b = rgb
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) <= DARK_MAX_LUM


def _judge(samples):
    """多数票：边缘/描边偶尔有白点，不能让一个点把结论带跑。"""
    if not samples:
        return None
    orange = sum(1 for s in samples if _is_orange(s))
    dark = sum(1 for s in samples if _is_dark(s))
    need = max(2, int(len(samples) * 0.6 + 0.999))   # 六成以上同色才算数
    if orange >= need:
        return True
    if dark >= need:
        return False
    return None       # 灰白等中间色，多半是没抓到按钮


class _Rect:
    __slots__ = ("left", "top", "right", "bottom")

    def __init__(self, l, t, r, b):
        self.left, self.top, self.right, self.bottom = l, t, r, b

    def width(self):
        return self.right - self.left

    def height(self):
        return self.bottom - self.top


# UIA 常量：Name 属性 id / TreeScope_Descendants
_UIA_NAME_ID = 30005
_UIA_DESCENDANTS = 4


def detect_uia():
    """返回 (on, detail)；on 为 None 表示读不到。

    走底层 COM 而不是 uiautomation 的高层 TextControl()：后者每找一次要 0.9s（跨进程逐个比对属性），
    换成一次 FindFirst 只要 0.3s，对 TPS 主线程的打扰小很多。
    """
    try:
        import uiautomation as auto
    except Exception as e:                      # noqa: BLE001
        return None, f"未安装 uiautomation：{e}"

    hwnd = _find_tps_hwnd()
    if not hwnd:
        return None, "没找到 TPS 窗口"

    try:
        iuia = auto.uiautomation._AutomationClient.instance().IUIAutomation
        root = iuia.ElementFromHandle(hwnd)
        cond = iuia.CreatePropertyCondition(_UIA_NAME_ID, "Normalize (N)")
        label = root.FindFirst(_UIA_DESCENDANTS, cond)
        if label is None:
            return None, "窗口里没有 Normalize 按钮（可能不在击球分析界面）"
        btn = iuia.ControlViewWalker.GetParentElement(label)
        rr = btn.CurrentBoundingRectangle
        rect = _Rect(rr.left, rr.top, rr.right, rr.bottom)
        if rect.width() < 10 or rect.height() < 10:
            return None, "按钮尺寸异常"
        samples = _sample_button(hwnd, rect)
        if not samples:
            return None, "PrintWindow 抓图失败"
        on = _judge(samples)
        return on, f"按钮底色 {samples[:3]}"
    except Exception as e:                      # noqa: BLE001
        return None, f"UIA 异常：{str(e)[:90]}"


def detect_layout():
    """兜底：读 layout 文件。返回 (on, layout名, detail)。"""
    try:
        if not os.path.isdir(TPS_DIR):
            return None, None, "找不到 TPS 配置目录"
        states = {}
        for f in os.listdir(TPS_DIR):
            if not f.endswith(".layout"):
                continue
            try:
                s = open(os.path.join(TPS_DIR, f), encoding="utf-8", errors="replace").read()
                m = re.search(r"<IsNormalizationEnabled>(\w+)</IsNormalizationEnabled>", s)
                if m:
                    states[f] = m.group(1).lower() == "true"
            except OSError:
                continue
        if not states:
            return None, None, "layout 里没有归一化字段"

        # 用 Settings.xml 的 LastWorkspace 定位当前工作区
        ws = None
        try:
            sx = open(os.path.join(TPS_DIR, "Settings.xml"), encoding="utf-8", errors="replace").read()
            m = re.search(r"<LastWorkspace>([^<]+)</LastWorkspace>", sx)
            ws = m.group(1).strip() if m else None
        except OSError:
            pass
        if ws:
            for name, val in states.items():
                if name.lower().startswith(ws.lower()):
                    return val, name, f"layout（工作区 {ws}）"
        fixed = [k for k, v in states.items() if v]
        if len(set(states.values())) == 1:
            return next(iter(states.values())), None, "layout（各工作区一致）"
        return None, None, f"各工作区不一致：{[k for k, v in states.items() if v]}"
    except Exception as e:                      # noqa: BLE001
        return None, None, f"读 layout 失败：{str(e)[:80]}"


def detect():
    on, detail = detect_uia()
    if on is not None:
        return {"on": on, "method": "uia", "layout": None, "detail": detail, "at": time.time()}
    on2, layout, detail2 = detect_layout()
    method = "layout" if on2 is not None else None
    return {
        "on": on2, "method": method, "layout": layout,
        "detail": detail2 if on2 is not None else f"{detail}；{detail2}",
        "at": time.time(),
    }


class Watcher:
    """后台每几秒刷一次，读失败就保留上一次的结论但标 stale。"""

    def __init__(self, interval=10.0):
        self.interval = interval
        self._stop = threading.Event()
        self._t = None

    def start(self):
        if self._t and self._t.is_alive():
            return
        self._t = threading.Thread(target=self._loop, daemon=True, name="norm-watch")
        self._t.start()

    def _loop(self):
        while not self._stop.is_set():
            try:
                res = detect()
            except Exception as e:              # noqa: BLE001
                res = {"on": None, "method": None, "layout": None,
                       "detail": f"检测异常：{str(e)[:80]}", "at": time.time()}
            with _lock:
                # 读不到时别把已知状态抹掉，只标记来源失效
                if res["on"] is None and _status["on"] is not None:
                    _status["stale"] = True
                    _status["detail"] = res["detail"]
                else:
                    _status.update(res)
                    _status["stale"] = False
            self._stop.wait(self.interval)

    def stop(self):
        self._stop.set()

    @staticmethod
    def status():
        with _lock:
            return dict(_status)


_watcher = None


def watcher():
    global _watcher
    if _watcher is None:
        _watcher = Watcher()
    return _watcher


if __name__ == "__main__":
    t0 = time.time()
    r = detect()
    print(r, f"{time.time()-t0:.2f}s")
