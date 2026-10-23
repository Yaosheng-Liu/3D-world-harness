"""Focused checks for the scene review handoff and local HTTP boundary."""

from __future__ import annotations

import json
import base64
import asyncio
import io
import os
import struct
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from PIL import Image


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import APIError, SceneStore  # noqa: E402
from gateway import WorkspaceGateway  # noqa: E402
from server import make_server  # noqa: E402
import mcp_server  # noqa: E402


def tiny_glb(path: Path) -> None:
    scene = json.dumps({"asset": {"version": "2.0"}, "scenes": [{}], "scene": 0}).encode()
    scene += b" " * (-len(scene) % 4)
    path.write_bytes(struct.pack("<4sII", b"glTF", 2, 12 + 8 + len(scene)) + struct.pack("<I4s", len(scene), b"JSON") + scene)


def image_data_url(*, large: bool = False) -> tuple[str, bytes]:
    if large:
        image = Image.frombytes("RGB", (2200, 1400), os.urandom(2200 * 1400 * 3))
        kind, mime = "JPEG", "image/jpeg"
    else:
        image = Image.new("RGB", (24, 24), "#d86643")
        kind, mime = "PNG", "image/png"
    output = io.BytesIO()
    image.save(output, format=kind, quality=90)
    data = output.getvalue()
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}", data


def seed_chair(store: SceneStore) -> None:
    store.replace_scene(store.scene()["revision"], [{"id": "chair_back", "type": "box", "position": [0, 0, 1], "size": [1, 1, 1]}])


