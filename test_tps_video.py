"""小范围离线测试：不连接 TPS、不读取真实原片、不调用网络或安装依赖。"""
import inspect
import json
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest import mock
from contextlib import ExitStack, contextmanager

from PIL import Image, ImageDraw
import tps_video as video


MID = "32718f00-3a06-4c80-b33d-378983221267"


class PrepareVideoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "source"
        self.source.mkdir()
        self.original = self.source / "clip.mov"
        self.original.write_bytes(b"offline immutable source")
        self.root = self.base / "output"
        self.clip = {"measurement_id": MID, "stroke_id": MID.upper(), "path": str(self.original),
                     "status": "Success", "rotation": "0", "video_speed_factor": 1.0,
                     "has_explicit_frame_timestamps": False, "impact_real_seconds": 2.0,
                     "camera_name": "iPhone"}
        self.timeline = [(i, i * 0.033 + i * i * 0.00001) for i in range(101)]

    def export(self, ffmpeg, original, output, records):
        for record in records:
            Image.new("RGB", (24, 24), "gray").save(output / record["file"], "JPEG")

    @contextmanager
    def offline(self, timeline=None, sampler=None, exporter=None):
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(video, "TPS_FFMPEG", self.original))
            query = stack.enter_context(mock.patch.object(video, "query_clips", side_effect=AssertionError("不得查询数据库")))
            stack.enter_context(mock.patch.object(video, "probe_frames", return_value=self.timeline if timeline is None else timeline))
            stack.enter_context(mock.patch.object(video, "_visual_samples", side_effect=sampler or RuntimeError("离线测试视觉不可用")))
            stack.enter_context(mock.patch.object(video, "_export_jpegs", side_effect=exporter or self.export))
            stack.enter_context(mock.patch.object(video, "contact_sheet", return_value=None))
            stack.enter_context(mock.patch.object(video, "shot_context", return_value=None))
            yield query

    def prepare(self, **kwargs):
        return video.prepare_video(MID, clip=self.clip, output_root=self.root, **kwargs)

    def assert_manifest(self, manifest):
        revision = manifest["revision"]
        self.assertTrue(revision.startswith(MID + "_"))
        self.assertEqual(Path(revision).name, revision)
        output = self.root / revision
        self.assertTrue(output.resolve().is_relative_to(self.root.resolve()))
        self.assertEqual(json.loads((output / "manifest.json").read_text(encoding="utf-8")), manifest)
        stages = manifest["frames"]
        self.assertEqual([s["name"] for s in stages], [c[0] for c in video.CANDIDATES])
        indices = [s["frame_index_zero_based"] for s in stages]
        self.assertEqual(len(indices), 8)
        self.assertTrue(all(a < b for a, b in zip(indices, indices[1:])))
        candidates = manifest["calibration"]["candidates"]
        self.assertEqual(len(candidates), min(21, manifest["decoded_frame_count"]))
        self.assertTrue(all(b["frame_index_zero_based"] == a["frame_index_zero_based"] + 1
                            for a, b in zip(candidates, candidates[1:])))
        files = [s["file"] for s in stages + candidates]
        self.assertEqual(len(files), len(set(files)))
        for record in stages + candidates:
            self.assertEqual(Path(record["file"]).name, record["file"])
            self.assertEqual(record["actual_container_seconds"], self.timeline[record["frame_index_zero_based"]][1])
            with Image.open(output / record["file"]) as image:
                image.verify()
        for stage in stages:
            self.assertTrue(0 <= stage["confidence"] <= 1)
            if stage["stage_status"] != "manual_confirmed":
                self.assertLessEqual(stage["confidence"], 0.45)
        calibration = manifest["calibration"]
        self.assertFalse(calibration["ball_evidence"]["is_ball_model"])
        if calibration["status"] == "unavailable":
            self.assertIsNone(calibration["impact_frame_index"])
            self.assertIsNone(calibration["impact_container_seconds"])
            self.assertIsNone(manifest["impact_container_seconds"])
            self.assertEqual(calibration["confidence"], 0)
            self.assertTrue(all(s["real_seconds_from_impact_mark"] is None for s in stages))
        else:
            self.assertIn(calibration["impact_frame_index"], [c["frame_index_zero_based"] for c in candidates])
            self.assertEqual(stages[6]["frame_index_zero_based"], calibration["impact_frame_index"])
            self.assertEqual(stages[6]["actual_container_seconds"], calibration["impact_container_seconds"])
        return output

    def test_exact_signature(self):
        signature = inspect.signature(video.prepare_video)
        self.assertEqual(list(signature.parameters), ["measurement_id", "impact_frame", "confirmed", "output_root", "clip", "camera_id"])
        for name in list(signature.parameters)[1:]:
            self.assertEqual(signature.parameters[name].kind, inspect.Parameter.KEYWORD_ONLY)

    def test_automatic_fallback_and_new_revisions(self):
        original_hash = video.sha256(self.original)
        with self.offline() as query:
            first = self.prepare()
            first_output = self.assert_manifest(first)
            first_hash = video.sha256(first_output / "manifest.json")
            second = self.prepare()
            self.assert_manifest(second)
            query.assert_not_called()
        self.assertNotEqual(first["revision"], second["revision"])
        self.assertEqual(video.sha256(first_output / "manifest.json"), first_hash)
        self.assertEqual(video.sha256(self.original), original_hash)
        self.assertEqual(first["calibration"]["status"], "candidate")
        self.assertEqual(first["top_detection"]["status"], "fallback_candidate")
        self.assertEqual(len(list(self.root.iterdir())), 2)

    def test_manual_uses_actual_vfr_pts_with_explicit_timestamps(self):
        self.timeline = [(i, 1.25 + i * 0.031 + i * i * 0.000017) for i in range(101)]
        self.clip.update(has_explicit_frame_timestamps=True, video_speed_factor=8.0, impact_real_seconds=9999)
        with self.offline():
            result = self.prepare(impact_frame=61, confirmed=True)
        self.assert_manifest(result)
        self.assertEqual(result["calibration"]["status"], "manual_confirmed")
        self.assertEqual(result["calibration"]["method"], "manual_frame_index")
        self.assertEqual(result["calibration"]["impact_frame_index"], 61)
        self.assertEqual(result["impact_container_seconds"], self.timeline[61][1])
        self.assertNotEqual(result["impact_container_seconds"], 61 / 30)
        self.assertEqual(result["frames"][6]["stage_status"], "manual_confirmed")

    def test_unconfirmed_frame_is_not_manual_confirmation(self):
        self.clip["has_explicit_frame_timestamps"] = True
        with self.offline():
            result = self.prepare(impact_frame=60)
        self.assert_manifest(result)
        self.assertEqual(result["calibration"]["status"], "candidate")
        self.assertLessEqual(result["calibration"]["confidence"], 0.45)

    def test_manual_cannot_be_replaced_by_visual_candidate(self):
        with self.offline(), mock.patch.object(video, "_ball_disappearance", side_effect=AssertionError("不得替换人工帧")):
            result = self.prepare(impact_frame=60, confirmed=True)
        self.assertEqual(result["calibration"]["impact_frame_index"], 60)
        self.assertEqual(result["calibration"]["method"], "manual_frame_index")

    @staticmethod
    def ball_samples(indices, departure=61, reappear=None, two_balls=False, exposure=False, occlusion=False):
        samples = []
        for i in indices:
            image = Image.new("RGB", (320, 320), (40, 100, 45))
            draw = ImageDraw.Draw(image)
            if i < departure or (reappear is not None and i >= reappear):
                draw.ellipse((158, 263, 162, 267), fill="white")
                if two_balls:
                    draw.ellipse((198, 263, 202, 267), fill="white")
            elif exposure:
                image = Image.new("RGB", (320, 320), "black")
            elif occlusion:
                draw.rectangle((148, 253, 172, 277), fill="black")
            samples.append((i, image))
        return samples

    def test_ball_disappearance_is_only_low_confidence_candidate(self):
        def samples(ffmpeg, original, indices, size=96):
            return self.ball_samples(indices) if size == 320 else [(i, Image.new("RGB", (96, 96), "gray")) for i in indices]
        with self.offline(sampler=samples):
            result = self.prepare(impact_frame=60)
        self.assert_manifest(result)
        self.assertEqual(result["calibration"]["impact_frame_index"], 61)
        self.assertEqual(result["calibration"]["status"], "candidate")
        self.assertEqual(result["calibration"]["confidence"], 0.30)
        self.assertIn("heuristic", result["calibration"]["method"])
        self.assertTrue(result["calibration"]["ball_detection_performed"])

    def test_no_ball_preserves_tps_candidate_even_with_motion(self):
        def samples(ffmpeg, original, indices, size=96):
            images = []
            for i in indices:
                image = Image.new("RGB", (size, size), "gray")
                if i >= 61:
                    ImageDraw.Draw(image).rectangle((size // 3, size // 2, size // 2, size * 3 // 4), fill="white")
                images.append((i, image))
            return images
        with self.offline(sampler=samples):
            result = self.prepare()
        expected = video._nearest_frame(self.timeline, self.clip["impact_real_seconds"])
        self.assertEqual(result["calibration"]["impact_frame_index"], expected)
        self.assertEqual(result["calibration"]["method"], "tps_time_candidate")
        self.assertEqual(result["calibration"]["ball_evidence"]["status"], "unavailable")

    def test_ball_rejects_occlusion_exposure_ambiguity_and_static_ball(self):
        for kwargs in [{"reappear": 64}, {"two_balls": True}, {"exposure": True},
                       {"occlusion": True}, {"departure": 99}]:
            with self.subTest(kwargs=kwargs):
                result = video._ball_disappearance(self.ball_samples(range(50, 72), **kwargs))
                self.assertEqual(result["status"], "unavailable")
        self.assertEqual(video._ball_disappearance([])["status"], "unavailable")

    def test_invalid_confirmation_and_indices_leave_no_output(self):
        cases = [{"confirmed": True}, {"confirmed": "true", "impact_frame": 60},
                 *({"impact_frame": n, "confirmed": True} for n in [True, -1, 1.2, "60", 101, 5, 100])]
        with self.offline():
            for kwargs in cases:
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    self.prepare(**kwargs)
        self.assertFalse(self.root.exists())

    def test_unsafe_mapping_returns_unavailable_not_fake_anchor(self):
        with self.offline():
            for changes in [{"has_explicit_frame_timestamps": True}, {"impact_real_seconds": 99},
                            {"impact_real_seconds": 0}]:
                with self.subTest(changes=changes), mock.patch.dict(self.clip, changes):
                    result = self.prepare()
                    self.assert_manifest(result)
                    self.assertEqual(result["calibration"]["status"], "unavailable")
                    self.assertEqual(result["calibration"]["method"], "unavailable_tps_mapping")
        self.timeline = [(i, pts + 1) for i, pts in self.timeline]
        with self.offline():
            result = self.prepare()
        self.assert_manifest(result)
        self.assertEqual(result["calibration"]["status"], "unavailable")

    def test_short_clip_and_invalid_metadata(self):
        with self.offline(timeline=self.timeline[:7]), self.assertRaises(ValueError):
            self.prepare(impact_frame=6, confirmed=True)
        with self.offline():
            for changes in [{"measurement_id": str(uuid.uuid4())}, {"stroke_id": str(uuid.uuid4())},
                            {"rotation": "90"}, {"status": "Failed"}, {"impact_real_seconds": float("nan")},
                            *({"video_speed_factor": n} for n in [0, -1, float("nan"), float("inf")])]:
                with self.subTest(changes=changes), mock.patch.dict(self.clip, changes), self.assertRaises(ValueError):
                    self.prepare(impact_frame=60, confirmed=True)
        self.assertFalse(self.root.exists())

    def test_source_output_path_safety(self):
        for root in [self.source, self.source / "nested", video.VIDEO_ROOT / "unsafe"]:
            with self.subTest(root=str(root)), self.offline(), self.assertRaises(ValueError):
                video.prepare_video(MID, clip=self.clip, output_root=root, impact_frame=60, confirmed=True)
        collision = self.base / "existing_file"
        collision.write_bytes(b"preserve")
        with self.offline(), self.assertRaises(ValueError):
            video.prepare_video(MID, clip=self.clip, output_root=collision, impact_frame=60, confirmed=True)
        self.assertEqual(collision.read_bytes(), b"preserve")

    def test_revision_collision_does_not_overwrite(self):
        self.root.mkdir()
        fixed = mock.Mock(hex="fixed")
        previous = self.root / f"{MID}_fixed_8frames"
        previous.mkdir()
        sentinel = previous / "keep.jpg"
        sentinel.write_bytes(b"previous artifact")
        with self.offline(), mock.patch.object(video.uuid, "uuid4", return_value=fixed), self.assertRaises(FileExistsError):
            self.prepare(impact_frame=60, confirmed=True)
        self.assertEqual(sentinel.read_bytes(), b"previous artifact")

    def test_changed_source_does_not_get_verified_manifest(self):
        def changed(ffmpeg, original, output, records):
            self.export(ffmpeg, original, output, records)
            self.original.write_bytes(b"external modification in fixture")
        with self.offline(exporter=changed), self.assertRaises(RuntimeError):
            self.prepare(impact_frame=60, confirmed=True)
        self.assertEqual(list(self.root.glob("*/manifest.json")), [])

    def test_stage_constraints_at_minimum_eight_frames(self):
        frames = [(i, 1.0 + i * 0.1) for i in range(8)]
        top = video._top_candidate(frames, 6, 0.00001, [])
        chosen = video._stage_frames(frames, 6, 0.00001, top, "manual_confirmed", 1)
        self.assertEqual([s["frame_index_zero_based"] for s in chosen], list(range(8)))

    def test_visual_heuristic_static_and_skin_reversal(self):
        frames = [(i, i * 0.025) for i in range(121)]
        static = [(i, Image.new("RGB", (96, 96), (180, 130, 95))) for i in range(25, 98)]
        fallback = video._top_candidate(frames, 100, 1, static)
        self.assertEqual(fallback["status"], "fallback_candidate")
        moving = []
        for i in range(25, 98):
            image = Image.new("RGB", (96, 96), "gray")
            y = round(26 + abs(i - 70) * 0.65)
            ImageDraw.Draw(image).rectangle((42, y, 48, y + 6), fill=(185, 125, 85))
            moving.append((i, image))
        top = video._top_candidate(frames, 100, 1, moving)
        self.assertEqual(top["status"], "heuristic_candidate")
        self.assertLessEqual(abs(top["frame_index_zero_based"] - 70), 3)
        self.assertFalse(top["is_pose_model"])
        self.assertLessEqual(top["confidence"], 0.45)
        chosen = video._stage_frames(frames, 100, 1, top, "candidate", 0.1)
        self.assertEqual(chosen[3]["frame_index_zero_based"], top["frame_index_zero_based"])

    def test_probe_retains_nonzero_pts_and_integer_time_base_precision(self):
        log = ("config in time_base: 1/90000, frame_rate: 30/1\n"
               "n: 0 pts: 112501 pts_time:1.25001\n"
               "n: 1 pts: 115578 pts_time:1.2842\n")
        with mock.patch.object(video, "run_ffmpeg", return_value=log) as run:
            frames = video.probe_frames("ffmpeg", self.original)
        self.assertIn("-copyts", run.call_args.args)
        self.assertEqual(frames, [(0, 112501 / 90000), (1, 115578 / 90000)])
        for bad in ["", "n: 1 pts: 0 pts_time:0", "n: 0 pts: 2 pts_time:2\nn: 1 pts: 1 pts_time:1"]:
            with self.subTest(log=bad), mock.patch.object(video, "run_ffmpeg", return_value=bad), self.assertRaises(RuntimeError):
                video.probe_frames("ffmpeg", self.original)

    def test_time_fallback_uses_eight_original_pts_offsets(self):
        frames = [(i, i * .01) for i in range(401)]
        top = video._top_candidate(frames, 200, 1, [])
        chosen = video._stage_frames(frames, 200, 1, top, "candidate", .1)
        self.assertEqual([s["frame_index_zero_based"] for s in chosen], [85, 120, 150, 170, 184, 196, 200, 220])

    def test_preview_exact_frame_no_overwrite_and_out_of_range(self):
        self.root.mkdir()
        destination = self.root / "left.jpg"
        def export(ffmpeg, *args):
            Image.new("RGB", (24, 24), "gray").save(Path(args[-1]), "JPEG")
        with mock.patch.object(video, "run_ffmpeg", side_effect=export) as run:
            actual = video.extract_preview(self.original, "ffmpeg", 59, destination)
        self.assertEqual(actual, destination.resolve())
        self.assertIn("select=eq(n\\,59)", run.call_args.args)
        self.assertIn("-n", run.call_args.args)
        previous_hash = video.sha256(destination)
        with mock.patch.object(video, "run_ffmpeg") as run, self.assertRaises(FileExistsError):
            video.extract_preview(self.original, "ffmpeg", 60, destination)
        run.assert_not_called()
        self.assertEqual(video.sha256(destination), previous_hash)
        for index in [True, -1, 1.2, "59"]:
            with self.subTest(index=index), self.assertRaises(ValueError):
                video.extract_preview(self.original, "ffmpeg", index, self.root / "bad.jpg")
        with mock.patch.object(video, "run_ffmpeg"), self.assertRaises(ValueError):
            video.extract_preview(self.original, "ffmpeg", 999999, self.root / "missing.jpg")
        with self.assertRaises(ValueError):
            video.extract_preview(self.original, "ffmpeg", 1, self.source / "unsafe.jpg")
        with self.assertRaises(ValueError):
            video.extract_preview(self.original, "ffmpeg", 1, self.root / "missing" / "preview.jpg")

    def test_visual_sampling_is_bounded_and_checks_byte_count(self):
        with self.assertRaises(ValueError):
            video._visual_samples("ffmpeg", self.original, list(range(98)))
        result = mock.Mock(returncode=0, stdout=b"short", stderr=b"")
        with mock.patch.object(video.subprocess, "run", return_value=result), self.assertRaises(RuntimeError):
            video._visual_samples("ffmpeg", self.original, [3])
        result.stdout = bytes(96 * 96 * 3 * 2)
        with mock.patch.object(video.subprocess, "run", return_value=result) as run:
            samples = video._visual_samples("ffmpeg", self.original, [4, 3, 3])
        self.assertEqual([i for i, _ in samples], [3, 4])
        self.assertIn("vfr", run.call_args.args[0])

    def test_legacy_still_requires_exactly_eight_offsets(self):
        for offsets in [[0] * 7, [0] * 9, [float("nan")] * 8]:
            with self.subTest(count=len(offsets)), self.assertRaises(ValueError):
                video.extract(self.clip, "ffmpeg", self.root, offsets)


class CameraSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "source"
        self.source.mkdir()
        self.clips = []
        for i, (name, angle, tag) in enumerate([
                ("TM4Cam_TM4_18034029", "Internal", "DL"),
                ("iPhone", "Other", "OT"),
                ("USB Camera", "DownTheLine", "DL")], 1):
            source = self.source / f"recording_{tag}_{i}.mov"
            source.write_bytes(b"offline source")
            self.clips.append({"measurement_id": MID, "stroke_id": MID,
                               "video_clip_id": str(uuid.UUID(int=i)), "path": str(source),
                               "relative_path": source.name, "status": "Success", "rotation": "0",
                               "camera_name": name, "camera_angle": angle})

    def test_internal_dl_is_not_external(self):
        camera = video.camera_descriptor(self.clips[0])
        self.assertTrue(camera["is_internal"])
        self.assertEqual(camera["label"], "TrackMan 内置 · DL")
        self.assertEqual(video.select_clip(MID, self.clips[:1]), self.clips[0])

    def test_ot_then_dl_then_internal_then_other(self):
        self.assertEqual(video.select_clip(MID, list(reversed(self.clips)))["video_clip_id"], self.clips[1]["video_clip_id"])
        self.assertEqual(video.select_clip(MID, [self.clips[0], self.clips[2]])["video_clip_id"], self.clips[2]["video_clip_id"])
        other = dict(self.clips[1], camera_angle="FaceOn", relative_path="recording.mov")
        self.assertEqual(video.select_clip(MID, [other, self.clips[0]])["video_clip_id"], self.clips[0]["video_clip_id"])
        self.assertEqual(video.select_clip(MID, [other])["video_clip_id"], other["video_clip_id"])
        self.assertEqual(video.camera_descriptor(self.clips[1])["label"], "iPhone · OT")

    def test_filters_foreign_guid_status_missing_rotation_and_duplicate(self):
        invalid = [dict(self.clips[0], measurement_id=str(uuid.uuid4())),
                   dict(self.clips[0], stroke_id=str(uuid.uuid4())),
                   dict(self.clips[0], video_clip_id="bad-guid"),
                   dict(self.clips[0], video_clip_id=str(uuid.UUID(int=0))),
                   dict(self.clips[0], status="Failed"),
                   dict(self.clips[0], path=str(self.base / "missing.mov")),
                   dict(self.clips[0], rotation="90")]
        self.assertEqual(video.supported_clips(MID, invalid), [])
        self.assertEqual(video.supported_clips(MID, [self.clips[0]] * 2), self.clips[:1])
        self.assertIsNone(video.select_clip(MID, invalid))

    def test_explicit_id_validated_without_fallback(self):
        self.assertEqual(video.select_clip(MID, self.clips, self.clips[0]["video_clip_id"].upper()), self.clips[0])
        for camera_id in (str(uuid.uuid4()), "not-a-guid", "", str(uuid.UUID(int=0))):
            with self.subTest(camera_id=camera_id), self.assertRaises(ValueError):
                video.select_clip(MID, self.clips, camera_id)
        with self.assertRaises(ValueError):
            video.select_clip(MID, [], self.clips[0]["video_clip_id"])

    def test_key_stable_across_shots_and_clip_guids(self):
        original = video.camera_descriptor(self.clips[1])
        next_shot = dict(self.clips[1], video_clip_id=str(uuid.uuid4()),
                         measurement_id=str(uuid.uuid4()), relative_path="next_OT.mov")
        self.assertEqual(original["key"], video.camera_descriptor(next_shot)["key"])
        self.assertEqual(set(original), {"id", "key", "label", "is_internal", "available"})

    def test_prepare_default_and_explicit_use_central_selection(self):
        for clip in self.clips:
            clip["video_speed_factor"] = 1
        for requested, expected in [(None, self.clips[1]), (self.clips[2]["video_clip_id"], self.clips[2])]:
            with self.subTest(camera_id=requested), \
                    mock.patch.object(video, "query_clips", return_value=self.clips) as query, \
                    mock.patch.object(video, "TPS_FFMPEG", Path(expected["path"])), \
                    mock.patch.object(video, "sha256", return_value="offline"), \
                    mock.patch.object(video, "probe_frames", side_effect=RuntimeError("在解码前停止")) as probe:
                with self.assertRaises(RuntimeError):
                    video.prepare_video(MID, camera_id=requested, output_root=self.base / "output")
                self.assertEqual(probe.call_args.args[1], Path(expected["path"]).resolve())
                query.assert_called_once_with(MID)
        with mock.patch.object(video, "query_clips", return_value=self.clips), self.assertRaises(ValueError):
            video.prepare_video(MID, camera_id=str(uuid.uuid4()))
        with self.assertRaises(ValueError):
            video.prepare_video(MID, clip=self.clips[0], camera_id=self.clips[1]["video_clip_id"])
        self.assertFalse((self.base / "output").exists())


if __name__ == "__main__":
    unittest.main()
