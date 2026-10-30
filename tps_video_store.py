"""本杆 VideoClips 目录及按 (measurement, clip GUID) 隔离的本地缓存。"""
import copy
import json
from pathlib import Path
import queue
import re
import threading
import time
import uuid

import tps_video as video

ROOT = video.HERE / "video_frames"
SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
CAMERA_SOURCE = "TPS VideoClips（本杆录像，非实时连接设备）"


def guid(value):
    parsed = uuid.UUID(str(value))
    if not parsed.int:
        raise ValueError("击球或录像 ID 不能为空")
    return str(parsed)


def safe_file(revision, filename):
    if not SAFE_NAME.fullmatch(str(revision)) or not SAFE_NAME.fullmatch(str(filename)):
        raise ValueError("非法照片路径")
    directory = ROOT / revision
    path = directory / filename
    if not path.resolve().is_relative_to(ROOT.resolve()) or path.resolve().parent != directory.resolve():
        raise ValueError("照片路径越界")
    for component in (ROOT, directory, path):
        if component.is_symlink() or (hasattr(component, "is_junction") and component.is_junction()):
            raise ValueError("照片不能使用链接目录")
        if component.exists() and getattr(component.lstat(), "st_file_attributes", 0) & 0x400:
            raise ValueError("照片不能使用重解析点")
    return path


