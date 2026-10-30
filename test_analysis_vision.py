"""离线视觉契约测试：只用临时配置和图片，所有 AI 请求均由 mock 接管。"""
import base64
import copy
import io
import json
import os
import stat
import tempfile
import unittest
import urllib.error
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import analysis


MID = "12345678-1234-4234-9234-123456789abc"
OTHER_MID = "87654321-4321-4321-8321-cba987654321"
# 一像素灰度 JPEG：固定量化表与只编码零系数的 Huffman 表，不需要图像依赖。
JPEG = (
    b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    + b"\xff\xdb\x00\x43\x00" + b"\x01" * 64
    + b"\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00"
    + b"\xff\xc4\x00\x26\x00\x01" + b"\x00" * 16
    + b"\x10\x01" + b"\x00" * 16
    + b"\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00\x3f\xff\xd9"
)


def response(data=None):
    if data is None:
        data = {"summary": "按雷达数据组织的总结", "highlights": [], "issues": [], "drills": [],
                "visual_findings": [{"title": "候选阶段姿态", "detail": "候选阶段身体轮廓可见，遮挡使判断有限。",
                                     "evidence_frames": [4, 5], "confidence": 0.6}]}
    return {"choices": [{"message": {"content": json.dumps(data, ensure_ascii=False)}}]}


class AnalysisVisionTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.tmp = self.stack.enter_context(tempfile.TemporaryDirectory(prefix="analysis_vision_test_"))
        self.here = Path(self.tmp)
        self.stack.enter_context(patch.object(analysis, "HERE", self.here))
        self.stack.enter_context(patch.object(analysis, "CONFIG_FILE", self.here / "sandbox_config.json"))
        self.stack.enter_context(patch.dict(os.environ, {}, clear=True))
        self.stack.enter_context(patch("urllib.request.urlopen", side_effect=AssertionError("禁止真实网络")))
        self.stack.enter_context(patch("urllib.request.build_opener", side_effect=AssertionError("禁止真实网络")))
        self.post = self.stack.enter_context(patch.object(analysis, "_post_json", return_value=response()))
        self.cfg = {"base_url": "https://text.invalid/v1", "model": "text-only-model",
                    "api_key": "fake-text-secret", "vision_base_url": "https://vision.invalid/v1",
                    "vision_model": "explicit-vision-model", "vision_api_key": "fake-vision-secret",
                    "vision_enabled": True}
        self.m = {"measurement_id": MID, "club": "Sand Wedge", "clubZh": "56°",
                  "clubRaw": "SW 56", "club_src": "tps", "ballSpeed": 160.0,
                  "clubSpeed": 100.0, "smash": 1.25, "impactHeight": 12, "impactOffset": 7}
        self.video = {"expected_measurement_id": MID, "clip": {"measurement_id": MID},
                      "revision": "test_revision_1", "status": "candidate",
                      "calibration": {"status": "candidate", "confidence": 0.4,
                                      "impact_manually_confirmed": False},
                      "frames8": [{"file": f"frame_{i}.jpg", "stage": f"阶段{i}",
                                   "stage_status": "candidate", "stage_confidence": i / 10}
                                  for i in range(1, 9)]}
        self.directory = self.here / "video_frames" / self.video["revision"]
        self.directory.mkdir(parents=True)
        self.images = []
        for i, frame in enumerate(self.video["frames8"], 1):
            image = JPEG[:-2] + b"\xff\xfe\x00\x03" + bytes([i]) + JPEG[-2:]
            (self.directory / frame["file"]).write_bytes(image)
            self.images.append(image)

    def run_analysis(self, **kwargs):
        return analysis.analyze(self.m, cfg=self.cfg, video=self.video, **kwargs)

    def assert_rules_without_images(self, result, calls=0):
        self.assertEqual(result["input_mode"], "rules")
        self.assertEqual(result["source"], "rules")
        self.assertEqual(result["image_count"], 0)
        self.assertEqual(result["visual_findings"], [])
        self.assertIn("图片未参与", result["notice"])
        self.assertEqual(self.post.call_count, calls)
        self.assert_no_secrets(result)

    def assert_no_secrets(self, obj):
        serialized = json.dumps(obj, ensure_ascii=False)
        for field in ("api_key", "vision_api_key"):
            if self.cfg.get(field):
                self.assertNotIn(self.cfg[field], serialized)

    def test_eight_images_order_same_guid_and_independent_credentials(self):
        original_m, original_video, original_cfg = copy.deepcopy((self.m, self.video, self.cfg))
        result = self.run_analysis()
        self.assertEqual((result["input_mode"], result["image_count"], result["model"]),
                         ("vision", 8, self.cfg["vision_model"]))
        self.assertEqual(result["video_revision"], self.video["revision"])
        self.assertEqual(result["visual_findings"][0]["evidence_frames"], [4, 5])
        self.assertIn("候选", result["notice"])
        self.assertEqual(self.post.call_count, 1)
        url, body, headers, timeout = self.post.call_args.args
        self.assertEqual(url, self.cfg["vision_base_url"] + "/chat/completions")
        self.assertEqual(headers["Authorization"], "Bearer " + self.cfg["vision_api_key"])
        self.assertEqual(timeout, 60)
        self.assertEqual(body["model"], self.cfg["vision_model"])
        self.assertEqual(body["response_format"], {"type": "json_object"})
        content = body["messages"][1]["content"]
        self.assertEqual(len(content), 9)
        self.assertEqual(content[0]["type"], "text")
        self.assertIn("球速：160.0mph", content[0]["text"])
        self.assertIn("实际球杆：56°", content[0]["text"])
        self.assertIn("正趾负跟", content[0]["text"])
        radar_text, metadata_text = content[0]["text"].split("同杆视频阶段/校准元数据：\n", 1)
        self.assertEqual(json.loads(radar_text.split("同杆归一化实测 m（击球点 mm，禁止覆盖）：\n", 1)[1]), self.m)
        metadata = json.loads(metadata_text)
        self.assertEqual(metadata["measurement_id"], MID)
        self.assertEqual(metadata["calibration"]["confidence"], 0.4)
        self.assertFalse(metadata["impact_manually_confirmed"])
        for i, (part, expected) in enumerate(zip(content[1:], self.images), 1):
            self.assertEqual(part["type"], "image_url")
            prefix, encoded = part["image_url"]["url"].split(",", 1)
            self.assertEqual(prefix, "data:image/jpeg;base64")
            self.assertEqual(base64.b64decode(encoded, validate=True), expected)
            self.assertEqual(metadata["stages"][i - 1]["stage"], f"阶段{i}")
            self.assertEqual(metadata["stages"][i - 1]["confidence"], i / 10)
        system = body["messages"][0]["content"]
        for restriction in ("禁止断言精确触球", "精确 3D 关节角", "不得覆盖雷达数值", "不编造"):
            self.assertIn(restriction, system)
        self.assert_no_secrets(body)
        self.assert_no_secrets(result)
        self.assertEqual((self.m, self.video, self.cfg), (original_m, original_video, original_cfg))

    def test_vision_does_not_require_text_key(self):
        self.cfg["api_key"] = ""
        self.assertEqual(self.run_analysis()["input_mode"], "vision")
        self.assertEqual(self.post.call_count, 1)

    def test_manual_confirmation_is_explicit_boolean(self):
        for confirmed in (True, False, "true", 1):
            with self.subTest(confirmed=confirmed):
                self.video["calibration"]["impact_manually_confirmed"] = confirmed
                result = self.run_analysis()
                self.assertEqual(result["input_mode"], "vision")
                self.assertEqual("候选" in result["notice"], confirmed is not True)

    def test_trusted_manifest_manual_status_is_supported(self):
        self.video["calibration"] = {"status": "manual_confirmed", "confidence": 1}
        result = self.run_analysis()
        self.assertEqual(result["input_mode"], "vision")
        self.assertNotIn("未经人工确认", result["notice"])
        metadata = json.loads(self.post.call_args.args[1]["messages"][1]["content"][0]["text"].split(
            "同杆视频阶段/校准元数据：\n", 1)[1])
        self.assertTrue(metadata["impact_manually_confirmed"])
        self.video["calibration"]["status"] = "automatic_candidate"
        self.assertIn("未经人工确认", self.run_analysis()["notice"])

    def test_disabled_or_incomplete_vision_uses_only_text(self):
        variants = [{"vision_enabled": False}, {"vision_enabled": "true"}, {"vision_enabled": 1},
                    {"vision_model": ""}, {"vision_base_url": ""}, {"vision_api_key": ""},
                    {"vision_model": "   "}]
        original = self.cfg.copy()
        for changes in variants:
            with self.subTest(changes=changes):
                self.cfg = {**original, **changes, "model": "model-with-vision-in-name"}
                self.post.reset_mock()
                with patch.object(analysis, "_vision_content", side_effect=AssertionError("文本不应读图")):
                    result = self.run_analysis()
                self.assertEqual(result["input_mode"], "text")
                self.assertEqual(result["image_count"], 0)
                self.assertEqual(result["visual_findings"], [])
                self.assertIn("图片未参与", result["notice"])
                url, body, headers, _ = self.post.call_args.args
                self.assertEqual(url, self.cfg["base_url"] + "/chat/completions")
                self.assertEqual(headers["Authorization"], "Bearer " + self.cfg["api_key"])
                self.assertIsInstance(body["messages"][1]["content"], str)
                self.assertNotIn("image_url", json.dumps(body))

    def test_ordinary_text_is_unchanged_even_with_enabled_vision(self):
        with patch.object(analysis, "_vision_content", side_effect=AssertionError("未提供 video 不应读图")):
            result = analysis.analyze(self.m, cfg=self.cfg)
        body = self.post.call_args.args[1]
        self.assertEqual(result["input_mode"], "text")
        self.assertEqual(result["notice"], "")
        self.assertIsNone(result["video_revision"])
        self.assertIsInstance(body["messages"][1]["content"], str)
        self.assertEqual(body["messages"][0]["content"], analysis.SYSTEM_PROMPT)
        self.assertEqual(body["max_tokens"], 1400)

    def test_rules_when_ai_disabled_or_no_usable_key(self):
        self.assert_rules_without_images(self.run_analysis(use_ai=False))
        self.cfg.update(api_key="", vision_api_key="")
        self.assert_rules_without_images(self.run_analysis())

    def test_explicit_empty_cfg_does_not_load_real_config(self):
        with patch.object(analysis, "load_config", side_effect=AssertionError("不应隐式读取配置")):
            self.assert_rules_without_images(analysis.analyze(self.m, cfg={}, video=self.video))
            with self.assertRaises(RuntimeError):
                analysis.llm_analyze(self.m, analysis.rule_diagnose(self.m), cfg={}, video=self.video)

    def test_guid_and_expected_id_are_checked_before_request(self):
        mutations = [lambda v: v.pop("expected_measurement_id"),
                     lambda v: v.update(expected_measurement_id=OTHER_MID),
                     lambda v: v["clip"].update(measurement_id=OTHER_MID),
                     lambda v: v["clip"].update(measurement_id="bad-guid"),
                     lambda v: v["clip"].update(measurement_id="00000000-0000-0000-0000-000000000000"),
                     lambda v: v.update(measurement_id=OTHER_MID),
                     lambda v: v["frames8"][0].update(measurement_id=OTHER_MID),
                     lambda v: v["frames8"][0].update(revision="other_revision")]
        original = copy.deepcopy(self.video)
        for index, mutate in enumerate(mutations):
            with self.subTest(case=index):
                self.video = copy.deepcopy(original)
                mutate(self.video)
                self.assert_rules_without_images(self.run_analysis())
        self.video = original
        self.m["measurement_id"] = OTHER_MID
        self.assert_rules_without_images(self.run_analysis())

    def test_guid_normalization_accepts_uppercase(self):
        self.video["clip"]["measurement_id"] = MID.upper()
        self.assertEqual(self.run_analysis()["input_mode"], "vision")

    def test_revision_and_filename_paths_are_rejected(self):
        original = copy.deepcopy(self.video)
        for revision in ("..", ".", "../escape", "..\\escape", "D:/escape", "/escape", "bad:rev", ""):
            with self.subTest(revision=revision):
                self.video = copy.deepcopy(original)
                self.video["revision"] = revision
                self.assert_rules_without_images(self.run_analysis())
        for filename in ("../escape.jpg", "..\\escape.jpg", "D:/escape.jpg", "/escape.jpg",
                         "\\\\host\\escape.jpg", "https://remote.invalid/a.jpg", "x.png", "missing.jpg",
                         "frame_2.jpg", "frame_2.JPG", "x.jpg:stream"):
            with self.subTest(filename=filename):
                self.video = copy.deepcopy(original)
                self.video["frames8"][0]["file"] = filename
                self.assert_rules_without_images(self.run_analysis())

    def test_manifest_must_have_exactly_eight_frames(self):
        original = copy.deepcopy(self.video)
        for frames in ([], original["frames8"][:7], original["frames8"] + [original["frames8"][0]],
                       {}, [None] * 8):
            with self.subTest(count=len(frames)):
                self.video = {**original, "frames8": frames}
                self.assert_rules_without_images(self.run_analysis())
        self.assert_rules_without_images(analysis.analyze(self.m, cfg=self.cfg, video={}))

    def test_calibration_invalid_shape_fails_closed(self):
        self.video["calibration"] = "candidate"
        self.assert_rules_without_images(self.run_analysis())

    def test_total_jpeg_limit_includes_all_eight(self):
        size = sum(map(len, self.images))
        with patch.object(analysis, "MAX_VISION_IMAGE_BYTES", size - 1):
            self.assert_rules_without_images(self.run_analysis())
        with patch.object(analysis, "MAX_VISION_IMAGE_BYTES", size):
            self.assertEqual(self.run_analysis()["input_mode"], "vision")

    def test_jpeg_content_and_truncation_are_checked(self):
        first = self.directory / self.video["frames8"][0]["file"]
        for invalid in (b"\x89PNG\r\n\x1a\n", self.images[0][:-2], b""):
            with self.subTest(length=len(invalid)):
                first.write_bytes(invalid)
                self.assert_rules_without_images(self.run_analysis())

    def test_symlink_escape_is_rejected(self):
        first = self.directory / self.video["frames8"][0]["file"]
        outside = self.here / "outside.jpg"
        outside.write_bytes(self.images[0])
        first.unlink()
        try:
            first.symlink_to(outside)
        except OSError:
            # 没有 Windows 链接权限时仍覆盖拒绝分支，不访问任何真实文件。
            first.write_bytes(self.images[0])
            original = Path.is_symlink
            with patch.object(Path, "is_symlink", autospec=True,
                              side_effect=lambda p: p == first or original(p)):
                self.assert_rules_without_images(self.run_analysis())
        else:
            self.assert_rules_without_images(self.run_analysis())

    def test_revision_and_root_reparse_points_are_rejected(self):
        original = Path.lstat
        for target in (self.directory, self.directory.parent):
            with self.subTest(target=target.name):
                def fake_lstat(path, *args, **kwargs):
                    if path == target:
                        return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
                    return original(path, *args, **kwargs)
                with patch.object(Path, "lstat", autospec=True, side_effect=fake_lstat):
                    self.assert_rules_without_images(self.run_analysis())

    def test_file_changes_during_read_fail_before_request(self):
        original = analysis._checked_frame_path
        count = 0
        def changing_path(revision, filename):
            nonlocal count
            count += 1
            path = original(revision, filename)
            if count == 10:
                path.write_bytes(self.images[0] + b"changed")
            return path
        with patch.object(analysis, "_checked_frame_path", side_effect=changing_path):
            self.assert_rules_without_images(self.run_analysis())

    def test_growth_after_preflight_still_obeys_read_limit(self):
        original = analysis._checked_frame_path
        limit = sum(map(len, self.images))
        count = 0
        def growing_path(revision, filename):
            nonlocal count
            count += 1
            path = original(revision, filename)
            if count == 9:
                path.write_bytes(b"\xff\xd8\xff" + b"x" * limit + b"\xff\xd9")
            return path
        with patch.object(analysis, "MAX_VISION_IMAGE_BYTES", limit), \
                patch.object(analysis, "_checked_frame_path", side_effect=growing_path):
            self.assert_rules_without_images(self.run_analysis())

    def test_opened_file_identity_is_checked(self):
        changed = SimpleNamespace(st_mode=stat.S_IFREG, st_dev=-1, st_ino=-1)
        with patch.object(analysis.os, "fstat", return_value=changed):
            self.assert_rules_without_images(self.run_analysis())

    def test_unsafe_vision_url_is_rejected_without_echoing_secrets(self):
        for url in ("file:///local", "https://user:fake-vision-secret@vision.invalid/v1",
                    "https://vision.invalid/v1?key=fake-vision-secret"):
            with self.subTest(url=url):
                self.cfg["vision_base_url"] = url
                self.assert_rules_without_images(self.run_analysis())

    def test_vision_http_errors_never_retry_or_call_text(self):
        for code in (400, 422, 401, 429, 500):
            with self.subTest(code=code):
                self.post.reset_mock()
                self.post.side_effect = urllib.error.HTTPError(
                    "https://vision.invalid/v1", code, "mock", {},
                    io.BytesIO((self.cfg["api_key"] + self.cfg["vision_api_key"]).encode()))
                self.assert_rules_without_images(self.run_analysis(), calls=1)

    def test_vision_network_timeout_and_parse_failures_degrade_once(self):
        for error in (TimeoutError("fake-vision-secret"), urllib.error.URLError("fake-text-secret"),
                      RuntimeError("fake-text-secret fake-vision-secret")):
            with self.subTest(error=type(error).__name__):
                self.post.reset_mock()
                self.post.side_effect = error
                self.assert_rules_without_images(self.run_analysis(), calls=1)
        self.post.side_effect = None
        for bad in ({}, {"choices": []}, {"choices": [{"message": {"content": "not json"}}]},
                    response({"summary": ""}), response(["bad shape"])):
            with self.subTest(response_shape=type(bad).__name__):
                self.post.reset_mock()
                self.post.return_value = bad
                self.assert_rules_without_images(self.run_analysis(), calls=1)

    def test_text_compatibility_retry_is_preserved_without_images(self):
        error = urllib.error.HTTPError("https://text.invalid/v1", 400, "mock", {}, io.BytesIO(b"format"))
        self.post.side_effect = [error, response()]
        result = analysis.analyze(self.m, cfg=self.cfg)
        self.assertEqual(result["input_mode"], "text")
        self.assertEqual(self.post.call_count, 2)
        for call in self.post.call_args_list:
            self.assertIsInstance(call.args[1]["messages"][1]["content"], str)
        self.assertNotIn("response_format", self.post.call_args.args[1])

    def test_text_errors_redact_both_keys(self):
        self.post.side_effect = RuntimeError("fake-text-secret fake-vision-secret")
        result = analysis.analyze(self.m, cfg=self.cfg)
        self.assertEqual(result["input_mode"], "rules")
        self.assert_no_secrets(result)
        self.assertIn("已隐藏", result["notice"])

    def test_visual_findings_require_real_evidence_and_valid_confidence(self):
        valid = {"title": "可见轮廓", "detail": "轮廓可见但存在遮挡。", "evidence_frames": [2, 2, 8], "confidence": 0.5}
        invalid = [{**valid, "evidence_frames": frames} for frames in ([], [0], [9], [True], [1.0], "1")]
        invalid += [{**valid, "confidence": value} for value in (-0.1, 1.1, True, "high", float("nan"))]
        invalid += [{**valid, "detail": "3D 关节角为 90°。"}, {**valid, "detail": "球速为 120mph。"},
                    {**valid, "detail": "第六帧是精确触球瞬间。"}, {**valid, "title": ""}, None]
        self.post.return_value = response({"summary": "雷达总结", "visual_findings": [valid] + invalid,
                                          "image_count": 99, "input_mode": "rules", "ballSpeed": 999})
        result = self.run_analysis()
        self.assertEqual(len(result["visual_findings"]), 1)
        self.assertEqual(result["visual_findings"][0]["evidence_frames"], [2, 8])
        self.assertEqual(result["image_count"], 8)
        self.assertEqual(result["input_mode"], "vision")
        self.assertNotIn("ballSpeed", result)
        self.assertEqual(self.m["ballSpeed"], 160)

    def test_absent_visual_findings_are_not_invented(self):
        self.post.return_value = response({"summary": "雷达总结"})
        result = self.run_analysis()
        self.assertEqual(result["input_mode"], "vision")
        self.assertEqual(result["visual_findings"], [])

    def test_llm_entry_supports_video_and_custom_timeout(self):
        result = analysis.llm_analyze(self.m, analysis.rule_diagnose(self.m, "Sand Wedge"),
                                      cfg=self.cfg, timeout=45, video=self.video)
        self.assertEqual(result["input_mode"], "vision")
        self.assertEqual(self.post.call_args.args[3], 45)

    def test_club_source_fix_survives_rules_text_and_vision(self):
        for mode in ("rules", "text", "vision"):
            with self.subTest(mode=mode):
                result = analysis.analyze(self.m, cfg=self.cfg, use_ai=mode != "rules",
                                          video=self.video if mode == "vision" else None)
                self.assertEqual(result["club"], "Sand Wedge")
                self.assertEqual(result["club_label"], "56°")
                self.assertEqual(result["club_source"], "tps")
                self.assertFalse(result["club_guessed"])
        manual = self.run_analysis(club="Driver")
        self.assertEqual(manual["club_label"], "一号木")
        self.assertEqual(manual["club_source"], "manual")
        self.assertIn("实际球杆：一号木", self.post.call_args.args[1]["messages"][1]["content"][0]["text"])
        guessed = analysis.analyze({"ballSpeed": 160}, use_ai=False, cfg=self.cfg)
        self.assertTrue(guessed["club_guessed"])
        self.assertEqual(guessed["club_source"], "guess")

    def test_impact_units_mm_and_positive_toe_negative_heel(self):
        for height, offset, vertical, horizontal, level in (
                (12, 7, "偏上", "偏趾", "good"), (-16, -11, "偏下", "偏跟", "warn"),
                (0, 0, "居中", "居中", "good"), (15, 0, "偏上", "居中", "warn")):
            with self.subTest(height=height, offset=offset):
                m = {"impactHeight": height, "impactOffset": offset}
                diag = analysis.rule_diagnose(m, "7 Iron")
                finding = diag["findings"][0]
                self.assertIn(vertical, finding["detail"])
                self.assertIn(horizontal, finding["detail"])
                self.assertIn(f"{height:+.1f} mm", finding["detail"])
                self.assertIn(f"{offset:+.1f} mm", finding["detail"])
                self.assertNotIn(" in", finding["detail"])
                self.assertEqual(finding["level"], level)
                prompt = analysis.build_prompt(m, diag)
                self.assertIn(f"{height:.1f}mm", prompt)
                self.assertIn("正趾负跟", prompt)

    def test_config_defaults_and_legacy_text_values(self):
        cfg = analysis.load_config()
        self.assertIs(cfg["vision_enabled"], False)
        self.assertIs(cfg["include_photos"], False)
        for field in ("vision_base_url", "vision_model", "vision_api_key"):
            self.assertEqual(cfg[field], "")
        legacy = {key: self.cfg[key] for key in ("base_url", "api_key", "model")}
        analysis.CONFIG_FILE.write_text(json.dumps(legacy), encoding="utf-8")
        cfg = analysis.load_config()
        for key, value in legacy.items():
            self.assertEqual(cfg[key], value)
        self.assertIs(cfg["vision_enabled"], False)

    def test_config_partial_saves_preserve_text_and_vision(self):
        analysis.save_config(self.cfg)
        analysis.save_config({"model": "changed-text"})
        cfg = analysis.load_config()
        self.assertEqual(cfg["vision_model"], self.cfg["vision_model"])
        self.assertEqual(cfg["vision_api_key"], self.cfg["vision_api_key"])
        self.assertIs(cfg["vision_enabled"], True)
        analysis.save_config({"vision_model": "changed-vision", "vision_enabled": False})
        cfg = analysis.load_config()
        self.assertEqual(cfg["model"], "changed-text")
        self.assertEqual(cfg["api_key"], self.cfg["api_key"])
        self.assertEqual(cfg["base_url"], self.cfg["base_url"])
        self.assertEqual(cfg["vision_model"], "changed-vision")
        self.assertIs(cfg["vision_enabled"], False)
        stored = json.loads(analysis.CONFIG_FILE.read_text(encoding="utf-8"))
        self.assertIs(stored["vision_enabled"], False)

    def test_photo_config_partial_updates_preserve_false_and_both_keys(self):
        analysis.save_config({**self.cfg, "include_photos": True})
        saved = analysis.save_config({"include_photos": False})
        self.assertIs(saved["include_photos"], False)
        for key in ("api_key", "vision_api_key", "vision_enabled"):
            self.assertEqual(saved[key], self.cfg[key])
        analysis.save_config({"model": "changed-text"})
        analysis.save_config({"vision_model": "changed-vision"})
        with patch.dict(os.environ, {"TM_AI_INCLUDE_PHOTOS": "true"}):
            cfg = analysis.load_config()
        self.assertIs(cfg["include_photos"], False)
        self.assertIs(json.loads(analysis.CONFIG_FILE.read_text(encoding="utf-8"))["include_photos"], False)
        self.assertEqual(cfg["api_key"], self.cfg["api_key"])
        self.assertEqual(cfg["vision_api_key"], self.cfg["vision_api_key"])

    def test_photo_environment_is_optional_and_does_not_enable_vision(self):
        for value, expected in (("true", True), ("false", False), ("", False)):
            with self.subTest(value=value), patch.dict(os.environ, {"TM_AI_INCLUDE_PHOTOS": value}):
                cfg = analysis.load_config()
                self.assertIs(cfg["include_photos"], expected)
                self.assertIs(cfg["vision_enabled"], False)

    def test_global_opt_out_after_image_read_sends_only_text_once(self):
        allowed = iter((True, False))
        result = analysis.analyze(self.m, cfg=self.cfg, video=self.video,
                                  photos_allowed=lambda: next(allowed))
        self.assertEqual((result["input_mode"], result["image_count"]), ("text", 0))
        self.assertIn("开关关闭", result["notice"])
        self.post.assert_called_once()
        url, body, headers, _ = self.post.call_args.args
        self.assertEqual(url, self.cfg["base_url"] + "/chat/completions")
        self.assertEqual(headers["Authorization"], "Bearer " + self.cfg["api_key"])
        self.assertIsInstance(body["messages"][1]["content"], str)
        self.assertNotIn("image_url", json.dumps(body))

    def test_global_opt_out_before_image_read_does_not_read_images(self):
        with patch.object(analysis, "_vision_content", side_effect=AssertionError("关闭后禁止读图")):
            result = analysis.analyze(self.m, cfg=self.cfg, video=self.video, photos_allowed=lambda: False)
        self.assertEqual(result["input_mode"], "text")
        self.assertIn("开关关闭", result["notice"])
        self.post.assert_called_once()

    def test_global_opt_out_without_text_key_does_not_call_model(self):
        self.cfg["api_key"] = ""
        result = analysis.analyze(self.m, cfg=self.cfg, video=self.video, photos_allowed=lambda: False)
        self.assertEqual(result["input_mode"], "rules")
        self.post.assert_not_called()

    def test_vision_environment_and_boolean_false_precedence(self):
        environment = {"TM_VISION_BASE_URL": self.cfg["vision_base_url"],
                       "TM_VISION_MODEL": self.cfg["vision_model"],
                       "TM_VISION_API_KEY": self.cfg["vision_api_key"]}
        for raw, expected in (("true", True), ("1", True), ("yes", True), ("on", True),
                              ("false", False), ("0", False), ("", False), ("bad", False)):
            with self.subTest(enabled=raw), patch.dict(os.environ, {**environment, "TM_VISION_ENABLED": raw}):
                cfg = analysis.load_config()
                self.assertIs(cfg["vision_enabled"], expected)
                self.assertEqual(cfg["vision_model"], self.cfg["vision_model"])
                self.assertEqual(cfg["vision_base_url"], self.cfg["vision_base_url"])
                self.assertEqual(cfg["vision_api_key"], self.cfg["vision_api_key"])
        analysis.CONFIG_FILE.write_text(json.dumps({"vision_enabled": False}), encoding="utf-8")
        with patch.dict(os.environ, {"TM_VISION_ENABLED": "true"}):
            self.assertIs(analysis.load_config()["vision_enabled"], False)


if __name__ == "__main__":
    unittest.main()
