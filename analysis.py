#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
analysis.py — 把一杆 TrackMan 数据变成「发现了什么 / 有什么问题 / 怎么改」

两层结构，故意分开：
  1. rule_diagnose()  纯规则，零依赖、零网络，按各球杆基准逐项判定。永远可用。
  2. llm_analyze()    把「规则结论 + 原始数据」一起交给大模型，产出教练口吻的完整分析。
                      大模型不负责算数，只负责组织与表达——避免它把 1.29 说成 1.45。

这样即便没配 API key、断网、模型超时，用户也一定能看到有依据的诊断，不会白等。

数值口径：m 的速度/距离为英制（mph / yd / ft），角度/旋转为度/rpm；击球点为 mm。
"""

import base64
import json
import math
import os
import re
import stat
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_FILE = HERE / "ai_config.json"

# ---------------------------------------------------------------- 球杆基准
# 来源：TrackMan 公开的挥杆/飞行基准 + 常见业余到职业区间的中位值。
# smash = 球速/杆头速度；launch 度；spin rpm；attack 度；loft 动态杆面角；spinloft 度
BENCH = {
    "Driver":         dict(smash=1.48, launch=(11, 15), spin=(2200, 2900), attack=(0, 4),   loft=(11, 16), spinloft=(12, 18)),
    "3 Wood":         dict(smash=1.45, launch=(9, 13),  spin=(3200, 4000), attack=(-2, 2),  loft=(10, 15), spinloft=(14, 20)),
    "5 Wood":         dict(smash=1.42, launch=(10, 14), spin=(3800, 4600), attack=(-3, 1),  loft=(12, 17), spinloft=(16, 22)),
    "2 Iron":         dict(smash=1.41, launch=(11, 15), spin=(4400, 5200), attack=(-3, 1),  loft=(16, 21), spinloft=(18, 24)),
    "3 Iron":         dict(smash=1.40, launch=(13, 17), spin=(4800, 5600), attack=(-4, 0),  loft=(18, 23), spinloft=(20, 26)),
    "4 Iron":         dict(smash=1.39, launch=(14, 18), spin=(5200, 6000), attack=(-4, 0),  loft=(20, 25), spinloft=(22, 28)),
    "5 Iron":         dict(smash=1.37, launch=(15, 19), spin=(5600, 6500), attack=(-5, -1), loft=(22, 27), spinloft=(24, 30)),
    "6 Iron":         dict(smash=1.36, launch=(16, 20), spin=(6100, 7000), attack=(-5, -1), loft=(25, 30), spinloft=(26, 32)),
    "7 Iron":         dict(smash=1.35, launch=(17, 21), spin=(6600, 7500), attack=(-5, -1), loft=(28, 33), spinloft=(28, 34)),
    "8 Iron":         dict(smash=1.33, launch=(19, 24), spin=(7200, 8200), attack=(-6, -2), loft=(31, 36), spinloft=(30, 36)),
    "9 Iron":         dict(smash=1.31, launch=(21, 26), spin=(7800, 9000), attack=(-6, -2), loft=(34, 39), spinloft=(32, 38)),
    "Pitching Wedge": dict(smash=1.29, launch=(24, 29), spin=(8500, 9800), attack=(-6, -2), loft=(38, 44), spinloft=(34, 40)),
    "Gap Wedge":      dict(smash=1.27, launch=(25, 30), spin=(9000, 10500), attack=(-6, -2), loft=(42, 48), spinloft=(36, 42)),
    "Sand Wedge":     dict(smash=1.25, launch=(27, 33), spin=(9500, 11000), attack=(-7, -3), loft=(46, 53), spinloft=(38, 45)),
    "Lob Wedge":      dict(smash=1.23, launch=(30, 36), spin=(9500, 11500), attack=(-7, -3), loft=(52, 60), spinloft=(40, 48)),
}

CLUB_ORDER = list(BENCH.keys())

CLUB_ZH = {
    "Driver": "一号木", "3 Wood": "3号木", "5 Wood": "5号木",
    "2 Iron": "2号铁", "3 Iron": "3号铁", "4 Iron": "4号铁",
    "5 Iron": "5号铁", "6 Iron": "6号铁", "7 Iron": "7号铁",
    "8 Iron": "8号铁", "9 Iron": "9号铁",
    "Pitching Wedge": "P杆", "Gap Wedge": "G杆",
    "Sand Wedge": "S杆", "Lob Wedge": "L杆",
}


def club_zh(name):
    return CLUB_ZH.get(name, name or "—")


def guess_club(m):
    """按球速推断球杆（payload 里通常没有球杆字段，只能猜，用户可覆盖）。"""
    bs = m.get("ballSpeed")
    if not isinstance(bs, (int, float)):
        return None
    for name, lo in (("Driver", 150), ("3 Wood", 138), ("5 Wood", 128),
                     ("4 Iron", 120), ("5 Iron", 113), ("6 Iron", 106),
                     ("7 Iron", 98), ("8 Iron", 90), ("9 Iron", 82),
                     ("Pitching Wedge", 72), ("Gap Wedge", 62)):
        if bs >= lo:
            return name
    return "Sand Wedge"


_ALIAS = {
    "driver": "Driver", "1w": "Driver", "一号木": "Driver",
    "3w": "3 Wood", "三号木": "3 Wood",
    "5w": "5 Wood", "五号木": "5 Wood",
    "2i": "2 Iron", "3i": "3 Iron", "4i": "4 Iron", "5i": "5 Iron",
    "6i": "6 Iron", "7i": "7 Iron", "8i": "8 Iron", "9i": "9 Iron",
    "pw": "Pitching Wedge", "gw": "Gap Wedge", "aw": "Gap Wedge",
    "sw": "Sand Wedge", "lw": "Lob Wedge",
    "p杆": "Pitching Wedge", "g杆": "Gap Wedge",
    "s杆": "Sand Wedge", "l杆": "Lob Wedge",
}


def _match_club(s):
    t = str(s or "").strip().lower()
    if not t:
        return None
    if t in _ALIAS:
        return _ALIAS[t]
    for k in CLUB_ORDER:
        if k.lower() == t:
            return k
    if re.search(r"pitching|wedge\s*p|\bpw\b", t):
        return "Pitching Wedge"
    if re.search(r"gap\s*wedge|\bgw\b|\baw\b", t):
        return "Gap Wedge"
    if re.search(r"sand\s*wedge|\bsw\b", t):
        return "Sand Wedge"
    if re.search(r"lob\s*wedge|\blw\b", t):
        return "Lob Wedge"
    if re.search(r"driver", t):
        return "Driver"
    mt = re.search(r"([2-9])\s*(?:iron|\bi\b|号铁)", t)
    if mt:
        return f"{mt.group(1)} Iron"
    return None


def resolve_club(m, override=None):
    """决定用哪个球杆做基准：用户指定 > payload 自带 > 按球速推断。"""
    if override and override in BENCH:
        return override, False
    hit = _match_club(m.get("club"))
    if hit:
        return hit, False
    return (guess_club(m) or "7 Iron"), True


def _fmt(v, d=1):
    return f"{v:.{d}f}" if isinstance(v, (int, float)) else "—"


def _band(v, lo, hi):
    """返回 -1 偏低 / 0 正常 / 1 偏高"""
    if not isinstance(v, (int, float)):
        return None
    if v < lo:
        return -1
    if v > hi:
        return 1
    return 0


# ---------------------------------------------------------------- 规则诊断

def rule_diagnose(m, club=None):
    """逐项对照基准，产出结构化发现。不联网。"""
    club = club or guess_club(m) or "7 Iron"
    b = BENCH.get(club, BENCH["7 Iron"])
    club_disp = club_zh(club)
    out = []

    def add(level, area, title, detail, metric=""):
        out.append(dict(level=level, area=area, title=title, detail=detail, metric=metric))

    # --- 1. 能量传递效率（smash factor）---
    sm = m.get("smash")
    if isinstance(sm, (int, float)):
        base = b["smash"]
        gap = sm - base
        metric = f"效率 {sm:.2f} / {club_disp} 基准 {base:.2f}"
        if gap >= -0.02:
            add("good", "能量传递", "击球扎实，能量传递到位",
                f"球速与杆头速度的比值达到 {sm:.2f}，接近甚至超过 {club_disp}的基准值 {base:.2f}，"
                f"说明击球点靠近甜点、杆面控制稳定。", metric)
        elif gap >= -0.06:
            add("warn", "能量传递", "能量有轻微损失",
                f"效率 {sm:.2f} 比 {club_disp} 基准 {base:.2f} 低 {abs(gap):.2f}。"
                f"通常意味着击球点偏离甜点 1-2 个纹路，或杆面开合导致部分能量转成侧旋。", metric)
        else:
            add("bad", "能量传递", "能量损失明显，优先解决",
                f"效率 {sm:.2f} 比 {club_disp} 基准 {base:.2f} 低 {abs(gap):.2f}，"
                f"换算成球速大约损失 {abs(gap) * (m.get('clubSpeed') or 0):.1f} mph。"
                f"这个量级通常来自击球点明显偏离（跟部/趾部/低点），而不是挥速问题。", metric)

    # --- 2. 杆面与轨迹的关系（曲球根源）---
    ftp = m.get("faceToPath")
    if isinstance(ftp, (int, float)):
        side = "左" if ftp < 0 else "右"
        metric = f"杆面/轨迹差 {ftp:+.1f}°"
        if abs(ftp) <= 2:
            add("good", "杆面控制", "杆面与轨迹基本吻合，球路稳定",
                f"杆面相对挥杆轨迹只差 {ftp:+.1f}°，属于巡回赛级别的一致性。"
                f"这种击球出球方向可预测，是打稳定球路的基础。", metric)
        elif abs(ftp) <= 5:
            add("warn", "杆面控制", f"有明显{side}曲倾向",
                f"杆面相对轨迹{'闭合' if ftp < 0 else '打开'} {abs(ftp):.1f}°，"
                f"每 100 码会产生约 {abs(ftp) * 1.75:.0f} 码的{side}偏。"
                f"不是错误，但需要知道自己习惯的球路，瞄球时提前补偿。", metric)
        else:
            add("bad", "杆面控制", f"{side}曲幅度大，球路难以控制",
                f"杆面相对轨迹差 {abs(ftp):.1f}°，远超 5° 的可控范围，"
                f"这意味着球在空中会持续向{side}弯。"
                f"根源通常是握杆偏{'强' if ftp < 0 else '弱'}，或下杆时手腕提前释放/保持。", metric)

    # --- 3. 攻击角 vs 球杆 ---
    aa = m.get("attack")
    if isinstance(aa, (int, float)):
        lo, hi = b["attack"]
        metric = f"攻击角 {aa:+.1f}° / 基准 {lo}~{hi}°"
        is_wood = club in ("Driver", "3 Wood", "5 Wood")
        if lo <= aa <= hi:
            add("good", "触球角度", "攻击角与球杆匹配",
                f"{club_disp}的攻击角基准是 {lo}~{hi}°，你打的是 {aa:+.1f}°，"
                f"说明最低点控制得不错，{'向上击球把球扫起来' if is_wood else '向下挤压的力度合适'}。", metric)
        elif aa < lo:
            add("warn", "触球角度", "下砸偏多，弹道被压低",
                f"{aa:+.1f}° 比 {club_disp} 基准 {lo}~{hi}° 更陡，"
                f"下杆最低点在球前太多。这会{'让球起飞角偏低、后旋偏高' if not is_wood else '造成负攻击角，损失距离'}，"
                f"同时容易打厚。", metric)
        else:
            add("warn", "触球角度", "向上扫得过多，容易打薄",
                f"{aa:+.1f}° 比 {club_disp} 基准 {lo}~{hi}° 更浅，"
                f"最低点在球后，杆头提前到达最低点开始上升，"
                f"{'对木杆是加分项，但再往上就会打薄' if is_wood else '铁杆会打薄、弹道飘、距离不稳定'}。", metric)

    # --- 4. 发射角 + 后旋的组合（弹道质量）---
    la, spin = m.get("launch"), m.get("spin")
    if isinstance(la, (int, float)) and isinstance(spin, (int, float)):
        blo, bhi = b["launch"]
        slo, shi = b["spin"]
        lb, sb = _band(la, blo, bhi), _band(spin, slo, shi)
        metric = f"发射角 {la:.1f}° / 基准 {blo}~{bhi}°，后旋 {spin:.0f} / 基准 {slo}~{shi}"
        if lb == 0 and sb == 0:
            add("good", "弹道组合", "发射角与后旋落在最佳窗口",
                f"{la:.1f}° 配 {spin:.0f} rpm，这是 {club_disp} 距离与停球能力平衡得最好的区间。", metric)
        elif sb == -1:
            ratio = spin / ((slo + shi) / 2)
            low = ratio < 0.7
            add("bad" if low else "warn", "弹道组合",
                "后旋严重不足，球停不住" if low else "后旋偏低，停球能力弱",
                f"后旋 {spin:.0f} rpm，{club_disp}的合理区间是 {slo}~{shi}，约为基准的 {ratio * 100:.0f}%。"
                + ("这种球落地后会大量滚动，攻果岭时无法控距。" if low
                   else "滚动会比预期多，控距需要留余量，打旗杆时要瞄短一点。")
                + "常见原因：动态杆面角偏小（杆身前倾过多）、击球点偏上、"
                  "或球面与草面之间有东西（长草击出的 flyer）。", metric)
        elif sb == 1:
            add("warn", "弹道组合", "后旋偏高，球会“飘”",
                f"后旋 {spin:.0f} rpm 高于 {club_disp} 基准 {slo}~{shi}，"
                f"旋转过多会让球在风中不稳、距离缩短。常见于杆面角偏大或攻球角度过陡。", metric)
        elif lb == -1:
            add("warn", "弹道组合", "发射角偏低，弹道穿透但停不住",
                f"发射角 {la:.1f}° 低于 {club_disp} 基准 {blo}~{bhi}°，"
                f"球会低平穿风，但落地角度小、滚动多。", metric)
        elif lb == 1:
            add("warn", "弹道组合", "发射角偏高，容易“捞球”",
                f"发射角 {la:.1f}° 高于 {club_disp} 基准 {blo}~{bhi}°，"
                f"通常是杆身前倾不足（球位太前或手腕提前松）导致动态杆面角偏大。", metric)

    # --- 5. 动态杆面角 ---
    loft = m.get("loft")
    if isinstance(loft, (int, float)):
        lo, hi = b["loft"]
        metric = f"动态杆面角 {loft:.1f}° / 基准 {lo}~{hi}°"
        if loft < lo - 1:
            add("warn", "杆面角", "动态杆面角偏小，杆身前倾过多",
                f"{loft:.1f}° 低于 {club_disp} 基准 {lo}~{hi}°。杆身前倾能压低弹道、增加距离，"
                f"但过头会把球打成低平直线，同时牺牲后旋。", metric)
        elif loft > hi + 1:
            add("warn", "杆面角", "动态杆面角偏大，有“加 loft”动作",
                f"{loft:.1f}° 高于 {club_disp} 基准 {lo}~{hi}°，"
                f"下杆时手腕没有保持前倾，等于把球“捞”起来，距离会掉。", metric)

    # --- 6. 旋转轴（侧旋方向）---
    sa = m.get("spinAxis")
    if isinstance(sa, (int, float)):
        metric = f"旋转轴 {sa:+.1f}°"
        side_txt = ""
        if isinstance(m.get("side"), (int, float)) and abs(m["side"]) >= 0.5:
            side_txt = f"落点侧偏约 {_fmt(abs(m['side']), 1)} yd。"

        # 杆面/轨迹差与旋转轴指向相反时，两个信号打架，不适合下强结论
        conflict = (isinstance(ftp, (int, float)) and abs(ftp) > 2
                    and abs(sa) > 5 and ftp * sa < 0)
        # 面轨差同号且量级相当，"旋转轴"只是同一件事的另一面，不重复报；
        # 如果旋转轴明显比面轨差更严重，仍然单独提示
        dup = (isinstance(ftp, (int, float)) and abs(ftp) > 2 and ftp * sa > 0
               and abs(sa) <= abs(ftp) * 1.8 + 2)
        if dup:
            pass
        elif conflict:
            add("warn", "旋转轴", "杆面关系与旋转轴不一致",
                f"杆面相对轨迹差 {ftp:+.1f}° 指向{'左' if ftp < 0 else '右'}，"
                f"但旋转轴 {sa:+.1f}° 指向{'左' if sa < 0 else '右'}。"
                f"两者通常同向，出现相反说明触球瞬间有额外的杆面翻转或击球点偏移，"
                f"建议先看清球路到底是往哪边弯，再判断原因。", metric)
        elif abs(sa) <= 5:
            add("good", "旋转轴", "旋转轴接近垂直，弹道笔直",
                f"旋转轴偏 {sa:+.1f}°，球基本沿目标线飞行。", metric)
        elif abs(sa) <= 12:
            add("warn", "旋转轴", f"有中等{'左' if sa < 0 else '右'}曲",
                f"旋转轴偏 {sa:+.1f}°，球在空中会稳定地向{'左' if sa < 0 else '右'}弯。{side_txt}", metric)
        else:
            add("bad", "旋转轴", f"旋转轴倾角大，{'左' if sa < 0 else '右'}曲明显",
                f"旋转轴偏 {sa:+.1f}°，超过 12° 后球会持续单向弯曲，"
                f"基本无法用瞄球补偿。必须从杆面与轨迹的关系上解决。{side_txt}", metric)

    # --- 7. 击球点（Impact）---
    ih, io = m.get("impactHeight"), m.get("impactOffset")
    if isinstance(ih, (int, float)) or isinstance(io, (int, float)):
        parts = []
        if isinstance(ih, (int, float)):
            parts.append(f"垂直 {'偏上' if ih > 10 else '偏下' if ih < -10 else '居中'}（{ih:+.1f} mm）")
        if isinstance(io, (int, float)):
            parts.append(f"水平 {'偏趾' if io > 5 else '偏跟' if io < -5 else '居中'}（{io:+.1f} mm）")
        near_center = abs(ih or 0) < 15 and abs(io or 0) < 10
        add("good" if near_center else "warn",
            "击球点", "击球点" + ("在甜点附近" if near_center else "偏离甜点"),
            "；".join(parts) + "。" + ("这是效率能上去的直接原因。" if near_center else "击球点每偏离甜点 1 个纹路，球速大约掉 2-3 mph。"))

    return dict(club=club, findings=out, bench=b)


# ---------------------------------------------------------------- 规则版结论

def _rule_result(diag, m):
    """没有大模型时，用规则结果拼出同结构的分析。"""
    cz = club_zh(diag["club"])
    good = [f for f in diag["findings"] if f["level"] == "good"]
    bad = [f for f in diag["findings"] if f["level"] == "bad"]
    warn = [f for f in diag["findings"] if f["level"] == "warn"]

    if bad:
        summary = f"这一杆的主要短板是{bad[0]['area']}——{bad[0]['title']}。先解决它，其余问题多半会跟着好转。"
    elif warn:
        summary = f"整体结构不差，最值得改的是{warn[0]['area']}：{warn[0]['title']}。"
    else:
        summary = "这一杆各项指标都在基准区间内，属于可以直接照抄的动作。"

    drills = []
    for f in (bad + warn)[:2]:
        area = f["area"]
        if area == "能量传递":
            drills.append(dict(name="甜点贴纸训练", why="把注意力从“用力”转到“打准”",
                               how=f"在杆面贴一张打击贴纸，用 {cz} 打 20 个球，只记录球印位置，不看距离。目标：10 个球里 7 个落在中心"))
        elif area == "杆面控制":
            drills.append(dict(name="半挥杆定杆面", why="减速才能感知杆面朝向",
                               how=f"用 {cz} 做 9 点到 3 点的半挥，每杆击球后停住保持 2 秒，检查杆面是否指向目标"))
        elif area == "触球角度":
            drills.append(dict(name="球位与重心训练", why="攻击角主要由球位和重心决定",
                               how="在球前后各放一条毛巾，先打不到毛巾再逐渐缩小间距，强制控制最低点位置"))
        elif area == "弹道组合":
            drills.append(dict(name="压低/抬高弹道交替", why="建立对动态杆面角的控制感",
                               how="同一支杆连续打 3 低 3 高，低球用手腕前倾，高球保持杆面角，体会杆身前倾量的差别"))
        elif area == "杆面角":
            drills.append(dict(name="前压手腕练习", why="动态杆面角来自手腕角度",
                               how="上杆顶点时让左手腕保持平直，下杆时想象“用杆头前沿切进球后面”，打 20 球看后旋是否上升"))
        elif area == "旋转轴":
            drills.append(dict(name="门柱训练", why="强制出球方向",
                               how=f"在目标前 3 码插两根杆留 1 码宽的门，用 {cz} 连续穿过，建立杆面回正的体感"))
        if len(drills) >= 2:
            break

    if not drills:
        drills.append(dict(name="保持并加压测试", why="当前动作已经稳定，需要验证抗压性",
                           how="换不同目标距离交替击球，每杆换一个目标，检验这套动作能否复现"))

    return dict(
        summary=summary,
        highlights=[dict(title=f["title"], detail=f["detail"]) for f in good[:3]],
        issues=[dict(severity={"bad": "high", "warn": "mid", "good": "low"}[f["level"]],
                     title=f["title"], detail=f["detail"], evidence=f["metric"])
                for f in (bad + warn)[:4]],
        drills=drills,
        source="rules",
    )


# ---------------------------------------------------------------- 大模型

SYSTEM_PROMPT = """你是 TrackMan 认证的高尔夫挥杆教练，有 15 年巡回赛和业余教学经验。
你会收到一杆击球的完整 TrackMan 雷达数据，以及一份已经算好的客观诊断（对照该球杆的标准区间）。

