#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读 TPS 视频，生成触球校准候选及八张有序、不重复的阶段照片。

人工确认使用解码帧的真实 PTS；局部运动/肤色辅助仅为保守启发式，
不是球检测或姿态模型。无网络调用、无原片修改，每次输出全新 revision。
旧 extract 接口保留显式时间映射和新目录安全检查。
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET

HERE = Path(__file__).resolve().parent
VIDEO_ROOT = Path(os.environ.get("PROGRAMDATA", "C:/ProgramData")) / "TrackMan/TrackMan Performance Studio/Videos"
TPS_FFMPEG = Path("C:/Program Files/TrackMan Performance Studio/Modules/VmsFiles/ffmpeg.exe")
TICKS_PER_SECOND = 10_000_000
# Real-time offsets; these are candidate windows, not detected positions.
CANDIDATES = [
    ("address", "准备候选", -1.15),
    ("backswing", "上杆前段候选", -0.80),
    ("backswing_late", "上杆后段候选", -0.50),
    ("top", "顶点候选", -0.30),
    ("downswing_early", "下杆前段候选", -0.16),
    ("downswing", "下杆后段候选", -0.04),
    ("impact_mark", "TPS触球标记附近", 0.0),
    ("follow_through", "随挥候选", 0.20),
]


def local_name(element):
    return element.tag.rsplit("}", 1)[-1]


def child(element, name):
    return next((e for e in element if local_name(e) == name), None)


def text(element, name, default=""):
    if element is None:
        return default
    e = child(element, name)
    return (e.text or "").strip() if e is not None else default


def duration_seconds(value):
    match = re.fullmatch(r"PT(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?", value)
    if not match or not any(match.groups()):
        raise ValueError(f"Unsupported impact duration: {value!r}")
    h, m, s = [float(x or 0) for x in match.groups()]
    return h * 3600 + m * 60 + s


def parse_clips(xml, stroke_id, club_type=""):
    root = ET.fromstring(xml)
    measurement = child(root, "Measurement")
    mid = next(((e.text or "").strip() for e in measurement.iter()
                if local_name(e) == "MeasurementId"), "") if measurement is not None else ""
    if str(uuid.UUID(mid)) != str(uuid.UUID(stroke_id)):
        raise ValueError("Stroke.Id and XML MeasurementId do not match")
    container = child(root, "VideoClips")
    clips = []
    for clip in container if container is not None else []:
        metadata = child(clip, "Metadata")
        video_mid = text(metadata, "MeasurementId")
        if not video_mid or str(uuid.UUID(video_mid)) != str(uuid.UUID(mid)):
            continue
        relative = text(clip, "Path")
        path = (VIDEO_ROOT / relative).resolve()
        if not path.is_relative_to(VIDEO_ROOT.resolve()):
            raise ValueError("Video path escapes TPS Videos directory")
        impact = duration_seconds(text(clip, "ImpactTime"))
        ticks = int(text(metadata, "ImpactOffset"))
        if abs(impact - ticks / TICKS_PER_SECOND) > 0.001:
            raise ValueError("ImpactTime and ImpactOffset disagree")
        factor = float(text(metadata, "VideoSpeedFactor", "1"))
        if not math.isfinite(factor) or factor <= 0:
            raise ValueError("Invalid video speed factor")
        stamp_node = child(metadata, "FrameTimeStamps") if metadata is not None else None
        clips.append({
            "measurement_id": mid, "stroke_id": stroke_id,
            "club_type": club_type, "video_clip_id": text(clip, "Id"),
            "path": str(path), "relative_path": relative,
            "camera_name": text(metadata, "CameraName"),
            "camera_angle": text(metadata, "CameraAngle"),
            "status": text(clip, "Status"), "rotation": text(clip, "Rotation", "0"),
            "impact_real_seconds": impact, "impact_offset_ticks": ticks,
            "impact_absolute": text(metadata, "ImpactTime"),
            "video_speed_factor": factor,
            "metadata_fps": float(text(metadata, "VideoFramesPrSecond", "0")),
            "has_explicit_frame_timestamps": stamp_node is not None and bool(list(stamp_node)),
        })
    return clips


def camera_descriptor(clip):
    """只公开本杆 clip 的名称/角度，不把文件名或路径当作硬件身份。"""
    name = str(clip.get("camera_name") or "未知摄像头").strip()
    angle = str(clip.get("camera_angle") or "").strip()
    internal = angle.casefold() == "internal" or name.casefold().startswith("tm4cam_")
    tokens = re.split(r"[^a-z0-9]+", Path(clip.get("relative_path") or clip.get("path", "")).stem.casefold())
    tag = ("OT" if angle.casefold() in ("ot", "other") else
           "DL" if angle.casefold() in ("dl", "downtheline") else
           "OT" if "ot" in tokens else "DL" if "dl" in tokens else angle)
    label = "TrackMan 内置" if internal else name
    return {"id": str(uuid.UUID(str(clip["video_clip_id"]))),
            "label": label + (" · " + tag if tag else ""),
            "key": name + "|" + angle + "|" + tag,
            "is_internal": internal, "available": True}