class SceneStoreTests(unittest.TestCase):
    def test_empty_codex_thread_can_be_recreated_before_first_feedback_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = SceneStore(root / "data")
            workspace = store.ensure_workspace(root)
            store.workspace_thread("unpersisted-thread")
            store.workspace_thread("persisted-thread")
            self.assertEqual(store.workspace()["thread_id"], "persisted-thread")
            self.assertEqual(store.workspace_events()["items"][-1]["type"], "empty_thread_recreated")
            store.submit_feedback(workspace["session_id"], {"scene_revision": 1, "note": "Start reconstruction."})
            with self.assertRaisesRegex(APIError, "already bound"):
                store.workspace_thread("another-thread")

    def test_new_project_starts_without_invented_geometry_and_accepts_freehand(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = SceneStore(temporary)
            self.assertEqual(store.scene()["objects"], [])
            session = store.create_session()
            image_url, _ = image_data_url()
            reference = store.add_reference(session["session_id"], "room.png", image_url)
            packet = store.submit_feedback(session["session_id"], {"scene_revision": 1, "note": "Build this room.", "annotations": [{"type": "freehand", "pane": "reference", "reference_image_id": reference["id"], "coordinates": {"x": 0.1, "y": 0.2}, "points": [{"x": 0.1, "y": 0.2}, {"x": 0.3, "y": 0.4}]}]})
            self.assertEqual(packet["annotations"][0]["type"], "freehand")
            photo_only = store.submit_feedback(session["session_id"], {"scene_revision": 1})
            self.assertEqual(photo_only["reference_images"][0]["id"], reference["id"])

    def test_feedback_persists_and_stale_revision_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = SceneStore(temporary)
            seed_chair(store)
            session = store.create_session()
            revision = store.scene()["revision"]
            feedback = store.submit_feedback(session["session_id"], {
                "scene_revision": revision,
                "annotations": [
                    {"type": "target_box", "object_id": "chair_back", "coordinate_frame": "world", "center": [0, 0.51, 1.7], "size": [1.25, 0.16, 1.7], "anchor": "bottom"},
                    {"type": "guide_line", "object_id": "chair_back", "coordinate_frame": "world", "start": [0, 0.5, 0.9], "end": [0, 0.5, 2.3]},
                ],
                "note": "Raise the backrest to this line.",
            })
            self.assertEqual(feedback["annotations"][0]["type"], "target_box")
            self.assertEqual(SceneStore(temporary).feedback(session["session_id"])["items"][0]["feedback_id"], feedback["feedback_id"])
            second_round = store.submit_feedback(session["session_id"], {"scene_revision": revision, "note": "One more observation"})
            self.assertEqual(store.get_session(session["session_id"])["feedback_count"], 2)
            self.assertEqual(store.feedback(session["session_id"], cursor=1)["items"][0]["feedback_id"], second_round["feedback_id"])
            second = store.create_session()
            store.update_scene(revision, [{"op": "update", "object_id": "chair_back", "fields": {"size": [1.25, 0.16, 1.5]}}])
            with self.assertRaisesRegex(APIError, "scene revision changed"):
                store.submit_feedback(second["session_id"], {"scene_revision": revision, "note": "stale"})

    def test_multiple_object_prompts_reach_codex_and_remain_in_visual_packet(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = SceneStore(root / "data")
            seed_chair(store)
            session_id = store.ensure_workspace(root)["session_id"]
            prompts = [
                {"object": "椅背", "prompt": "顶部靠近左图红线。", "object_id": "chair_back"},
                {"object": "缺失的柜子", "prompt": "请参考右上角补出这个柜子。"},
            ]
            packet = store.submit_feedback(session_id, {"scene_revision": store.scene()["revision"], "object_prompts": prompts})
            self.assertEqual(packet["object_prompts"], prompts)
            queued = store.workspace_events()["items"][-1]
            self.assertEqual(queued["payload"]["note"], "")
            self.assertEqual(queued["payload"]["object_prompts_summary"], "椅背：顶部靠近左图红线。\n缺失的柜子：请参考右上角补出这个柜子。")
            self.assertEqual(SceneStore(store.data_dir).feedback_by_id(packet["feedback_id"])["object_prompts"], prompts)
            gateway = WorkspaceGateway(store, root)
            self.assertTrue(gateway.state()["object_prompts_supported"])
            text, paths = gateway._turn_input(packet)
            self.assertEqual(paths, [])
            self.assertIn('"object": "椅背", "prompt": "顶部靠近左图红线。", "object_id": "chair_back"', text)
            self.assertIn('"object": "缺失的柜子", "prompt": "请参考右上角补出这个柜子。"', text)
            self.assertLess(text.index('"object": "椅背"'), text.index('"object": "缺失的柜子"'))
            visual = mcp_server._visual_tool_result({"items": [packet]})
            self.assertEqual(visual.structured_content["items"][0]["object_prompts"], prompts)
            legacy = store.submit_feedback(session_id, {"scene_revision": store.scene()["revision"], "note": "Keep the chair."})
            self.assertEqual(legacy["object_prompts"], [])
            store.replace_scene(store.scene()["revision"], [])
            stale = store.submit_feedback(session_id, {"scene_revision": packet["scene_revision"], "confirm_stale": True, "object_prompts": prompts})
            self.assertEqual(stale["object_prompts"], prompts)
            self.assertTrue(stale["submitted_from_stale_snapshot"])
            large_prompts = [{"object": "item", "prompt": "x" * 2000}] * 3
            store.submit_feedback(session_id, {"scene_revision": store.scene()["revision"], "object_prompts": large_prompts})
            summary = store.workspace_events()["items"][-1]["payload"]["object_prompts_summary"]
            self.assertEqual(len(summary), 4000)
            self.assertTrue(summary.endswith("…"))

    def test_one_note_resolves_multiple_inline_object_and_annotation_references(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = SceneStore(root / "data")
            store.replace_scene(1, [
                {"id": "chair_back", "name": "椅背", "type": "box", "position": [0, 0, 1], "size": [1, 1, 1]},
                {"id": "cabinet", "name": "柜子", "type": "box", "position": [2, 0, 1], "size": [1, 1, 2]},
            ])
            session_id = store.ensure_workspace(root)["session_id"]
            image_url, _ = image_data_url()
            reference = store.add_reference(session_id, "room.png", image_url)
            note = "把 [[object:cabinet]] 向标记 [[annotation:mark-1]] 靠近，再让 [[object:chair_back]] 与柜子等高。柜子 [[object:cabinet]] 别太深。"
            mark = {"id": "mark-1", "type": "rectangle", "pane": "reference", "reference_image_id": reference["id"],
                    "coordinates": {"x": 0.1, "y": 0.2, "x2": 0.5, "y2": 0.7}}
            packet = store.submit_feedback(session_id, {"scene_revision": 2, "note": note, "annotations": [mark]})
            self.assertEqual(packet["note"], note)
            self.assertEqual(packet["object_prompts"], [])
            self.assertEqual([(item["kind"], item["id"]) for item in packet["inline_references"]],
                             [("object", "cabinet"), ("annotation", "mark-1"), ("object", "chair_back")])
            self.assertEqual(packet["inline_references"][0]["object"]["name"], "柜子")
            self.assertEqual(packet["inline_references"][1]["annotation"]["coordinates"], mark["coordinates"])
            self.assertEqual(SceneStore(store.data_dir).feedback_by_id(packet["feedback_id"])["inline_references"], packet["inline_references"])
            gateway = WorkspaceGateway(store, root)
            self.assertTrue(gateway.state()["inline_references_supported"])
            message, paths = gateway._turn_input(packet)
            self.assertIn(note, message)
            self.assertIn('[[object:cabinet]] → {"id": "cabinet", "name": "柜子"', message)
            self.assertIn('[[annotation:mark-1]] → {"id": "mark-1"', message)
            self.assertEqual(len(paths), 1)

    def test_inline_references_reject_unknown_ambiguous_and_malformed_targets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = SceneStore(temporary)
            seed_chair(store)
            session_id = store.create_session()["session_id"]
            mark = {"id": "mark-1", "type": "point", "pane": "scene", "coordinates": {"x": 0.2, "y": 0.3}}
            cases = [
                ({"note": "Move [[object:missing]]"}, "inline object reference"),
                ({"note": "Use [[annotation:missing]]", "annotations": [mark]}, "inline annotation reference"),
                ({"note": "Use [[annotation:mark-1]]", "annotations": [mark, mark]}, "ambiguous"),
                ({"note": "Move [[object:chair back]]"}, "invalid inline reference"),
                ({"note": "Move [[object:chair_back]"}, "invalid inline reference"),
            ]
            for fields, error in cases:
                with self.subTest(fields=fields), self.assertRaisesRegex(APIError, error):
                    store.submit_feedback(session_id, {"scene_revision": 2, **fields})
            self.assertEqual(store.get_session(session_id)["feedback_count"], 0)

    def test_one_note_can_reference_multiple_glb_nodes_without_selecting_each_one(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = SceneStore(root / "data")
            asset_name = "a" * 32 + ".glb"
            tiny_glb(store.assets_dir / asset_name)
            store.replace_scene(1, [{"id": "scene_preview", "type": "model", "url": f"/assets/{asset_name}",
                                     "position": [0, 0, 0], "size": [1, 1, 1]}])
            session_id = store.ensure_workspace(root)["session_id"]
            note = "把 [[node:scene_preview:0/2]] 的顶面抬高，并将 [[node:scene_preview:0/5]] 往左移。"
            nodes = [
                {"parent_object_id": "scene_preview", "node_path": [0, 2], "node_name": "Cabinet"},
                {"parent_object_id": "scene_preview", "node_path": [0, 5], "node_name": "Lamp"},
            ]
            packet = store.submit_feedback(session_id, {"scene_revision": 2, "note": note,
                                                        "referenced_scene_nodes": nodes})
            self.assertEqual([item["id"] for item in packet["inline_references"]],
                             ["scene_preview:0/2", "scene_preview:0/5"])
            self.assertEqual(packet["inline_references"][0]["scene_node"], nodes[0])
            self.assertEqual(packet["selected_scene_nodes"], [])
            message, _ = WorkspaceGateway(store, root)._turn_input(packet)
            self.assertIn('[[node:scene_preview:0/5]] → {"parent_object_id": "scene_preview", "node_path": [0, 5]', message)
            self.assertIn("节点路径是用户查看器中的子节点索引", message)

            invalid = [
                ({"note": note, "referenced_scene_nodes": [nodes[0]]}, "no matching submitted scene node"),
                ({"note": note, "referenced_scene_nodes": nodes + [nodes[1]]}, "must be unique"),
                ({"note": "[[node:scene_preview:0/2]]", "referenced_scene_nodes": nodes}, "not cited in note"),
                ({"note": "[[node:scene_preview:0/99999]]", "referenced_scene_nodes": nodes}, "no matching submitted scene node"),
                ({"note": "[[node:scene_preview:0/02]]", "referenced_scene_nodes": nodes}, "no matching submitted scene node"),
                ({"note": "[[node:scene_preview:0/x]]", "referenced_scene_nodes": nodes}, "invalid inline reference"),
            ]
            for fields, error in invalid:
                with self.subTest(fields=fields), self.assertRaisesRegex(APIError, error):
                    store.submit_feedback(session_id, {"scene_revision": 2, **fields})

    def test_inline_object_reference_can_identify_selected_object_in_confirmed_stale_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = SceneStore(temporary)
            seed_chair(store)
            session_id = store.create_session()["session_id"]
            store.replace_scene(2, [])
            packet = store.submit_feedback(session_id, {"scene_revision": 2, "confirm_stale": True,
                                                        "selected_object_ids": ["chair_back"],
                                                        "note": "旧图里的 [[object:chair_back]] 需要保留。"})
            self.assertEqual(packet["inline_references"][0]["object"], {"id": "chair_back"})
            self.assertTrue(packet["inline_references"][0]["from_stale_snapshot"])

    def test_object_prompts_validate_text_and_optional_scene_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = SceneStore(temporary)
            session_id = store.create_session()["session_id"]
            bad_prompts = [
                {"object": "", "prompt": "move it"},
                {"object": "cup", "prompt": " "},
                {"object": "cup", "prompt": "move it", "object_id": "missing"},
                {"object": "cup", "prompt": "move it", "operation": "delete"},
            ]
            for prompt in bad_prompts:
                with self.subTest(prompt=prompt), self.assertRaises(APIError):
                    store.submit_feedback(session_id, {"scene_revision": 1, "object_prompts": [prompt]})
            with self.assertRaisesRegex(APIError, "at most 24"):
                store.submit_feedback(session_id, {"scene_revision": 1, "object_prompts": [{"object": "cup", "prompt": "move it"}] * 25})
            self.assertEqual(store.get_session(session_id)["feedback_count"], 0)

    def test_visual_packet_persists_originals_and_reuses_session_after_scene_update(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_url, original = image_data_url()
            source = root / "room.png"
            source.write_bytes(original)
            store = SceneStore(root / "data")
            seed_chair(store)
            session = store.create_session([str(source)])
            reference = session["reference_images"][0]
            source.unlink()
            self.assertEqual((store.media_dir / reference["url"].rsplit("/", 1)[-1]).read_bytes(), original)
            revision = store.scene()["revision"]
            packet = store.submit_feedback(session["session_id"], {
                "scene_revision": revision,
                "note": "The cabinet top should meet line 1.",
                "annotations": [
                    {"id": "a1", "pane": "reference", "reference_image_id": reference["id"], "type": "line", "coordinates": {"x": 0.1, "y": 0.2, "x2": 0.9, "y2": 0.2}, "group_id": "1"},
                    {"id": "a2", "pane": "scene", "type": "point", "coordinates": {"x": 0.4, "y": 0.6}, "group_id": "1", "object_id": "chair_back"},
                ],
                "selected_object_ids": ["chair_back"], "camera": {"position": [2, 3, 4]},
                "reference_annotated_data_urls": [{"reference_id": reference["id"], "data_url": data_url}],
                "scene_original_data_url": data_url, "scene_annotated_data_url": data_url,
            })
            self.assertEqual(packet["reference_images"][0]["id"], reference["id"])
            self.assertEqual(packet["selected_object_ids"], ["chair_back"])
            self.assertEqual((store.media_dir / packet["scene_original_url"].rsplit("/", 1)[-1]).read_bytes(), original)
            with patch.object(mcp_server, "DATA_DIR", store.data_dir):
                visual = mcp_server._visual_tool_result(store.feedback(session["session_id"]))
            self.assertEqual(sum(block.type == "image" for block in visual.content), 4)
            self.assertEqual(visual.structured_content["items"][0]["scene_revision"], revision)
            scene = store.update_scene(revision, [{"op": "update", "object_id": "chair_back", "fields": {"size": [1.25, 0.16, 1.6]}}])
            store.submit_feedback(session["session_id"], {"scene_revision": scene["revision"], "note": "Now the height is right."})
            reopened = SceneStore(store.data_dir)
            self.assertEqual(reopened.get_session(session["session_id"])["feedback_count"], 2)
            self.assertEqual(reopened.feedback(session["session_id"])["next_cursor"], 2)
            self.assertEqual(reopened.feedback(session["session_id"])["items"][0]["reference_images"][0]["url"], reference["url"])

    def test_visual_annotation_rejects_unknown_reference_and_invalid_coordinates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = SceneStore(temporary)
            session = store.create_session()
            revision = store.scene()["revision"]
            for mark in (
                {"pane": "reference", "reference_image_id": "missing", "type": "point", "coordinates": {"x": 0.5, "y": 0.5}},
                {"pane": "scene", "type": "arrow", "coordinates": {"x": 0.5, "y": 0.5, "x2": 1.2, "y2": 0.1}},
            ):
                with self.assertRaises(APIError):
                    store.submit_feedback(session["session_id"], {"scene_revision": revision, "annotations": [mark]})
            image_url, _ = image_data_url()
            with self.assertRaisesRegex(APIError, "MIME type"):
                store.add_reference(session["session_id"], "wrong.jpg", image_url.replace("image/png", "image/jpeg", 1))
            self.assertEqual(store.get_session(session["session_id"])["feedback_count"], 0)

    def test_model_import_and_missing_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = SceneStore(Path(temporary) / "data")
            model = Path(temporary) / "small.glb"
            tiny_glb(model)
            imported = store.import_model(str(model), object_id="sample_model")
            self.assertEqual(imported["object"]["type"], "model")
            self.assertTrue((store.assets_dir / imported["object"]["url"].rsplit("/", 1)[-1]).is_file())
            with self.assertRaisesRegex(APIError, "does not exist"):
                store.import_model(str(Path(temporary) / "missing.glb"))
            before = store.scene()["revision"]
            preview = store.set_scene_preview(str(model))
            self.assertEqual(preview["revision"], before + 1)
            self.assertEqual([obj["id"] for obj in preview["objects"]], ["scene_preview"])

    def test_existing_v1_state_loads_without_losing_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = SceneStore(temporary)
            session = store.create_session()
            state = json.loads(store.state_path.read_text(encoding="utf-8"))
            state["schema_version"] = 1
            state["sessions"][session["session_id"]].pop("reference_images")
            store.state_path.write_text(json.dumps(state), encoding="utf-8")
            loaded = SceneStore(temporary)
            self.assertEqual(loaded.get_session(session["session_id"])["reference_images"], [])
            self.assertEqual(loaded.get_session(session["session_id"])["status"], "open")

    def test_model_child_selection_is_preserved_as_visual_reference(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            model = root / "room.glb"
            tiny_glb(model)
            store = SceneStore(root / "data")
            scene = store.set_scene_preview(str(model))
            session = store.create_session()
            node = {"parent_object_id": "scene_preview", "node_path": [0, 2], "node_name": "Cabinet top"}
            packet = store.submit_feedback(session["session_id"], {
                "scene_revision": scene["revision"], "note": "Move this cabinet toward the photo mark.",
                "selected_object_ids": ["scene_preview"], "selected_scene_nodes": [node],
                "annotations": [{"pane": "scene", "type": "point", "object_id": "scene_preview", "scene_node": node, "coordinates": {"x": 0.5, "y": 0.4}}],
            })
            self.assertEqual(packet["selected_scene_nodes"], [node])
            self.assertEqual(SceneStore(store.data_dir).feedback(session["session_id"])["items"][0]["annotations"][0]["scene_node"], node)
            another = store.create_session()
            with self.assertRaisesRegex(APIError, "child indices"):
                store.submit_feedback(another["session_id"], {
                    "scene_revision": scene["revision"], "note": "invalid path", "selected_object_ids": ["scene_preview"],
                    "selected_scene_nodes": [{**node, "node_path": [0, -1]}],
                })


class FakeAdapter:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.responses: list[tuple] = []
        self.callback = None
        self.connected = True
        self.starts = 0

    def start(self) -> str:
        self.starts += 1
        self.connected = True
        return "thread-test-persistent"

    def status(self) -> dict:
        return {"connected": self.connected, "turn_state": "idle"}

    def refresh(self) -> dict:
        self.connected = True
        return self.status()

    def start_turn(self, text: str, image_paths: list[str], message_id: str) -> dict:
        call = {"text": text, "image_paths": image_paths, "message_id": message_id, "turn_id": f"turn-{len(self.calls) + 1}"}
        self.calls.append(call)
        return {"thread_id": "thread-test-persistent", "turn_id": call["turn_id"], "status": "running"}

    def respond_to_request(self, request_id: str, result: dict) -> None:
        self.responses.append((request_id, result))

    def interrupt(self, turn_id: str | None = None) -> dict:
        return {"turn_id": turn_id, "status": "interruption_requested"}

    def close(self) -> None:
        pass


class GatewayTests(unittest.TestCase):
    def test_mcp_registers_workspace_and_external_review_tools(self) -> None:
        names = [tool.name for tool in asyncio.run(mcp_server.mcp.list_tools())]
        self.assertEqual(names, ["workspace_open", "workspace_get_context", "workspace_get_feedback", "workspace_export_pose_sources", "workspace_import_human_pose", "workspace_get_human_pose", "workspace_publish_scene",
                                 "workspace_inspect_world", "workspace_initialize_world", "workspace_edit_world", "workspace_restore_world", "workspace_build_world", "workspace_simulate_world", "workspace_evaluate_world", "workspace_publish_world",
                                 "workspace_set_reference_clip", "workspace_request_feedback", "request_visual_feedback", "wait_visual_feedback", "get_visual_feedback"])

    @staticmethod
    def wait_for(predicate, timeout: float = 3) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        raise AssertionError("timed out waiting for gateway state")

    @staticmethod
    def gateway_with_adapter(root: Path) -> tuple[WorkspaceGateway, FakeAdapter]:
        project = root / "project"
        project.mkdir()
        adapter = FakeAdapter()
        gateway = WorkspaceGateway(SceneStore(root / "data"), project, adapter=adapter)
        gateway.start()
        return gateway, adapter

    @staticmethod
    def pending(gateway: WorkspaceGateway, request_id: str, kind: str, details: dict) -> str:
        gateway.on_adapter_event({"method": "adapter/request_pending", "params": {"request_id": request_id, "method": kind, "params": details}})
        return gateway.state()["approvals"][-1]["approval_id"]

    def test_elicitation_nonempty_form_url_and_empty_form_responses(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            gateway, adapter = self.gateway_with_adapter(Path(temporary))
            form = {"mode": "form", "message": "Choose one", "requestedSchema": {"type": "object", "properties": {"choice": {"type": "string", "enum": ["left", "right"], "description": "x" * 4500}}, "required": ["choice"]}}
            approval_id = self.pending(gateway, "rpc-form", "mcpServer/elicitation/request", form)
            shown = gateway.state()["approvals"][0]
            self.assertGreater(len(json.dumps(shown["details"])), 4000)
            self.assertLessEqual(len(shown["prompt"]), 4000)
            self.assertNotIn("request_id", shown)
            self.assertEqual(SceneStore(gateway.store.data_dir).workspace()["approvals"][0]["details"], form)
            with self.assertRaisesRegex(APIError, "required field"):
                gateway.respond_to_approval(approval_id, {"decision": "accept", "content": {}})
            with self.assertRaisesRegex(APIError, "offered option"):
                gateway.respond_to_approval(approval_id, {"decision": "accept", "content": {"choice": "middle"}})
            self.assertEqual(adapter.responses, [])
            gateway.respond_to_approval(approval_id, {"decision": "accept", "content": {"choice": "left"}})
            self.assertEqual(adapter.responses[-1], ("rpc-form", {"action": "accept", "content": {"choice": "left"}}))

            empty_id = self.pending(gateway, "rpc-empty", "mcpServer/elicitation/request", {"mode": "form", "message": "Okay?", "requestedSchema": {"type": "object", "properties": {}}})
            gateway.respond_to_approval(empty_id, {"decision": "accept"})
            self.assertEqual(adapter.responses[-1], ("rpc-empty", {"action": "accept", "content": {}}))

            url_id = self.pending(gateway, "rpc-url", "mcpServer/elicitation/request", {"mode": "url", "url": "https://example.test/verify", "message": "Open verification"})
            gateway.respond_to_approval(url_id, {"decision": "accept"})
            self.assertEqual(adapter.responses[-1], ("rpc-url", {"action": "accept"}))
            verify_id = self.pending(gateway, "rpc-verify", "mcpServer/elicitation/request", {"mode": "openai/userVerification", "message": "Verify"})
            gateway.respond_to_approval(verify_id, {"decision": "decline"})
            self.assertEqual(adapter.responses[-1], ("rpc-verify", {"action": "decline"}))
            openai_id = self.pending(gateway, "rpc-openai", "mcpServer/elicitation/request", {"mode": "openai/form", "message": "Describe", "requestedSchema": {"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]}})
            gateway.respond_to_approval(openai_id, {"decision": "accept", "content": {"note": "move the shelf"}})
            self.assertEqual(adapter.responses[-1], ("rpc-openai", {"action": "accept", "content": {"note": "move the shelf"}}))
            legacy_id = self.pending(gateway, "rpc-legacy", "mcpServer/elicitation/request", {"mode": "openaiForm", "message": "Confirm", "requestedSchema": {"type": "object", "properties": {}}})
            gateway.respond_to_approval(legacy_id, {"decision": "accept"})
            self.assertEqual(adapter.responses[-1], ("rpc-legacy", {"action": "accept", "content": {}}))

    def test_form_nullable_bounds_and_offered_choices(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            gateway, adapter = self.gateway_with_adapter(Path(temporary))
            schema = {
                "type": "object",
                "properties": {
                    "amount": {"type": "number", "minimum": None, "maximum": 2.0},
                    "color": {"type": "string", "oneOf": [{"const": "red", "title": "Red"}, {"const": "blue", "title": "Blue"}]},
                    "tags": {"type": "array", "items": {"type": "string", "enum": ["near", "far"]}, "minItems": 1},
                },
                "required": ["amount", "color", "tags"],
            }
            approval_id = self.pending(gateway, "rpc-choices", "mcpServer/elicitation/request", {"mode": "form", "message": "Choose", "requestedSchema": schema})
            with self.assertRaisesRegex(APIError, "maximum"):
                gateway.respond_to_approval(approval_id, {"decision": "accept", "content": {"amount": 3, "color": "red", "tags": ["near"]}})
            with self.assertRaisesRegex(APIError, "offered option"):
                gateway.respond_to_approval(approval_id, {"decision": "accept", "content": {"amount": 1, "color": "green", "tags": ["near"]}})
            with self.assertRaisesRegex(APIError, "unoffered choice"):
                gateway.respond_to_approval(approval_id, {"decision": "accept", "content": {"amount": 1, "color": "red", "tags": ["elsewhere"]}})
            self.assertEqual(adapter.responses, [])
            gateway.respond_to_approval(approval_id, {"decision": "accept", "content": {"amount": 1, "color": "red", "tags": ["near"]}})
            self.assertEqual(adapter.responses[-1], ("rpc-choices", {"action": "accept", "content": {"amount": 1, "color": "red", "tags": ["near"]}}))

    def test_permission_response_never_grants_beyond_requested_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            gateway, adapter = self.gateway_with_adapter(Path(temporary))
            requested = {"network": {"enabled": True}, "fileSystem": {"read": ["/tmp/reference"]}}
            details = {"permissions": requested, "reason": "read reference image"}
            approval_id = self.pending(gateway, "rpc-permit", "item/permissions/requestApproval", details)
            with self.assertRaisesRegex(APIError, "exactly match"):
                gateway.respond_to_approval(approval_id, {"decision": "accept", "permissions": {"network": {"enabled": True}, "fileSystem": {"read": ["/"]}}})
            self.assertEqual(adapter.responses, [])
            gateway.respond_to_approval(approval_id, {"decision": "accept", "permissions": requested, "scope": "turn"})
            self.assertEqual(adapter.responses[-1], ("rpc-permit", {"permissions": requested, "scope": "turn"}))
            decline_id = self.pending(gateway, "rpc-deny", "item/permissions/requestApproval", details)
            gateway.respond_to_approval(decline_id, {"decision": "decline"})
            self.assertEqual(adapter.responses[-1], ("rpc-deny", {"permissions": {}, "scope": "turn"}))

    def test_user_input_answers_and_decline_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            gateway, adapter = self.gateway_with_adapter(Path(temporary))
            details = {"questions": [{"id": "layout", "header": "Layout", "question": "Which?", "options": [{"label": "Left", "description": "Use left"}]}, {"id": "material", "header": "Material", "question": "Which?", "options": None}]}
            approval_id = self.pending(gateway, "rpc-questions", "item/tool/requestUserInput", details)
            with self.assertRaisesRegex(APIError, "every requested question"):
                gateway.respond_to_approval(approval_id, {"decision": "accept", "answers": {"layout": {"answers": ["Left"]}}})
            answers = {"layout": {"answers": ["Left"]}, "material": {"answers": ["wood"]}}
            gateway.respond_to_approval(approval_id, {"decision": "accept", "answers": answers})
            self.assertEqual(adapter.responses[-1], ("rpc-questions", {"answers": answers}))
            decline_id = self.pending(gateway, "rpc-skip", "item/tool/requestUserInput", details)
            gateway.respond_to_approval(decline_id, {"decision": "decline"})
            self.assertEqual(adapter.responses[-1], ("rpc-skip", {"answers": {"layout": {"answers": []}, "material": {"answers": []}}}))

    def test_oversized_approval_details_fail_closed_but_allow_decline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            gateway, adapter = self.gateway_with_adapter(Path(temporary))
            details = {"questions": [{"id": "secret", "header": "Secret", "question": "x" * 70_000}]}
            approval_id = self.pending(gateway, "rpc-large", "item/tool/requestUserInput", details)
            shown = gateway.state()["approvals"][0]
            self.assertTrue(shown["details_truncated"])
            self.assertIsNone(shown["details"])
            with self.assertRaisesRegex(APIError, "too large"):
                gateway.respond_to_approval(approval_id, {"decision": "accept", "answers": {"secret": {"answers": ["yes"]}}})
            self.assertEqual(adapter.responses, [])
            gateway.respond_to_approval(approval_id, {"decision": "decline"})
            self.assertEqual(adapter.responses[-1], ("rpc-large", {"answers": {"secret": {"answers": []}}}))
            unknown_id = self.pending(gateway, "rpc-unknown", "unknown/request", {"description": "x" * 70_000})
            with self.assertRaisesRegex(APIError, "interrupt this turn from the workbench"):
                gateway.respond_to_approval(unknown_id, {"decision": "decline"})
            self.assertEqual(len(adapter.responses), 1)
            known_size_unknown_id = self.pending(gateway, "rpc-unknown-short", "unknown/request", {"description": "unsupported request"})
            with self.assertRaisesRegex(APIError, "interrupt this turn from the workbench"):
                gateway.respond_to_approval(known_size_unknown_id, {"decision": "decline"})
            self.assertEqual(len(adapter.responses), 1)
            many_questions = {"questions": [{"id": f"q{index}", "question": "x" * 700} for index in range(101)]}
            many_id = self.pending(gateway, "rpc-many", "item/tool/requestUserInput", many_questions)
            with self.assertRaisesRegex(APIError, "interrupt this turn from the workbench"):
                gateway.respond_to_approval(many_id, {"decision": "decline"})
            self.assertEqual(len(adapter.responses), 1)

    def test_empty_thread_recreation_updates_gateway_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            gateway, adapter = self.gateway_with_adapter(Path(temporary))
            self.assertEqual(gateway.state()["thread_id"], "thread-test-persistent")
            gateway.on_adapter_event({"method": "adapter/empty_thread_recreated", "params": {"old_thread_id": "thread-test-persistent", "thread_id": "thread-new-empty"}})
            self.assertEqual(gateway.state()["thread_id"], "thread-new-empty")
            self.assertTrue(any(event["type"] == "empty_thread_recreated" for event in gateway.store.workspace_events()["items"]))

    def test_direct_image_turn_idempotency_scene_updates_and_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            store = SceneStore(root / "data")
            adapter = FakeAdapter()
            gateway = WorkspaceGateway(store, project, adapter=adapter)
            gateway.start()
            session_id = gateway.state()["session_id"]
            data_url, original = image_data_url()
            reference = store.add_reference(session_id, "room.png", data_url)
            first_payload = {"idempotency_key": "first-message-0001", "scene_revision": 1, "note": "Build the cabinet shown in the photo.", "reference_annotated_data_urls": [{"reference_id": reference["id"], "data_url": data_url}]}
            first = gateway.submit(session_id, first_payload)
            self.wait_for(lambda: len(adapter.calls) == 1)
            self.assertEqual(adapter.calls[0]["message_id"], first["feedback_id"])
            self.assertEqual(len(adapter.calls[0]["image_paths"]), 2)
            self.assertEqual(Path(adapter.calls[0]["image_paths"][0]).read_bytes(), original)
            duplicate = gateway.submit(session_id, first_payload)
            self.assertEqual(duplicate["feedback_id"], first["feedback_id"])
            self.assertEqual(len(gateway.state()["queue"]), 1)
            with self.assertRaisesRegex(APIError, "another submission"):
                gateway.submit(session_id, {**first_payload, "note": "different"})
            second = gateway.submit(session_id, {"idempotency_key": "second-message-0001", "scene_revision": 1,
                                                "note": "Make it taller.", "scene_original_data_url": data_url})
            self.assertEqual(len(adapter.calls), 1)
            model = root / "updated.glb"
            tiny_glb(model)
            scene = store.set_scene_preview(str(model), expected_revision=1)
            self.assertEqual(scene["revision"], 2)
            gateway.on_adapter_event({"method": "turn/completed", "params": {"turn": {"id": "turn-1", "status": "completed"}}})
            self.wait_for(lambda: len(adapter.calls) == 2)
            self.wait_for(lambda: any(item["feedback_id"] == second["feedback_id"] and item["status"] == "running" for item in gateway.state()["queue"]))
            self.assertIn("场景版本 1，发送时当前版本为 2", adapter.calls[1]["text"])
            self.assertNotIn("用户已确认", adapter.calls[1]["text"])
            self.assertEqual(store.feedback_by_id(second["feedback_id"])["scene_revision"], 1)
            self.assertEqual((store.data_dir / second["scene_original_url"].lstrip("/")).read_bytes(), original)
            self.assertFalse(any(event["type"] == "stale_feedback_confirmation_required" for event in store.workspace_events()["items"]))
            gateway.on_adapter_event({"method": "adapter/request_pending", "params": {"request_id": "rpc-3", "method": "item/commandExecution/requestApproval", "params": {"command": "true"}}})
            approval = gateway.state()["approvals"][0]
            self.assertEqual(gateway.state()["agent"]["status"], "awaiting_approval")
            gateway.respond_to_approval(approval["approval_id"], {"decision": "decline"})
            self.assertEqual(adapter.responses, [("rpc-3", {"decision": "decline"})])
            gateway.on_adapter_event({"method": "adapter/request_pending", "params": {"request_id": "rpc-4", "method": "mcpServer/elicitation/request", "params": {"mode": "form", "message": "Allow workspace_publish_scene?", "requestedSchema": {"type": "object", "properties": {}}, "_meta": {"codex_approval_kind": "mcp_tool_call"}}}})
            approval = gateway.state()["approvals"][0]
            gateway.respond_to_approval(approval["approval_id"], {"decision": "accept"})
            self.assertEqual(adapter.responses[-1], ("rpc-4", {"action": "accept", "content": {}}))
            gateway.on_adapter_event({"method": "item/completed", "params": {"item": {"type": "agentMessage", "text": "I updated the cabinet source and published the GLB."}}})
            self.assertTrue(any(event["type"] == "assistant_message" for event in store.workspace_events()["items"]))
            gateway.on_adapter_event({"method": "turn/completed", "params": {"turn": {"id": "turn-2", "status": "completed"}}})
            self.assertEqual(gateway.state()["thread_id"], "thread-test-persistent")
            self.assertEqual(gateway.state()["agent"]["status"], "idle")

    def test_restart_resumes_saved_visual_feedback_without_replaying_delivered_packets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            store = SceneStore(root / "data")
            staging = WorkspaceGateway(store, project, adapter=FakeAdapter())
            session_id = staging.ensure()["session_id"]
            store.workspace_thread("thread-test-persistent")
            image_url, original = image_data_url()
            packets = []
            for name in ("resume", "legacy-text", "already-sent"):
                payload = {"idempotency_key": "saved-stale-" + name, "scene_revision": 1, "note": name}
                if name != "legacy-text":
                    payload["scene_original_data_url"] = image_url
                packets.append(store.submit_feedback(session_id, payload))
            store.replace_scene(1, [])
            with store.lock:
                for item in store.state["workspace"]["queue"]:
                    item["status"] = "blocked_stale"
                    if item["feedback_id"] == packets[2]["feedback_id"]:
                        item["turn_id"] = "already-delivered-turn"
                store._save()

            reloaded = SceneStore(store.data_dir)
            adapter = FakeAdapter()
            gateway = WorkspaceGateway(reloaded, project, adapter=adapter)
            gateway.start()
            try:
                self.wait_for(lambda: len(adapter.calls) == 1)
                self.assertEqual(adapter.calls[0]["message_id"], packets[0]["feedback_id"])
                self.assertIn("场景版本 1，发送时当前版本为 2", adapter.calls[0]["text"])
                self.assertNotIn("用户已确认", adapter.calls[0]["text"])
                stored = reloaded.feedback_by_id(packets[0]["feedback_id"])
                self.assertEqual(stored["scene_revision"], 1)
                self.assertEqual((reloaded.data_dir / stored["scene_original_url"].lstrip("/")).read_bytes(), original)
                queued = {item["feedback_id"]: item for item in gateway.state()["queue"]}
                self.assertEqual(queued[packets[1]["feedback_id"]]["status"], "blocked_stale")
                self.assertEqual(queued[packets[2]["feedback_id"]]["status"], "blocked_stale")
                self.assertEqual(queued[packets[2]["feedback_id"]]["turn_id"], "already-delivered-turn")
                gateway._restore_blocked_visual_feedback()
                self.assertEqual(len(adapter.calls), 1)
                self.assertEqual(sum(event["type"] == "saved_visual_feedback_resumed" for event in reloaded.workspace_events()["items"]), 1)
            finally:
                gateway.close()

    def test_restart_keeps_active_legacy_packet_out_of_automatic_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = SceneStore(root / "data")
            gateway = WorkspaceGateway(store, root, adapter=FakeAdapter())
            session_id = gateway.ensure()["session_id"]
            image_url, _ = image_data_url()
            packet = store.submit_feedback(session_id, {"idempotency_key": "active-old-packet", "scene_revision": 1,
                                                       "note": "saved", "scene_original_data_url": image_url})
            with store.lock:
                item = store.state["workspace"]["queue"][0]
                item["status"] = "blocked_stale"
                store.state["workspace"]["active_feedback_id"] = packet["feedback_id"]
                store._save()
            gateway._restore_blocked_visual_feedback()
            reloaded = SceneStore(store.data_dir).workspace()
            self.assertEqual(reloaded["queue"][0]["status"], "blocked_stale")
            self.assertEqual(reloaded["active_feedback_id"], packet["feedback_id"])
            self.assertEqual(gateway.adapter.calls, [])

    def test_disconnect_during_turn_never_replays_without_explicit_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            store = SceneStore(root / "data")
            adapter = FakeAdapter()
            gateway = WorkspaceGateway(store, project, adapter=adapter)
            gateway.start()
            session_id = gateway.state()["session_id"]
            packet = gateway.submit(session_id, {"idempotency_key": "disconnect-message-0001", "scene_revision": 1, "note": "Look at this room."})
            self.wait_for(lambda: gateway.state()["queue"][0]["status"] == "running")
            gateway.on_adapter_event({"method": "adapter/disconnected", "params": {}})
            self.assertEqual(gateway.state()["queue"][0]["status"], "delivery_uncertain")
            gateway.on_adapter_event({"method": "adapter/reconnected", "params": {}})
            gateway.wake()
            time.sleep(0.1)
            self.assertEqual(len(adapter.calls), 1)
            self.assertEqual(SceneStore(store.data_dir).workspace()["queue"][0]["feedback_id"], packet["feedback_id"])
            self.assertEqual(gateway.state()["agent"]["status"], "delivery_uncertain")
            gateway.confirm_queue(packet["feedback_id"], {"retry_uncertain": True})
            self.wait_for(lambda: len(adapter.calls) == 2)
            gateway.close()

    def test_idle_disconnect_reconnects_to_the_same_thread_before_next_submission(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            store = SceneStore(root / "data")
            adapter = FakeAdapter()
            gateway = WorkspaceGateway(store, project, adapter=adapter)
            gateway.start()
            adapter.connected = False
            gateway.on_adapter_event({"method": "adapter/disconnected", "params": {}})
            gateway.submit(gateway.state()["session_id"], {"idempotency_key": "reconnect-message-0001", "scene_revision": 1, "note": "Continue after reconnect."})
            self.wait_for(lambda: len(adapter.calls) == 1)
            self.assertEqual(adapter.starts, 2)
            self.assertEqual(gateway.state()["thread_id"], "thread-test-persistent")
            gateway.close()


class HTTPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        web_dir = root / "web"
        web_dir.mkdir()
        (web_dir / "index.html").write_text("<html>viewer</html>", encoding="utf-8")
        self.server = make_server(port=0, data_dir=root / "data", web_dir=web_dir)
        seed_chair(self.server.scene_store)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.token = (root / "data" / "control_token").read_text(encoding="ascii").strip()
        self.browser_token = (root / "data" / "browser_token").read_text(encoding="ascii").strip()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temporary.cleanup()

    def request(self, method: str, path: str, payload: dict | None = None, *, key: bool = False, browser: bool = True, origin: str | None = None):
        headers = {"Accept": "application/json"}
        if browser:
            headers["X-Workspace-Capability"] = self.browser_token
        body = None
        if payload is not None:
            body = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        if key:
            headers["X-Scene-Harness-Key"] = self.token
        if origin:
            headers["Origin"] = origin
        request = urllib.request.Request(self.base + path, data=body, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=10) as response:
                data = response.read()
                return response.status, json.loads(data) if response.headers.get_content_type() == "application/json" else data
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, json.loads(exc.read())

    def test_session_feedback_and_revision_update(self) -> None:
        status, health = self.request("GET", "/api/health")
        self.assertEqual((status, health["service"]), (200, "scene-feedback-harness"))
        status, scene = self.request("GET", "/api/scene")
        self.assertEqual(status, 200)
        status, session = self.request("POST", "/api/sessions", {})
        self.assertEqual(status, 201)
        status, denied = self.request("POST", f"/api/sessions/{session['session_id']}/feedback", {"idempotency_key": "another-key-0001", "scene_revision": scene["revision"], "note": "Ignored"}, browser=False)
        self.assertEqual(status, 403)
        self.assertIn(f"127.0.0.1:{self.server.server_port}", session["url"])
        status, feedback = self.request("POST", f"/api/sessions/{session['session_id']}/feedback", {
            "idempotency_key": "test-taller-0001", "scene_revision": scene["revision"], "annotations": [{"type": "target_box", "object_id": "chair_back", "center": [0, 0, 1.8], "size": [1, 0.2, 1.6], "anchor": "bottom"}], "note": "Taller"
        })
        self.assertEqual(status, 201)
        status, result = self.request("GET", f"/api/sessions/{session['session_id']}/feedback")
        self.assertEqual(result["items"][0]["feedback_id"], feedback["feedback_id"])
        status, denied = self.request("POST", "/api/scene/update", {"expected_revision": scene["revision"], "changes": []})
        self.assertEqual(status, 403)
        status, updated = self.request("POST", "/api/scene/update", {"expected_revision": scene["revision"], "changes": [{"op": "update", "object_id": "chair_back", "fields": {"size": [1.25, 0.16, 1.6]}}]}, key=True)
        self.assertEqual(status, 200)
        self.assertEqual(updated["revision"], scene["revision"] + 1)
        status, conflict = self.request("POST", "/api/scene/update", {"expected_revision": scene["revision"], "changes": [{"op": "delete", "object_id": "chair_back"}]}, key=True)
        self.assertEqual(status, 409)

    def test_model_asset_and_static_page(self) -> None:
        status, page = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"viewer", page)
        model = Path(self.temporary.name) / "small.glb"
        tiny_glb(model)
        status, imported = self.request("POST", "/api/models/import", {"local_path": str(model), "object_id": "small_model"}, key=True)
        self.assertEqual(status, 201)
        status, asset = self.request("GET", imported["object"]["url"])
        self.assertEqual(status, 200)
        self.assertEqual(asset, model.read_bytes())

    def test_workspace_publish_requires_expected_revision(self) -> None:
        model = Path(self.temporary.name) / "room.glb"
        tiny_glb(model)
        status, current = self.request("GET", "/api/scene")
        self.assertEqual(status, 200)
        status, published = self.request("POST", "/api/workspace/publish", {"local_path": str(model), "expected_revision": current["revision"]}, key=True)
        self.assertEqual(status, 200)
        self.assertEqual(published["revision"], current["revision"] + 1)
        status, conflict = self.request("POST", "/api/workspace/publish", {"local_path": str(model), "expected_revision": current["revision"]}, key=True)
        self.assertEqual(status, 409)
        status, after = self.request("GET", "/api/scene")
        self.assertEqual(after, published)

    def test_browser_upload_and_large_two_reference_visual_packet(self) -> None:
        image_url, image_bytes = image_data_url(large=True)
        self.assertGreater(len(image_bytes) * 4 // 3 * 4, 8 * 1024 * 1024)
        status, session = self.request("POST", "/api/sessions", {})
        self.assertEqual(status, 201)
        references = []
        for index in range(2):
            status, reference = self.request("POST", f"/api/sessions/{session['session_id']}/references", {"name": f"reference-{index}.jpg", "data_url": image_url})
            self.assertEqual(status, 201)
            references.append(reference)
        status, stored_session = self.request("GET", f"/api/sessions/{session['session_id']}")
        self.assertEqual(len(stored_session["reference_images"]), 2)
        status, original = self.request("GET", references[0]["url"])
        self.assertEqual((status, original), (200, image_bytes))
        status, scene = self.request("GET", "/api/scene")
        status, feedback = self.request("POST", f"/api/sessions/{session['session_id']}/feedback", {
            "idempotency_key": "test-large-packet-0001", "scene_revision": scene["revision"], "note": "Align the furniture with marks 1 and 2.",
            "reference_annotated_data_urls": [{"reference_id": item["id"], "data_url": image_url} for item in references],
            "scene_original_data_url": image_url, "scene_annotated_data_url": image_url,
        })
        self.assertEqual(status, 201)
        self.assertEqual(len(feedback["reference_annotated_images"]), 2)
        self.assertEqual(feedback["reference_images"], references)
        status, denied = self.request("POST", "/api/sessions", {"reference_images": ["/tmp/secret.png"]})
        self.assertEqual(status, 403)
        tiny_url, _ = image_data_url()
        status, denied = self.request("POST", f"/api/sessions/{session['session_id']}/references", {"name": "other.png", "data_url": tiny_url}, origin="https://example.com")
        self.assertEqual(status, 403)

    def test_mcp_tool_returns_actual_images_after_visual_submission(self) -> None:
        data_url, image_bytes = image_data_url()
        with patch.object(mcp_server, "PORT", self.server.server_port), patch.object(mcp_server, "BASE_URL", self.base), patch.object(mcp_server, "DATA_DIR", Path(self.temporary.name) / "data"):
            opened = asyncio.run(mcp_server.mcp.call_tool("workspace_open", {"open_browser": False}))
            session_id = opened.structured_content["session_id"]
            status, reference = self.request("POST", f"/api/sessions/{session_id}/references", {"name": "photo.png", "data_url": data_url})
            self.assertEqual(status, 201)
            status, scene = self.request("GET", "/api/scene")
            self.assertEqual(status, 200)
            status, submitted = self.request("POST", f"/api/sessions/{session_id}/feedback", {"idempotency_key": "test-mcp-image-0001", "scene_revision": scene["revision"], "note": "Move this edge", "reference_annotated_data_urls": [{"reference_id": reference["id"], "data_url": data_url}], "scene_original_data_url": data_url, "scene_annotated_data_url": data_url})
            self.assertEqual(status, 201)
            result = asyncio.run(mcp_server.mcp.call_tool("workspace_get_feedback", {"feedback_id": submitted["feedback_id"]}))
            self.assertEqual(sum(block.type == "image" for block in result.content), 4)
            self.assertEqual(result.structured_content["items"][0]["feedback_id"], submitted["feedback_id"])
            context = asyncio.run(mcp_server.mcp.call_tool("workspace_get_context", {}))
            self.assertEqual(context.structured_content["thread_id"], None)
            self.assertEqual(sum(block.type == "image" for block in context.content), 1)

    def test_invalid_workspace_publish_does_not_replace_scene(self) -> None:
        model = Path(self.temporary.name) / "preview.glb"
        tiny_glb(model)
        status, before = self.request("GET", "/api/scene")
        self.assertEqual(status, 200)
        with patch.object(mcp_server, "PORT", self.server.server_port), patch.object(mcp_server, "BASE_URL", self.base), patch.object(mcp_server, "DATA_DIR", Path(self.temporary.name) / "data"):
            with self.assertRaises(ValueError):
                mcp_server.workspace_publish_scene(str(model), expected_revision=before["revision"] + 1)
        status, after = self.request("GET", "/api/scene")
        self.assertEqual(after, before)


class ExternalReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        web_dir = self.root / "web"
        web_dir.mkdir()
        (web_dir / "index.html").write_text("<html>review</html>", encoding="utf-8")
        self.data_dir = self.root / "data"
        self.server = make_server(port=0, data_dir=self.data_dir, web_dir=web_dir, project_dir=self.project, external_review=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.token = (self.data_dir / "control_token").read_text(encoding="ascii").strip()
        self.browser_token = (self.data_dir / "browser_token").read_text(encoding="ascii").strip()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temporary.cleanup()

    def request(self, method: str, path: str, payload: dict | None = None, *, key: bool = False, browser: bool = False):
        headers = {"Accept": "application/json"}
        if key:
            headers["X-Scene-Harness-Key"] = self.token
        if browser:
            headers["X-Workspace-Capability"] = self.browser_token
        body = None
        if payload is not None:
            body = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base + path, data=body, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, json.loads(exc.read())

    def test_existing_task_receives_annotated_images_and_reference_import_is_idempotent(self) -> None:
        data_url, image_bytes = image_data_url()
        reference = self.project / "reference.png"
        reference.write_bytes(image_bytes)
        model = self.project / "preview.glb"
        tiny_glb(model)
        with patch.object(mcp_server, "BASE_URL", self.base), patch.object(mcp_server, "PORT", self.server.server_port), patch.object(mcp_server, "DATA_DIR", self.data_dir), patch.object(mcp_server, "PROJECT_DIR", self.project):
            opened = asyncio.run(mcp_server.request_visual_feedback(reference_images=[str(reference)], scene_glb_path=str(model), wait_for_submit=False, open_browser=False))
            session_id = opened.structured_content["session_id"]
            cursor = opened.structured_content["next_cursor"]
            self.assertFalse(opened.structured_content["browser_opened"])
            self.assertEqual(opened.structured_content["delivery_mode"], "external")
            status, state = self.request("GET", "/api/workspace/state")
            self.assertEqual((status, state["agent"]["status"], state["thread_id"]), (200, "external_idle", None))
            self.assertEqual(state["scene_revision"], 2)
            asyncio.run(mcp_server.request_visual_feedback(reference_images=[str(reference)], wait_for_submit=False, open_browser=False))
            status, context = self.request("GET", "/api/workspace/context")
            self.assertEqual(len(context["reference_images"]), 1)
            status, packet = self.request("POST", f"/api/sessions/{session_id}/feedback", {"idempotency_key": "external-review-0001", "scene_revision": 2, "note": "柜子顶部请贴近图中的红线", "reference_annotated_data_urls": [{"reference_id": context["reference_images"][0]["id"], "data_url": data_url}], "scene_original_data_url": data_url, "scene_annotated_data_url": data_url}, browser=True)
            self.assertEqual((status, packet["delivery"]["status"]), (201, "awaiting_mcp"))
            result = asyncio.run(mcp_server.wait_visual_feedback(session_id, cursor=cursor, timeout_sec=2))
            self.assertEqual(result.structured_content["items"][0]["feedback_id"], packet["feedback_id"])
            self.assertEqual(sum(block.type == "image" for block in result.content), 4)
            status, state = self.request("GET", "/api/workspace/state")
            self.assertEqual(state["agent"]["status"], "external_idle")
            self.assertEqual(state["queue"][0]["status"], "returned_to_mcp")

    def test_external_review_is_bound_to_its_project_and_control_key(self) -> None:
        data_url, data = image_data_url()
        outside = self.root / "outside.png"
        outside.write_bytes(data)
        status, denied = self.request("POST", "/api/workspace/references", {"reference_images": [str(outside)]})
        self.assertEqual(status, 403)
        status, denied = self.request("POST", "/api/workspace/references", {"reference_images": [str(outside)]}, key=True)
        self.assertEqual(status, 400)
        self.assertIn("project directory", denied["error"])
        status, denied = self.request("POST", "/api/workspace/publish", {"local_path": str(outside), "expected_revision": 1}, key=True)
        self.assertEqual(status, 400)
        status, state = self.request("GET", "/api/workspace/state")
        status, denied = self.request("POST", "/api/workspace/external/request", {"session_id": "a" * 32}, key=True)
        self.assertEqual(status, 409)
        status, denied = self.request("GET", f"/api/workspace/external/feedback?session_id={'b' * 32}&cursor=0", key=True)
        self.assertEqual(status, 409)
        self.assertEqual(state["agent"]["status"], "external_idle")

    def test_reference_labels_distinguish_camera_views_with_the_same_filename(self) -> None:
        _, image_bytes = image_data_url()
        paths = []
        for camera in ("C10095_rgb", "C10119_rgb"):
            directory = self.project / camera
            directory.mkdir()
            path = directory / "000480.jpg"
            path.write_bytes(image_bytes)
            paths.append(str(path))
        status, result = self.request("POST", "/api/workspace/references", {"reference_images": paths}, key=True)
        self.assertEqual(status, 200)
        self.assertEqual([item["name"] for item in result["reference_images"]],
                         ["C10095_rgb_000480.jpg", "C10119_rgb_000480.jpg"])
        status, result = self.request("POST", "/api/workspace/references", {"reference_images": paths}, key=True)
        self.assertEqual((status, len(result["reference_images"])), (200, 2))

    def test_external_mode_cannot_adopt_a_direct_delivery_workspace(self) -> None:
        store = self.server.scene_store
        gateway = WorkspaceGateway(store, self.project)
        with self.assertRaisesRegex(APIError, "bound to external delivery"):
            gateway.ensure()

    def test_headless_default_waits_for_browser_feedback(self) -> None:
        data_url, _ = image_data_url()
        with patch.object(mcp_server, "BASE_URL", self.base), patch.object(mcp_server, "PORT", self.server.server_port), patch.object(mcp_server, "DATA_DIR", self.data_dir), patch.object(mcp_server, "PROJECT_DIR", self.project), patch.dict(os.environ, {"DISPLAY": "", "WAYLAND_DISPLAY": ""}):
            async def exercise():
                pending = asyncio.create_task(mcp_server.request_visual_feedback(timeout_sec=3))
                for _ in range(100):
                    status, state = await asyncio.to_thread(self.request, "GET", "/api/workspace/state")
                    if status == 200 and state.get("request_feedback"):
                        break
                    await asyncio.sleep(0.01)
                else:
                    self.fail("MCP review request did not become ready")
                self.assertFalse(pending.done(), "headless review must keep the invoking tool call open")
                session_id = state["session_id"]
                status, submitted = await asyncio.to_thread(
                    self.request, "POST", f"/api/sessions/{session_id}/feedback",
                    {"idempotency_key": "headless-wait-0001", "scene_revision": state["scene_revision"],
                     "note": "Move this edge", "scene_original_data_url": data_url,
                     "scene_annotated_data_url": data_url}, browser=True,
                )
                self.assertEqual((status, submitted["delivery"]["status"]), (201, "awaiting_mcp"))
                return submitted, await asyncio.wait_for(pending, timeout=4)

            start = time.monotonic()
            submitted, result = asyncio.run(exercise())
            self.assertLess(time.monotonic() - start, 5)
            self.assertFalse(result.structured_content["browser_opened"])
            self.assertEqual(result.structured_content["items"][0]["feedback_id"], submitted["feedback_id"])
            self.assertEqual(sum(block.type == "image" for block in result.content), 2)
            status, state = self.request("GET", "/api/workspace/state")
            self.assertEqual((status, state["agent"]["status"]), (200, "external_idle"))

    def test_explicit_url_only_review_returns_immediately_in_headless_mode(self) -> None:
        with patch.object(mcp_server, "BASE_URL", self.base), patch.object(mcp_server, "PORT", self.server.server_port), patch.object(mcp_server, "DATA_DIR", self.data_dir), patch.object(mcp_server, "PROJECT_DIR", self.project), patch.dict(os.environ, {"DISPLAY": "", "WAYLAND_DISPLAY": ""}):
            start = time.monotonic()
            opened = asyncio.run(mcp_server.request_visual_feedback(wait_for_submit=False, timeout_sec=600))
            self.assertLess(time.monotonic() - start, 3)
            self.assertFalse(opened.structured_content["browser_opened"])
            self.assertIn("session_id=", opened.structured_content["url"])
            self.assertEqual(opened.structured_content["delivery_mode"], "external")
            self.assertIn("next_cursor", opened.structured_content)

    def test_bound_desktop_review_returns_url_without_an_mcp_wait(self) -> None:
        thread_id = "01a0d906-146e-7762-a1f9-49baeda8e270"
        self.server.scene_store.workspace_thread(thread_id)
        with patch.object(mcp_server, "BASE_URL", self.base), patch.object(mcp_server, "PORT", self.server.server_port), patch.object(mcp_server, "DATA_DIR", self.data_dir), patch.object(mcp_server, "PROJECT_DIR", self.project), patch.object(mcp_server, "_wait_for_external_feedback", side_effect=AssertionError("bound task must not wait for an MCP result")):
            opened = asyncio.run(mcp_server.request_visual_feedback(open_browser=False))
            self.assertEqual(opened.structured_content["thread_id"], thread_id)
            self.assertIn("no MCP wait is needed", opened.structured_content["message"])
            with self.assertRaisesRegex(ValueError, "delivers feedback directly"):
                asyncio.run(mcp_server.wait_visual_feedback(opened.structured_content["session_id"]))


if __name__ == "__main__":
    unittest.main()