你的任务不是重新算数字，而是像一个真正站在打位旁的教练那样，用中文讲清楚三件事：
1. 这一杆发生了什么（亮点 + 问题的因果关系）
2. 问题的根源在动作的哪个环节
3. 具体怎么练（可执行的训练，不要说"多练习""保持稳定"这类空话）

要求：
- 直接引用数据作为证据，但不要罗列所有数字，只挑支撑结论的那几个
- 说人话，避免堆砌术语；必须用术语时顺带解释一句
- 判断要果断，给明确优先级：先改哪个，后改哪个
- 不要编造数据里没有的信息（比如球杆型号、球员水平、场地条件）
- 如果数据之间互相矛盾（例如发射角偏高但动态杆面角偏小），要指出这个矛盾并说明可能的测量或击球原因

严格输出 JSON，不要任何解释性文字、不要 markdown 代码块，结构如下：
{
  "summary": "一句话总结这一杆，40 字以内，点明最核心的一件事",
  "highlights": [{"title": "标题 10 字内", "detail": "为什么这是亮点，引数据，40-70 字"}],
  "issues": [{"severity": "high|mid|low", "title": "问题标题", "detail": "成因分析，说清动作环节，60-100 字", "evidence": "支撑数据，如 Smash 1.29 / 基准 1.35"}],
  "drills": [{"name": "训练名称", "why": "为什么练这个，30 字内", "how": "具体做法 + 数量 + 判断标准，50-90 字"}]
}
highlights 最多 3 条，issues 按严重度排序最多 3 条，drills 最多 2 个。"""


def _payload_for_prompt(m, diag):
    keys = [
        ("球头速度", "clubSpeed", "mph"), ("球速", "ballSpeed", "mph"),
        ("效率(Smash)", "smash", ""), ("发射角", "launch", "°"),
        ("发射方向", "direction", "°"), ("后旋", "spin", "rpm"),
        ("旋转轴", "spinAxis", "°"), ("杆头轨迹", "clubPath", "°"),
        ("杆面角", "faceAngle", "°"), ("杆面/轨迹差", "faceToPath", "°"),
        ("攻击角", "attack", "°"), ("动态杆面角", "loft", "°"),
        ("落点", "carry", "yd"), ("总距离", "total", "yd"),
        ("最大高度", "height", "ft"), ("滞空", "hang", "s"),
        ("侧向偏移", "side", "yd"), ("曲率", "curve", "yd"),
        ("落地角", "landing", "°"),
        ("击球点高度（正上负下）", "impactHeight", "mm"),
        ("击球点水平偏移（正趾负跟）", "impactOffset", "mm"),
    ]
    lines = []
    for label, k, unit in keys:
        v = m.get(k)
        if isinstance(v, (int, float)):
            d = 0 if k in ("spin",) else 2 if k == "smash" else 1
            lines.append(f"- {label}：{v:.{d}f}{unit}")
    return "\n".join(lines)


def build_prompt(m, diag):
    bench = diag["bench"]
    rules = "\n".join(
        f"- [{f['level']}] {f['area']}：{f['title']}（{f['metric']}）"
        for f in diag["findings"]
    )
    b = (f"效率基准 {bench['smash']}，发射角 {bench['launch'][0]}~{bench['launch'][1]}°，"
         f"后旋 {bench['spin'][0]}~{bench['spin'][1]} rpm，"
         f"攻击角 {bench['attack'][0]}~{bench['attack'][1]}°，"
         f"动态杆面角 {bench['loft'][0]}~{bench['loft'][1]}°")
    guessed = diag.get("club_guessed", resolve_club(m)[1])
    source = diag.get("club_source") or ("guess" if guessed else m.get("club_src") or "payload")
    source_text = {"manual": "用户手动指定", "tps": "TPS 记录",
                   "payload": "击球数据自带", "guess": "球速推测（未确认）"}.get(source, "击球数据自带")
    if guessed:
        club_info = (f"实际球杆：未确认\n推测球杆：{club_zh(diag['club'])}\n"
                     "球杆尚未确认，以下基准仅供参考；不要把推测当作实际球杆。")
    else:
        label = (diag.get("club_label") if source == "manual" else
                 m.get("clubZh") or m.get("clubRaw") or m.get("club") or diag.get("club_label"))
        club_info = f"实际球杆：{label or club_zh(diag['club'])}"
        if source == "tps" and m.get("clubRaw") and m["clubRaw"] != label:
            club_info += f"\nTPS 原始球杆名称：{m['clubRaw']}"
        club_info += "\n不要仅凭球速改判已提供的球杆；分析基准是参考分类，不等于实际倾角或型号完全相同。"
    return f"""{club_info}
