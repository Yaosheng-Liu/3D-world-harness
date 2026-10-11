**语言 / Languages:** [简体中文](README.md) · **English** · [日本語](README.ja.md) · [한국어](README.ko.md) · [Русский](README.ru.md)

[Changelog (Chinese)](CHANGELOG.md) · [Maintenance guide (Chinese)](CONTRIBUTING.md)

# Codex Visual Reconstruction Workbench

In the default mode, mark the reference image and the current 3D scene, write one prompt, and click “Send.” The workbench sends your text, original image, annotated image, and scene snapshot as **a user message in the same workbench-owned Codex thread**. Once Codex edits the project and publishes a GLB, the result appears on this page. You do not need to return to a terminal to trigger another read. An existing Codex desktop task can use the same review UI through the external MCP mode below.

![Reference and scene with a collapsible glass conversation dock](preview.png)

History and `本次反馈` now share the conversation header and expand independently with reversible height animations. The prompt starts compact. Drag the small grip above the prompt or evidence panel to adjust each height; arrow keys fine-tune it and a double-click restores the default. Heights and evidence visibility persist per scene, with viewport limits to keep Send reachable. The dock starts compact: 480px wide on desktop, expanding to 760px on hover, keyboard focus, or touch tap to enlarge only the input area and show existing evidence; phones keep a stable width of viewport minus 20px. Open history manually with `展开记录`; after automatic shrinking, history stays closed the next time the dock expands. Evidence and details fold temporarily while size, position, and drafts remain intact. After the pointer leaves and actual typing stops, the dock shrinks after a short delay and closes `@`, `/`, and attachment menus. Lingering focus, a completed text selection, or an idle menu does not prevent shrinking. Active typing including IME composition, keyboard `Tab` navigation, touch editing, actual dragging, and pending answers keep it expanded. Unread counts, inserted references, input, and Send remain available; reduced motion disables the animation.

Drag blank space in the conversation header to move the dock; buttons and status text keep their actions. Focus the header to move with arrow keys, or hold `Shift` for larger steps; `Home` or a double-click on blank space resets only the position. Positions persist separately for each workspace session and comparison/immersive layout. Custom positions may extend partway beyond the sides or bottom, with only a little overflow at the top; the header stays reachable so you can drag the dock back. Reloading, expanding or shrinking the dock, and reducing the viewport retain the saved position preference while keeping the header accessible.

The scene toolbar’s `清单` opens objects and marks; `截图` captures a new view, and gallery cards reopen saved screenshots. Reference and scene tools are independent, so drawing on a reference does not block live 3D rotation. `清除本图` clears only marks on the active image after confirmation, keeping its screenshot; the global clear is in `侧栏 → 清空本轮图片与标记`. Both show their scope and counts and support undo. Expand `本次反馈` in the conversation header to inspect saved evidence. The model receives images related to this round’s citations, actual marks, and needed current views; uncited marks are still included. Unmarked annotation copies without selection highlights and identical images are omitted. Saved chat messages reopen the original, annotated, and overlay images even after reload. Video names and frame numbers appear above the reference; playback, seeking, and frame stepping stay below it. When a GLB has multiple animation actions, their selector appears directly below the timeline; it stays hidden for a single action. Type `/` in the prompt to select timestamp references.

The gallery starts with a `3D` card for the live scene, followed by still screenshots and saved video moments in creation order. Cards start with 112×63 scene screenshot covers in a 16:9 ratio and smoothly expand in place to 160×90 on mouse hover or visible keyboard focus, respecting reduced motion settings. Touchscreens keep the compact size and support direct taps. Cards show stable names such as `截图1` and `截图2`. Timed screenshots show only one timestamp with millisecond precision, such as `00:09.900`, without a time interval. Deleted numbers are never reused. Hovering or focusing a card does not open a floating preview popup. Timeline ticks navigate saved moments in the current view; click a tick to open a chooser when several camera angles share that time. Opening a video card pauses and restores its view/time. Subsequent playback or seeking keeps the right screenshot and marks fixed until you choose the first `3D` card or another saved card. Marking a different reference frame preserves its own evidence without replacing the displayed screenshot. Still and dynamic collections retain their separate eight-item limits.

## Plugin and MCP Events (experimental)

Install the workbench as a local Codex plugin, or connect an MCP 2.0 host through event subscriptions and visual evidence read tools. Installing a local plugin does not guarantee native event wake-up of an existing Codex task; verify host subscriptions and callbacks before switching delivery. See [Plugin and MCP Events setup](docs/mcp-events-plugin.md) (Chinese) for project binding, ZIP packaging, and registered hosted connections.

## How it works

```text
Browser: reference image + 3D scene + annotations + text
                      │ User clicks “Send”
                      ▼
              Local Workspace Gateway
                      │ Real image inputs and execution events
                      ▼
        Codex App Server: same project, same thread
                      │ Edit source files, export GLB, call MCP tools
                      ▼
              Workbench refreshes the scene
```

In the default mode, the Gateway keeps one persistent Codex thread for this project. It starts `codex app-server` over stdio and sends images as `localImage` input items. This mode does not inject messages into other Codex desktop or terminal sessions you may have open. MCP is used to read context, request a user review, and publish the scene; **the web page's Send button starts a user turn directly**.

This interface helps people point out problems. It does not define a reconstruction algorithm or ask people to enter coordinates or geometric constraints. Codex can use the modeling, reconstruction, and editing tools already available in the project.

## Page layout and navigation

The warm off-white and dark themes retain the glass conversation dock. A single upper-left button opens the workspace sidebar for scenes, creation, comparison/immersive layout, theme, tasks, activity and help. Detail pages stay inside the same drawer with a back button; reasoning effort and permissions are under advanced creation settings. Layout and theme changes keep the drawer open; outside click or Esc closes it while preserving forms and scroll positions. The drawer overlays the scene without resizing the canvas or changing its camera. Removing the header row gives both panes more vertical room. Running and attention indicators appear only in the conversation, with Stop beside its status.

