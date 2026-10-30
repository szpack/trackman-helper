"""有界离线集成测试：临时原片/照片、mock 目录，无真实 DB/Key/模型调用。"""
import copy
from contextlib import ExitStack, redirect_stdout
import io
import json
import os
from pathlib import Path
import queue
import tempfile
import threading
import unittest
from unittest import mock
from types import SimpleNamespace
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer
import uuid

from PIL import Image
import analysis
import trackman_dashboard as dashboard
import tps_video as video
import tps_video_store as storage
from tps_video_store import VideoStore

MID = "32718f00-3a06-4c80-b33d-378983221267"
INTERNAL = "00000000-0000-0000-0000-000000000001"
OT = "00000000-0000-0000-0000-000000000002"
HERE = Path(__file__).resolve().parent
TEMP_CONFIG_LOADER = analysis.load_config


def sample_manifest():
    """仅供显式预览：只载入已存在的指定 sample clip，绝不查库/等待/生成。"""
    old = json.loads((HERE / "video_frames" / (MID + "_8frames") / "manifest.json").read_text(encoding="utf-8"))
    store = VideoStore(start_worker=False, catalog_ttl=float("inf"))
    clips = video.supported_clips(MID, [old["clip"]])
    store.catalogs[MID] = {"at": 0, "clips": clips, "notice": "",
                           "cameras": [video.camera_descriptor(c) for c in clips]}
    cid = clips[0]["video_clip_id"] if clips else None
    result = store._load(MID, cid) if cid else None
    if result is None:
        raise RuntimeError("指定 sample clip 的既有八图不可用；预览不会生成新照片")
    store.active[MID] = cid
    event = threading.Event()
    event.set()
    store.entries[(MID, cid)] = {"status": "ready", "manifest": result, "notice": "",
                                 "generation": 0, "event": event}
    return result, old, store


def make_hub(manifest, old, store=None):
    # 不运行正常 Hub.__init__，也不读配置、历史或启动后台 TPS/归一化线程。
    hub = dashboard.Hub.__new__(dashboard.Hub)
    hub.lock = threading.Lock()
    hub.shots = [{"seq": 264, "ts": manifest["clip"]["impact_absolute"],
                  "m": copy.deepcopy(old["shot_context"]["metrics"]), "raw": {"Id": MID}}]
    hub.clients = set()
    hub.pending = {}
    hub.timers = {}
    hub.analyses = {}
    hub._analysis_generation = {}
    hub._auto_analysis_jobs = {}
    hub._cfg_lock = threading.RLock()
    hub.ai_queue = queue.Queue()
    hub.use_ai = False
    hub.ai_cfg = {"base_url": "", "model": "", "api_key": "", "vision_enabled": False, "include_photos": False,
                  "vision_base_url": "", "vision_model": "", "vision_api_key": ""}
    hub.persist = None
    hub.status = {"tps": False, "measuring": False, "message": "只读离线预览（未订阅 TPS、未调用 AI）"}
    hub.norm = {"on": None, "conf": None, "detail": "预览不检测 TPS"}
    hub._cfg_mtime = None
    hub._refresh_cfg = lambda: None
    hub.video_store = store
    if store:
        store.notify = hub._video_changed
    return hub


