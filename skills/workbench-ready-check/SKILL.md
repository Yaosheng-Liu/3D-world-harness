---
name: workbench-ready-check
description: Check prepared scene folders before delivery or workbench import, and diagnose ready-instance failures. Use for workbench-ready.json/workbench_ready.json packages, scene catalogs, dynamic multi-view references, camera/frame validation, or agent self-checking of workbench-ready output. Runs the same read-only validator used by automatic folder imports.
---

# Workbench ready self-check

Run this skill before handing a prepared scene to the visual workbench, or when
an import reports a failed instance. Use the existing validator; do not replace
it with file-existence checks or a second definition of “ready”.

## Run

Use the absolute path of the scene instance or parent catalog on the workbench
service host. A browser user's local folder is a different location. From this
skill directory run:

```bash
python3 scripts/check_ready.py /absolute/path/to/ready-scenes --json
```

The helper finds the enclosing plugin or checkout, its configured
`SCENE_FEEDBACK_ROOT`, or `~/scene_feedback_harness`. If it cannot find the
runtime, pass `--harness-root /absolute/path/to/scene_feedback_harness`. It uses
that runtime's virtual environment when available and invokes
`scripts/check_workbench_ready.py`. It does not start a service, import a scene,
create a Codex task, or modify the source. Report an unavailable runtime or
dependency explicitly; do not claim a check ran when it did not.

## Act on the result

- Exit `0`: all discovered instances are importable. Read and report any
  `warning` limitations; “importable” does not establish visual alignment quality.
- Exit `1`: `partial`, `blocked`, `empty`, or a truncated scan. Inspect
  `instances[].checks` with status `error`, `errors`, and `truncated`. Report
  the affected instance and exact file/reason. `can_import:true` means at least
  one instance passed, not that the entire folder passed.
- Exit `2`: invalid path, arguments, report destination, or runtime lookup.
  Correct the location or invocation before diagnosing scene geometry.

Fix concrete failures in the requested output package and run the same command
again. Narrow the folder when scanning is truncated. Keep the user's editable
source and precise view, frame, time, and camera bindings. Do not remove declared
references or cameras, lower sampling, or relabel failed instances merely to
make the check pass. Source edits follow the reconstruction task's authorization;
checking itself is read-only. Do not import just to test validity.

## Preparing or repairing a package

Read `docs/folder-import.md` in the resolved runtime for the supported schemas
and limits. Standard markers require `schema_version: 1`, a contained GLB, and
reference images or a reference manifest. Existing video-ready packages and
parent `manifest.json` catalogs have their documented compatibility formats;
do not rewrite them unnecessarily. Relative reference paths are resolved from
the declaring manifest, and all declared assets must stay inside the instance.

The validator checks GLB/container and viewer compatibility, full reference-image
decoding, reference views, frame timing, and declared camera data. Missing cameras,
missing editable Blender sources, and supported model-only catalogs can be
importable warnings. It does not reopen Blender, run tracking or reconstruction,
or prove geometry, motion, or image alignment quality.

Folder import automatically repeats validation. New instances use the prepared
source; already registered instances use the saved workbench result so reimport
preserves edits. This standalone command checks the supplied source package.
Distinguish those targets when investigating a reimport failure.

Finish with the checked absolute path, pass/fail count, concrete remaining
warnings or errors, and whether scanning was complete. Save JSON only when useful
using an explicit new `--output` path; existing files are never overwritten.