球杆来源：{source_text}
分析基准：{diag['club']}（{club_zh(diag['club'])}）

实测数据：
{_payload_for_prompt(m, diag)}

该球杆的基准区间：
{b}

规则引擎的初判（可作为参考，你可以修正或补充）：
{rules}

请输出这一杆的分析 JSON。"""


def _enabled(value):
    return value is True or (isinstance(value, str) and value.strip().lower() in ("true", "1", "yes", "on"))


def load_config():
    cfg = {
        "base_url": os.environ.get("TM_AI_BASE_URL", ""),
        "api_key": os.environ.get("TM_AI_API_KEY", ""),
        "model": os.environ.get("TM_AI_MODEL", ""),
        "vision_base_url": os.environ.get("TM_VISION_BASE_URL", ""),
        "vision_model": os.environ.get("TM_VISION_MODEL", ""),
        "vision_api_key": os.environ.get("TM_VISION_API_KEY", ""),
        "vision_enabled": _enabled(os.environ.get("TM_VISION_ENABLED", "")),
        "include_photos": _enabled(os.environ.get("TM_AI_INCLUDE_PHOTOS", "")),
    }
    if CONFIG_FILE.exists():
        try:
            d = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            for k in cfg:
                if k in ("vision_enabled", "include_photos"):
                    if k in d:
                        cfg[k] = _enabled(d[k])
                elif d.get(k):
                    cfg[k] = d[k]
        except Exception:
            pass
    if not cfg["base_url"]:
        cfg["base_url"] = "https://api.deepseek.com/v1"
    if not cfg["model"]:
        # 2026-07 起 deepseek-chat / deepseek-reasoner 两个旧别名已退役，
        # 现在官方模型是 deepseek-flash 与 deepseek-v4-pro。
        cfg["model"] = "deepseek-flash"
    return cfg


def save_config(cfg):
    # 旧界面只提交文本字段；视觉设置单独保存时也不能清掉文本配置。
    keep = load_config()
    for k in keep:
        if k in cfg:
            keep[k] = _enabled(cfg[k]) if k in ("vision_enabled", "include_photos") else cfg[k]
    CONFIG_FILE.write_text(json.dumps(keep, ensure_ascii=False, indent=2), encoding="utf-8")
    return keep


def _post_json(url, payload, headers, timeout):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    # 本机服务（如 Ollama）必须绕开系统代理，否则会 502
    host = url.split("//", 1)[-1].split("/", 1)[0].split(":")[0]
    if host in ("127.0.0.1", "localhost", "::1"):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _extract_json(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except Exception:
        pass
    s, e = text.find("{"), text.rfind("}")
    if s >= 0 and e > s:
        return json.loads(text[s:e + 1])
    raise ValueError("模型没有返回合法 JSON")


def _is_deepseek(url):
    return "deepseek.com" in (url or "").lower()


def _explain_http(code, detail):
    """把接口错误码翻译成用户能直接照做的话。"""
    if code == 401:
        return "API Key 无效或已过期，请到「AI 设置」里重新填写"
    if code == 402:
        return "账户余额不足，请到 DeepSeek 平台充值后再试"
    if code == 404:
        return f"模型名或接口地址不对（{detail[:120]}）"
    if code == 429:
        return "请求太频繁被限流，稍等几秒再试"
    return f"接口返回 {code}：{detail}"


MAX_VISION_IMAGE_BYTES = 16 * 1024 * 1024  # 八张 JPEG 原始字节总量，不含 base64。

VISION_PROMPT = """
你还会收到同一杆的八张二维 JPEG，编号和阶段严格按 manifest 顺序对应。
阶段、触球标记和校准置信度是输入元数据，不代表照片已经证明这些阶段。
校准 confidence=1 可能表示人工确认来源，不是算法准确率。
触球未经人工确认时只能称为候选，禁止断言精确触球帧或精确触球瞬间。
即使人工确认，也不能从稀疏二维照片计算精确触球时间或精确 3D 关节角。
不得仅凭照片推导精确速度、距离、旋转、角度、击球点或其他测量数据，不得覆盖雷达数值。
summary/highlights/issues/drills 仅按雷达数据和规则组织；视觉结论独立写入 visual_findings。
visual_findings 为数组，每项含 title、detail、evidence_frames（非空整数数组，范围 1..8）、
confidence（0..1 数值）。只描述可见的定性迹象，说明不确定性；看不清就返回空数组，不编造。
"""


def _vision_ready(cfg):
    return cfg.get("vision_enabled") is True and all(
        isinstance(cfg.get(k), str) and cfg[k].strip()
        for k in ("vision_base_url", "vision_model", "vision_api_key")
    )


def _video_revision(video):
    revision = video.get("revision") if isinstance(video, dict) else None
    if isinstance(revision, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", revision):
        return revision
    return None


def _guid(value):
    if not isinstance(value, str):
        raise ValueError("视频杆 ID 缺失或不是 GUID")
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        raise ValueError("视频杆 ID 不是合法 GUID") from None
    if parsed.int == 0:
        raise ValueError("视频杆 ID 不能是空 GUID")
    return parsed


def _checked_frame_path(revision, filename):
    root = HERE / "video_frames"
    directory = root / revision
    path = directory / filename
    for component in (root, directory, path):
        info = component.lstat()
        if (component.is_symlink() or
                getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
            raise ValueError("图片路径不能使用符号链接或重解析点")
    if not root.is_dir() or not directory.is_dir() or not path.is_file():
        raise ValueError("图片路径不是本地普通文件")
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root.resolve(strict=True)) or resolved.parent != directory.resolve(strict=True):
        raise ValueError("图片路径超出当前 revision")
    return path


def _vision_content(m, diag, video):
    if not isinstance(video, dict) or not isinstance(video.get("clip"), dict):
        raise ValueError("视频 manifest 缺少 clip")
    mid = _guid(video["clip"].get("measurement_id"))
    if mid != _guid(video.get("expected_measurement_id")):
        raise ValueError("视频与当前真实杆 ID 不一致")
    for context in (m, video, video["clip"]):
        for key in ("measurement_id", "measurementId"):
            if key in context and _guid(context[key]) != mid:
                raise ValueError("视频 manifest 与实测杆 ID 不一致")
    revision = _video_revision(video)
    if revision is None:
        raise ValueError("视频 revision 不是安全目录名")
    frames = video.get("frames8")
    if not isinstance(frames, list) or len(frames) != 8:
        raise ValueError("视频 manifest 必须有且仅有八张图片")
    calibration = video.get("calibration") or {}
    if not isinstance(calibration, dict):
        raise ValueError("视频校准元数据格式错误")
    confirmed = (calibration.get("impact_manually_confirmed") is True or
                 video.get("impact_manually_confirmed") is True or
                 calibration.get("status") == "manual_confirmed")
    paths, stages, names = [], [], set()
    total = 0
    for index, frame in enumerate(frames, 1):
        if not isinstance(frame, dict):
            raise ValueError("视频帧元数据格式错误")
        for key in ("measurement_id", "measurementId"):
            if key in frame and _guid(frame[key]) != mid:
                raise ValueError("图片与当前真实杆 ID 不一致")
        if "revision" in frame and frame["revision"] != revision:
            raise ValueError("图片 revision 与 manifest 不一致")
        filename = frame.get("file")
        if (not isinstance(filename, str) or
                not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\.(?:jpg|jpeg)", filename, re.I) or
                filename.casefold() in names):
            raise ValueError("图片必须为当前 revision 下八个不同的 JPEG 文件名")
        names.add(filename.casefold())
        path = _checked_frame_path(revision, filename)
        size = path.stat().st_size
        total += size
        if total > MAX_VISION_IMAGE_BYTES:
            raise ValueError("八张 JPEG 总量超过 16 MiB 上限")
        paths.append((path, filename))
        stages.append({
            "frame": index,
            "stage": frame.get("stage") or frame.get("phase") or frame.get("label") or "阶段未确认",
            "status": frame.get("stage_status") or frame.get("status") or "未确认",
            "confidence": frame.get("stage_confidence", frame.get("confidence", "未提供")),
        })
    metadata = {
        "measurement_id": str(mid), "revision": revision,
        "status": video.get("status") or video.get("impact_alignment_status") or "未提供",
        "stages": stages,
        "calibration": {k: calibration[k] for k in ("status", "confidence", "impact_confidence") if k in calibration},
        "impact_manually_confirmed": confirmed,
        "impact_note": "人工确认标记，仍不支持精确三维测量" if confirmed else "触球候选，未经人工确认，禁止断言精确触球",
    }
    content = [{"type": "text", "text": build_prompt(m, diag) +
                "\n同杆归一化实测 m（击球点 mm，禁止覆盖）：\n" + json.dumps(m, ensure_ascii=False) +
                "\n同杆视频阶段/校准元数据：\n" + json.dumps(metadata, ensure_ascii=False)}]
    total = 0
    for path, filename in paths:
        # 开启前后复查，拒绝读取期间被替换的链接/文件；只进行有上限的读取。
        _checked_frame_path(revision, filename)
        before = path.lstat()
        with path.open("rb") as source:
            opened = os.fstat(source.fileno())
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise ValueError("图片读取时发生替换")
            image = source.read(MAX_VISION_IMAGE_BYTES - total + 1)
        _checked_frame_path(revision, filename)
        after = path.lstat()
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError("图片读取时发生变化")
        total += len(image)
        if total > MAX_VISION_IMAGE_BYTES:
            raise ValueError("八张 JPEG 总量超过 16 MiB 上限")
        if not image.startswith(b"\xff\xd8\xff") or not image.endswith(b"\xff\xd9"):
            raise ValueError("图片内容不是 JPEG")
        content.append({"type": "image_url", "image_url": {
            "url": "data:image/jpeg;base64," + base64.b64encode(image).decode("ascii")}})
    return content, confirmed


def _visual_findings(data, confirmed):
    findings = []
    raw = data.get("visual_findings")
    if not isinstance(raw, list):
        return findings
    for item in raw:
        if not isinstance(item, dict):
            continue
        title, detail = item.get("title"), item.get("detail")
        frames, confidence = item.get("evidence_frames"), item.get("confidence")
        if (not isinstance(title, str) or not title.strip() or not isinstance(detail, str) or not detail.strip() or
                not isinstance(frames, list) or not frames or
                any(type(n) is not int or not 1 <= n <= 8 for n in frames) or
                type(confidence) not in (int, float) or not math.isfinite(confidence) or not 0 <= confidence <= 1):
            continue
        text = title + detail
        if re.search(r"\d\s*(?:°|度|rpm\b|mph\b|mm\b|yd\b|ft\b|m/s\b)", text, re.I):
            continue
        if not confirmed and re.search(r"(?:精确|确切|准确)(?:的)?(?:触球|接触)|触球瞬间", text):
            continue
        findings.append(dict(title=title.strip()[:40], detail=detail.strip(),
                             evidence_frames=list(dict.fromkeys(frames)), confidence=confidence))
    return findings[:8]


def _safe_error(error, cfg):
    text = str(error)
    secrets = [cfg.get(k) for k in ("api_key", "vision_api_key")]
    for secret in sorted((s for s in secrets if isinstance(s, str) and s), key=len, reverse=True):
        text = text.replace(secret, "[已隐藏]")
    return text


def llm_analyze(m, diag, cfg=None, timeout=60, video=None, photos_allowed=None):
    cfg = cfg if cfg is not None else load_config()
    vision = video is not None and _vision_ready(cfg)
    opted_out = vision and photos_allowed is not None and not photos_allowed()
    vision = vision and not opted_out
    confirmed = False
    if vision:
        parsed = urllib.parse.urlsplit(cfg["vision_base_url"].strip())
        if (parsed.scheme not in ("http", "https") or not parsed.hostname or
                parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("视觉接口地址必须是无凭据和查询参数的 HTTP(S) base URL")
        content, confirmed = _vision_content(m, diag, video)
        # 八图读取也可能耗时；发送模型请求前再次检查全局撤销许可。
        if photos_allowed is not None and not photos_allowed():
            vision, opted_out = False, True
    if not vision:
        content = build_prompt(m, diag)
    prefix = "vision_" if vision else ""
    if not cfg.get(prefix + "api_key"):
        raise RuntimeError("未配置 API Key")
    base_url = cfg[prefix + "base_url"].strip()
    url = base_url.rstrip("/") + "/chat/completions"
    model = cfg[prefix + "model"].strip()
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT + (VISION_PROMPT if vision else "")},
            {"role": "user", "content": content},
        ],
        "temperature": 0.4,
        "max_tokens": 2000 if vision else 1400,
    }
    if _is_deepseek(url):
        # DeepSeek V4 默认开思考模式（effort=high），一杆分析要多等十几秒、
        # 还按输出 token 计费。这里只需要按给定数据组织语言，直接关掉。
        body["thinking"] = {"type": "disabled"}
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {cfg[prefix + 'api_key']}",
    }
    try:
        resp = _post_json(url, {**body, "response_format": {"type": "json_object"}}, headers, timeout)
    except urllib.error.HTTPError as e:
        if vision:
            # 图片请求绝不因兼容性错误再付费重试，也不回显服务端正文。
            raise RuntimeError(f"视觉接口返回 HTTP {e.code}（未重试）") from None
        detail = _safe_error(e.read().decode("utf-8", "ignore"), cfg)[:300]
        # 普通文本保留既有兼容流程。
        if e.code in (400, 422):
            retry = {k: v for k, v in body.items() if k != "thinking"}
            try:
                resp = _post_json(url, retry, headers, timeout)
            except urllib.error.HTTPError:
                raise RuntimeError(_explain_http(e.code, detail)) from None
            except Exception as retry_error:
                raise RuntimeError(_safe_error(retry_error, cfg)) from None
        else:
            raise RuntimeError(_explain_http(e.code, detail)) from None
    except urllib.error.URLError as e:
        raise RuntimeError("视觉接口连接失败（未重试）" if vision else
                           f"连不上接口：{_safe_error(e.reason, cfg)}") from None
    except Exception as e:
        raise RuntimeError("视觉请求失败（未重试）" if vision else _safe_error(e, cfg)) from None

    text = resp["choices"][0]["message"]["content"]
    data = _extract_json(text)
    out = dict(
        summary=str(data.get("summary") or "").strip(),
        highlights=[dict(title=str(h.get("title", ""))[:40], detail=str(h.get("detail", "")))
                    for h in (data.get("highlights") or [])][:3],
        issues=[dict(severity=h.get("severity", "mid"),
                     title=str(h.get("title", ""))[:40],
                     detail=str(h.get("detail", "")),
                     evidence=str(h.get("evidence", "")))
                for h in (data.get("issues") or [])][:3],
        drills=[dict(name=str(d.get("name", ""))[:40],
                     why=str(d.get("why", "")),
                     how=str(d.get("how", "")))
                for d in (data.get("drills") or [])][:2],
        source="ai",
        model=model,
        input_mode="vision" if vision else "text",
        image_count=8 if vision else 0,
        video_revision=_video_revision(video),
        visual_findings=_visual_findings(data, confirmed) if vision else [],
    )
    if not out["summary"]:
        raise ValueError("模型返回内容为空")
    out["notice"] = ("图片未参与：照片开关关闭" if opted_out else
                     "触球未经人工确认，图片仅作候选；不支持精确触球或 3D 关节角" if vision and not confirmed else
                     "图片未参与：视觉能力未启用" if video is not None and cfg.get("vision_enabled") is not True else
                     "图片未参与：视觉配置缺失" if video is not None and not vision else "")
    return out


def club_label(m, resolved, override=None):
    """给界面看的球杆名：手动指定 > TPS 记录的原名（"56°"）> 基准名（"S杆"）。

    为什么不用 club_zh：56° 挖起杆在基准表里归到 Sand Wedge，直接显示会变成"S杆"，
    用户会以为自己那根 56° 没被认出来。
    """
    if override and override in BENCH:
        return club_zh(override)
    raw = str(m.get("clubZh") or "").strip()
    return raw or club_zh(resolved)


def analyze(m, club=None, use_ai=True, cfg=None, video=None, photos_allowed=None):
    """先规则诊断，再尝试文本或八图片模型；失败不追加付费请求。

    video 是内部可信 manifest，expected_measurement_id 必须由后端填入当前真实杆 GUID。
    frames8 的顺序就是图片证据编号 1..8；文件仅从 video_frames/<revision>/ 读取。
    人工确认来自 calibration.impact_manually_confirmed=True 或 status=manual_confirmed。
    """
    resolved, guessed = resolve_club(m, club)
    diag = rule_diagnose(m, resolved)
    result = _rule_result(diag, m)
    result["club"] = diag["club"]
    result["club_zh"] = club_zh(diag["club"])
    result["club_label"] = club_label(m, diag["club"], club)
    result["club_guessed"] = guessed
    source = "manual" if club in BENCH else "guess" if guessed else m.get("club_src") or "payload"
    result["club_source"] = source
    diag.update(club_label=result["club_label"], club_guessed=guessed, club_source=source)
    result.update(input_mode="rules", image_count=0, video_revision=_video_revision(video),
                  visual_findings=[], model="")
    image_notice = "；图片未参与" if video is not None else ""
    if not use_ai:
        result["notice"] = "未启用 AI，以下为规则诊断" + image_notice
        return result
    cfg = cfg if cfg is not None else load_config()
    if not cfg.get("api_key") and not (video is not None and _vision_ready(cfg)):
        result["notice"] = "未配置 AI Key，以下为规则诊断（在设置里填入 Key 即可启用 AI）" + image_notice
        return result
    try:
        ai = llm_analyze(m, diag, cfg, video=video, photos_allowed=photos_allowed)
        ai["club"] = diag["club"]
        ai["club_zh"] = club_zh(diag["club"])
        ai["club_label"] = result["club_label"]
        ai["club_guessed"] = guessed
        ai["club_source"] = source
        return ai
    except Exception as e:
        result["notice"] = f"AI 调用失败（{_safe_error(e, cfg)}），已降级为规则诊断" + image_notice
        return result


if __name__ == "__main__":
    import sys
    sample = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {
        "clubSpeed": 74.55, "ballSpeed": 96.45, "smash": 1.2937, "launch": 22.417,
        "direction": -0.233, "spin": 3064.73, "spinAxis": -8.202, "clubPath": 3.6,
        "faceAngle": -0.786, "faceToPath": -4.386, "attack": -2.0, "loft": 25.561,
        "carry": 133.5, "total": 149.5, "height": 65.35, "hang": 5.000415,
        "side": -9.59, "curve": -9.05, "landing": 40.16,
    }
    r = analyze(sample, use_ai=False)
    print(json.dumps(r, ensure_ascii=False, indent=2))