class LocalPhotoIntegration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "originals"
        self.source.mkdir()
        self.root = self.base / "video_frames"
        self.root.mkdir()
        self.clips = []
        self.manifests = {}
        for cid, name, angle, tag, color in [
                (INTERNAL, "TM4Cam_TM4_18034029", "Internal", "DL", "red"),
                (OT, "iPhone", "Other", "OT", "blue")]:
            original = self.source / f"shot_{tag}.mov"
            original.write_bytes(("离线不可变原片 " + cid).encode())
            clip = {"measurement_id": MID, "stroke_id": MID, "video_clip_id": cid,
                    "path": str(original), "relative_path": original.name,
                    "camera_name": name, "camera_angle": angle, "status": "Success", "rotation": "0",
                    "impact_absolute": "2026-01-01T00:00:00", "video_speed_factor": 1,
                    "has_explicit_frame_timestamps": False, "impact_real_seconds": .8}
            self.clips.append(clip)
            revision = MID + "_" + cid.replace("-", "") + "_8frames"
            directory = self.root / revision
            directory.mkdir()
            frames = []
            for p, (stage, label, _) in enumerate(video.CANDIDATES, 1):
                filename = f"{p:02d}_{stage}.jpg"
                Image.new("RGB", (24, 24), color).save(directory / filename, "JPEG")
                frames.append({"name": stage, "label": label, "file": filename,
                               "frame_index_zero_based": p * 4, "actual_container_seconds": p * .12,
                               "stage_status": "candidate", "confidence": .1})
            manifest = {"schema_version": 2, "revision": revision, "clip": clip,
                        "source_video_sha256": video.sha256(original), "source_unchanged_after_extraction": True,
                        "decoded_frame_count": 40, "impact_alignment_status": "candidate",
                        "frames": frames, "calibration": {"status": "candidate", "candidates": [],
                                                           "impact_frame_index": 28, "confidence": .1},
                        "shot_context": {"metrics": {"ballSpeed": 100, "launch": 20, "spin": 4000}}}
            (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            self.manifests[cid] = manifest
        self.original_hashes = {c["path"]: video.sha256(Path(c["path"])) for c in self.clips}
        self.original_outputs = {str(p): video.sha256(p) for p in self.root.glob("*/*")}
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.dict(os.environ, {}, clear=True))
        self.stack.enter_context(mock.patch.object(storage, "ROOT", self.root))
        self.stack.enter_context(mock.patch.object(video, "VIDEO_ROOT", self.source))
        self.stack.enter_context(mock.patch.object(analysis, "HERE", self.base))
        self.stack.enter_context(mock.patch.object(analysis, "CONFIG_FILE", self.base / "sandbox_config.json"))
        self.stack.enter_context(mock.patch.object(dashboard, "CONFIG_FILE", self.base / "sandbox_config.json"))
        self.stack.enter_context(mock.patch.object(dashboard.tps_club, "find_club", return_value=None))
        self.query = self.stack.enter_context(mock.patch.object(video, "query_clips", return_value=self.clips))
        for module, attribute in [(video, "prepare_video"), (video, "extract_preview"),
                                  (video, "probe_frames"), (analysis, "_post_json"),
                                  (analysis, "load_config"), (dashboard, "load_config")]:
            self.stack.enter_context(mock.patch.object(module, attribute,
                                                       side_effect=AssertionError("禁止真实 DB/提帧/Key/网络模型")))
        self.store = VideoStore(start_worker=False, catalog_ttl=60)
        self.manifest = self.manifests[OT]
        self.old = self.manifest
        self.hub = make_hub(self.manifest, self.old, self.store)
        self.events = []
        self.hub._broadcast = lambda event, state: self.events.append((event, state))

    def tearDown(self):
        for path, expected in self.original_hashes.items():
            self.assertEqual(video.sha256(Path(path)), expected)
        self.assertEqual({str(p): video.sha256(p) for p in self.root.glob("*/*")}, self.original_outputs)

    def run_queue(self):
        self.hub.ai_queue.put(None)
        with redirect_stdout(io.StringIO()):
            self.hub._analysis_loop()

    def ready_both(self):
        self.hub.video_info(264, camera_id=INTERNAL)
        self.hub.video_info(264, camera_id=OT)

    def enable_mock_vision(self):
        self.hub.use_ai = True
        self.hub.ai_cfg.update(include_photos=True, vision_enabled=True, vision_model="offline-image-model",
                               vision_base_url="http://localhost:1234/v1", vision_api_key="offline-dummy")

    def dispatch(self, path, data=None, *, get=False):
        handler = dashboard.Handler.__new__(dashboard.Handler)
        handler.hub = self.hub
        handler.path = path
        body = json.dumps(data).encode() if data is not None else b"{}"
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler._same_origin = lambda: True
        handler._send = mock.Mock()
        handler.do_GET() if get else handler.do_POST()
        code, payload = handler._send.call_args.args[:2]
        return code, json.loads(payload)

    def test_selection_change_requeues_only_pending_auto_analysis(self):
        for timing in ("queued", "waiting"):
            for selection in ("preference", "late_external"):
                for enabled in (True, False):
                    if timing == "waiting" and not enabled:
                        continue  # 关闭全局后没有图像等待阶段。
                    with self.subTest(timing=timing, selection=selection, enabled=enabled):
                        self.hub = make_hub(self.manifest, self.old, self.store)
                        self.hub._broadcast = lambda *args: None
                        self.enable_mock_vision()
                        self.store.active.clear()
                        self.store.manual.clear()
                        self.store.catalogs.clear()
                        self.query.return_value = [self.clips[0]]
                        self.assertEqual(self.hub.video_info(264)["camera_id"], INTERNAL)
                        info = {"club": "7 Iron", "clubZh": "7号铁", "clubShort": "7I"}
                        with mock.patch.object(dashboard.tps_club, "find_club", return_value=info), \
                                redirect_stdout(io.StringIO()):
                            self.hub._attach_club(MID, self.hub.shots[0], 264)
                        old_job = self.hub._auto_analysis_jobs[264]
                        self.assertEqual(old_job[5], INTERNAL)
                        if timing == "queued":
                            old_job[7] = OT  # 补排必须重新从 shots 获取 raw GUID，不能继承旧 job。
                        self.hub.shots[0]["m"].update(ballSpeed=117, club="Sand Wedge")
                        self.hub.shots[0]["club_override"] = "Sand Wedge"
                        self.hub.ai_cfg["include_photos"] = enabled
                        old_generation = self.hub._analysis_generation.get(264, 0)

                        def switch():
                            self.query.return_value = self.clips
                            self.store.catalogs.clear()
                            kwargs = {"camera_id": OT} if selection == "preference" else {}
                            self.assertEqual(self.hub.video_info(264, **kwargs)["camera_id"], OT)
                            new_job = self.hub._auto_analysis_jobs[264]
                            self.assertIsNot(new_job, old_job)
                            self.assertEqual(new_job[4], old_generation + 1)
                            self.assertEqual(new_job[7], MID)
                            self.assertEqual(new_job[1]["ballSpeed"], 117)
                            self.assertEqual(new_job[2], "Sand Wedge")
                            self.assertIs(new_job[3], enabled)
                            self.assertEqual(new_job[5], OT if enabled else None)

                        enqueue = self.hub._enqueue_text_analysis
                        def unlocked_enqueue(*args, **kwargs):
                            if "expected_generation" in kwargs:
                                self.assertTrue(self.hub.lock.acquire(blocking=False), "补排 helper 必须在 Hub 锁外调用")
                                self.hub.lock.release()
                            return enqueue(*args, **kwargs)

                        def waiting(mid, *, timeout, camera_id):
                            self.assertEqual(mid, MID)
                            if camera_id == INTERNAL:
                                switch()
                            return copy.deepcopy(self.manifests[camera_id])

                        result = {"summary": "新镜头补排的唯一结果", "source": "ai",
                                  "input_mode": "vision" if enabled else "text", "image_count": 8 if enabled else 0}
                        with mock.patch.object(self.hub, "_enqueue_text_analysis", side_effect=unlocked_enqueue), \
                                mock.patch.object(self.store, "wait", side_effect=waiting) as wait, \
                                mock.patch.object(dashboard, "analyze", return_value=result) as call:
                            if timing == "queued":
                                switch()
                            self.run_queue()
                            if not self.hub.ai_queue.empty():
                                self.run_queue()  # 等待期间补排位于离线 loop 的结束标记之后。
                        call.assert_called_once()
                        self.assertEqual(call.call_args.args[0]["ballSpeed"], 117)
                        self.assertEqual(call.call_args.kwargs["club"], "Sand Wedge")
                        if enabled:
                            self.assertEqual(call.call_args.kwargs["video"]["camera_id"], OT)
                            self.assertEqual(call.call_args.kwargs["video"]["expected_measurement_id"], MID)
                            self.assertEqual(len(call.call_args.kwargs["video"]["frames8"]), 8)
                            self.assertEqual(wait.call_args.kwargs["camera_id"], OT)
                        else:
                            wait.assert_not_called()
                            self.assertIsNone(call.call_args.kwargs["video"])
                        self.assertEqual(self.hub.analyses[264]["summary"], result["summary"])
                        self.assertIs(self.hub.analyses[264]["photos_requested"], enabled)
                        self.assertEqual(self.hub._auto_analysis_jobs, {})
                        self.hub.video_info(264, camera_id=INTERNAL)
                        self.assertTrue(self.hub.ai_queue.empty(), "已完成历史切镜头不得自动重算")

        # 手动分析虽然也排队，但没有自动任务标记，切镜头不能追加模型请求。
        self.hub = make_hub(self.manifest, self.old, self.store)
        self.hub._broadcast = lambda *args: None
        self.enable_mock_vision()
        self.hub.video_info(264, camera_id=INTERNAL)
        self.hub.request_analysis(264, include_photos=True)
        self.assertEqual(self.hub._auto_analysis_jobs, {})
        self.hub.video_info(264, camera_id=OT)
        self.assertEqual(self.hub.ai_queue.qsize(), 1)
        with mock.patch.object(dashboard, "analyze") as call:
            self.run_queue()
        call.assert_not_called()

        # 补排过程中若用户已提交新请求，generation 保护不得用旧切换覆盖它。
        self.hub._enqueue_text_analysis(264, self.hub.shots[0]["m"], None)
        generation = self.hub._analysis_generation[264]
        refresh = self.hub._refresh_cfg
        def supersede():
            self.hub._refresh_cfg = refresh
            self.hub.request_analysis(264, include_photos=False)
        self.hub._refresh_cfg = supersede
        self.hub.video_info(264, camera_id=INTERNAL)
        self.assertEqual(self.hub._analysis_generation[264], generation + 2)
        with mock.patch.object(dashboard, "analyze", return_value={"source": "rules"}) as call:
            self.run_queue()
        call.assert_called_once()
        self.assertIsNone(call.call_args.kwargs["video"])

    def test_global_on_automatically_analyzes_subsequent_new_shots_with_eight_images(self):
        self.enable_mock_vision()
        response = {"choices": [{"message": {"content": json.dumps({"summary": "新杆八图离线分析"})}}]}
        ids = [str(uuid.UUID(int=100)), str(uuid.UUID(int=101))]
        def waiting(mid, *, timeout, camera_id):
            self.assertIn(mid, ids)
            self.assertEqual(camera_id, OT)
            self.assertLessEqual(timeout, 90)
            manifest = copy.deepcopy(self.manifests[OT])
            manifest["clip"]["measurement_id"] = mid
            return manifest
        with mock.patch.object(self.store, "select", return_value=OT) as select, \
                mock.patch.object(self.store, "current_camera", return_value=OT), \
                mock.patch.object(self.store, "wait", side_effect=waiting), \
                mock.patch.object(self.hub, "_club_retry_later"), \
                mock.patch.object(analysis, "_post_json", return_value=response) as post, \
                redirect_stdout(io.StringIO()):
            for mid in ids:
                rec = {"seq": 0, "ts": "offline", "m": dict(self.hub.shots[0]["m"], club="7 Iron"),
                       "raw": {"Id": mid}, "_final": True}
                self.hub.pending[mid] = rec
                self.hub._finalize(mid)
                self.run_queue()
                result = self.hub.analyses[rec["seq"]]
                self.assertTrue(result["photos_requested"])
                self.assertEqual((result["input_mode"], result["image_count"]), ("vision", 8))
                body = post.call_args.args[1]["messages"][1]["content"]
                self.assertEqual(sum(p["type"] == "image_url" for p in body), 8)
                self.assertIn(mid, body[0]["text"])
            self.assertEqual(post.call_count, 2)
            self.assertEqual([call.args[0] for call in select.call_args_list], ids)
        self.assertTrue(self.hub.ai_cfg["include_photos"])

    def test_photo_wait_error_or_wrong_shot_falls_back_once_without_images(self):
        self.enable_mock_vision()
        self.hub.ai_cfg.update(api_key="dummy-text", model="dummy-text", base_url="https://text.invalid/v1")
        self.ready_both()
        wrong_mid = copy.deepcopy(self.manifests[OT])
        wrong_mid["clip"]["measurement_id"] = INTERNAL
        response = {"choices": [{"message": {"content": json.dumps({"summary": "只分析参数"})}}]}
        for failure in (OSError("secret https://private.invalid"), wrong_mid, self.manifests[INTERNAL]):
            with self.subTest(kind=type(failure).__name__):
                self.hub.request_analysis(264)
                with mock.patch.object(self.store, "wait", side_effect=failure if isinstance(failure, Exception) else None,
                                       return_value=copy.deepcopy(failure)), \
                        mock.patch.object(analysis, "_post_json", return_value=response) as post:
                    self.run_queue()
                post.assert_called_once()
                self.assertIsInstance(post.call_args.args[1]["messages"][1]["content"], str)
                result = self.hub.analyses[264]
                self.assertEqual((result["input_mode"], result["image_count"]), ("text", 0))
                self.assertIn("图片就绪等待失败", result["notice"])
                self.assertNotIn("secret", result["notice"])
                self.assertNotIn("https://", result["notice"])

    def test_unavailable_selected_camera_does_not_drop_auto_analysis_or_switch(self):
        self.enable_mock_vision()
        with mock.patch.object(self.store, "current_camera", return_value=OT), \
                mock.patch.object(self.store, "select", side_effect=ValueError("所选录像暂不可用")), \
                mock.patch.object(self.store, "wait", return_value=None) as wait, \
                mock.patch.object(dashboard, "analyze", return_value={"input_mode": "rules", "image_count": 0}) as call:
            self.hub._enqueue_text_analysis(264, self.hub.shots[0]["m"], None)
            self.assertEqual(self.hub.ai_queue.queue[0][5], OT)
            self.run_queue()
        self.assertEqual(wait.call_args.kwargs["camera_id"], OT)
        self.assertIsNone(call.call_args.kwargs["video"])
        self.assertIn("图片就绪等待失败", self.hub.analyses[264]["notice"])

    def test_auto_flag_does_not_drift_when_global_turns_on_after_enqueue(self):
        self.enable_mock_vision()
        self.hub.ai_cfg["include_photos"] = False
        self.hub._enqueue_text_analysis(264, self.hub.shots[0]["m"], None)
        self.hub.ai_cfg["include_photos"] = True
        with mock.patch.object(self.store, "wait") as wait, \
                mock.patch.object(dashboard, "analyze", return_value={"input_mode": "text", "image_count": 0}) as call:
            self.run_queue()
        wait.assert_not_called()
        self.assertIsNone(call.call_args.kwargs["video"])
        self.assertFalse(self.hub.analyses[264]["photos_requested"])
        self.query.assert_not_called()

    def test_startup_last_shot_and_late_club_retry_follow_global_mode(self):
        info = {"club": "7 Iron", "clubZh": "7号铁", "clubShort": "7I"}
        for automatic in ("startup", "backfill"):
            for enabled in (False, True):
                with self.subTest(automatic=automatic, enabled=enabled), redirect_stdout(io.StringIO()):
                    self.enable_mock_vision()
                    self.hub.ai_cfg["include_photos"] = enabled
                    self.hub.shots[0]["m"].pop("club", None)
                    self.hub.analyses.clear()
                    with mock.patch.object(dashboard.tps_club, "find_club", return_value=info):
                        if automatic == "startup":
                            self.hub._backfill_clubs()
                        else:
                            self.hub._club_retry(MID, 264)
                    job = self.hub.ai_queue.get_nowait()
                    self.assertIs(job[3], enabled)
                    self.assertEqual(job[7], MID)
                    self.hub._auto_analysis_jobs.clear()

    def test_close_after_eight_images_are_read_sends_no_image_request(self):
        self.enable_mock_vision()
        self.hub.ai_cfg.update(api_key="dummy-text-secret", model="dummy-text", base_url="https://text.invalid/v1")
        self.ready_both()
        self.hub.request_analysis(264)
        original = analysis._vision_content
        def reading(*args, **kwargs):
            content = original(*args, **kwargs)
            self.hub.ai_cfg["include_photos"] = False
            return content
        response = {"choices": [{"message": {"content": json.dumps({"summary": "关闭后仅参数"})}}]}
        with mock.patch.object(analysis, "_vision_content", side_effect=reading) as images, \
                mock.patch.object(analysis, "_post_json", return_value=response) as post:
            self.run_queue()
        images.assert_called_once()
        post.assert_called_once()
        self.assertIsInstance(post.call_args.args[1]["messages"][1]["content"], str)
        self.assertEqual(self.hub.analyses[264]["input_mode"], "text")
        self.assertEqual(self.hub.analyses[264]["image_count"], 0)
        self.assertIn("开关关闭", self.hub.analyses[264]["notice"])

    def test_generation_change_during_eight_image_read_does_not_pay_for_stale_job(self):
        self.enable_mock_vision()
        self.ready_both()
        self.hub.request_analysis(264)
        original = analysis._vision_content
        def reading(*args, **kwargs):
            content = original(*args, **kwargs)
            self.hub.video_info(264, camera_id=INTERNAL)
            return content
        with mock.patch.object(analysis, "_vision_content", side_effect=reading), \
                mock.patch.object(analysis, "_post_json") as post:
            self.run_queue()
        post.assert_not_called()
        self.assertNotIn(264, self.hub.analyses)

    def test_late_metadata_generation_change_stops_wait_without_cross_shot_model(self):
        self.enable_mock_vision()
        clock = SimpleNamespace(now=0.0)
        def sleeping(seconds):
            clock.now += seconds
            self.hub._analysis_generation[264] = 1
        with mock.patch.object(self.store, "select", return_value=None), \
                mock.patch.object(self.store, "current_camera", return_value=None), \
                mock.patch.object(self.store, "wait") as wait, \
                mock.patch.object(dashboard.time, "monotonic", side_effect=lambda: clock.now), \
                mock.patch.object(dashboard.time, "sleep", side_effect=sleeping), \
                mock.patch.object(dashboard, "analyze") as call:
            self.hub._enqueue_text_analysis(264, self.hub.shots[0]["m"], None)
            self.run_queue()
        wait.assert_not_called()
        call.assert_not_called()
        self.assertEqual(clock.now, .25)
        self.assertNotIn(264, self.hub.analyses)
        self.assertEqual(self.hub._auto_analysis_jobs, {})

    def test_global_off_prevents_images_for_subsequent_auto_shots(self):
        self.enable_mock_vision()
        self.hub.ai_cfg["include_photos"] = False
        with mock.patch.object(dashboard, "analyze", return_value={"input_mode": "text", "image_count": 0}) as call:
            self.hub._enqueue_text_analysis(264, self.hub.shots[0]["m"], None)
            self.run_queue()
        self.assertIsNone(call.call_args.kwargs["video"])
        self.assertFalse(self.hub.analyses[264]["photos_requested"])
        self.assertIn("开关关闭", self.hub.analyses[264]["notice"])
        self.query.assert_not_called()

    def test_queued_photos_respect_global_opt_out_for_auto_and_explicit_requests(self):
        for automatic in (True, False):
            with self.subTest(automatic=automatic):
                self.enable_mock_vision()
                self.hub.video_info(264, camera_id=OT)
                if automatic:
                    self.hub._enqueue_text_analysis(264, self.hub.shots[0]["m"], None)
                else:
                    self.hub.request_analysis(264, include_photos=True, camera_id=OT)
                self.hub.ai_cfg["include_photos"] = False
                with mock.patch.object(self.store, "wait") as wait, \
                        mock.patch.object(dashboard, "analyze", return_value={"input_mode": "text", "image_count": 0}) as call:
                    self.run_queue()
                wait.assert_not_called()
                self.assertIsNone(call.call_args.kwargs["video"])
                self.assertTrue(self.hub.analyses[264]["photos_requested"])
                self.assertIn("开关关闭", self.hub.analyses[264]["notice"])

    def test_global_close_during_wait_removes_images_before_model(self):
        self.enable_mock_vision()
        self.ready_both()
        self.hub.request_analysis(264)
        def waiting(*args, **kwargs):
            self.hub.ai_cfg["include_photos"] = False
            return copy.deepcopy(self.manifests[OT])
        with mock.patch.object(self.store, "wait", side_effect=waiting), \
                mock.patch.object(dashboard, "analyze", return_value={"input_mode": "text", "image_count": 0}) as call:
            self.run_queue()
        self.assertIsNone(call.call_args.kwargs["video"])
        self.assertIn("开关关闭", self.hub.analyses[264]["notice"])

    def test_auto_enqueue_refreshes_and_captures_global_camera_and_raw_guid(self):
        self.enable_mock_vision()
        self.hub.ai_cfg["include_photos"] = False
        self.ready_both()
        def refresh():
            self.hub.ai_cfg["include_photos"] = True
        self.hub._refresh_cfg = refresh
        self.hub._enqueue_text_analysis(264, {"measurement_id": "不可信的 m ID"}, None)
        job = self.hub.ai_queue.queue[0]
        self.assertTrue(job[3])
        self.assertEqual((job[5], job[7]), (OT, MID))
        self.store.active[MID] = INTERNAL  # 不广播，仅验证等待不会重新选择默认镜头。
        with mock.patch.object(self.store, "wait", return_value=copy.deepcopy(self.manifests[OT])) as wait, \
                mock.patch.object(dashboard, "analyze", return_value={"input_mode": "vision", "image_count": 8}):
            self.run_queue()
        self.assertEqual(wait.call_args.kwargs["camera_id"], OT)

    def test_repeated_auto_backfill_coalesces_same_generation_before_model(self):
        self.enable_mock_vision()
        self.ready_both()
        self.hub._enqueue_text_analysis(264, self.hub.shots[0]["m"], None)
        self.hub._enqueue_text_analysis(264, dict(self.hub.shots[0]["m"], club="Sand Wedge"), "Sand Wedge")
        self.assertEqual(self.hub.ai_queue.qsize(), 1)
        def waiting(*args, **kwargs):
            self.hub._enqueue_text_analysis(264, dict(self.hub.shots[0]["m"], club="7 Iron"), "7 Iron")
            return copy.deepcopy(self.manifests[OT])
        with mock.patch.object(self.store, "wait", side_effect=waiting), \
                mock.patch.object(dashboard, "analyze", return_value={"input_mode": "vision", "image_count": 8}) as call:
            self.run_queue()
        call.assert_called_once()
        self.assertEqual(call.call_args.args[0]["club"], "7 Iron")
        self.assertEqual(call.call_args.kwargs["club"], "7 Iron")
        self.assertEqual(self.hub._auto_analysis_jobs, {})

    def test_late_auto_metadata_uses_bounded_mock_clock_and_same_shot(self):
        self.enable_mock_vision()
        clock = SimpleNamespace(now=0.0)
        def sleeping(seconds):
            clock.now += seconds
        with mock.patch.object(self.store, "select", side_effect=[None, None, None, OT]) as select, \
                mock.patch.object(self.store, "current_camera", return_value=None), \
                mock.patch.object(self.store, "wait", return_value=copy.deepcopy(self.manifests[OT])) as wait, \
                mock.patch.object(dashboard.time, "monotonic", side_effect=lambda: clock.now), \
                mock.patch.object(dashboard.time, "sleep", side_effect=sleeping), \
                mock.patch.object(dashboard, "analyze", return_value={"input_mode": "vision", "image_count": 8}) as call:
            self.hub._enqueue_text_analysis(264, self.hub.shots[0]["m"], None)
            self.assertIsNone(self.hub.ai_queue.queue[0][5])
            self.run_queue()
        self.assertEqual(clock.now, .5)
        self.assertTrue(all(c.args[0] == MID for c in select.call_args_list))
        self.assertEqual(wait.call_args.kwargs["timeout"], 89.5)
        self.assertEqual(wait.call_args.kwargs["camera_id"], OT)
        self.assertEqual(call.call_args.kwargs["video"]["expected_measurement_id"], MID)
        self.assertEqual(self.hub.analyses[264]["video_camera_id"], OT)

    def test_late_auto_metadata_times_out_without_real_sleep_or_images(self):
        self.enable_mock_vision()
        clock = SimpleNamespace(now=0.0)
        def sleeping(seconds):
            clock.now += seconds
        with mock.patch.object(self.store, "select", return_value=None) as select, \
                mock.patch.object(self.store, "current_camera", return_value=None), \
                mock.patch.object(self.store, "wait") as wait, \
                mock.patch.object(dashboard.time, "monotonic", side_effect=lambda: clock.now), \
                mock.patch.object(dashboard.time, "sleep", side_effect=sleeping), \
                mock.patch.object(dashboard, "analyze", return_value={"input_mode": "text", "image_count": 0}) as call:
            self.hub._enqueue_text_analysis(264, self.hub.shots[0]["m"], None)
            self.run_queue()
        self.assertEqual(clock.now, 90)
        self.assertLessEqual(select.call_count, 361)
        wait.assert_not_called()
        self.assertIsNone(call.call_args.kwargs["video"])
        self.assertIn("图片就绪等待失败", self.hub.analyses[264]["notice"])

    def test_switch_camera_does_not_disable_global_or_automatically_call_ai(self):
        self.enable_mock_vision()
        self.ready_both()
        self.hub.video_info(264, camera_id=INTERNAL)
        self.assertTrue(self.hub.ai_queue.empty())
        self.assertTrue(self.hub.ai_cfg["include_photos"])

    def test_photo_api_boolean_persistence_and_safe_broadcast_without_ai(self):
        self.enable_mock_vision()
        self.hub.ai_cfg.update(api_key="dummy-text-secret", base_url="https://text.invalid/v1", model="dummy-text")
        self.hub.analyses[264] = {"summary": "不自动重算"}
        with mock.patch.object(analysis, "load_config", TEMP_CONFIG_LOADER), \
                mock.patch.object(dashboard, "analyze") as analyze:
            analysis.save_config(self.hub.ai_cfg)
            for enabled in (False, True):
                code, data = self.dispatch("/api/ai-photo-mode", {"include_photos": enabled,
                                            "vision_enabled": False, "api_key": "不应覆盖"})
                self.assertEqual(code, 200)
                self.assertIs(data["include_photos"], enabled)
                self.assertTrue(data["ok"])
                self.assertTrue(data["vision_enabled"])
                cfg = analysis.load_config()
                self.assertIs(cfg["include_photos"], enabled)
                self.assertEqual(cfg["api_key"], "dummy-text-secret")
                self.assertEqual(cfg["vision_api_key"], "offline-dummy")
                self.assertEqual(self.hub.ai_cfg, cfg)
                self.assertEqual(self.events[-1][0], "ai_config")
                for field in ("api_key", "vision_api_key", "base_url", "vision_base_url", "model", "vision_model"):
                    self.assertNotIn(field, data)
                    self.assertNotIn(field, self.events[-1][1])
            code, data = self.dispatch("/api/ai", get=True)
            self.assertEqual(code, 200)
            self.assertTrue(data["include_photos"])
            code, _ = self.dispatch("/api/ai-config", {"model": "updated-text", "api_key": "***", "vision_api_key": ""})
            self.assertEqual(code, 200)
            cfg = analysis.load_config()
            self.assertTrue(cfg["include_photos"])
            self.assertEqual(cfg["api_key"], "dummy-text-secret")
            self.assertEqual(cfg["vision_api_key"], "offline-dummy")
            self.assertTrue(self.hub.ai_queue.empty())
            self.assertEqual(self.hub.analyses[264]["summary"], "不自动重算")
            analyze.assert_not_called()

    def test_photo_api_rejects_nonbool_and_reports_save_failure(self):
        for value in (None, "true", 1, 0, [], {}):
            with self.subTest(value=value), mock.patch.object(dashboard, "save_config") as save:
                code, data = self.dispatch("/api/ai-photo-mode", {"include_photos": value})
                self.assertEqual(code, 400)
                self.assertFalse(data["ok"])
                save.assert_not_called()
        code, _ = self.dispatch("/api/ai-photo-mode", {})
        self.assertEqual(code, 400)
        old_cfg = self.hub.ai_cfg.copy()
        with mock.patch.object(dashboard, "save_config", side_effect=OSError("secret https://private.invalid")):
            code, data = self.dispatch("/api/ai-photo-mode", {"include_photos": True})
        self.assertEqual(code, 500)
        self.assertFalse(data["ok"])
        self.assertNotIn("secret", json.dumps(data))
        self.assertNotIn("https://", json.dumps(data))
        self.assertEqual(self.hub.ai_cfg, old_cfg)
        self.assertEqual(self.events, [])

    def test_analyze_api_omission_uses_global_but_legacy_string_stays_text_only(self):
        self.enable_mock_vision()
        for data, expected in (({"seq": 264}, True), ({"seq": 264, "include_photos": False}, False),
                               ({"seq": 264, "include_photos": "true"}, False)):
            code, result = self.dispatch("/api/analyze", data)
            self.assertEqual(code, 200)
            self.assertTrue(result["ok"])
            self.assertIs(self.hub.ai_queue.get_nowait()[3], expected)

    def test_photo_notices_distinguish_disabled_missing_wait_and_request_failure(self):
        self.enable_mock_vision()
        self.ready_both()
        original = self.hub.ai_cfg.copy()
        cases = [("开关关闭", {"include_photos": False}, None),
                 ("视觉能力未启用", {"vision_enabled": False}, None),
                 ("配置缺失", {"vision_api_key": ""}, None),
                 ("图片就绪等待失败", {}, None),
                 ("请求失败", {}, copy.deepcopy(self.manifests[OT]))]
        for notice, changes, manifest in cases:
            with self.subTest(notice=notice):
                self.hub.ai_cfg = {**original, **changes}
                self.hub.request_analysis(264, include_photos=True, camera_id=OT)
                with mock.patch.object(self.store, "wait", return_value=manifest), \
                        mock.patch.object(dashboard, "analyze", return_value={"input_mode": "rules", "image_count": 0}):
                    self.run_queue()
                self.assertIn(notice, self.hub.analyses[264]["notice"])
                self.assertTrue(self.hub.analyses[264]["photos_requested"])
                self.assertEqual(self.hub.analyses[264]["image_count"], 0)

    def test_automatic_and_unchecked_never_pass_images_or_query_catalog(self):
        for include in (False, "true", 1):
            with self.subTest(include=include), mock.patch.object(dashboard, "analyze", return_value={"summary": "offline", "source": "rules"}) as call:
                self.hub.request_analysis(264, include_photos=include)
                self.run_queue()
                self.assertIsNone(call.call_args.kwargs["video"])
                self.assertIsNone(self.hub.analyses[264]["video_camera_id"])
        with mock.patch.object(dashboard, "analyze", return_value={"summary": "offline", "source": "rules"}) as call:
            self.hub._enqueue_text_analysis(264, self.hub.shots[0]["m"], None)
            self.run_queue()
            self.assertIsNone(call.call_args.kwargs["video"])
        self.query.assert_not_called()
        self.assertTrue(self.store.jobs.empty())

    def test_explicit_checked_uses_selected_guid_and_eight_images(self):
        self.enable_mock_vision()
        response = {"choices": [{"message": {"content": json.dumps({"summary": "离线验证", "visual_findings": []})}}]}
        for cid in (INTERNAL, OT):
            with self.subTest(camera_id=cid), mock.patch.object(analysis, "_post_json", return_value=response) as post:
                self.hub.video_info(264, camera_id=cid)
                self.hub.request_analysis(264, include_photos=True, camera_id=cid)
                self.run_queue()
                content = post.call_args.args[1]["messages"][1]["content"]
                self.assertEqual(len([c for c in content if c["type"] == "image_url"]), 8)
                self.assertIn(MID, content[0]["text"])
                self.assertIn(self.manifests[cid]["revision"], content[0]["text"])
            self.assertEqual(self.hub.analyses[264]["input_mode"], "vision")
            self.assertEqual(self.hub.analyses[264]["video_camera_id"], cid)
            self.assertTrue(self.hub.analyses[264]["photos_requested"])

    def test_new_request_supersedes_old_image_permission(self):
        with mock.patch.object(dashboard, "analyze", return_value={"summary": "offline", "source": "rules"}) as call:
            self.hub.request_analysis(264, include_photos=True)
            self.hub.request_analysis(264, include_photos=False)
            self.run_queue()
            call.assert_called_once()
            self.assertIsNone(call.call_args.kwargs["video"])

    def test_calibration_callback_follows_global_mode(self):
        self.ready_both()
        state = self.store.state(MID)
        state["calibration_changed"] = True
        for enabled in (True, False):
            self.hub.ai_cfg["include_photos"] = enabled
            self.hub._video_changed(MID, state)
            job = self.hub.ai_queue.get_nowait()
            self.assertIs(job[3], enabled)
            self.assertEqual(job[5], OT if enabled else None)
        other = self.store.state(MID, camera_id=INTERNAL)
        other["calibration_changed"] = True
        self.hub._video_changed(MID, other)
        self.assertTrue(self.hub.ai_queue.empty())
        self.assertEqual(self.events[-1][1]["camera_id"], OT)  # 非当前摄像头不广播回退事件。

    def test_switch_during_calibration_broadcast_does_not_enqueue_old_analysis(self):
        self.ready_both()
        state = self.store.state(MID, camera_id=OT)
        state["calibration_changed"] = True
        def broadcast(event, data):
            if event == "video" and data["camera_id"] == OT:
                self.hub.video_info(264, camera_id=INTERNAL)
        self.hub._broadcast = broadcast
        self.hub._video_changed(MID, state)
        self.assertEqual(self.store.current_camera(MID), INTERNAL)
        self.assertTrue(self.hub.ai_queue.empty())

    def test_catalog_ttl_and_force_refresh(self):
        first = self.hub.video_info(264)
        self.assertEqual(first["camera_id"], OT)
        for _ in range(8):
            self.hub.video_info(264)
        self.query.assert_called_once_with(MID)
        with mock.patch.object(self.store.jobs, "put_nowait") as submit:
            self.hub.video_info(264, force=True)
            self.assertEqual(submit.call_args.args[0][1], OT)
        self.assertEqual(self.query.call_count, 2)
        self.store.catalogs[MID]["at"] -= 61
        self.store.state(MID)
        self.assertEqual(self.query.call_count, 3)

    def test_empty_catalog_and_unavailable_db_are_missing(self):
        for result, error in [([], None), (None, RuntimeError("DB unavailable"))]:
            with self.subTest(error=error):
                self.store.catalogs.clear()
                self.query.return_value = result
                self.query.side_effect = error
                state = self.hub.video_info(264)
                self.assertEqual(state["status"], "missing")
                self.assertEqual(state["cameras"], [])
                self.assertIsNone(state["camera_id"])
                self.assertIn("VideoClips", state["camera_source"])
                self.assertTrue(self.store.jobs.empty())
        state = self.hub.video_info(264, camera_id=INTERNAL)
        self.assertEqual(state["status"], "missing")

    def test_invalid_explicit_id_never_falls_back_or_enqueues(self):
        self.hub.video_info(264)
        previous = self.store.current_camera(MID)
        for cid in ("", "invalid", str(uuid.uuid4())):
            with self.subTest(cid=cid):
                with self.assertRaises(ValueError):
                    self.hub.video_info(264, camera_id=cid)
                with self.assertRaises(ValueError):
                    self.hub.request_analysis(264, include_photos=True, camera_id=cid)
                with self.assertRaises(ValueError):
                    self.hub.request_analysis(264, include_photos=False, camera_id=cid)
                self.assertEqual(self.store.current_camera(MID), previous)
        self.assertTrue(self.store.jobs.empty())
        self.assertTrue(self.hub.ai_queue.empty())

    def test_double_camera_cache_images_and_load_are_isolated(self):
        self.ready_both()
        self.assertEqual(set(self.store.entries), {(MID, INTERNAL), (MID, OT)})
        for cid in (INTERNAL, OT):
            manifest = self.store.manifest(MID, camera_id=cid)
            self.assertEqual(manifest["clip"]["video_clip_id"], cid)
            self.assertEqual(self.store._load(MID, cid)["revision"], self.manifests[cid]["revision"])
            image = self.store.image(MID, manifest["revision"], manifest["frames"][0]["file"], camera_id=cid)
            with Image.open(image) as picture:
                pixel = picture.getpixel((0, 0))
                self.assertGreater(pixel[0] if cid == INTERNAL else pixel[2], 200)
        self.assertEqual(self.store.current_camera(MID), OT)
        # 链接固定 cid，切换默认后旧相机自己的链接仍可访问，但不能跨相机借用 revision。
        self.hub.video_info(264, camera_id=INTERNAL)
        self.assertTrue(self.store.image(MID, self.manifest["revision"], "01_address.jpg", camera_id=OT).is_file())
        with self.assertRaises(ValueError):
            self.store.image(MID, self.manifest["revision"], "01_address.jpg", camera_id=INTERNAL)

    def test_wrong_revision_and_traversal_rejected_without_switch(self):
        self.ready_both()
        for cid, revision in [(OT, "wrong-version"), (INTERNAL, self.manifest["revision"])]:
            with self.subTest(cid=cid), self.assertRaises(ValueError):
                self.store.ensure(MID, impact_frame=24, revision=revision, camera_id=cid)
        self.assertEqual(self.store.current_camera(MID), OT)
        with self.assertRaises(ValueError):
            self.store.image(MID, self.manifest["revision"], "../../ai_config.json", camera_id=OT)
        with self.assertRaises(ValueError):
            self.store.preview(MID, self.manifest["revision"], 24, camera_id=INTERNAL)
        with self.assertRaises(ValueError):
            self.store.ensure(MID, impact_frame=505, revision=self.manifest["revision"], camera_id=OT)
        self.assertTrue(self.store.jobs.empty())

    def test_prepare_queue_captures_clip_and_calibration_per_camera(self):
        self.ready_both()
        before = self.manifests[INTERNAL]
        self.store.ensure(MID, force=True, camera_id=INTERNAL, impact_frame=24, revision=before["revision"])
        self.hub.video_info(264, camera_id=OT)
        job = self.store.jobs.queue[0]
        self.assertEqual(job[1], INTERNAL)
        self.assertEqual(job[4]["video_clip_id"], INTERNAL)
        self.assertEqual(job[5]["revision"], before["revision"])
        self.store.jobs.put(None)
        result = copy.deepcopy(before)
        result["revision"] = MID + "_new_internal_8frames"
        result["calibration"]["status"] = "manual_confirmed"
        with mock.patch.object(video, "prepare_video", return_value=result) as prepare:
            self.store._loop()
        self.assertEqual(prepare.call_args.kwargs["camera_id"], INTERNAL)
        self.assertEqual(prepare.call_args.kwargs["clip"]["video_clip_id"], INTERNAL)
        self.assertTrue(prepare.call_args.kwargs["confirmed"])
        self.assertEqual(self.store.current_camera(MID), OT)
        self.assertEqual(self.store.manifest(MID, camera_id=OT)["revision"], self.manifest["revision"])
        self.assertEqual(self.store.manifest(MID, camera_id=INTERNAL)["revision"], result["revision"])
        self.assertTrue(self.hub.ai_queue.empty())
        self.assertEqual(self.store.jobs.unfinished_tasks, 0)
        with self.assertRaises(ValueError):
            self.store.ensure(MID, camera_id=INTERNAL, impact_frame=24, revision=before["revision"])

    def test_stale_prepare_generation_and_wrong_clip_result_rejected(self):
        self.ready_both()
        self.store.ensure(MID, force=True, camera_id=INTERNAL)
        entry = self.store.entries[(MID, INTERNAL)]
        entry["generation"] += 1
        self.store.jobs.put(None)
        with mock.patch.object(video, "prepare_video", return_value=self.manifests[INTERNAL]):
            self.store._loop()
        self.assertEqual(entry["status"], "pending")
        self.assertEqual(self.store.jobs.unfinished_tasks, 0)
        entry["status"] = "ready"
        self.store.ensure(MID, force=True, camera_id=INTERNAL)
        self.store.jobs.put(None)
        with mock.patch.object(video, "prepare_video", return_value=self.manifests[OT]):
            self.store._loop()
        self.assertEqual(entry["status"], "error")
        self.assertEqual(entry["manifest"]["clip"]["video_clip_id"], INTERNAL)

    def test_wait_keeps_captured_camera_without_changing_active(self):
        self.ready_both()
        event = self.store.entries[(MID, OT)]["event"]
        with mock.patch.object(event, "wait", side_effect=lambda timeout: self.hub.video_info(264, camera_id=INTERNAL)):
            result = self.store.wait(MID, camera_id=OT)
        self.assertEqual(result["clip"]["video_clip_id"], OT)
        self.assertEqual(self.store.current_camera(MID), INTERNAL)

    def test_ai_submission_camera_does_not_drift_and_old_job_is_invalidated(self):
        self.ready_both()
        self.hub.request_analysis(264, include_photos=True)
        self.assertEqual(self.hub.ai_queue.queue[0][5], OT)
        generation = self.hub._analysis_generation[264]
        self.hub.video_info(264, camera_id=INTERNAL)
        self.assertGreater(self.hub._analysis_generation[264], generation)
        with mock.patch.object(dashboard, "analyze") as call:
            self.run_queue()
        call.assert_not_called()

    def test_switch_during_wait_or_model_call_drops_old_result(self):
        self.enable_mock_vision()
        for timing in ("wait", "model"):
            with self.subTest(timing=timing):
                self.hub.video_info(264, camera_id=OT)
                self.hub.request_analysis(264, include_photos=True, camera_id=OT)
                if timing == "wait":
                    def waiting(mid, *, timeout, camera_id):
                        self.assertEqual(camera_id, OT)
                        self.hub.video_info(264, camera_id=INTERNAL)
                        return copy.deepcopy(self.manifests[OT])
                    with mock.patch.object(self.store, "wait", side_effect=waiting), mock.patch.object(dashboard, "analyze") as call:
                        self.run_queue()
                        call.assert_not_called()
                else:
                    def modeling(*args, **kwargs):
                        self.assertEqual(kwargs["video"]["clip"]["video_clip_id"], OT)
                        self.hub.video_info(264, camera_id=INTERNAL)
                        return {"summary": "不得覆盖新摄像头", "source": "ai"}
                    with mock.patch.object(dashboard, "analyze", side_effect=modeling):
                        self.run_queue()
                self.assertNotIn(264, self.hub.analyses)

    def test_submit_without_video_cannot_pick_up_later_default(self):
        self.enable_mock_vision()
        self.query.return_value = []
        self.hub.request_analysis(264, include_photos=True)
        self.assertIsNone(self.hub.ai_queue.queue[0][5])
        self.query.return_value = self.clips
        self.store.catalogs.clear()
        self.hub.video_info(264, camera_id=OT)
        with mock.patch.object(self.store, "wait") as wait, mock.patch.object(dashboard, "analyze", return_value={"source": "rules"}) as call:
            self.run_queue()
        wait.assert_not_called()
        self.assertIsNone(call.call_args.kwargs["video"])
        self.assertIsNone(self.hub.analyses[264]["video_camera_id"])

    def test_switch_while_submitting_rejects_stale_capture(self):
        self.ready_both()
        select = self.store.select
        def capture(mid, camera_id):
            selected = select(mid, camera_id)
            self.hub.video_info(264, camera_id=INTERNAL)
            return selected
        with mock.patch.object(self.store, "select", side_effect=capture), self.assertRaises(ValueError):
            self.hub.request_analysis(264, include_photos=True)
        self.assertTrue(self.hub.ai_queue.empty())

    def test_photo_request_without_real_mid_keeps_text_compatibility(self):
        self.hub.shots[0]["raw"] = {}
        with mock.patch.object(dashboard, "analyze", return_value={"source": "rules"}) as call:
            self.assertTrue(self.hub.request_analysis(264, include_photos=True))
            self.run_queue()
        self.assertIsNone(call.call_args.kwargs["video"])
        self.query.assert_not_called()
        self.assertFalse(self.hub.request_analysis(999, include_photos=True))
        with self.assertRaises(ValueError):
            self.hub.request_analysis(264, include_photos=True, camera_id=OT)
        state = self.hub.video_info(264)
        self.assertEqual(state["cameras"], [])
        self.assertIsNone(state["camera_id"])

    def test_ttl_catalog_rechecks_local_availability_without_db(self):
        self.ready_both()
        self.store.catalogs[MID]["clips"] = [dict(c, path=str(self.base / "unavailable.mov")) for c in self.clips]
        calls = self.query.call_count
        state = self.hub.video_info(264)
        self.assertEqual(state["status"], "missing")
        self.assertEqual(state["cameras"], [])
        self.assertEqual(self.query.call_count, calls)
        with self.assertRaises(ValueError):
            self.hub.video_info(264, camera_id=OT)

    def test_local_http_contract_and_cross_origin_guard(self):
        class TestHandler(dashboard.Handler):
            hub = self.hub
        server = ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:" + str(server.server_address[1])
        # 只安装本地 HTTP handler，避免离线测试初始化系统 TLS/代理配置。
        opener = urllib.request.OpenerDirector()
        for handler in (urllib.request.ProxyHandler({}), urllib.request.HTTPHandler(),
                        urllib.request.HTTPDefaultErrorHandler(), urllib.request.HTTPErrorProcessor()):
            opener.add_handler(handler)
        def request(path, data=None, origin=None):
            body = json.dumps(data).encode() if data is not None else None
            return opener.open(urllib.request.Request(
                base + path, data=body, headers={"Content-Type": "application/json", "Origin": origin or base}), timeout=5)
        try:
            with request("/api/video?seq=264") as response:
                data = json.load(response)
            self.assertEqual(data["status"], "ready")
            self.assertEqual(data["camera_id"], OT)
            self.assertEqual(len(data["cameras"]), 2)
            self.assertNotIn("path", json.dumps(data))
            self.assertIn("camera_id=" + OT, data["frames"][0]["url"])
            with request("/api/video?seq=264&camera_id=" + INTERNAL) as response:
                internal = json.load(response)
            self.assertEqual(internal["camera_id"], INTERNAL)
            with request(data["frames"][0]["url"]) as response:
                self.assertEqual(response.headers["Content-Type"], "image/jpeg")
                self.assertTrue(response.read().startswith(b"\xff\xd8\xff"))
            revision = self.manifests[INTERNAL]["revision"]
            with request(f"/api/video-frame?seq=264&camera_id={INTERNAL}&revision={revision}&frame_index=24") as response:
                self.assertTrue(response.read().startswith(b"\xff\xd8\xff"))
            for path, body in [("/api/analyze", {"seq": 264, "include_photos": True, "camera_id": OT}),
                               ("/api/video-prepare", {"seq": 264, "camera_id": OT}),
                               ("/api/video-calibrate", {"seq": 264, "camera_id": OT, "revision": data["revision"], "frame_index": 24})]:
                with mock.patch.object(self.store.jobs, "put_nowait"):
                    with request(path, body) as response:
                        self.assertEqual(response.status, 200)
            self.assertEqual(self.hub.ai_queue.get_nowait()[5], OT)
            for path, body in [("/api/video?seq=264&camera_id=", None),
                               ("/api/video?seq=264&camera_id=" + str(uuid.uuid4()), None),
                               (data["frames"][0]["url"].replace(OT, INTERNAL), None),
                               ("/api/analyze", {"seq": 264, "camera_id": str(uuid.uuid4())}),
                               ("/api/video-prepare", {"seq": 264, "camera_id": "bad"}),
                               ("/api/video-calibrate", {"seq": 264, "camera_id": INTERNAL, "revision": data["revision"], "frame_index": 24})]:
                with self.subTest(path=path), self.assertRaises(urllib.error.HTTPError) as error:
                    request(path, body)
                self.assertEqual(error.exception.code, 400)
            for path, body in [("/api/video?seq=264", None),
                               ("/api/analyze", {"seq": 264, "include_photos": True}),
                               ("/api/video-prepare", {"seq": 264, "camera_id": INTERNAL})]:
                with self.subTest(path=path), self.assertRaises(urllib.error.HTTPError) as error:
                    request(path, body, "http://untrusted.invalid")
                self.assertEqual(error.exception.code, 403)
            with request("/api/analyze", {"seq": 264, "include_photos": "true"}) as response:
                self.assertTrue(json.load(response)["ok"])
            self.assertIs(self.hub.ai_queue.get_nowait()[3], False)
            self.query.side_effect = RuntimeError("offline DB down")
            self.store.catalogs.clear()
            with request("/api/video?seq=264") as response:
                self.assertEqual(json.load(response)["status"], "missing")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_late_external_updates_automatic_but_not_manual_camera(self):
        self.query.return_value = [self.clips[0]]
        first = self.hub.video_info(264)
        self.assertEqual(first["camera_id"], INTERNAL)
        self.assertTrue(first["camera_auto"])
        self.query.return_value = self.clips
        self.store.catalogs.clear()
        later = self.hub.video_info(264)
        self.assertEqual(later["camera_id"], OT)
        self.assertTrue(later["camera_auto"])
        # 用户明确选择内置后，隐式重读或 force 不得擅自改回外接。
        chosen = self.hub.video_info(264, camera_id=INTERNAL)
        self.assertFalse(chosen["camera_auto"])
        self.store.catalogs.clear()
        self.assertEqual(self.hub.video_info(264)["camera_id"], INTERNAL)
        self.assertEqual(self.hub.video_info(264, force=True)["camera_id"], INTERNAL)


def preview(port=8771):
    manifest, old, store = sample_manifest()
    hub = make_hub(manifest, old, store)
    hub.analyses[264] = analysis.analyze(hub.shots[0]["m"], use_ai=False, cfg=hub.ai_cfg)
    class PreviewHandler(dashboard.Handler):
        def do_POST(self):
            self._send(403, b'{"error":"read-only no-AI preview"}')
        def do_GET(self):
            if self.path.split("?")[0] == "/api/status":
                self._send(200, json.dumps({**self.hub.status, "normalize": self.hub.norm}, ensure_ascii=False).encode())
                return
            if self.path.split("?")[0] == "/api/video-frame":
                self._send(403, b'{"error":"preview cannot generate new frames"}')
                return
            super().do_GET()
    PreviewHandler.hub = hub
    server = ThreadingHTTPServer(("127.0.0.1", port), PreviewHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


if __name__ == "__main__":
    import sys
    if "--preview" in sys.argv:
        preview()
    else:
        unittest.main()