def supported_clips(measurement_id, clips):
    """集中校验同杆归属和可用性；DL 文件名绝不意味着外接。"""
    mid = str(uuid.UUID(str(measurement_id)))
    result = []
    seen = set()
    for clip in clips:
        try:
            cid = str(uuid.UUID(str(clip["video_clip_id"])))
            if (not uuid.UUID(cid).int or cid in seen or
                    str(uuid.UUID(str(clip["measurement_id"]))) != mid or
                    (clip.get("stroke_id") and str(uuid.UUID(str(clip["stroke_id"]))) != mid) or
                    clip.get("status") != "Success" or str(clip.get("rotation", "0")) not in ("0", "0.0") or
                    not Path(clip["path"]).is_file()):
                continue
            result.append(dict(clip, video_clip_id=cid))
            seen.add(cid)
        except (ValueError, TypeError, KeyError, OSError):
            continue
    return result


def select_clip(measurement_id, clips, camera_id=None):
    clips = supported_clips(measurement_id, clips)
    if camera_id is not None:
        cid = str(uuid.UUID(str(camera_id)))
        selected = next((c for c in clips if c["video_clip_id"] == cid), None)
        if selected is None:
            raise ValueError("摄像头录像不属于本杆或不可用")
        return selected
    def priority(clip):
        descriptor = camera_descriptor(clip)
        tag = descriptor["label"].rsplit(" · ", 1)[-1]
        rank = (2 if descriptor["is_internal"] else
                0 if tag == "OT" else 1 if tag == "DL" else 3)
        return rank, descriptor["key"], descriptor["id"]
    return min(clips, key=priority) if clips else None


def query_clips(measurement_id=None, take=24):
    """SELECT-only, never starts/attaches a database or invokes a write fallback."""
    try:
        import pyodbc
    except ImportError as exc:
        raise RuntimeError("This Python needs existing pyodbc; select another installed interpreter") from exc
    from tps_club import _pipe_names, DB_NAME
    errors = []
    for pipe in _pipe_names():
        cn = None
        try:
            cs = f"DRIVER={{SQL Server}};Server=np:\\\\.\\pipe\\{pipe};Database={DB_NAME};Trusted_Connection=yes;"
            cn = pyodbc.connect(cs, timeout=4, autocommit=True)
            cursor = cn.cursor()
            if measurement_id:
                mid = str(uuid.UUID(measurement_id))
                cursor.execute("SELECT CONVERT(nvarchar(36), Id), ClubType, CAST(Data AS nvarchar(max)) FROM dbo.Strokes WHERE Id=?", mid)
            else:
                cursor.execute(f"SELECT TOP ({int(take)}) CONVERT(nvarchar(36), Id), ClubType, CAST(Data AS nvarchar(max)) FROM dbo.Strokes ORDER BY DateCreated DESC")
            rows = cursor.fetchall()
            return [clip for row in rows for clip in parse_clips(row[2], str(row[0]), str(row[1] or ""))]
        except Exception as exc:
            errors.append(str(exc)[:200])
        finally:
            if cn is not None:
                cn.close()
    raise RuntimeError("Could not read TPS LocalDB: " + "; ".join(errors or ["no active LocalDB pipe"]))