| Control | Purpose |
| --- | --- |
| Upper-left sidebar button | Switch independent scenes; CLI mode or a connected Codex Desktop can create a scene and reconstruction task |
| Folder icon beside Scenes | `打开文件夹` browses service-host folders and imports ready instances without switching the current scene or creating a task |
| `卸载` on a scene row | Confirm to remove it from the list and unload its workbench; files, feedback and Codex tasks remain. The default scene is protected |
| `侧栏 → 任务` | With Codex Desktop connected, switch the current scene’s recipient or create a task with a name, model, reasoning effort, and permissions |
| `侧栏 → 执行记录` | Execution and feedback queue; approvals, retries, and delivery checks also appear above the conversation prompt |
| Conversation dock | History appears above the prompt. `收起记录` hides history; `收起会话` hides the whole dock, and `展开会话` brings it back |
| Empty reference area and image | With no reference loaded, use `选择图片` to add static images; use the mouse wheel to zoom the reference image, and hover or tap the upper area to open annotation tools |
| Scene header and `对齐` | `截图` saves the current view; opacity is shown directly, with clear, high contrast controls for ground orientation and `对齐`; alignment sits between the two views in comparison mode and above the scene in immersive mode |
| Adaptive annotation dock | Select, point, box, line, arrow, eraser, undo/redo, and clear; the left reference also offers `智能轮廓` (Intelligent contour). `更多` holds text, freehand, and correspondence numbers. Snapshot name labels are toggled here |
| Scene header’s `物体 / 部件` switch | Select item or part directly, with a clear active state; hidden for screenshots and restored in live 3D |
| `物体与标记` icon at the scene’s upper-right edge | Object and mark references, with a mark count. `引用` inserts at the prompt cursor, closes the panel, and returns focus to the prompt |
| Visible opacity slider | Starts at 45%; 0% hides the reference overlay. Disabled when unavailable; use `对齐` when camera metadata is available |
| Reference thumbnails | Click an image to switch dynamic views while keeping the current time on the shared timeline |
| Left image toolbar’s `关键点` | Edit the latest available human evidence for the original frame; the right-side `关键点修改` section lists only current edits |
| Timeline | Playback, seeking, and frame stepping; GLBs with multiple animation actions show a selector directly below the timeline |
| `/` in the prompt or timeline time | Cite moments, the marked span, or the whole clip; double-click the time to cite the current clock with a short timestamp reference |

Pending decisions stay visible when history is collapsed. A collapsed conversation shows `! 待确认` instead of the spinner; opening it focuses the pending card. Forms survive status refreshes and failed responses, and resolved approvals appear in chat history.

Use `沉浸` in the sidebar to fill the app with the scene; `对照` restores the split workspace. In immersive mode, the upper left keeps only the sidebar launcher; scene actions float at the upper right and `参考` opens the reference window on the left. Each image’s annotation dock adapts to the blank space around its visible content: a horizontal row above or a vertical column on the right. If both fit, it chooses the roomier side; with no suitable blank space, it overlays the top. Placement updates with window size, zoom, pan, split/immersive layout and screenshot changes, and stays steady during drawing, panning or an open tool menu. Hover the upper area, right-side blank space or toolbar to open it; the top of the image remains an entry point. Touch taps on the entry point and visible keyboard focus also open it. Moving into the tools or More menu keeps it open; leaving the entry area and tools closes it after a short delay while preserving the selected tool and ongoing drawing, panning or keypoint dragging. Opening the tools, choosing a tool or opening the reference window alone does not capture a screenshot. Camera controls and chat float above the scene. Reference-video playback stays inside the reference panel and hides with that panel; model-only animation retains its separate timeline. The moon/sun button switches appearance; the initial theme follows the system and explicit choices and layout persist in this browser. Scene materials, camera pose, marks and saved images stay intact. Transitions respect reduced motion.

The 3D/screenshot preview strip sits in a separate area below the right image in comparison mode and floats at the bottom of the scene view in immersive mode. Scroll it horizontally when space is limited. When a card expands in comparison mode, both canvases keep matching heights and aligned top and bottom edges; the conversation dock makes room for the strip. In immersive mode, the rightmost arrow folds the scene toolbar; click it again in the same place to restore the tools. This browser remembers the choice; comparison mode always shows the toolbar.

In immersive mode, drag the reference window’s title bar to move it, or drag an edge or corner to resize it. The reset icon beside the title restores its default position and size. Keyboard users can focus the title bar and use arrow keys to move, or focus the lower-right resize handle and use arrow keys to resize. Geometry is saved in this browser and does not change the split comparison layout.

In 3D, left-drag rotates, right-drag pans in screen space, and the wheel zooms toward the cursor. Double-click an item or part in live 3D to insert a numbered reference to the exact target at the current selection level. Object-list rows and the selected name also support double-click citation. Single clicks still select, and orbiting or dragging does not insert a reference. Select a target and press `F` to focus and orbit it; typing never triggers this shortcut. The camera pose persists with the draft; saved images and marks remain unchanged. Ground orientation defaults to GLB Y-up, with Z-up compatibility for the old room demo and primitives. Scene/asset extras can declare `up_axis: "y"` or `"z"`; the visible ground orientation selector offers a per-session Y/Z override. Only the viewer camera, grid and lighting change; model coordinates stay intact.

Frozen screenshots on the right support cursor-centered wheel zoom from 0.1× to 8× (minimum 10%). Drag empty space in Select mode, hold Space and left-drag, or middle/right-drag to pan. Each screenshot retains its own viewing position across switching and draft reloads. The image, overlay and marks move together; the fixed camera, original pixels and normalized mark coordinates remain unchanged, and viewing zoom is not sent. Live 3D keeps its existing wheel controls.

Collapsing the dock keeps the current unsent draft, marks, camera, and frozen evidence intact, so you can keep selecting and annotating. Inserting an object, mark, or human-pose reference opens the prompt automatically. With history expanded, drag the thin handle along the top edge to resize the dock; pulling upward shows more history. Double-click it to restore the default size, or focus it and use the up/down arrow keys. Collapsing history hides the resize handle and keeps the prompt compact; explicitly expanding history restores resizing and the previous height. The custom height is saved per workspace session in this browser and restored on reload; smaller windows clamp the height to fit. New messages show an unread count without opening the dock. Reading older messages preserves your scroll position; use the latest-message button to catch up. The dock replays recent persisted workspace messages, displaying up to 100 entries. In unbound external MCP mode, later model replies still appear in the original Codex task.

The prompt fills the dock width, with a circular up-arrow send button in its lower-right corner. Its tooltip and accessible name show `发送反馈` (send feedback), or `加入下一轮` (queue for the next turn) while the task is running. You can also send with `Ctrl/Cmd + Enter`; confirming an IME composition does not submit. When viewing an older scene snapshot, a button such as `标注 v13 · 查看最新 v15 ↗` appears below the scene toolbar; click it to view the latest live scene. The current round’s unsent snapshot and marks remain available. Once the server confirms that it saved feedback, they are cleared from the editor; the submitted packet retains the complete evidence.

## What you can do on the page

