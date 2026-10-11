---
name: visual-feedback
description: Use Scene Feedback to review a reconstruction against reference images, enter manual visual debugging, or apply a submitted visual feedback event. Open the existing image and scene workbench, inspect original and annotated evidence, and publish the next scene result.
---

# Visual reconstruction feedback

Use the tools from this plugin's `scene_feedback` server. The connection is bound
to one project. Read `workspace_get_context` before making changes; do not assume
that a different open Codex task or scene is the target.

When the user asks to enter manual debugging (including “进入人工调试模式”),
call `request_visual_feedback` with the current project reference images and an
exported GLB preview when available. Preserve the editable reconstruction source.
The workbench lets the user draw on reference images and frozen scene screenshots.
Marks express user intent; they are not geometric constraints or ground truth.

In MCP Events mode, the request opens the workbench and returns immediately.
If this host supports subscriptions, subscribe to the project's submitted visual
feedback event using its advertised schema and the user's requested response.
Do not invent a callback URL or signing secret: the host supplies them. Local
Codex installations can use these tools without supporting native event-triggered
turns. Report an unavailable subscription mechanism accurately.

When a visual feedback event arrives, use its `feedback_id` to call
`workspace_get_feedback`. Read the original images, annotated images, scene
screenshots, selected objects, frame/view references, camera, and scene revision.
The event is a summary; the read tool provides the actual image evidence.
Follow the user's note in that evidence using the project's existing editing and
reconstruction tools. Keep old screenshot versions and camera metadata truthful.

After editing, export a self-contained GLB and call `workspace_publish_scene`
with the current revision. Preserve the editable source and the current dynamic
sequence when the change does not replace it. Re-read context if a revision
conflict occurs. Stop monitoring when the user requests it; avoid subscribing to
scene publication events that would repeatedly trigger your own updates.

For human reconstruction, use the separate `capsule-human-tracking` skill when
available. This plugin only exports reference-source snapshots, imports bound
2D pose evidence, and displays or cites those results. Use
`workspace_export_pose_sources` before running the external tracker and
`workspace_import_human_pose` to publish its result. The source snapshot binds
the project, session, view, frame, time and actual images. Preserve that binding;
do not rename a foreign result to fit the current workspace. Distinguish measured
or inferred 2D observations from projections of an existing 3D model.

When preparing a scene folder for workbench import, use this plugin's
`workbench-ready-check` skill before delivery. Folder import runs the same
validator automatically; inspect per-instance failures and warnings rather
than treating a ready marker or file existence as proof the package is valid.