def run_ffmpeg(ffmpeg, *args):
    result = subprocess.run([str(ffmpeg), "-nostdin", "-hide_banner", *map(str, args)],
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    if result.returncode:
        raise RuntimeError(result.stderr[-3000:])
    return result.stderr


def probe_frames(ffmpeg, video):
    # Decode once and retain actual presentation timestamps, rather than n/fps.
    log = run_ffmpeg(ffmpeg, "-copyts", "-i", video, "-map", "0:v:0", "-vf", "showinfo", "-an", "-f", "null", "-")
    rows = re.findall(r"\bn:\s*(\d+)\s+pts:\s*(-?\d+)\s+pts_time:\s*([\d.eE+-]+)", log)
    time_base = re.search(r"config in time_base:\s*(\d+)/(\d+)", log)
    scale = int(time_base[1]) / int(time_base[2]) if time_base else None
    frames = [(int(n), int(pts) * scale if scale is not None else float(seconds))
              for n, pts, seconds in rows]
    if not frames or any(n != i for i, (n, _) in enumerate(frames)):
        raise RuntimeError("Could not read a continuous decoded frame timeline")
    if any(not math.isfinite(p) for _, p in frames) or any(b[1] < a[1] for a, b in zip(frames, frames[1:])):
        raise RuntimeError("Invalid/non-monotonic presentation timestamps")
    return frames


def sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def shot_context(mid):
    # Existing dashboard history is optional and matched by GUID, never by time.
    path = HERE / "dashboard_shots.jsonl"
    if not path.exists():
        return None
    found = None
    with path.open(encoding="utf-8") as source:
        for line in source:
            try:
                rec = json.loads(line)
            except (ValueError, TypeError):
                continue
            if str(rec.get("raw", {}).get("Id", "")).lower() == mid.lower():
                found = {"seq": rec.get("seq"), "metrics": rec.get("m", {}),
                         "source": "dashboard_shots.jsonl", "units": "dashboard normalized (mph/yd/ft/mm/deg/rpm/s)"}
    return found


def contact_sheet(output, frames):
    """Optional local Pillow preview; preserves the unannotated originals."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None
    destination = output / "contact_sheet.jpg"
    if destination.exists():
        raise FileExistsError("Contact sheet already exists")
    font_path = Path("C:/Windows/Fonts/msyh.ttc")
    font = ImageFont.truetype(str(font_path), 20) if font_path.is_file() else ImageFont.load_default()
    columns = 4
    rows = math.ceil(len(frames) / columns)
    canvas = Image.new("RGB", (16 + columns * 366, 64 + rows * 430), "#f4f6f8")
    draw = ImageDraw.Draw(canvas)
    anchor = "人工触球帧锚点" if any(f["stage_status"] == "manual_confirmed" for f in frames) else "自动触球候选锚点"
    draw.text((16, 12), f"{len(frames)}张挥杆候选照片 · {anchor}（非姿态模型）", font=font, fill="#182230")
    for i, frame in enumerate(frames):
        x, y = 16 + (i % columns) * 366, 54 + (i // columns) * 430
        with Image.open(output / frame["file"]) as source:
            image = source.convert("RGB")
            image.thumbnail((350, 350))
            canvas.paste(image, (x + (350 - image.width) // 2, y))
        draw.text((x, y + 356), f"{i + 1:02d} {frame['label']}", font=font, fill="#182230")
        draw.text((x, y + 384), f"视频 {frame['actual_container_seconds']:.3f}s · 帧 {frame['frame_index_zero_based']}", font=font, fill="#48576a")
    with destination.open("xb") as target:
        canvas.save(target, "JPEG", quality=95)
    return destination.name


def extract(clip, ffmpeg, output, offsets, impact_container=None):
    if len(offsets) != len(CANDIDATES) or any(not math.isfinite(x) for x in offsets):
        raise ValueError(f"Exactly {len(CANDIDATES)} finite offsets are required")
    video = Path(clip["path"])
    if not video.is_file() or clip["status"] != "Success":
        raise ValueError("Clip is missing or not marked Success")
    if output.exists():
        raise FileExistsError("Output directory must be new; no files will be overwritten")
    if output.resolve().is_relative_to(VIDEO_ROOT.resolve()) or video.parent == output.resolve():
        raise ValueError("Output must not be in the TPS original-video directory")
    if clip["rotation"] not in ("0", "0.0"):
        raise ValueError("Nonzero TPS clip rotation needs explicit orientation support")
    factor = clip["video_speed_factor"]
    if clip["has_explicit_frame_timestamps"] and impact_container is None:
        raise ValueError("Explicit TPS FrameTimeStamps: automatic slow-motion mapping is not supported")
    impact = clip["impact_real_seconds"] * factor if impact_container is None else impact_container
    if not math.isfinite(impact) or impact < 0:
        raise ValueError("Invalid container impact timestamp")
    before = sha256(video)
    frames = probe_frames(ffmpeg, video)
    if not frames[0][1] <= impact <= frames[-1][1]:
        raise ValueError("Impact mark falls outside decoded video PTS")
    if impact_container is None and abs(frames[0][1]) > 0.001:
        raise ValueError("Nonzero start PTS needs explicit --impact-container mapping")
    chosen = []
    for (name, label, _), offset in zip(CANDIDATES, offsets):
        target = impact + offset * factor
        if not frames[0][1] <= target <= frames[-1][1]:
            raise ValueError(f"Candidate {name} lies outside clip; adjust --offsets")
        index, actual = min(frames, key=lambda item: abs(item[1] - target))
        chosen.append({"name": name, "label": label, "target_container_seconds": target,
                       "frame_index_zero_based": index, "actual_container_seconds": actual,
                       "real_seconds_from_impact_mark": (actual - impact) / factor,
                       "stage_status": "time_sample_not_phase_detection"})
    if len({x["frame_index_zero_based"] for x in chosen}) != len(CANDIDATES):
        raise ValueError("Offsets map to duplicate frames; adjust --offsets")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.mkdir(exist_ok=False)
    for i, frame in enumerate(chosen, 1):
        frame["file"] = f"{i:02d}_{frame['name']}.jpg"
        run_ffmpeg(ffmpeg, "-loglevel", "error", "-n", "-i", video, "-map", "0:v:0",
                   "-vf", f"select=eq(n\\,{frame['frame_index_zero_based']})",
                   "-fps_mode", "vfr", "-frames:v", "1", "-q:v", "2", output / frame["file"])
        if not (output / frame["file"]).is_file():
            raise RuntimeError("Expected image was not generated")
    after = sha256(video)
    if before != after:
        raise RuntimeError("Source video changed during extraction; outputs must not be treated as verified")
    preview = contact_sheet(output, chosen)
    manifest = {
        "contact_sheet": preview,
        "schema_version": 1, "clip": clip, "source_video_sha256": before,
        "source_unchanged_after_extraction": True, "decoded_frame_count": len(frames),
        "first_pts_seconds": frames[0][1], "last_pts_seconds": frames[-1][1],
        "impact_container_seconds": impact,
        "impact_alignment_status": "candidate_mapping_not_exact_contact_verification",
        "mapping": "TPS sample_time / speedFactor = real_time; inverse for this clip" if impact_container is None else "explicit user override",
        "frames": chosen, "shot_context": shot_context(clip["measurement_id"]),
        "limitations": ["Eight impact-relative time samples, not automatic phase recognition.",
                        "TPS impact mark is not a visually verified exact contact frame.",
                        "Sample-clock scaling does not establish upstream impact calibration or MOV ctts/elst corrections.",
                        "Do not infer precise 3D joint angles or club geometry from these 2D photos.",
                        "No images or shot data were sent to an AI service."],
        "timing_evidence": {
            "assembly": "TrackMan.Video.dll", "method_token": "0x06000068",
            "assembly_sha256": "6286c99e192238721a53ff1055f3dcc1d929cf359839689a9b2a52fc366e41dd",
            "mvid": "cba53a12-f72e-457c-92fe-570e289546cb",
            "il_offsets": "IL_033c..IL_0346 (code-relative); sample clock / speedFactor / timescale",
            "rea_parent": "ev_c02e46a26f53c282bd0c16d93fc9a13f0aa9ce78be7df45986e3c3883bbc521b"
        }
    }
    with (output / "manifest.json").open("x", encoding="utf-8") as target:
        json.dump(manifest, target, ensure_ascii=False, indent=2)
    return manifest


def _nearest_frame(frames, seconds, lower=0, upper=None):
    upper = len(frames) - 1 if upper is None else upper
    if lower > upper:
        raise ValueError("视频没有足够的不同帧来保持八阶段顺序")
    return min(range(lower, upper + 1), key=lambda i: abs(frames[i][1] - seconds))


def extract_preview(video, ffmpeg, frame_index, destination):
    """提取从零开始的指定解码帧，返回 Path；不按 FPS 换算、不覆盖目标。

    调用方可用已知帧索引 ±1 逐帧查看；越界/已存在/原片目录目标会报错。
    """
    if isinstance(frame_index, bool) or not isinstance(frame_index, int) or frame_index < 0:
        raise ValueError("frame_index 必须是从零开始的非负整数")
    video, destination = Path(video).resolve(), Path(destination).resolve()
    if not video.is_file():
        raise ValueError("原片不存在")
    if destination.exists():
        raise FileExistsError("校准截图已存在，禁止覆盖")
    if destination.suffix.lower() not in (".jpg", ".jpeg"):
        raise ValueError("destination 必须是 JPG 文件")
    if destination.is_relative_to(VIDEO_ROOT.resolve()) or destination.is_relative_to(video.parent):
        raise ValueError("校准截图不能写入原片目录")
    if not destination.parent.is_dir():
        raise ValueError("校准截图的父目录必须已存在")
    run_ffmpeg(ffmpeg, "-loglevel", "error", "-n", "-copyts", "-i", video,
               "-map", "0:v:0", "-an", "-vf", f"select=eq(n\\,{frame_index})",
               "-fps_mode", "vfr", "-frames:v", "1", "-q:v", "2", destination)
    if not destination.is_file() or destination.stat().st_size == 0:
        raise ValueError("frame_index 超出解码帧范围，未生成截图")
    return destination


def _visual_samples(ffmpeg, video, indices, size=96):
    """只在内存解码有界低分辨率样本，不落盘或向模型发送大批图片。"""
    from PIL import Image
    indices = sorted(set(indices))
    if not indices or len(indices) > 97:
        raise ValueError("局部视觉采样必须为 1..97 帧")
    selection = "+".join(f"eq(n\\,{i})" for i in indices)
    if size not in (96, 320):
        raise ValueError("局部采样分辨率必须是 96 或 320")
    result = subprocess.run(
        [str(ffmpeg), "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(video),
         "-map", "0:v:0", "-an", "-vf",
         f"select={selection},scale={size}:{size}:force_original_aspect_ratio=decrease,"
         f"pad={size}:{size}:(ow-iw)/2:(oh-ih)/2",
         "-fps_mode", "vfr", "-frames:v", str(len(indices)), "-pix_fmt", "rgb24",
         "-f", "rawvideo", "pipe:1"], capture_output=True, timeout=120)
    frame_bytes = size * size * 3
    if result.returncode or len(result.stdout) != len(indices) * frame_bytes:
        raise RuntimeError("局部采样失败: " + result.stderr.decode("utf-8", errors="replace")[-1000:])
    return [(i, Image.frombytes("RGB", (size, size), result.stdout[p * frame_bytes:(p + 1) * frame_bytes]))
            for p, i in enumerate(indices)]


def _ball_disappearance(samples):
    """低分辨率白色小连通域静止后持久消失；仅在绿色地面 ROI 使用。

    鞋子/大白块、短暂遮挡、全局曝光变化、多个可能的球均拒绝。
    消失可能晚于真正触球，不是精确球检测或姿态识别。
    """
    result = {"status": "unavailable", "frame_index_zero_based": None,
              "reason": "未观察到唯一、稳定的小白球持久消失证据", "is_ball_model": False}
    if len(samples) < 7 or any(b[0] != a[0] + 1 for a, b in zip(samples, samples[1:])):
        return result
    from PIL import ImageChops, ImageStat
    size = samples[0][1].width
    if size < 256:
        return result
    pixels = samples[0][1].load()
    white = lambda rgb: min(rgb) >= 190 and max(rgb) - min(rgb) <= 60
    points = {(x, y) for y in range(int(size * .62), int(size * .94))
              for x in range(int(size * .20), int(size * .85)) if white(pixels[x, y])}
    blobs = []
    while points:
        point = points.pop()
        blob, queue = [point], [point]
        while queue:
            x, y = queue.pop()
            for adjacent in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                if adjacent in points:
                    points.remove(adjacent)
                    blob.append(adjacent)
                    queue.append(adjacent)
        xs, ys = zip(*blob)
        width, height = max(xs) - min(xs) + 1, max(ys) - min(ys) + 1
        if not (4 <= len(blob) <= 70 and 2 <= width <= 12 and 2 <= height <= 12
                and .5 <= width / height <= 2 and len(blob) / (width * height) >= .45):
            continue
        cx, cy = round(sum(xs) / len(xs)), round(sum(ys) / len(ys))
        radius = max(width, height) // 2 + 3
        ring = [(x, y) for y in range(cy - radius, cy + radius + 1)
                for x in range(cx - radius, cx + radius + 1)
                if max(abs(x - cx), abs(y - cy)) >= radius - 1]
        r, g, b = [sum(pixels[x, y][c] for x, y in ring) / len(ring) for c in range(3)]
        if not (g > r * 1.08 and g > b * 1.08 and g < 180):
            continue
        blobs.append((cx, cy, blob, ring))
    options = []
    for cx, cy, blob, ring in blobs:
        counts, ring_changes = [], []
        for _, image in samples:
            current = image.load()
            counts.append(sum(white(current[x, y]) for x, y in blob))
            ring_changes.append(sum(sum(abs(a - b) for a, b in zip(current[x, y], pixels[x, y])) / 3
                                    for x, y in ring) / len(ring))
        visible = [count >= max(3, len(blob) * .65) for count in counts]
        absent = [count <= len(blob) * .15 for count in counts]
        if not all(visible[:3]):
            continue
        departure = next((p for p in range(3, len(samples) - 3)
                          if all(visible[:p]) and all(absent[p:])), None)
        if departure is None or max(ring_changes[departure:]) > 18:
            continue
        # 两侧非球区域必须稳定，排除相机移动或全局曝光变化。
        background_change = max(
            ImageStat.Stat(ImageChops.difference(samples[0][1], image).crop(
                (0, int(size * .2), int(size * .12), int(size * .9)))).mean[0]
            for _, image in samples[departure:])
        if background_change > 8:
            continue
        options.append({"frame_index_zero_based": samples[departure][0],
                        "last_visible_frame_index": samples[departure - 1][0],
                        "stationary_frames": departure, "absent_frames": len(samples) - departure,
                        "roi_center_normalized": [round(cx / size, 4), round(cy / size, 4)]})
    if len(options) == 1:
        result.update(options[0], status="heuristic_candidate",
                      reason="绿色地面唯一小白连通域稳定可见后持久消失；首个消失帧仅作触球候选")
    return result


def _calibration_indices(frame_count, center):
    count = min(21, frame_count)
    begin = max(0, min(center - 10, frame_count - count))
    return list(range(begin, begin + count))


def _impact_candidates(frames, seed, factor, samples, manual, fallback_method):
    evidence = (_ball_disappearance(samples) if not manual else
                {"status": "not_attempted", "frame_index_zero_based": None,
                 "reason": "使用人工指定帧，不由视觉候选替换", "is_ball_model": False})
    selected, method, confidence = seed, fallback_method, 0.10
    index = evidence["frame_index_zero_based"]
    if (not manual and index is not None and 6 <= index < len(frames) - 1
            and abs(frames[index][1] - frames[seed][1]) <= .06 * factor):
        selected, method, confidence = index, "small_white_ball_persistent_disappearance_heuristic", 0.30
    elif manual:
        method, confidence = "manual_frame_index", 1.0
    return selected, method, confidence, _calibration_indices(len(frames), selected), evidence


def _top_candidate(frames, impact, factor, samples):
    """活动肤色区域高度回转启发式；不能确立手部身份或识别姿态。"""
    fallback = _nearest_frame(frames, frames[impact][1] - 0.30 * factor, 3, impact - 3)
    result = {"status": "fallback_candidate", "method": "impact_relative_time_window",
              "confidence": 0.15, "frame_index_zero_based": fallback,
              "sampled_frame_count": len(samples), "is_pose_model": False,
              "reason": "活动肤色高度回转证据不足，回退到时间窗口候选"}
    if len(samples) < 11:
        return result
    baseline = samples[0][1].load()
    heights = []
    for index, image in samples:
        pixels = image.load()
        points = []
        for y in range(16, 65):
            for x in range(17, 80):
                r, g, b = pixels[x, y]
                skin = r > 60 and g > 30 and b > 15 and r - g > 8 and r > b and max(r, g, b) - min(r, g, b) > 12
                changed = sum(abs(a - z) for a, z in zip(pixels[x, y], baseline[x, y])) > 55
                if skin and changed:
                    points.append(y / 96)
        heights.append(sum(points) / len(points) if len(points) >= 8 else None)
    valid = sum(y is not None for y in heights)
    result["skin_track_coverage"] = round(valid / len(heights), 3)
    if valid < len(heights) * 0.65:
        return result
    smooth = [None] * len(heights)
    for p in range(2, len(heights) - 2):
        local = [y for y in heights[p - 2:p + 3] if y is not None]
        if len(local) >= 4:
            smooth[p] = sum(local) / len(local)
    options = []
    span = max(3, len(samples) // 12)
    for p in range(span + 2, len(samples) - span - 2):
        index = samples[p][0]
        relative = (frames[index][1] - frames[impact][1]) / factor
        a, center, b = smooth[p - span], smooth[p], smooth[p + span]
        if not 3 <= index <= impact - 3 or not -0.90 <= relative <= -0.10 or None in (a, center, b):
            continue
        rise, fall = a - center, b - center
        # 上升后下降且幅度可见；静态头部、失去跟踪、单向运动不能通过。
        if rise >= 0.018 and fall >= 0.018:
            options.append((min(rise, fall), index))
    if options:
        strength, index = max(options)
        result.update(status="heuristic_candidate", method="moving_skin_height_reversal_heuristic",
                      confidence=round(min(0.45, 0.25 + strength * 2), 3),
                      frame_index_zero_based=index,
                      reason="活动肤色区域高度出现双侧回转；可能混入头部/背景，不是手部或姿态确认")
    return result


def _stage_frames(frames, impact, factor, top, impact_status, impact_confidence):
    """顶点两侧按实际 PTS 分段分配，约束索引而不是事后去重或重排阶段。"""
    top_index = top["frame_index_zero_based"]
    adaptive = top["status"] == "heuristic_candidate"
    anchor, top_pts = frames[impact][1], frames[top_index][1]
    address = _nearest_frame(frames, min(anchor - 1.15 * factor, top_pts - 0.25 * factor), 0, top_index - 3)
    start = frames[address][1]
    early_target = start + (top_pts - start) * .40 if adaptive else anchor - .80 * factor
    late_target = start + (top_pts - start) * .78 if adaptive else anchor - .50 * factor
    early = _nearest_frame(frames, early_target, address + 1, top_index - 2)
    late = _nearest_frame(frames, late_target, early + 1, top_index - 1)
    early_target = top_pts + (anchor - top_pts) * .52 if adaptive else anchor - .16 * factor
    late_target = top_pts + (anchor - top_pts) * .87 if adaptive else anchor - .04 * factor
    down_early = _nearest_frame(frames, early_target, top_index + 1, impact - 2)
    down_late = _nearest_frame(frames, late_target, down_early + 1, impact - 1)
    follow = _nearest_frame(frames, anchor + 0.20 * factor, impact + 1)
    indices = [address, early, late, top_index, down_early, down_late, impact, follow]
    if len(indices) != 8 or any(a >= b for a, b in zip(indices, indices[1:])):
        raise ValueError("八阶段索引必须严格递增且不重复")
    chosen = []
    for p, ((name, label, _), index) in enumerate(zip(CANDIDATES, indices), 1):
        status = "heuristic_phase_candidate" if adaptive else "time_window_candidate"
        confidence = 0.25 if adaptive else 0.15
        if name == "top":
            status, confidence = top["status"], top["confidence"]
        if name == "impact_mark":
            status, confidence = impact_status, impact_confidence
            label = ("触球（人工确认）" if status == "manual_confirmed" else
                     "时间占位（触球不可用）" if status == "unavailable" else "触球候选（待确认）")
        chosen.append({"name": name, "label": label, "file": f"{p:02d}_{name}.jpg",
                       "frame_index_zero_based": index, "actual_container_seconds": frames[index][1],
                       "real_seconds_from_impact_mark": (frames[index][1] - anchor) / factor,
                       "stage_status": status, "confidence": confidence})
    return chosen


def _export_jpegs(ffmpeg, video, output, records):
    # 一个输入解码、多路单帧输出；所有目标都在全新的 revision 中，禁止覆盖。
    args = ["-loglevel", "error", "-n", "-i", video, "-an"]
    for record in records:
        args += ["-map", "0:v:0", "-vf", f"select=eq(n\\,{record['frame_index_zero_based']})",
                 "-fps_mode", "vfr", "-frames:v", "1", "-q:v", "2", output / record["file"]]
    run_ffmpeg(ffmpeg, *args)
    if any(not (output / record["file"]).is_file() or (output / record["file"]).stat().st_size == 0 for record in records):
        raise RuntimeError("没有生成全部阶段图/独立校准候选图")


def prepare_video(measurement_id, *, impact_frame=None, confirmed=False, output_root=None, clip=None, camera_id=None):
    """返回 schema v2 manifest；file 相对于 output_root/revision，帧索引从零开始。

    confirmed=True 必须同时提供有效整数 impact_frame；confidence=1 仅表示
    用户确认的来源，不代表视觉模型精度。仅传 impact_frame 是未确认的候选中心。
    没有安全 TPS 映射时 calibration.status=unavailable，触球字段为 None，
    八图仅作时间候选占位；提供有效帧索引后才建立真实 PTS 锚点。
    每次创建全新 GUID 开头的目录，无缓存命中、原片修改、旧产物删除。
    """
    mid = str(uuid.UUID(str(measurement_id)))
    if not isinstance(confirmed, bool):
        raise ValueError("confirmed 必须是布尔值")
    if impact_frame is not None and (isinstance(impact_frame, bool) or not isinstance(impact_frame, int) or impact_frame < 0):
        raise ValueError("impact_frame 必须是从零开始的非负整数帧索引")
    if confirmed and impact_frame is None:
        raise ValueError("人工确认必须提供 impact_frame")
    if clip is None:
        clip = select_clip(mid, query_clips(mid), camera_id)
        if clip is None:
            raise ValueError("找不到该 measurement 的可用录像")
    clip = dict(clip)
    if camera_id is not None and str(uuid.UUID(str(clip.get("video_clip_id")))) != str(uuid.UUID(str(camera_id))):
        raise ValueError("clip 与 camera_id 不匹配")
    if str(uuid.UUID(str(clip["measurement_id"]))) != mid or (clip.get("stroke_id") and str(uuid.UUID(str(clip["stroke_id"]))) != mid):
        raise ValueError("clip 与 measurement_id/Stroke.Id 不匹配")
    json.dumps(clip, allow_nan=False)
    video = Path(clip["path"]).resolve()
    if not video.is_file() or clip["status"] != "Success":
        raise ValueError("原片不存在或不是 Success 状态")
    if str(clip["rotation"]) not in ("0", "0.0"):
        raise ValueError("非零 TPS 旋转尚未支持")
    factor = float(clip["video_speed_factor"])
    if not math.isfinite(factor) or factor <= 0:
        raise ValueError("无效慢动作倍率")
    root = Path(output_root) if output_root is not None else HERE / "video_frames"
    root = root.resolve()
    if root.is_relative_to(VIDEO_ROOT.resolve()) or root.is_relative_to(video.parent):
        raise ValueError("输出根目录不能位于 TPS 原片目录或原片所在目录内")
    if root.exists() and not root.is_dir():
        raise ValueError("output_root 必须是目录")
    ffmpeg = TPS_FFMPEG if TPS_FFMPEG.is_file() else shutil.which("ffmpeg")
    if not ffmpeg:
        raise ValueError("找不到已有 FFmpeg；不会安装依赖")
    before = sha256(video)
    frames = probe_frames(ffmpeg, video)
    if len(frames) < 8:
        raise ValueError("视频不足八帧")
    available = True
    fallback_method = "supplied_frame_candidate" if impact_frame is not None else "tps_time_candidate"
    if impact_frame is not None:
        if impact_frame >= len(frames):
            raise ValueError("impact_frame 超出解码帧范围")
        seed = impact_frame
        if not 6 <= seed < len(frames) - 1:
            raise ValueError("触球锚点前需至少六帧、后需至少一帧，不能输出八阶段")
    else:
        mark = float(clip["impact_real_seconds"]) * factor
        available = (not clip["has_explicit_frame_timestamps"] and abs(frames[0][1]) <= .001
                     and math.isfinite(mark) and frames[0][1] <= mark <= frames[-1][1])
        seed = _nearest_frame(frames, mark) if available else 6
        available = available and 6 <= seed < len(frames) - 1
        if not available:
            # 仅为浏览提供有序八图，不能把时间占位伪装成触球锚点。
            seed = _nearest_frame(frames, frames[0][1] + (frames[-1][1] - frames[0][1]) * 6 / 7,
                                  6, len(frames) - 2)
            fallback_method = "unavailable_tps_mapping"
    local_indices = list(range(max(0, seed - 16), min(len(frames), seed + 17)))
    visual_errors = []
    local_samples = []
    if available and not confirmed:
        try:
            local_samples = _visual_samples(ffmpeg, video, local_indices, size=320)
        except (ImportError, OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            visual_errors.append("局部视觉辅助不可用，回退到帧/时间候选: " + str(exc)[:200])
    impact, method, confidence, candidate_indices, ball_evidence = _impact_candidates(
        frames, seed, factor, local_samples, confirmed, fallback_method)
    if not available:
        confidence = 0.0
        visual_errors.append("TPS 映射不可用；八图为浏览时间候选，需要提供 impact_frame 校准")
    begin = _nearest_frame(frames, frames[impact][1] - 1.30 * factor, 0, impact - 1)
    end = _nearest_frame(frames, frames[impact][1] - 0.06 * factor, begin, impact - 1)
    top_indices = sorted({round(begin + p * (end - begin) / 96) for p in range(97)})
    try:
        top_samples = _visual_samples(ffmpeg, video, top_indices) if available else []
    except (ImportError, OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        top_samples = []
        visual_errors.append("顶点视觉辅助不可用，回退到时间窗口候选: " + str(exc)[:200])
    top = _top_candidate(frames, impact, factor, top_samples)
    top["actual_container_seconds"] = frames[top["frame_index_zero_based"]][1]
    status = "manual_confirmed" if confirmed else "candidate" if available else "unavailable"
    chosen = _stage_frames(frames, impact, factor, top, status, confidence)
    if not available:
        for frame in chosen:
            frame["real_seconds_from_impact_mark"] = None
            frame["confidence"] = 0.0
            frame["stage_status"] = "unavailable" if frame["name"] == "impact_mark" else "time_window_candidate"
        top["reason"] = "无触球锚点，仅作时间候选占位"
        top["confidence"] = 0.0
    candidates = [{"frame_index_zero_based": index, "actual_container_seconds": frames[index][1],
                   "file": f"calibration_{index:06d}.jpg"} for index in candidate_indices]
    revision = f"{mid}_{uuid.uuid4().hex}_8frames"
    output = root / revision
    if not output.resolve().is_relative_to(root) or output.exists():
        raise FileExistsError("revision 必须是输出根目录内的全新目录")
    root.mkdir(parents=True, exist_ok=True)
    output.mkdir(exist_ok=False)
    if not output.resolve().is_relative_to(root):
        raise ValueError("revision 路径逃逸 output_root")
    _export_jpegs(ffmpeg, video, output, [*chosen, *candidates])
    preview = contact_sheet(output, chosen)
    if sha256(video) != before:
        raise RuntimeError("提帧过程中原片改变，不能把产物标记为已验证")
    manifest = {
        "schema_version": 2, "revision": revision, "contact_sheet": preview,
        "clip": clip, "source_video_sha256": before, "source_unchanged_after_extraction": True,
        "decoded_frame_count": len(frames), "first_pts_seconds": frames[0][1], "last_pts_seconds": frames[-1][1],
        "impact_container_seconds": frames[impact][1] if available else None, "impact_alignment_status": status,
        "mapping": ("decoded frame PTS; manual confirmation" if confirmed else
                    "decoded frame PTS; unconfirmed candidate" if available else "unavailable; time-only browsing samples"),
        "calibration": {"status": status, "impact_frame_index": impact if available else None,
                        "impact_container_seconds": frames[impact][1] if available else None, "method": method,
                        "confidence": confidence, "candidates": candidates,
                        "seed_frame_index": seed if available else None,
                        "ball_detection_performed": bool(local_samples), "ball_evidence": ball_evidence},
        "top_detection": top, "frames": chosen, "shot_context": shot_context(mid),
        "limitations": ["自动触球结果仅为小白球持久消失/时间候选，必须由用户逐帧确认。",
                        "小白球 ROI 为保守启发式而非球检测模型；不可见、不稳定或遮挡时保留 TPS 候选。",
                        "首个消失帧可能晚于触球；显式 TPS 时间戳/非零起点/无效映射标为 unavailable，不能猜测校准。",
                        "顶点采用活动肤色高度回转启发式，不是姿态模型，可能混入头部/背景；失效回退候选。",
                        "其余阶段是顶点两侧的时间分段候选；顶点不稳定时回退八个触球相对时间候选，不是自动动作识别。",
                        "人工 confidence=1 表示用户确认来源，不是算法准确率；人工帧以实际解码 PTS 为锚点。",
                        "不从二维照片推断精确三维关节角度或杆头几何。",
                        "未调用付费模型、AI 服务或网络；未修改原片或删除旧产物。", *visual_errors],
    }
    with (output / "manifest.json").open("x", encoding="utf-8") as target:
        json.dump(manifest, target, ensure_ascii=False, indent=2, allow_nan=False)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--measurement-id", help="Exact TPS/Measurement GUID; omitted = recent matching clip")
    parser.add_argument("--camera", default="iPhone", help="Exact camera name")
    parser.add_argument("--list", action="store_true", help="List metadata only; do not create files")
    parser.add_argument("--output", type=Path, help="New output directory (defaults to project/video_frames/GUID_8frames)")
    parser.add_argument("--ffmpeg", type=Path, help="Existing FFmpeg executable")
    parser.add_argument("--offsets", default=",".join(str(x[2]) for x in CANDIDATES), help="Eight real-time seconds relative to impact, e.g. --offsets=-1.15,-0.8,-0.5,-0.3,-0.16,-0.04,0,0.2")
    parser.add_argument("--impact-container", type=float, help="Explicit container PTS override in seconds")
    args = parser.parse_args()
    try:
        clips = query_clips(args.measurement_id)
        if args.list:
            print(json.dumps(clips, ensure_ascii=False, indent=2))
            return 0
        clip = next((c for c in clips if c["camera_name"] == args.camera and c["status"] == "Success" and Path(c["path"]).is_file()), None)
        if clip is None:
            raise ValueError("No existing successful clip for the selected measurement/camera")
        offsets = [float(x) for x in args.offsets.split(",")]
        if len(offsets) != len(CANDIDATES) or any(not math.isfinite(x) for x in offsets):
            raise ValueError(f"Exactly {len(CANDIDATES)} finite offsets are required")
        ffmpeg = args.ffmpeg or (TPS_FFMPEG if TPS_FFMPEG.is_file() else shutil.which("ffmpeg"))
        if not ffmpeg:
            raise ValueError("No existing FFmpeg found")
        output = args.output or HERE / "video_frames" / f"{clip['measurement_id']}_8frames"
        result = extract(clip, ffmpeg, output, offsets, args.impact_container)
        print(json.dumps({"output": str(output.resolve()), "measurement_id": clip["measurement_id"],
                          "impact_container_seconds": result["impact_container_seconds"],
                          "frames": result["frames"], "source_unchanged": True}, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