- Type `@` in the prompt to search only the currently selected item or part and this round’s unsent marks and keypoint edits by name or number. Marks on other snapshots, views, and frames are included even when their associated object is not selected; select an item or part in the scene first to cite it. Use `↑`/`↓` to choose, `Enter` or click to insert, and `Esc` to close; a successful save clears this round’s mark and keypoint-edit candidates. Selections appear as short references, and one prompt can combine multiple types. Choosing a reference image freezes its original, existing marks, and exact source view and frame. Choosing a rendered image freezes its original, existing marks, camera, and scene revision; dynamic scenes also record the view and frame. Switching views, seeking, or orbiting later cannot move a selected image.
- Type `/` in the prompt to cite the current reference frame, frozen screenshots, this round’s timed marks, or saved moments; several moments, the marked span, or the whole clip can be cited. The prompt shows short references such as `🕒1` and `🕒2`. Click a source card for exact times, views, reference frames, and differing sample times; sending restores the complete precise description. Double-click the current timeline time, or focus it and press `Enter` or Space, to cite the actual current clock. Updates still in progress do not insert an old frame; static scenes have no time entry. Citing the same source and moment reuses its number, different times have separate numbers, and draft reloads retain the original binding. Use `↑`/`↓`, `Enter` or click, and `Esc`; IME composition works normally. Finer source precision is retained, without setting an editing scope or changing the view or screenshots. Double-click the progress thumb to cite its current time, the reference frame counter to cite that sampled frame, or a saved screenshot’s time to cite its fixed moment without navigating. A pending thumb citation waits for that frame to load and is canceled by a later seek or source change. Double-click time citations show no notification for success or waiting; actual failures still show a message.
- The native text input and chat history show an icon plus a number for each reference: object `🧊1`, part `🧩1`, mark `📍1`, image `🖼️1`, human evidence `🧍1`, correction `✏️1`, time `🕒1`, and scene reference `🏞️1`. Long filenames and internal IDs stay in source details. Each type has independent stable numbers. Reusing a source keeps its number; existing drafts migrate automatically while retaining their exact source bindings. Rounded source cards inside the composer provide full names, source details, and removal. Deleting a short reference removes the whole reference, while native text editing and undo remain available. Chat history displays the same icons; archived data still uses protocol tokens such as `[[image:…]]` and `[[object:…]]` to bind exact sources. Original images, marks, camera, frame time, scene revision, and sent content remain intact. Click a source card or inserted reference icon in the prompt to open a small nearby popover; image previews show the original or annotated image, and source labels above the prompt remain clickable. Hover alone does not open it; leaving both the reference and popover closes it automatically. There is no modal backdrop or background dimming, and input focus stays in place. Touch and keyboard users can close it with the close button, `Esc`, or a click outside. Native drag selection, IME composition, and undo remain available. Invalid mark references retain their icon, number and original name; source cards label them deleted or invalid. Sending lists the affected references and locates them in the prompt, so you can remove the reference or undo the mark deletion.
- Use the reference button on a sidebar scene row to insert another loaded, available scene from the same workbench as `🏞️1`. Its frozen version, geometry copies and available model preview/first GT image remain usable after the source changes or unloads. The source is read-only; your current scene and task binding stay intact. The model receives the name, version, archive path and preview/GT, with geometry read on demand. Model previews are generated in the background; confirmed unchanged snapshots can reuse their preview across rounds and reloads. If preview generation fails, available GT can serve as the reference; without either, the reference is not added. Up to four scenes per round, with GLB copy budgets of 200 MiB per scene and 400 MiB per feedback. This stays within one workbench service and leaves `@` candidates unchanged. See [scene references](docs/folder-import.md#引用其他场景作为参考).
- The model receives a compact summary retaining your original text, IDs, exact source times, views, `frame_index`, and revisions; source metadata is explained once. Keypoint summaries list only changed joints with their before/after values. MCP text and `structuredContent` also default to the summary, retaining the feedback ID and full archive paths. Full keypoints, camera matrices, and earlier evidence remain in `state.json` and the original correction archives. Use `workspace_get_feedback(feedback_id, include_details=True)` for full fields, and archive paths for other original images; this option does not expand the image selection.
- Switch between reference images and pan or zoom them on the left; rotate, zoom, and select nodes in a GLB scene on the right.
- Use the scene header’s `物体 / 部件` switch to choose item or part selection directly. Item selection picks a top-level GLB scene node (one whole physical item in the current asset); part selection picks the detailed named node you click. Primitive scene objects are always selected as a whole. Either level can be referenced in the same prompt. Item-level references display as `🧊物体` (object), and part-level references as `🧩部件` (part). Exact node tokens stay intact, and both reference types for the same node can coexist. A red outline follows the selected target’s shape: item mode outlines the whole selected group, part mode only the selected target, and the outline follows view and animation changes.
- Draw points, rectangles, lines, arrows, freehand strokes, and text on either side. Related marks can share a number; a missing object can be marked only on the reference image.
- On the left reference image, `智能轮廓` (Intelligent contour) follows image edges between clicked anchors and previews as you move. Click the first anchor to close, or press Enter/double-click to finish an open path; Backspace removes the last anchor and Esc cancels. Switching image, frame, tool or session drops the unfinished path. Completed contours become ordinary freehand marks with undo, erasing, prompt references and feedback; zooming and panning leave their image coordinates intact. Add anchors where edges are weak.
- Use `橡皮擦` (Eraser, shortcut `8`) to click or sweep across visible marks. It removes each whole mark touched, commits on release, and groups one gesture into one undo step. Escape or pointer cancellation discards the preview. Points, rectangle edges, lines, arrows, text and freehand strokes are supported; empty rectangle interiors are not erased. Only marks visible on the current reference image or selected snapshot/frame are affected. Source images and other snapshots remain intact; erasing live 3D does not create a screenshot. Narrow screens show an eraser icon.
- The eraser has a fixed size; the cursor matches its reach and touched marks are still removed whole. Drag the center divider to resize the reference and scene; double-click or press Enter to reset, or use arrow keys to adjust. The split ratio persists per session. Narrow screens stack the panes and hide the divider.
- The selection arrow selects marks on the current snapshot; use the gallery’s first `3D` card to return to live 3D. Backspace / Delete removes only the selected mark and supports undo. Drag a mark into the conversation to quote it; a collapsed conversation opens automatically without moving the mark. Use `隐藏名称 / 显示名称` in the annotation dock to hide/show snapshot name labels; the preference survives reload. Strokes, text annotations, references and names in submitted feedback stay intact. Stable names such as 点1, 点2 and 方框1 continue across this scene’s snapshots and survive deletion, undo and reload. The `物体与标记` icon sits at the scene’s upper-right edge and shows the mark count. The collapsed conversation shows a spinner while Codex works or a message is being sent; opening and closing animate smoothly and respect reduced motion. In selection-arrow mode, double-click a visible mark on either image to cite it directly. Double-click a mark-list entry to cite it; a mouse single-click waits briefly before showing its source, while keyboard activation shows it immediately.
- Use the annotation dock’s `撤销 / 重做` (undo / redo) to reverse or restore adding, deleting, or clearing marks. Shortcuts are `Ctrl/Cmd + Z` and `Ctrl/Cmd + Shift + Z`. `侧栏 → 清空本轮图片与标记` (clear) asks for confirmation, then removes all marks, fixed snapshots, and saved dynamic moments, returns to the live 3D scene, and can be undone; it keeps the prompt and reference images. The prompt field keeps native text undo. Annotation history starts fresh after a reload or a successfully saved submission. Undo does not restore a submitted round’s marks into the next draft.
- Use `＋` at the lower left of the composer to reference the current image/video frame, current scene/snapshot, or a saved screenshot. Saved screenshots retain their camera, marks and overlay without changing the active view. Thumbnail previews and removal are inside the composer. Orbit and pan instructions are available in the sidebar Help page, without a permanent overlay on the scene. Object rows, the selected item or part, and mark entries can also be dragged in. Images use short references such as `🖼️1`, with up to eight per round. Each image retains its original and existing marks. A reference image records its exact source view and frame; a rendered image records its camera and scene revision, plus the view and frame time in a dynamic scene. Orbiting, seeking, or publishing a new scene does not change that evidence. Only images cited in the prompt are included as independent evidence; reload restores the draft, a confirmed save clears it, and failures retain it for retry.
- Write one freeform prompt to Codex. Put the cursor in your text and click “引用” (“Insert reference”) beside an object, a selected GLB node, or a visual mark to insert multiple references into that same prompt. They appear as short names such as `🧊1` (cabinet), `🧩1` (table leg), and `📍1` (point 1). For example, ask Codex to move a cabinet toward a marked spot and align its top with a marked line, then send the prompt once. You can mark and reference something that has not been modeled yet on the reference image; the scene still highlights only one selected object at a time. In the `物体与标记` (objects and marks) panel, click `引用全部标记` (reference all marks) to insert all missing mark references at the caret, including saved dynamic moments. Existing references are not repeated; the panel closes so you can continue writing.
- All drawing tools are available in live 3D. Your first actual drawing click or drag automatically saves **the screenshot, camera, selected object, and scene revision at that moment**, then completes the mark on that screenshot. Opening the tool panel or choosing a tool does not create an empty screenshot; later strokes stay on the same screenshot without adding one per stroke. Select mode still rotates live 3D and selects objects or parts. The gallery highlights the current card. Open a saved card to restore its original image and marks; reload preserves the selection. Choose the first `3D` card, adjust the live view, and draw again to capture a new screenshot while preserving earlier images and marks. Later strokes on a frozen screenshot stay on that image. Returning to live 3D and starting to draw again saves a new screenshot, even if the camera has not moved; earlier images and marks remain. Use `截图` to explicitly add the current view, up to eight static screenshots. Click a thumbnail to continue annotating that exact view. Deleting one screenshot removes only its own marks and can be undone. Dynamic scenes keep snapshots for each saved moment. Rotating the live 3D view will not move old marks onto other objects, and a newly published scene will not overwrite a snapshot being annotated.
- The selected part’s name, reference button and deselect action live under `物体与标记 → 当前选择` (“Objects and marks → Current selection”), freeing the space above the prompt.
- Inserting a mark reference or choosing `选择` (“Select”) stays on the current snapshot; click the first `3D` gallery card to return to live 3D selection. The current round’s unsent frozen screenshot and existing marks remain; click its saved card to return to that view and continue marking it. Scene arrows and other marks stay attached to their original screenshot. Rotating the live view or refreshing the page does not overwrite it: sending includes the marked screenshot and its original camera, rather than placing those arrows over the newly rotated view.
- Submission archives every saved static screenshot with its original and annotated pixels, camera, revision, and marks; the model receives only relevant images for this round. Successful submission clears the saved cards for the next round and keeps the `3D` card. The `scene_snapshots` evidence array is separate from the video timeline; legacy single-screenshot packets remain supported.
- Sending saves an immutable feedback packet: your exact words, original and annotated reference images, clean and annotated scene screenshots, selected nodes, camera, and scene revision. Marks are your hints; the original image is kept separately.
- Once the server confirms that it saved feedback over HTTP, it automatically clears the prompt, all marks, fixed snapshots, saved dynamic moments, object/node selection, and this round's references. The view returns to live 3D and an empty draft is saved for the next round, even if delivery to Codex is queued or uncertain. Submitted feedback evidence, reference images, and the current animation time remain; when a new scene result arrives, any draft already started for the next round remains. If the HTTP save fails or is unconfirmed, the local draft and outbox entry remain for correction and retry.
- New feedback submitted while Codex is running joins a queue for the next turn. Saved feedback is delivered automatically even if the scene changes while it waits. The model receives both the original snapshot revision and the current revision at delivery, so it can interpret your prompt against the older evidence. Even if the snapshot is already old when you send, clicking Send submits it directly without another confirmation; the model still receives its actual revision. Approval requests and stop actions are also handled on the page.
- Refreshing restores your project, thread, and the current unsent draft and original snapshots; it does not turn an old screenshot into a new revision or restore a successfully saved round to the editor. “View latest” switches to the live scene while retaining unsent evidence. To annotate the current result, return to `3D`, capture a new view, and keep the older snapshots. `侧栏 → 清空本轮图片与标记` removes all snapshots and marks only after confirmation; an accidental clear can be undone. Snapshot and saved feedback revisions are never rewritten.

Scene input currently uses a self-contained `.glb` file. Publishing requires geometry and texture resources to be embedded in the GLB's BIN chunk. Both external URIs and data URIs are rejected: even though a data URI can be self-contained, this version accepts only BIN-embedded resources. You can submit a reference image without an initial scene.

## Switch scenes and start a new reconstruction task

Click the upper-left sidebar button to open the scene list. Return to an existing scene, or enter a name and choose a model, reasoning effort, and permissions to create **a new scene and an independent Codex conversation**. Import reference images or video, then send your reconstruction instructions from the prompt box. An initial GLB is optional.

The folder icon `打开文件夹` beside Scenes browses directories on the **service host** or accepts an absolute path; LAN clients see the service host’s folders too. `打开并导入` recursively imports ready instances within the scan limits and adds them to the list without switching your current editing scene or creating a Codex task. Repeated sources and request retries reuse existing scenes; an individual failure does not block other instances. Missing files, invalid JSON, ambiguous GLBs, and scan truncation show reasons. Import reads the original GLB, `.blend`, and references without modifying them, while data and feedback are saved independently. See the [folder import specification](docs/folder-import.md) for markers, compatible layouts, paths, and scan limits. Existing video-ready packages using `status: "ready"`, `scene_glb`, and `reference_sequence` are supported without rewriting the marker or adding `schema_version`. A parent `manifest.json` can declare instances in a `scenes` list; GLB/Blender-only entries can be imported, with a notice when reference images are absent.

`打开并导入` automatically checks models, reference images, views, frame times and cameras: invalid instances report errors while valid ones continue importing; missing references or cameras show a manual-comparison warning. Agents can use `$workbench-ready-check` before handoff, or run `.venv/bin/python scripts/check_workbench_ready.py /absolute/path/to/ready-scenes --json` independently for a read-only report. Independent checks create no workbench data or model task. See [scope and exit codes](docs/folder-import.md#导入前检查-ready).

Scenes imported into a blank/Events service initially use event subscriptions and create no thread. Select a scene and open `任务` to explicitly connect a same-user Codex Desktop task on the service host or create a conversation for that scene; discovery uses the single local Desktop connection without a seed binding. Future feedback then goes directly to that conversation. Images, cameras, marks and drafts remain, and the binding survives restart and unload/reimport. Old events are not replayed or forwarded; other scenes stay unchanged. The blank homepage is only an entry; Events services still disable the new-scene page. See [scene connection details](docs/folder-import.md#为导入场景连接或新建对话).

Imported and newly created scenes, including unavailable ones, can be unloaded with `卸载` on their list row and an inline confirmation. The default scene is protected. Unloading the current scene saves its prompt draft and returns to the default scene. The unloaded state survives service restarts; source files, workbench data, feedback and Codex tasks remain, with no file deletion or task cancellation. Running work, unresolved delivery checks or an operation in progress block unloading with a specific reason. Reimporting the same source restores the original scene, session and edited result instead of replacing it with the initial model or creating a new task.

Each scene keeps its own references, cameras, dynamic views, reconstruction, feedback history, and receiving task. Switching saves the current draft; returning restores it. Separate tabs can view different scenes while previous tasks and queued feedback stay with their original scene. Finish an in-progress submission or import before switching.

New working directories live under `projects/<project_id>/workspace` in the service data directory, using the same LAN address and port. Each new task gets MCP configured for its own directory; existing MCP clients keep their original scene. If task creation is uncertain or binding fails, the scene stays in the list for recovery. Repeating the same creation request does not create another task.

The default CLI (App Server) mode supports scene creation without Desktop. Model, reasoning effort, permissions, and the independent conversation persist across restarts. `--external-review --shared-thread-id` can bind a known Desktop task at startup; imported scenes in blank/Events services can also discover local Desktop through `任务` and explicitly connect. The browser can use configured LAN access or SSH forwarding. The task panel connects or creates conversations **within the current scene** through local Desktop.

## Align to a reference camera

Selecting a reference with camera metadata automatically moves the 3D view to its capture pose. The opacity slider stays in the scene header; `对齐` sits between the two views in comparison mode, including between stacked views on narrow screens, and above the scene in immersive mode. Opacity starts at 45%; 0% hides the reference overlay, and the slider is disabled when unavailable. Live-view preferences are saved per workspace session. Capturing or marking freezes the reference image and its placement with that screenshot. Each screenshot has independent opacity controls (0% hides the overlay), with marks above the overlay; switching references or reloading preserves its evidence. Feedback includes an annotated comparison composite, the clean scene, original and actual overlay reference images, and reference ID, opacity, visibility, normalized placement, alignment and camera metadata in both Codex and MCP delivery. Disabled overlays keep metadata without exporting a visible-overlay composite. Older screenshots without this metadata require a new capture to compare safely. After orbiting manually, click the visible `对齐` (“Align”) button to return to that camera. When an undistorted copy is supplied, the overlay uses it to match the pinhole camera projection; the original reference remains separate for viewing and feedback. References without calibration can still be compared manually. Approximate intrinsics can leave residual alignment error near the image edges.

Each reference camera stores `camera_to_world` (a row-major 4×4 matrix in the GLB world) and pixel-based `intrinsics` (`width`, `height`, `fx`, `fy`, `cx`, `cy`). Attach `camera` and optional `alignment_image_data_url` to an existing image by its `reference_id` through the protected `POST /api/workspace/reference-cameras` endpoint. A `reference_cameras.json` manifest in the private workbench data directory matches camera metadata by filename prefix on future imports ([example](examples/reference_cameras.example.json)); send `{"apply_manifest":true}` to the same endpoint to apply it to existing images. Camera poses and the published GLB must use the same world coordinates.

## Dynamic scenes: multiple views on a shared timeline

Multiple reference views of one clip, each an image sequence or video, can share a timeline with an animated GLB. Play, pause, seek, and stepping to the previous or next sampled frame update both the reference and the 3D animation. The existing static-image and static-GLB workflow remains available. GLB animation supports node translation, rotation, scale, skinning, and morph targets with fixed topology. Variable-topology mesh caches, fluids, and live physics simulation are not integrated yet.

- Switching reference views keeps the current time and automatically aligns to the new view’s current-frame camera; views without camera metadata can be compared manually. Use the same time origin for each view’s `time_sec`. One submission can reference marks from multiple views; each mark retains its view name, `view_id`, frame/time, original image, and camera.
- For multi-view clips, click a reference thumbnail to switch dynamic views while keeping the current time; seeking continues in the selected view. Existing thumbnails whose camera calibration uniquely matches a dynamic view switch directly to it; other dynamic views have a first-frame thumbnail. Static thumbnails with no unique match only open their original static reference.
- Inspect static thumbnails while paused; playing, seeking, or stepping to the previous or next frame automatically returns to the dynamic reference sequence. A newly published sequence resumes synchronization; saved marks keep their original frames.
- Starting a mark pauses playback and records its time, reference frame, camera, and scene revision. Keep up to 8 saved moments in total across all views and return to a moment to review or annotate it.
- One prompt can reference marks, objects, and timestamp references from several views and moments. The separate scope setting has been removed. New feedback records evidence times; your prompt determines which part to change, with no default 0–1 second interval. Old feedback and explicit ranges sent by older clients remain readable without rewriting historical records. Drawn lines remain hints rather than automatic motion paths or geometric constraints.
- Single and bulk citations of timed marks show the short mark name. Open its source card to see the view, frame number, and time; structured feedback carries `view_id`. The ordinal starts at 1 within that view’s imported or sampled sequence; saved marks keep their own frame and time as the timeline moves. The model receives a zero-based `frame_index` derived from the saved reference ID and that view’s sequence order. Static-reference or GLB-only marks show only the time.
- Marks appear on their own view and frame. The feedback archive retains original and annotated references, scene snapshots, timestamps, cameras, object references, and scene revisions for the selected moments. The whole video is not sent frame by frame to the model.
- Each reference frame can carry its own camera pose, used for alignment when that frame is selected. Publishing a new GLB preserves the playback position; seeking does not increase `scene_revision`.
- Preserve `semantic_id` or `stable_id` in node `extras` when exporting a GLB to help identify the same object across frames and exports. A node path locates a node only within its scene revision. Names and paths are not guaranteed to survive a new export; this does not automatically track objects.

The reference header’s `导入` menu has been removed. With no reference loaded, use `选择图片` to add static images. Videos and frame sequences still use the MCP tool `workspace_set_reference_clip`; each video or sequence forms a view, with videos sampled on the server and sequences using their specified FPS. Video import requires executable `ffmpeg` and `ffprobe` on the service host; image sequences do not. Video is sampled on a common time grid at 10 FPS by default. There are at most 8 views and 600 frames per view. Imports exceeding the limits fail; lower the sampling rate or trim the clip first.

Codex imports project-local material with `workspace_set_reference_clip(manifest_path=None, video_path=None, fps=None, camera_manifest_path=None, clear=False, append_view=False, view_name=None)`. Supply either `manifest_path` or `video_path`. Use `append_view=True` to append views and preserve existing material and marks; `view_name` can name the new view. The default `append_view=False` preserves the old API’s replacement behavior and replaces the entire existing view group. Submit feedback that depends on old frames before replacing them. `clear=True` removes the entire reference view group without changing the scene revision.

Image sequences use the manifest’s own `fps` (30 when omitted); the tool’s `fps` parameter applies only to video (default 10). A frame can supply `time_sec`; otherwise its zero-based index divided by the manifest FPS is used. Frame rate is not inferred from filenames. FPS must be between 0.1 and 120. Size limits apply separately to each view: 40 MiB for a browser video file or an image sequence’s total image data; 250 MiB for a project-local video file, its total decoded frames, or an image sequence’s total image data. Every input file must remain inside the project.

An existing single clip continues as the primary view with its original `clip_id`, frames, and marks. The top-level `reference_clip` fields still describe the primary view; its `views` array contains only additional views. Appending keeps the primary `clip_id` stable, and each view’s own `clip_id` serves as its `view_id`. This feature organizes viewing and feedback across cameras; the same Codex task performs geometry changes.

Example image-sequence manifest (all input files must be inside the current project):

```json
{
  "name": "assembly-review",
  "fps": 10,
  "frames": [
    {"path": "/absolute/path/to/project/frames/0000.png", "time_sec": 0.0},
    {"path": "/absolute/path/to/project/frames/0001.png", "time_sec": 0.1}
  ]
}
```

See [reference_multiview.example.json](examples/reference_multiview.example.json) for a batch manifest. It may have a top-level `name`; each entry in `views` may specify `name`, `fps`, and `duration_sec`, and exactly one source: `frames`, `manifest_path`, or `video_path`. A frame supports `path` and optional `time_sec`, `name`, and `camera`; a video may supply `camera_manifest_path`. Relative paths use the outer manifest directory, while a nested manifest’s frame paths use its own directory. The first entry becomes the primary view; `append_view=True` appends the whole batch to an existing group. Replace the example camera values with actual calibration.

To preserve camera alignment, import through a manifest / MCP: add the `camera` schema defined above to each sequence frame. Camera poses are not inferred from image or video files alone. A video can supply a separate `camera_manifest_path`. Example fixed-camera manifest (replace these example values with actual calibration):

```json
{
  "camera": {
    "camera_to_world": [[1,0,0,0],[0,1,0,0],[0,0,1,2],[0,0,0,1]],
    "intrinsics": {"width":1920,"height":1080,"fx":1200,"fy":1200,"cx":960,"cy":540}
  }
}
```

For a moving camera, use `{"frames":[{"time_sec":0.0,"camera":…},…]}` covering the sampled video times. This version matches nearby samples; it does not interpolate or estimate cameras. Video camera intrinsics are rescaled to the sampled images. Image-sequence intrinsics must already match each image’s dimensions.

Import the manifest, or sample a video at the chosen FPS:

```text
workspace_set_reference_clip(manifest_path="/absolute/path/to/project/clip.json")
workspace_set_reference_clip(manifest_path="/absolute/path/to/project/reference_multiview.json", append_view=True)
workspace_set_reference_clip(video_path="/absolute/path/to/project/camera_B.mp4", fps=10, camera_manifest_path="/absolute/path/to/project/video-cameras.json", append_view=True, view_name="camera_B")
```

## Human results: generated by the capsule skill, reviewed in the workbench

The separate [capsule-human-tracking skill](external-skills/capsule-human-tracking/SKILL.md) runs human tracking and reconstruction. Existing 2D evidence appears on its original reference frame, with keypoint editing available from the left image toolbar. The workbench plugin handles editing and evidence exchange without running ViTPose or installing inference packages or checkpoints.

A reconstruction task calls `workspace_export_pose_sources` to snapshot this project's reference frames, uses the skill to process actual images or validate existing 2D tracks, then returns the result with `workspace_import_human_pose`. The snapshot binds the project, session, view, frame, time and image; results from another project or replaced references are rejected. A geometry revision alone does not invalidate unchanged reference images.

Camera and timeline changes display the matching source-frame skeleton. Results can declare their joint names and edges; previously completed COCO17 results remain readable. Source details distinguish observations, inferred estimates and projections of an existing 3D model. Projections are not measured 2D tracks.

The reference overlay and newly exported skeleton evidence images show only skeletons and keypoints, with no detection boxes or text labels; source and identity information remains in prompt references and structured evidence.

The reference image shows the latest available 2D result for its original frame, or the latest 3D projection when no 2D result exists. Use `关键点` (Keypoints) in the left image’s hover toolbar to edit that frame. While the current frame loads, no old-frame skeleton is shown.

Editing automatically creates a current-round card such as `关键点修改1` and inserts its reference into the prompt; further edits to the same source frame accumulate in that card. `物体与标记 → 关键点修改` lists only current drafts, replacing the human-result batches. Composer cards show the source view, frame, and edited joints. Use `@` to cite an edit, reopen its original view/frame, undo or redo, or remove the whole edit.

The standalone observer now defaults to **COCO-WholeBody133**: 17 body, 6 foot, 68 face and 21 landmarks per hand, using a verified 133-output ViTPose checkpoint. Existing COCO17 results remain readable; regenerate them to obtain hand observations.

Click `关键点` in the left image toolbar to drag any declared body, foot, face, or hand keypoint. Region filters offer all points and only the regions present in the result. To locate a missing point, select its joint and click the original image. Visible, occluded, and missing remain distinct; `恢复原点` restores one point. Edits stay bound to the original frame when views or time change. The feedback archive retains the original image, old and edited coordinates, and source JSON without overwriting predictions or model scores. Successful saving clears current edits; failures retain them. Existing hand drafts and `[[pose:…]]` / `[[pose_edit:…]]` references remain compatible.

The editor supports all declared keypoints; this skill’s `apply-corrections` still requires a canonical WholeBody133 parent and does not add points or change topology. The skill merges corrections into a new observation file, then uses `capsule.py wholebody-reconstruct` / `wholebody-build` to produce editable fixed capsule bodies and 20 finger segments per hand, Blender animation and optional GLB. Synchronized calibrated views and explicit actor association are required. Missing fingers hold their previous pose; uninitialized hands are reported as partial results. Face landmarks are reference evidence, without detailed face mesh reconstruction. See [WholeBody reconstruction](external-skills/capsule-human-tracking/references/wholebody-reconstruction.md).

## Start with a blank workbench

Start the dedicated blank entry, then click “打开文件夹” to import and choose a scene. The home page does not load the last viewed model or create a Codex task. Imported scenes, sessions and edits persist; unloading the current scene returns to the blank home.

```bash
.venv/bin/python scripts/start_blank_workbench.py
```

The default URL is `http://127.0.0.1:18776/`, with separate persistent data. See [blank entry setup](docs/folder-import.md#空白工作台入口) for LAN arguments and directories.

## Quick start: room and cabinet

The browser UI uses Chinese labels: `截图` means “Browse snapshots,” `3D` returns to live selection, and `发送反馈` means “Send feedback.”

You need Python 3.11+, a WebGL-capable browser, and a signed-in **`codex-cli 0.156.1`**. The App Server request and response formats were checked against JSON Schema generated by this version. Other versions produce an explicit error instead of silently using incompatible fields.

Run these commands from the root of the cloned repository:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements.txt
codex --version
.venv/bin/python examples/room_demo/seed_demo.py
.venv/bin/python backend/server.py --project-dir "$PWD" --data-dir "$PWD/examples/room_demo/output/data"
```

The Gateway automatically configures the workbench’s `scene_feedback` MCP tools in the project's Codex thread; you do not need to run `codex mcp add` manually. The Gateway passes its port, data directory, and project directory to that thread, preventing another project or global MCP setting from pointing it at the wrong workbench.

Open <http://127.0.0.1:18765/>. The target illustration is on the left, and the initial room GLB is on the right. Select the cabinet, point out its target position in the image, enter “The cabinet should be closer to the left wall; please adjust it according to the image on the left,” and click “Send.” The demo source parameters are in [`examples/room_demo/scene.json`](examples/room_demo/scene.json). [`build_scene.py`](examples/room_demo/build_scene.py) generates a GLB from those parameters; the result appears on the page after Codex calls `workspace_publish_scene`. `seed_demo.py` uses the separate `examples/room_demo/output/data` directory, so it does not overwrite regular workspace data.

For your own project, run:

```bash
.venv/bin/python backend/server.py --project-dir "/absolute/path/to/your/project" --data-dir "/absolute/path/to/private/workspace-data"
```

Add reference images in the browser. Have Codex build or export a self-contained GLB from your project's source files and publish it through MCP. The project directory and thread ID are stored in the data directory, allowing the same thread to be restored after a restart. A data directory is bound to one project and will not silently switch to another.

## Review from an existing Codex desktop task (external MCP mode)

An existing Codex desktop task can receive feedback in two ways. With `--external-review` alone, that task calls `request_visual_feedback`. By default the tool waits for browser submission **even when the server has no graphical desktop and cannot open a browser automatically**. After you click “Send,” the text and images return as an MCP tool result to the task that is still waiting. Explicitly passing `wait_for_submit=False` makes the tool return a URL, `session_id`, and `next_cursor` immediately; the task must then call `wait_visual_feedback(session_id, cursor=next_cursor)` to receive the submission. If the waiting call has already ended or timed out, the feedback remains saved but will not wake an idle task. Read it from the original task or use the task binding option below. `get_visual_feedback` can reread submitted feedback.

Install the dependencies above first. Use absolute paths in a terminal to register a global MCP server and run a separate review service:

```bash
REPO=/absolute/path/to/scene_feedback_harness
PROJECT=/absolute/path/to/your/existing/project
DATA=/absolute/path/to/private/external-review-data
codex mcp add scene_feedback_external \
  --env SCENE_FEEDBACK_PORT=18768 \
  --env SCENE_FEEDBACK_DATA_DIR="$DATA" \
  --env SCENE_FEEDBACK_PROJECT_DIR="$PROJECT" \
  -- "$REPO/.venv/bin/python" "$REPO/backend/mcp_server.py"
"$REPO/.venv/bin/python" "$REPO/backend/server.py" \
  --project-dir "$PROJECT" --data-dir "$DATA" --port 18768 --external-review
```

In `~/.codex/config.toml`, set `tool_timeout_sec = 900` under the `[mcp_servers.scene_feedback_external]` table created by `codex mcp add`. Use the review tools' default `timeout_sec` of 600 or less, leaving time to transfer images. **Restart Codex desktop** so the existing task refreshes its MCP tool catalog. In that task, say “进入人工调试模式” (“enter human review mode”) or explicitly ask it to call `request_visual_feedback` with project-local reference image paths and the current GLB path (omit the GLB if no initial scene exists). In the unbound mode, Send completes the waiting MCP call; if the tool was explicitly told not to wait, call `wait_visual_feedback` separately.

`PROJECT` is the reconstruction root under review; the reference images and GLB must be inside it. It may be a subdirectory of the Codex task's working directory. `DATA` is a private directory dedicated to that project. The example uses a separate port, data directory, and MCP server name to keep it apart from the default mode above. Published scenes can still update the page through `workspace_publish_scene`.

### Bind an existing desktop task for automatic continuation

To make “Send” continue **the existing Codex desktop task** without keeping an MCP tool call waiting, stop the service above and restart it with the same project and data directory plus that task's UUID. `THREAD_ID` is the Codex desktop task ID, **not** the workbench `session_id`:

```bash
THREAD_ID=your-existing-codex-task-uuid
"$REPO/.venv/bin/python" "$REPO/backend/server.py" \
  --project-dir "$PROJECT" --data-dir "$DATA" --port 18768 \
  --external-review --shared-thread-id "$THREAD_ID"
```

The workbench connects to the running **Codex Desktop App Server on the same host and under the same user**. Each submission becomes a new user turn in that existing task, with your text, original and annotated images, and scene snapshots; no new task is created. If the bound task is not loaded, it is automatically restored and delivery continues only through a single verified existing Desktop connection for the same local user, after its model and permissions match the original settings. If the connection is not unique, the settings cannot be checked, or they differ, feedback stays queued with an explanation; no separate App Server is started. Feedback waits in a queue while the task is busy. The page shows queued, running, and completed delivery states. A scene revision change while feedback waits does not block saved feedback; the model receives the snapshot revision and the current revision at delivery. Human approval and interaction requests appear in the workbench; it never approves them automatically. If a disconnect makes delivery uncertain, inspect the original task history before retrying to avoid duplicates. This mode uses `websocket-client`, installed through `backend/requirements.txt`. In bound mode, `request_visual_feedback` opens or updates the workbench and returns its URL; **no MCP result needs to remain pending**.

Older `blocked_stale` feedback safely returns to the queue and delivers automatically if it has saved snapshot evidence and no Codex turn has started. Its evidence and revision remain unchanged. Feedback known not to have been sent stays queued while the connection is unavailable or the original task is busy, then retries automatically. A definite send failure has a manual Retry button. If delivery is uncertain, the workbench checks the original task history by feedback ID. An unresolved item is isolated so later feedback can proceed, but it is **never resent automatically**. Check the original task first; use “Confirm not received, retry” only when the feedback is absent.

To change agents, open the task that should take over in Codex Desktop, then open the task entry in the sidebar and select it under `发送到哪个 Codex 任务` in the task panel. The current scene and references stay in place. New feedback goes to the selected task; unsent feedback remains assigned to its original task and resumes only if you switch back. The Codex task controls its own model; changing the workbench target does not change that model.

You can also expand `新建 Codex 任务` (“Create Codex task”) in that task panel, select an available model, reasoning effort, and permission mode, then click “Create and switch.” The default is workspace write with approvals when needed. You can explicitly choose full access (`never` approval policy, removing routine file and command sandbox approvals) or read only (writes require approval). Tool-specific consent or interaction may still need a response. This starts a fresh conversation without copying the previous chat history. The scene, references, and workbench annotations remain available. No task is created until you click the button. The new task's permissions cannot retroactively change a turn already running or awaiting approval.

For workbench-created tasks with a saved permission mode, reconnecting, switching back, restarting the service and later feedback turns retain that selection. Existing Desktop tasks keep their own permissions; older tasks without a recorded mode are not automatically granted broader access.

## Open the page from another device on the LAN

Keep the service on the machine that holds the project and runs Codex and MCP; other devices only need a browser. Stop the old service on this port, then use the same `REPO`, `PROJECT`, and `DATA` values as above. Replace the example IP with the **service host's** reachable LAN IPv4 address:

```bash
LAN_IP=192.168.1.10
"$REPO/.venv/bin/python" "$REPO/backend/server.py" \
  --project-dir "$PROJECT" --data-dir "$DATA" --port 18768 --external-review \
  --listen-host 0.0.0.0 --public-base-url "http://$LAN_IP:18768" --lan-access open
```

The example uses `--lan-access open`: open and bookmark the published LAN address directly, without a cookie or `access_token`. Anyone who can connect to that port can browse, submit feedback, and control the current workbench.

`--lan-access link` is the default mode with link authorization. The startup output or the MCP `request_visual_feedback` / `workspace_open` result provides a complete link containing `access_token`. For first access, open that **full link** in the other device's browser. After validation, the page removes the token from the address bar and saves a 30-day access cookie, renewed when you visit an authorized workbench home page. You can bookmark the cleaned link. Clearing cookies, changing browsers or host addresses, or using an app's isolated browser context requires the original authorized link again. Before first authorization, entering only `http://$LAN_IP:18768/` will not grant access; keep the access link private.

In `link` mode, when Codex and the service run on the same machine, bookmark the local entry `http://127.0.0.1:18768/p/<project_id>/open` (replace the project ID; the original `session_id` is optional). It authorizes access and returns to the configured LAN page, keeping that browser’s draft at the original address. This entry accepts local access only; other computers cannot use this localhost link and must first open the full authorized link.

Unsent drafts stay in the current browser and host address; the link does not transfer them. The default workbench mode accepts the same LAN flags with its existing port and data directory; omit `--external-review` there. For a persistent service, run the same command under a systemd user service. The workbench page and its related API are available over the LAN. Codex on the project host still calls MCP through `127.0.0.1`. If the page is unreachable, allow the selected TCP port in the host firewall. Plain HTTP LAN mode is intended for a trusted network.

For the desktop task binding option, also keep `--shared-thread-id "$THREAD_ID"` in the LAN startup command. The browser may be on another LAN device; the service connecting to Codex Desktop must still run under the same user on the desktop task's host.

## MCP tools

| Tool | Purpose |
| --- | --- |
| `workspace_open` | Return or open this project's workbench URL |
| `workspace_get_context` | Read current reference images, scene revision, and project context |
| `workspace_get_feedback` | Read a summary and relevant images by default; use `include_details=True` for full archived fields |
| `workspace_export_pose_sources` | Export a source snapshot of every current view and reference frame for the external capsule skill |
| `workspace_import_human_pose` | Validate and import 2D human results bound to that snapshot; no inference is started |
| `workspace_get_human_pose` | Read imported or historical completed results by view, source frame or page |
| `workspace_publish_scene` | Validate and publish a new GLB, check the expected revision, and notify the page to refresh |
| `workspace_set_reference_clip` | Import a single-view or multi-view manifest, or sample a project-local video with FFmpeg; `append_view=True` appends views |
| `workspace_request_feedback` | Default mode: ask the user to inspect something on the page and return immediately; their reply becomes the next user message |
| `request_visual_feedback` / `wait_visual_feedback` | Unbound external MCP mode: wait and return text and images as a tool result; bound desktop mode: the former returns the page URL and submission starts a new turn |
| `get_visual_feedback` | External MCP mode: reread submitted visual feedback |

The repository's [Visual Reconstruction Skill](.agents/skills/visual-reconstruction-feedback/SKILL.md) reminds Codex to distinguish original images from annotations, edit project source files, and publish a GLB when the result is ready. Default mode sends a new turn directly. Unbound external MCP mode returns feedback through a waiting `request_visual_feedback` or `wait_visual_feedback` call. Bound desktop mode starts a new turn after browser submission.

## Verification and limits

```bash
.venv/bin/python -m unittest discover -s backend/tests -v
```

The floating-dock browser regression uses temporary data and an ephemeral loopback port, with Codex disabled and no access to real sessions. Install the optional Playwright and Chromium dependencies to run it:

```bash
.venv/bin/python -m pip install playwright
.venv/bin/python -m playwright install chromium
.venv/bin/python tests/focus_workspace_smoke.py
```

It covers independent conversation collapse controls, unread messages, replay, restoration of unsent drafts and frozen evidence, reference insertion, duplicate-send protection, the timeline, and narrow screens.

The real App Server integration was tested with two different local images: Codex recognized a red image in the first turn, then recognized a blue image in the **same thread** after stdio was closed and restarted. The `thread/read` history contains `localImage` items for both turns. This verifies that the images reached the model, rather than merely sending their file paths.

An end-to-end Selenium run also exercised two browser → Codex turns in the same thread. In the first turn, numbered rectangles were drawn on the reference image and scene and submitted from the browser. Codex received five actual image inputs, edited the demo's `scene.json`, generated a GLB, and successfully published through the project-scoped `workspace_publish_scene` MCP tool twice. Approval was handled on the page; the workbench refreshed as the scene advanced from revision 2 to 3 and then 4. The cabinet's final center was `x=-0.8`. In the second turn, another line was drawn on the reference image and submitted from the page. Codex received five more `input_image` items in the same thread and replied with a confirmation. It did not edit files or publish another scene, so the final revision remained 4.

By default the service listens only on `127.0.0.1`. LAN mode `open` needs no access credentials; `link` requires the full authorized link and a cookie. Both retain Host/Origin and internal write permission checks, with browser write capabilities acquired automatically; private control interfaces and MCP Bearer authentication remain unchanged. Codex turns in the `workspace-write` sandbox enable `networkAccess: true` to reach the local MCP Gateway. Users decide approval requests on the page; the workbench does not approve them automatically. Runtime data and demo output are excluded from Git.

Project code is licensed under the [MIT License](LICENSE). The bundled Three.js files retain their [original MIT license](web/vendor/three/LICENSE).