class VideoStore:
    def __init__(self, notify=None, *, start_worker=True, catalog_ttl=3):
        self.notify = notify or (lambda mid, state: None)
        self.lock = threading.RLock()
        self.entries = {}
        self.active = {}
        self.manual = set()
        self.catalogs = {}
        self.catalog_ttl = catalog_ttl
        self.jobs = queue.Queue(maxsize=24)
        self.preview_lock = threading.Lock()
        if start_worker:
            threading.Thread(target=self._loop, daemon=True, name="video-prepare").start()

    def _catalog(self, mid, force=False):
        catalog = self.catalogs.get(mid)
        if force or catalog is None or time.monotonic() - catalog["at"] >= self.catalog_ttl:
            notice = ""
            try:
                clips = video.supported_clips(mid, video.query_clips(mid))
            except (OSError, RuntimeError, ValueError) as exc:
                clips = []
                notice = "TPS 本杆录像目录暂不可用（" + type(exc).__name__ + "）"
            catalog = self.catalogs[mid] = {"at": time.monotonic(), "clips": clips,
                                          "notice": notice,
                                          "cameras": [video.camera_descriptor(c) for c in clips]}
        return catalog

    def _resolve(self, mid, camera_id=None, *, force=False):
        cid = guid(camera_id) if camera_id is not None else None
        catalog = self._catalog(mid, force)
        # DB 不可用不沿用旧目录/照片，也不把明确选择静默回退到其他摄像头。
        if catalog["notice"]:
            return None
        clips = video.supported_clips(mid, catalog["clips"])
        # TTL 只节省 DB 查询；本地原片被移走时不能继续宣称可用。
        catalog["cameras"] = [video.camera_descriptor(c) for c in clips]
        if cid is None and mid in self.manual:
            current = self.active.get(mid)
            cid = current if any(c["video_clip_id"] == current for c in clips) else None
        return video.select_clip(mid, clips, cid)

    def select(self, mid, camera_id=None):
        """提交 AI 时捕获 clip GUID，只验证/读取目录，不切换 active 或启动提帧。"""
        mid = guid(mid)
        with self.lock:
            clip = self._resolve(mid, camera_id)
            return clip["video_clip_id"] if clip else None

    def current_camera(self, mid):
        with self.lock:
            return self.active.get(guid(mid))

    def _load(self, mid, camera_id):
        if not ROOT.is_dir():
            return None
        selected = next((c for c in self.catalogs[mid]["clips"] if c["video_clip_id"] == camera_id), None)
        if selected is None:
            return None
        paths = list(ROOT.glob(mid + "_*/manifest.json"))
        for path in sorted(paths, key=lambda p: p.stat().st_mtime, reverse=True)[:80]:
            try:
                revision = path.parent.name
                safe_file(revision, "manifest.json")
                if path.stat().st_size > 1024 * 1024:
                    continue
                result = json.loads(path.read_text(encoding="utf-8"))
                if (result.get("schema_version") != 2 or result.get("revision") != revision or
                        guid(result["clip"]["measurement_id"]) != mid or
                        guid(result["clip"].get("stroke_id", mid)) != mid or
                        guid(result["clip"]["video_clip_id"]) != camera_id or
                        len(result.get("frames", [])) != 8):
                    continue
                original = Path(result["clip"]["path"]).resolve()
                if (original != Path(selected["path"]).resolve() or
                        not original.is_relative_to(video.VIDEO_ROOT.resolve()) or not original.is_file()):
                    continue
                if video.sha256(original) != result.get("source_video_sha256"):
                    continue
                if any(not safe_file(revision, f["file"]).is_file() for f in result["frames"]):
                    continue
                return result
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return None

    def _state(self, mid, cid):
        catalog = self.catalogs.get(mid) or {"cameras": [], "notice": ""}
        entry = self.entries.get((mid, cid)) or {}
        camera = next((c for c in catalog["cameras"] if c["id"] == cid), None)
        result = {"status": entry.get("status", "missing"),
                  "notice": entry.get("notice", "") or (catalog["notice"] if cid is None else ""),
                  "camera_source": CAMERA_SOURCE, "cameras": copy.deepcopy(catalog["cameras"]),
                  "camera_id": cid, "camera_key": camera["key"] if camera else None,
                  "camera_auto": mid not in self.manual}
        if cid is None and not result["notice"]:
            result["notice"] = "本杆 VideoClips 没有可用录像（非实时连接设备列表）"
        manifest = entry.get("manifest")
        if manifest:
            result["revision"] = manifest["revision"]
        if result["status"] == "ready" and manifest:
            result.update(frames=copy.deepcopy(manifest["frames"]),
                          calibration=copy.deepcopy(manifest["calibration"]),
                          decoded_frame_count=manifest["decoded_frame_count"],
                          top_detection=copy.deepcopy(manifest.get("top_detection", {})))
        else:
            result["frames"] = []
        return result

    def ensure(self, mid, *, force=False, impact_frame=None, revision=None, camera_id=None, activate=True):
        mid = guid(mid)
        with self.lock:
            clip = self._resolve(mid, camera_id, force=force)
            if clip is None:
                if impact_frame is not None:
                    raise ValueError("本杆录像不可用，不能校准")
                return self._state(mid, None)
            cid = clip["video_clip_id"]
            previous = self.active.get(mid)
            changed = activate and previous is not None and previous != cid
            key = mid, cid
            entry = self.entries.get(key)
            if not entry:
                cached = self._load(mid, cid)
                entry = self.entries[key] = {
                    "status": "ready" if cached else "missing", "manifest": cached,
                    "notice": "", "generation": 0, "event": threading.Event(),
                }
                if cached:
                    entry["event"].set()
            if impact_frame is not None:
                manifest = entry.get("manifest")
                if not manifest or revision != manifest.get("revision") or guid(manifest["clip"]["video_clip_id"]) != cid:
                    raise ValueError("照片版本或摄像头已变化，请刷新后再校准")
                count = manifest["decoded_frame_count"]
                if type(impact_frame) is not int or not 6 <= impact_frame < count - 1:
                    raise ValueError("触球帧超出可用范围（前需六帧、后需一帧）")
            if activate:
                self.active[mid] = cid
                if camera_id is not None:
                    self.manual.add(mid)
            skip = (entry["status"] == "pending" or
                    (entry["status"] == "ready" and not force and impact_frame is None) or
                    (entry["status"] in ("error", "missing") and entry["generation"] and not force))
            queued = False
            if not skip:
                entry["generation"] += 1
                entry["status"], entry["notice"] = "pending", "正在本地校准候选并提取八张照片（不会调用 AI）"
                entry["event"].clear()
                try:
                    old = copy.deepcopy(entry.get("manifest")) if impact_frame is not None else None
                    self.jobs.put_nowait((mid, cid, entry["generation"], impact_frame, copy.deepcopy(clip), old))
                    queued = True
                except queue.Full:
                    entry["status"], entry["notice"] = "error", "照片准备队列已满，请稍后手动重试"
                    entry["event"].set()
            state = self._state(mid, cid)
            if changed:
                state["selection_changed"] = True
        if changed or queued or not skip:
            self.notify(mid, state)
        return state

    def state(self, mid, *, camera_id=None):
        mid = guid(mid)
        with self.lock:
            clip = self._resolve(mid, camera_id)
            return self._state(mid, clip["video_clip_id"] if clip else None)

    def manifest(self, mid, revision=None, *, camera_id=None):
        mid = guid(mid)
        with self.lock:
            clip = self._resolve(mid, camera_id)
            if not clip:
                return None
            cid = clip["video_clip_id"]
            entry = self.entries.get((mid, cid)) or {}
            manifest = entry.get("manifest")
            if manifest and (guid(manifest["clip"]["measurement_id"]) != mid or
                             guid(manifest["clip"]["video_clip_id"]) != cid):
                raise ValueError("照片不属于本杆或所选摄像头")
            if revision is not None and (not manifest or revision != manifest["revision"]):
                raise ValueError("照片版本已过期或不属于所选摄像头")
            if entry.get("status") != "ready" or not manifest:
                return None
            return copy.deepcopy(manifest)

    def wait(self, mid, timeout=90, *, camera_id=None):
        # 即使等待中用户切换 active，本次 camera_id 也不会漂移或切换默认值。
        state = self.ensure(mid, camera_id=camera_id, activate=False)
        cid = state["camera_id"]
        if cid is None:
            return None
        with self.lock:
            event = self.entries[(guid(mid), cid)]["event"]
        event.wait(timeout)
        return self.manifest(mid, camera_id=cid)

    def _loop(self):
        while True:
            job = self.jobs.get()
            if job is None:
                self.jobs.task_done()
                return
            mid, cid, generation, impact, clip, old = job
            try:
                try:
                    if old:
                        if guid(old["clip"]["video_clip_id"]) != cid:
                            raise ValueError("不能跨摄像头沿用校准")
                        if video.sha256(Path(old["clip"]["path"])) != old["source_video_sha256"]:
                            raise ValueError("原录像已变化，不能沿用旧触球校准")
                    manifest = video.prepare_video(mid, impact_frame=impact, confirmed=impact is not None,
                                                   clip=clip, camera_id=cid)
                    if guid(manifest["clip"]["measurement_id"]) != mid or guid(manifest["clip"]["video_clip_id"]) != cid:
                        raise ValueError("提帧返回的录像与提交摄像头不匹配")
                    status, notice = "ready", (
                        "触球帧已人工确认；其余阶段仍为启发式或时间分段候选"
                        if manifest["calibration"]["status"] == "manual_confirmed" else
                        "请点击校准触球帧逐帧确认；顶点和其他阶段为保守启发式候选")
                except Exception as exc:
                    manifest = None
                    status = "missing" if isinstance(exc, FileNotFoundError) else "error"
                    notice = "本地照片准备失败（" + type(exc).__name__ + "）"
                with self.lock:
                    entry = self.entries[(mid, cid)]
                    if entry["generation"] != generation:
                        continue
                    if manifest:
                        entry["manifest"] = manifest
                    entry["status"], entry["notice"] = status, notice
                    entry["event"].set()
                    state = self._state(mid, cid)
                    state["calibration_changed"] = impact is not None and status == "ready"
                self.notify(mid, state)
            finally:
                self.jobs.task_done()

    def image(self, mid, revision, filename, *, camera_id=None):
        manifest = self.manifest(mid, revision, camera_id=camera_id)
        if not manifest:
            raise ValueError("照片尚未就绪")
        allowed = {f["file"] for f in manifest["frames"] + manifest["calibration"]["candidates"]}
        if filename not in allowed:
            raise ValueError("照片不属于当前杆、摄像头或版本")
        path = safe_file(revision, filename)
        if path.suffix.lower() not in (".jpg", ".jpeg") or not path.is_file():
            raise ValueError("照片不可用")
        if path.stat().st_size > 16 * 1024 * 1024:
            raise ValueError("照片过大")
        return path

    def preview(self, mid, revision, index, *, camera_id=None):
        manifest = self.manifest(mid, revision, camera_id=camera_id)
        if not manifest or type(index) is not int or not 0 <= index < manifest["decoded_frame_count"]:
            raise ValueError("照片版本或帧索引无效")
        cid = manifest["clip"]["video_clip_id"]
        for f in manifest["calibration"]["candidates"] + manifest["frames"]:
            if f["frame_index_zero_based"] == index:
                return self.image(mid, revision, f["file"], camera_id=cid)
        destination = safe_file(revision, f"preview_{index:06d}.jpg")
        with self.preview_lock:
            if self.manifest(mid, revision, camera_id=cid) is None:
                raise ValueError("照片版本正在更新，请稍后重试")
            if not destination.is_file():
                original = Path(manifest["clip"]["path"])
                if video.sha256(original) != manifest["source_video_sha256"]:
                    raise ValueError("原录像已变化，请重新准备")
                video.extract_preview(original, video.TPS_FFMPEG, index, destination)
        return destination
