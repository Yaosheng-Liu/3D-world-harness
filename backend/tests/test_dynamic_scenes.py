"""Dynamic reference imports and immutable timed feedback delivery."""

from __future__ import annotations

import base64
import copy
import io
import json
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import APIError, SceneStore
from dynamic import sample_video
from gateway import WorkspaceGateway
from server import make_server
import mcp_server


def image_data(color: str = "red") -> tuple[bytes, str]:
    output = io.BytesIO()
    Image.new("RGB", (24, 24), color).save(output, format="PNG")
    data = output.getvalue()
    return data, "data:image/png;base64," + base64.b64encode(data).decode()


def calibrated_camera() -> dict:
    return {"camera_to_world": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 3], [0, 0, 0, 1]],
            "intrinsics": {"width": 24, "height": 24, "fx": 20, "fy": 20, "cx": 12, "cy": 12}}


class DynamicSceneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.store = SceneStore(self.root / "data")
        self.gateway = WorkspaceGateway(self.store, self.project, external_review=True)
        self.session = self.gateway.ensure()["session_id"]
        self.data, self.data_url = image_data()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def import_clip(self) -> dict:
        return self.store.set_reference_clip(self.session, {"name": "hand motion", "fps": 2, "frames": [
            {"name": "0001.png", "data_url": self.data_url, "time_sec": 0, "camera": calibrated_camera()},
            {"name": "0002.png", "data_url": self.data_url, "time_sec": 0.5},
        ]})["reference_clip"]

    def payload(self, clip: dict | None = None) -> dict:
        duration = clip["duration_sec"] if clip else 2
        fps = clip["fps"] if clip else 2
        return {"scene_revision": 1, "note": "Move this earlier.", "timeline": {"clip_id": clip["clip_id"] if clip else None,
                "time_sec": 0, "duration_sec": duration, "fps": fps, "scope": {"kind": "range", "start_sec": 0, "end_sec": 0.5}},
                "dynamic_frames": [{"id": "moment1", "time_sec": 0, "scene_revision": 1,
                "reference_frame_id": clip["frames"][0]["id"] if clip else None, "camera": {"position": [0, 0, 3]},
                "selected_object_ids": [], "selected_scene_nodes": [], "scene_original_data_url": self.data_url,
                "scene_annotated_data_url": self.data_url, **({"reference_annotated_data_url": self.data_url} if clip else {})}]}

    def test_sequence_import_camera_clone_clear_and_no_revision_change(self) -> None:
        clip = self.import_clip()
        self.assertEqual(clip["duration_sec"], 1)
        self.assertEqual(clip["source_type"], "sequence")
        self.assertEqual([frame["frame_index"] for frame in clip["frames"]], [0, 1])
        self.assertEqual(clip["frames"][0]["camera"], calibrated_camera())
        self.assertEqual(self.store.scene()["revision"], 1)
        self.assertEqual(self.gateway.state()["reference_clip"], clip)
        cloned = self.store.create_session(reference_session_id=self.session)
        self.assertEqual(cloned["reference_clip"], clip)
        self.store.set_reference_clip(self.session, {"clear": True})
        self.assertIsNone(self.store.get_session(self.session)["reference_clip"])
        self.assertEqual(self.store.get_session(cloned["session_id"])["reference_clip"], clip)

    def test_manifest_relative_paths_and_project_boundary(self) -> None:
        directory = self.project / "clip"
        directory.mkdir()
        (directory / "frame.png").write_bytes(self.data)
        manifest = directory / "clip.json"
        manifest.write_text(json.dumps({"name": "motion", "fps": 5, "frames": [{"path": "frame.png", "time_sec": 0, "camera": calibrated_camera()}]}))
        clip = self.gateway.set_reference_clip_paths({"manifest_path": str(manifest)})["reference_clip"]
        self.assertEqual(clip["frames"][0]["name"], "frame.png")
        outside = self.root / "outside.png"
        outside.write_bytes(self.data)
        manifest.write_text(json.dumps({"frames": [{"path": str(outside)}]}))
        with self.assertRaisesRegex(APIError, "inside the workspace"):
            self.gateway.set_reference_clip_paths({"manifest_path": str(manifest)})
        self.assertEqual(self.store.get_session(self.session)["reference_clip"], clip)

    def test_ten_fps_long_clip_keeps_all_timestamps(self):
        from ready_import import _validate_reference_manifest
        frame = self.project / "long-frame.png"
        frame.write_bytes(self.data)
        manifest = self.project / "long-clip.json"
        manifest.write_text(json.dumps({"fps": 10, "duration_sec": 120,
            "frames": [{"path": frame.name, "time_sec": i / 10} for i in range(1200)]}))
        _validate_reference_manifest(self.project, manifest)
        result = self.gateway.set_reference_clip_paths({"manifest_path": str(manifest)})["reference_clip"]
        self.assertEqual(len(result["frames"]), 1200)
        self.assertEqual(result["frames"][935]["time_sec"], 93.5)
        self.assertEqual(result["frames"][-1]["time_sec"], 119.9)
        manifest.write_text(json.dumps({"frames": [{"path": frame.name}] * 1201}))
        with self.assertRaisesRegex(APIError, "1200"):
            _validate_reference_manifest(self.project, manifest)

    def test_bad_timestamps_camera_and_limits_leave_previous_clip_intact(self) -> None:
        clip = self.import_clip()
        base = {"fps": 2, "frames": [{"data_url": self.data_url, "time_sec": 0}, {"data_url": self.data_url, "time_sec": 0}]}
        with self.assertRaisesRegex(APIError, "strictly increasing"):
            self.store.set_reference_clip(self.session, base)
        camera = calibrated_camera()
        camera["intrinsics"]["width"] = 25
        with self.assertRaisesRegex(APIError, "dimensions"):
            self.store.set_reference_clip(self.session, {"frames": [{"data_url": self.data_url, "camera": camera}]})
        with self.assertRaisesRegex(APIError, "1200"):
            self.store.set_reference_clip(self.session, {"frames": [{"data_url": self.data_url}] * 1201})
        with self.assertRaisesRegex(APIError, "exceed"):
            self.store.set_reference_clip(self.session, {"frames": [{"data_url": self.data_url}]}, byte_limit=1)
        self.assertEqual(self.store.get_session(self.session)["reference_clip"], clip)

    def test_dynamic_evidence_is_immutable_and_reaches_gateway_and_mcp_as_images(self) -> None:
        clip = self.import_clip()
        payload = self.payload(clip)
        payload["idempotency_key"] = "dynamic-test-1"
        payload["annotations"] = [{"id": "mark1", "type": "rectangle", "pane": "reference",
            "reference_image_id": clip["frames"][0]["id"], "frame_id": "moment1", "time_sec": 0,
            "clip_id": clip["clip_id"], "scene_revision": 1, "coordinates": {"x": 0.1, "y": 0.2, "x2": 0.4, "y2": 0.5}}]
        payload["note"] = "Move to [[annotation:mark1]] at this time."
        feedback = self.store.submit_feedback(self.session, payload)
        evidence = feedback["dynamic_frames"][0]
        self.assertEqual(evidence["reference_original_url"], clip["frames"][0]["url"])
        self.assertEqual(evidence["reference_camera"], calibrated_camera())
        self.assertEqual(evidence["frame_index"], 0)
        self.assertEqual(feedback["annotations"][0]["frame_index"], 0)
        self.assertFalse(any(key.endswith("_data_url") for key in evidence))
        message, paths = self.gateway._turn_input(feedback)
        direct_source = json.loads(message.splitlines()[2].removeprefix("证据："))["dynamic_frames"][0]
        self.assertEqual((direct_source["time_sec"], direct_source["frame_index"]), (0.0, 0))
        self.assertIn("start_sec", message)
        with patch.object(mcp_server, "DATA_DIR", self.store.data_dir):
            tool_result = mcp_server._visual_tool_result({"items": [copy.deepcopy(feedback)]}, include_details=True)
        self.assertEqual(sum(item.type == "image" for item in tool_result.content), len(paths))
        self.assertIn("camera", tool_result.structured_content["items"][0]["dynamic_frames"][0])
        summarized = json.loads(tool_result.content[0].text)["items"][0]
        source = summarized["dynamic_frames"][0]
        self.assertEqual((source["frame_index"], source["time_sec"], source["reference_frame_id"]),
                         (0, 0.0, evidence["reference_frame_id"]))
        self.assertEqual(source["reference_camera"]["archive"], "dynamic_frames[0].reference_camera")
        self.assertEqual(tool_result.structured_content["items"][0]["dynamic_frames"][0]["reference_camera"]["intrinsics"],
                         evidence["reference_camera"]["intrinsics"])
        self.assertTrue(any(item.type == "text" and "F1 · 参考原帧" in item.text for item in tool_result.content))
        # Replays are resolved before clip validation and remain exactly-once after replacement.
        self.store.set_reference_clip(self.session, {"clear": True})
        replay = self.store.submit_feedback(self.session, payload)
        self.assertEqual(replay["feedback_id"], feedback["feedback_id"])
        self.assertEqual(self.store.get_session(self.session)["feedback_count"], 1)
        self.assertEqual(SceneStore(self.store.data_dir).feedback_by_id(feedback["feedback_id"])["dynamic_frames"], feedback["dynamic_frames"])

    def test_missing_scope_preserves_prompt_times_and_evidence_without_a_default_range(self) -> None:
        for has_clip in (False, True):
            clip = self.import_clip() if has_clip else None
            with self.subTest(reference_clip=has_clip):
                payload = self.payload(clip)
                payload["timeline"].pop("scope")
                payload["note"] = "请把 0.25 秒到 0.75 秒的动作往前移。"
                original_payload = copy.deepcopy(payload)
                feedback = self.store.submit_feedback(self.session, payload)
                self.assertEqual(payload, original_payload)
                self.assertEqual(feedback["note"], payload["note"])
                self.assertFalse({"scope", "start_sec", "end_sec"} & feedback["timeline"].keys())
                self.assertEqual(feedback["timeline"]["time_sec"], 0)
                self.assertEqual(feedback["dynamic_frames"][0]["time_sec"], 0)
                message, paths = self.gateway._turn_input(feedback)
                direct_timeline = json.loads(message.splitlines()[2].removeprefix("证据："))["timeline"]
                self.assertNotIn("scope", direct_timeline)
                self.assertIn(payload["note"], message)
                self.assertIn("修改时段以提示为准", message)
                with patch.object(mcp_server, "DATA_DIR", self.store.data_dir):
                    result = mcp_server._visual_tool_result({"items": [copy.deepcopy(feedback)]}, include_details=True)
                delivered = result.structured_content["items"][0]
                self.assertEqual(delivered["timeline"], feedback["timeline"])
                self.assertEqual(delivered["note"], feedback["note"])
                self.assertEqual(delivered["dynamic_frames"][0]["camera"], feedback["dynamic_frames"][0]["camera"])
                self.assertEqual(sum(item.type == "image" for item in result.content), len(paths))
                texts = [item.text for item in result.content if item.type == "text"]
                self.assertTrue(any("modification times follow the prompt" in text for text in texts))
                self.assertNotIn("scope", json.loads(texts[0])["items"][0]["timeline"])
                persisted = SceneStore(self.store.data_dir).feedback_by_id(feedback["feedback_id"])
                self.assertEqual(persisted, feedback)

    def test_explicit_legacy_scopes_keep_their_saved_intent_and_delivery_message(self) -> None:
        clip = self.import_clip()
        saved = []
        for scope in ({"kind": "frame"}, {"kind": "range", "start_sec": 0.25, "end_sec": 0.75}, {"kind": "clip"}):
            with self.subTest(scope=scope):
                payload = self.payload(clip)
                payload["timeline"]["scope"] = scope
                feedback = self.store.submit_feedback(self.session, payload)
                self.assertEqual(feedback["timeline"]["scope"], scope)
                message, _ = self.gateway._turn_input(feedback)
                self.assertEqual(json.loads(message.splitlines()[2].removeprefix("证据："))["timeline"]["scope"], scope)
                with patch.object(mcp_server, "DATA_DIR", self.store.data_dir):
                    result = mcp_server._visual_tool_result({"items": [copy.deepcopy(feedback)]})
                self.assertEqual(result.structured_content["items"][0]["timeline"]["scope"], scope)
                saved.append(feedback)
        state_bytes = self.store.state_path.read_bytes()
        reloaded = SceneStore(self.store.data_dir)
        for feedback in saved:
            self.assertEqual(reloaded.feedback_by_id(feedback["feedback_id"]), feedback)
        self.assertEqual([item["feedback_id"] for item in reloaded.workspace()["queue"]], [item["feedback_id"] for item in saved])
        self.assertEqual(self.store.state_path.read_bytes(), state_bytes)

    def test_explicit_legacy_scope_validation_remains_required(self) -> None:
        for scope in (None, {"kind": "prompt"}, {"kind": "evidence"},
                      {"kind": "range", "start_sec": 0.75, "end_sec": 0.25},
                      {"kind": "range", "start_sec": 0, "end_sec": 3},
                      {"kind": "range", "start_sec": float("nan"), "end_sec": 0.5},
                      {"kind": "frame", "start_sec": 0}, {"kind": "clip", "end_sec": 1}):
            with self.subTest(scope=scope):
                payload = self.payload()
                payload["timeline"]["scope"] = scope
                with self.assertRaisesRegex(APIError, "scope"):
                    self.store.submit_feedback(self.session, payload)
        self.assertEqual(self.store.list_all_feedback(), [])
        self.assertEqual(self.store.workspace()["queue"], [])

    def test_irregular_clip_indices_follow_sequence_and_override_client_indices(self) -> None:
        clip = self.store.set_reference_clip(self.session, {"fps": 30, "frames": [
            {"name": "source_9025.png", "time_sec": 0, "data_url": self.data_url, "frame_index": 9025},
            {"name": "source_9140.png", "time_sec": 0.17, "data_url": self.data_url, "frame_index": 9140},
            {"name": "source_10000.png", "time_sec": 4.9, "data_url": self.data_url, "frame_index": 10000},
        ]})["reference_clip"]
        self.assertEqual([frame["frame_index"] for frame in clip["frames"]], [0, 1, 2])
        payload = self.payload(clip)
        payload["timeline"]["time_sec"] = 4.8
        payload["dynamic_frames"][0].update(time_sec=4.8, reference_frame_id=clip["frames"][2]["id"], frame_index=999)
        payload["annotations"] = [{"id": "irregular-mark", "type": "point", "pane": "scene", "frame_id": "moment1",
            "time_sec": 4.8, "clip_id": clip["clip_id"], "scene_revision": 1, "frame_index": 888,
            "coordinates": {"x": 0.2, "y": 0.3}}]
        payload["note"] = "Check [[annotation:irregular-mark]]."
        original_payload = copy.deepcopy(payload)
        saved = self.store.submit_feedback(self.session, payload)
        self.assertEqual(saved["dynamic_frames"][0]["frame_index"], 2)
        self.assertEqual(saved["annotations"][0]["frame_index"], 2)
        self.assertEqual(saved["inline_references"][0]["annotation"]["frame_index"], 2)
        self.assertEqual(payload, original_payload)
        message, _ = self.gateway._turn_input(saved)
        source_in_prompt = json.loads(message.splitlines()[2].removeprefix("证据："))["dynamic_frames"][0]
        self.assertEqual((source_in_prompt["frame_index"], source_in_prompt["time_sec"]), (2, 4.8))
        with patch.object(mcp_server, "DATA_DIR", self.store.data_dir):
            result = mcp_server._visual_tool_result({"items": [copy.deepcopy(saved)]})
        source = json.loads(result.content[0].text)["items"][0]["dynamic_frames"][0]
        self.assertEqual((source["frame_index"], source["time_sec"], source["reference_time_sec"]), (2, 4.8, 4.9))
        self.assertEqual(self.store.feedback_by_id(saved["feedback_id"]), saved)

    def test_old_clips_without_stored_indices_derive_ordinals_when_submitting(self) -> None:
        clip = self.import_clip()
        for frame in self.store.state["sessions"][self.session]["reference_clip"]["frames"]:
            frame.pop("frame_index")
        self.store._save()
        self.store = SceneStore(self.store.data_dir)
        legacy_clip = self.store.get_session(self.session)["reference_clip"]
        self.assertTrue(all("frame_index" not in frame for frame in legacy_clip["frames"]))
        payload = self.payload(legacy_clip)
        payload["dynamic_frames"][0].update(time_sec=0.5, reference_frame_id=legacy_clip["frames"][1]["id"], frame_index=-1)
        saved = self.store.submit_feedback(self.session, payload)
        self.assertEqual(saved["dynamic_frames"][0]["frame_index"], 1)
        self.assertEqual(self.store.get_session(self.session)["reference_clip"], legacy_clip)

    def test_clip_scope_frame_timestamp_and_annotation_associations_validate(self) -> None:
        clip = self.import_clip()
        mutations = [
            lambda p: p["timeline"].update(clip_id="unknown"),
            lambda p: p["timeline"].update(duration_sec=8),
            lambda p: p["timeline"].update(time_sec=99),
            lambda p: p["timeline"]["scope"].update(start_sec=0.7, end_sec=0.5),
            lambda p: p["dynamic_frames"][0].update(reference_frame_id="unknown"),
            lambda p: p["dynamic_frames"][0].update(reference_frame_id=clip["frames"][1]["id"]),
            lambda p: p["dynamic_frames"][0].update(time_sec=float("nan")),
            lambda p: p["dynamic_frames"][0].pop("scene_original_data_url"),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                payload = self.payload(clip)
                mutate(payload)
                with self.assertRaises(APIError):
                    self.store.submit_feedback(self.session, payload)
        payload = self.payload(clip)
        payload["annotations"] = [{"type": "point", "pane": "scene", "frame_id": "moment1", "time_sec": 0.1,
                "clip_id": clip["clip_id"], "scene_revision": 1, "coordinates": {"x": 0.5, "y": 0.5}}]
        with self.assertRaisesRegex(APIError, "timestamp"):
            self.store.submit_feedback(self.session, payload)

    def test_mixed_revision_evidence_requires_confirmation_and_oldest_top_revision(self) -> None:
        payload = self.payload()
        self.store.replace_scene(1, [])
        payload["dynamic_frames"].append({**payload["dynamic_frames"][0], "id": "moment2", "time_sec": 0.5, "scene_revision": 2})
        with self.assertRaisesRegex(APIError, "scene revision changed") as rejected:
            self.store.submit_feedback(self.session, payload)
        self.assertEqual(rejected.exception.detail, {"code": "feedback_revision_conflict", "current_scene_revision": 2})
        payload["confirm_stale"] = True
        packet = self.store.submit_feedback(self.session, payload)
        self.assertEqual([frame["scene_revision"] for frame in packet["dynamic_frames"]], [1, 2])
        self.assertTrue(packet["submitted_from_stale_snapshot"])
        payload["scene_revision"] = 2
        with self.assertRaisesRegex(APIError, "revision") as rejected:
            self.store.submit_feedback(self.session, payload)
        self.assertEqual(rejected.exception.detail["code"], "feedback_revision_conflict")
        payload = self.payload()
        payload["confirm_stale"] = True
        payload["dynamic_frames"][0]["scene_revision"] = 99
        with self.assertRaisesRegex(APIError, "revision") as rejected:
            self.store.submit_feedback(self.session, payload)
        self.assertEqual(rejected.exception.detail["code"], "feedback_revision_conflict")

    def test_legacy_blocked_dynamic_evidence_is_restored_without_relabeling(self) -> None:
        clip = self.import_clip()
        payload = self.payload(clip)
        payload["idempotency_key"] = "legacy-dynamic-image"
        packet = self.store.submit_feedback(self.session, payload)
        self.store.replace_scene(1, [])
        with self.store.lock:
            self.store.state["workspace"]["queue"][0]["status"] = "blocked_stale"
            self.store._save()
        self.gateway._restore_blocked_visual_feedback()
        reloaded = SceneStore(self.store.data_dir)
        self.assertEqual(reloaded.workspace()["queue"][0]["status"], "queued")
        self.assertEqual(reloaded.feedback_by_id(packet["feedback_id"]), packet)
        self.assertEqual(packet["dynamic_frames"][0]["scene_revision"], 1)

    def test_animation_only_and_stable_node_metadata(self) -> None:
        payload = self.payload()
        payload["dynamic_frames"][0]["frame_index"] = 20
        payload["annotations"] = [{"id": "animation-mark", "type": "point", "pane": "scene", "frame_id": "moment1",
            "time_sec": 0, "clip_id": None, "scene_revision": 1, "frame_index": 20,
            "coordinates": {"x": 0.2, "y": 0.3}}]
        packet = self.store.submit_feedback(self.session, payload)
        self.assertIsNone(packet["timeline"]["clip_id"])
        self.assertEqual(len(packet["dynamic_frames"]), 1)
        self.assertNotIn("frame_index", packet["dynamic_frames"][0])
        self.assertNotIn("frame_index", packet["annotations"][0])
        message, _ = self.gateway._turn_input(packet)
        self.assertNotIn("片段第", message)
        node = self.store._scene_node({"parent_object_id": "model", "node_path": [2], "stable_id": "chair/back", "semantic_id": "chair_back"}, {"model"})
        self.assertEqual(node["stable_id"], "chair/back")
        with self.assertRaisesRegex(APIError, "stable_id"):
            self.store._scene_node({**node, "stable_id": "bad\n"}, {"model"})

    def test_static_reference_can_be_marked_at_multiple_times_with_animation_identity(self) -> None:
        document = json.dumps({"asset": {"version": "2.0"}, "scenes": [{}], "scene": 0}).encode()
        document += b" " * (-len(document) % 4)
        path = self.project / "scene.glb"
        path.write_bytes(struct.pack("<4sII", b"glTF", 2, 20 + len(document)) + struct.pack("<I4s", len(document), b"JSON") + document)
        self.store.import_model(str(path), object_id="motion")
        clip = self.import_clip()
        reference = self.store.add_reference(self.session, "still.png", self.data_url)
        payload = self.payload(clip)
        revision = self.store.scene()["revision"]
        payload["scene_revision"] = revision
        frame = payload["dynamic_frames"][0]
        frame.update(scene_revision=revision, reference_frame_id=None, static_reference_id=reference["id"],
                     animation_clips=[{"object_id": "motion", "name": "Walk", "index": 0}], frame_index=30)
        payload["annotations"] = [{"id": "still-mark", "type": "point", "pane": "reference", "reference_image_id": reference["id"],
                                    "frame_id": frame["id"], "time_sec": 0, "clip_id": clip["clip_id"], "scene_revision": revision,
                                    "coordinates": {"x": 0.2, "y": 0.3}, "frame_index": 30}]
        packet = self.store.submit_feedback(self.session, payload)
        saved = packet["dynamic_frames"][0]
        self.assertEqual(saved["reference_original_url"], reference["url"])
        self.assertEqual(saved["static_reference_id"], reference["id"])
        self.assertEqual(saved["animation_clips"], [{"object_id": "motion", "name": "Walk", "index": 0}])
        self.assertNotIn("frame_index", saved)
        self.assertNotIn("frame_index", packet["annotations"][0])
        frame["animation_clips"][0]["name"] = "x" * 161
        with self.assertRaisesRegex(APIError, "clip name"):
            self.store.submit_feedback(self.session, payload)

    def test_video_missing_decoder_and_invalid_input_are_bounded(self) -> None:
        video = self.project / "broken.mp4"
        video.write_bytes(b"not a video")
        with patch("dynamic.shutil.which", return_value=None), self.assertRaisesRegex(APIError, "requires ffmpeg"):
            sample_video(video, 10)
        with self.assertRaisesRegex(APIError, "fully decoded"):
            sample_video(video, 10)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg unavailable")
    def test_real_video_sampling_and_overlong_rejection(self) -> None:
        video = self.project / "motion.mp4"
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=24x24:r=10:d=1", "-pix_fmt", "yuv420p", str(video)], check=True, timeout=15)
        result = self.gateway.set_reference_clip_paths({"video_path": str(video)})["reference_clip"]
        self.assertEqual(result["fps"], 10)
        self.assertEqual(result["source_type"], "video")
        self.assertTrue(result["sampled"])
        self.assertEqual(len(result["frames"]), 10)
        probe = {"format": {"format_name": "mp4", "duration": "121"}, "streams": [{"width": 24, "height": 24}]}
        with patch("dynamic.subprocess.run", return_value=subprocess.CompletedProcess([], 0, json.dumps(probe).encode(), b"")) as run:
            with self.assertRaisesRegex(APIError, "exceed 1200"):
                sample_video(video, 10)
            self.assertEqual(run.call_count, 1)


class DynamicHTTPTests(unittest.TestCase):
    def test_revision_rejections_have_a_recoverable_code_and_never_save_a_packet(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "project").mkdir()
            (root / "web").mkdir()
            server = make_server(port=0, data_dir=root / "data", web_dir=root / "web", project_dir=root / "project", external_review=True)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            base = f"http://127.0.0.1:{server.server_port}"
            store = server.scene_store
            session = store.workspace()["session_id"]
            store.replace_scene(1, [])
            _, data_url = image_data()
            dynamic = {"note": "review", "confirm_stale": True,
                       "timeline": {"clip_id": None, "time_sec": 0, "duration_sec": 2, "fps": 2, "scope": {"kind": "frame"}},
                       "dynamic_frames": [{"id": "frame1", "time_sec": 0, "camera": {}, "scene_original_data_url": data_url}]}
            cases = [
                {"scene_revision": 1, "note": "stale"},
                {**copy.deepcopy(dynamic), "scene_revision": 2},
                {**copy.deepcopy(dynamic), "scene_revision": 1},
            ]
            cases[1]["dynamic_frames"][0]["scene_revision"] = 1
            cases[2]["dynamic_frames"][0]["scene_revision"] = 2
            try:
                for index, payload in enumerate(cases):
                    payload["idempotency_key"] = f"rejected-packet-{index}"
                    request = urllib.request.Request(base + f"/api/sessions/{session}/feedback", data=json.dumps(payload).encode(),
                                                     headers={"Content-Type": "application/json", "X-Workspace-Capability": store.browser_token}, method="POST")
                    with self.assertRaises(urllib.error.HTTPError) as rejected:
                        opener.open(request)
                    self.assertEqual(rejected.exception.code, 409)
                    body = json.load(rejected.exception)
                    rejected.exception.close()
                    self.assertEqual(body["code"], "feedback_revision_conflict")
                    self.assertEqual(body["current_scene_revision"], 2)
                    self.assertEqual(store.list_all_feedback(), [])
                    self.assertEqual(store.workspace()["queue"], [])
            finally:
                server.shutdown()
                server.server_close()

    def test_clip_routes_capabilities_and_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "project").mkdir()
            (root / "web").mkdir()
            server = make_server(port=0, data_dir=root / "data", web_dir=root / "web", project_dir=root / "project", external_review=True)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            base = f"http://127.0.0.1:{server.server_port}"
            store = server.scene_store
            session = store.workspace()["session_id"]
            data, data_url = image_data()
            try:
                with opener.open(base + "/api/health") as response:
                    self.assertIs(json.load(response)["prompt_time_supported"], True)
                def post(path: str, payload: dict, key: str | None = None) -> dict:
                    headers = {"Content-Type": "application/json"}
                    if key is not None:
                        headers["X-Workspace-Capability" if key == store.browser_token else "X-Scene-Harness-Key"] = key
                    request = urllib.request.Request(base + path, data=json.dumps(payload).encode(), headers=headers, method="POST")
                    with opener.open(request) as response:
                        return json.load(response)
                # Browser writes accept the separately issued browser capability.
                payload = {"fps": 2, "frames": [{"data_url": data_url, "time_sec": 0}]}
                with self.assertRaises(urllib.error.HTTPError) as rejected:
                    post(f"/api/sessions/{session}/clip", payload)
                self.assertEqual(rejected.exception.code, 403)
                rejected.exception.close()
                result = post(f"/api/sessions/{session}/clip", payload, store.browser_token)
                with opener.open(base + "/api/workspace/context") as response:
                    self.assertEqual(json.load(response)["reference_clip"], result["reference_clip"])
                with self.assertRaises(urllib.error.HTTPError) as rejected:
                    post("/api/workspace/clip", {"clear": True}, store.browser_token)
                self.assertEqual(rejected.exception.code, 403)
                rejected.exception.close()
                self.assertIsNone(post("/api/workspace/clip", {"clear": True}, store.control_token)["reference_clip"])
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
