"""Read-only stage history, bound to saved reports rather than the live preview."""
from datetime import datetime, timezone
import hashlib
from pathlib import Path
from urllib.parse import urlencode

from reconstruction_review import project_file, read_json, verify_files
from world_build import file_hash
from world_model import scene_hash


def _catalog(project):
    project = Path(project).resolve()
    entries, errors = [], []

    def add(path, stage, title, *, run=None, actor=None):
        relative = str(path.relative_to(project))
        entry = {"id": hashlib.sha256((stage + ":" + relative).encode()).hexdigest()[:20], "stage": stage,
                 "title": title, "run_id": run, "actor": actor,
                 "recorded_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
                 "summary": "", "facts": [], "views": [], "documents": [], "issues": [],
                 "items": [], "_assets": {}}
        entries.append(entry)
        return entry

    def asset(entry, value, label, digest=None):
        path = project_file(project, value)
        if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".glb", ".json"}:
            raise ValueError("unsupported history artifact")
        relative = str(path.relative_to(project))
        key = hashlib.sha256(relative.encode()).hexdigest()[:20]
        entry["_assets"][key] = (path, digest)
        return {"label": label, "url": "/api/workspace/reconstruction/history/asset?" +
                urlencode({"entry": entry["id"], "asset": key})}

    def document(entry, path, label):
        entry["documents"].append(asset(entry, path, label))

    def build(entry, receipt, scene):
        if receipt["scene_hash"] != scene_hash(scene):
            raise ValueError("saved build does not match the stage scene")
        base = Path(receipt["build_dir"])
        def built(value, label):
            path = project_file(project, value)
            relative = str(path.relative_to(project_file(project, base / "job.json").parent))
            digest = receipt["files"].get(relative)
            if not digest:
                raise ValueError("build artifact has no receipt hash")
            return asset(entry, path, label, digest)
        entry["mesh"] = built(receipt["glb_path"], "此阶段的三维模型")
        entry["camera"] = next(iter(scene.get("views", [])), None)
        renders = {Path(name).stem: name for name in receipt["render_paths"]}
        for view in scene.get("views", []):
            if view["id"] not in renders:
                continue
            row = {"id": view["id"], "render": built(renders[view["id"]], "该版本渲染")}
            if view.get("reference_image"):
                row["reference"] = asset(entry, view["reference_image"], "原始参考",
                                         receipt.get("asset_hashes", {}).get(view["reference_image"]))
            entry["views"].append(row)
        entry["facts"].append(["模型对象", str(len(scene["objects"]))])
        entry["scene_hash"] = receipt["scene_hash"]
        entry["object_names"] = {obj["id"]: obj.get("name", obj["id"]) for obj in scene["objects"]}

    def collect(path, action):
        # A partial/removed old run must not hide other recorded stages. Surface the error.
        start = len(entries)
        try:
            action(path)
        except (ValueError, KeyError, OSError, TypeError) as exc:
            del entries[start:]
            errors.append({"record": str(path.relative_to(project)), "message": str(exc)})

    def initialization(path):
        scene = read_json(project_file(project, path))
        entry = add(path, "initialization", "初始场景 · DA3 + TSDF")
        entry["kind"] = "geometry"
        entry["summary"] = "保存的观测几何，尚未逐物体精细建模；缺口、遮挡和光照残留属于初始化限制。"
        document(entry, path, "初始场景记录")
        receipt = path.with_name("build_receipt.json")
        if receipt.is_file():
            build(entry, read_json(project_file(project, receipt)), scene)
            document(entry, receipt, "预览构建记录")

    initial = project / "initialization/scene.json"
    if initial.is_file():
        collect(initial, initialization)

    def pipeline(path):
        run = path.parent.name
        state = read_json(project_file(project, path))
        roles = {"plan": state.get("planner"), "model": state.get("builder")}
        for relative, kind, title in (("round_*/plan/plan.json", "plan", "物体计划"),
                                      ("round_*/segments/segments.json", "segments", "SAM 3 分割证据"),
                                      ("round_*/geometry/geometry.json", "geometry", "逐物体观测几何")):
            for report in sorted(path.parent.glob(relative)):
                def evidence(report):
                    data = read_json(project_file(project, report))
                    entry = add(report, "initialization", title, run=run, actor=roles.get(kind))
                    entry["kind"] = kind
                    entry["summary"] = data.get("summary") or "初始化证据供建模参考，不代表最终还原质量。"
                    entry["facts"] = [["记录对象", str(len(data["objects"]))]]
                    entry["items"] = [{"id": obj["id"], "name": obj.get("name", obj["id"]),
                                       "state": obj.get("status", obj.get("kind", "")),
                                       "detail": obj.get("reason", obj.get("uncertainty", ""))}
                                      for obj in data["objects"]]
                    document(entry, report, title + "记录")
                collect(report, evidence)
        for receipt_path in sorted(path.parent.glob("round_*/model/build_receipt.json")):
            def model(receipt_path):
                receipt = read_json(project_file(project, receipt_path))
                job = read_json(project_file(project, Path(receipt["build_dir"]) / "job.json"))
                round_name = receipt_path.parents[1].name
                number = int(round_name.split("_")[1])
                entry = add(receipt_path, "modeling", f"第 {number} 轮 · " + ("首次建模" if number == 1 else "反馈返修"),
                            run=run, actor=roles["model"])
                build(entry, receipt, job["scene"])
                entry["summary"] = "此轮保存的建模结果；是否通过以对应候选的独立审核为准。"
                report_path = receipt_path.with_name("modeling_report.json")
                if report_path.is_file():
                    report = read_json(project_file(project, report_path))
                    entry["summary"] = (f"建模 Agent 已提交 {len(report.get('objects', []))} 项初始化物体的处理说明。"
                                         "下方查看这一轮的实际结果，完整说明保留在处理明细和原始记录中；此记录不代表验收通过。")
                    entry["items"] = [{"id": obj["plan_id"], "name": obj["plan_id"],
                                       "state": obj["resolution"], "detail": obj["reason"]}
                                      for obj in report.get("objects", [])]
                    document(entry, report_path, "建模 Agent 处理报告")
                document(entry, receipt_path, "构建记录")
            collect(receipt_path, model)

    for path in sorted((project / "object_pipeline").glob("*/status.json")):
        collect(path, pipeline)

    def review(path):
        manifest = read_json(project_file(project, path))
        directory = path.parent
        state_path = directory.parent / "status.json"
        state = read_json(project_file(project, state_path)) if state_path.is_file() else {}
        evidence = [str(project_file(project, manifest[key]).relative_to(project)) for key in ("scene_path", "receipt_path")]
        verify_files(project, {name: manifest["files"][name] for name in evidence})
        scene = read_json(project_file(project, manifest["scene_path"]))
        receipt = read_json(project_file(project, manifest["receipt_path"]))
        number = int(directory.name.split("_")[1])
        check = add(path, "checking", f"审核批次 · 第 {number} 轮检查", run=directory.parent.name)
        check["summary"] = "检查冻结候选的构建、物体覆盖和可编辑性；程序通过不等同于视觉质量通过。"
        check["facts"] = [[key, "通过" if passed else "未通过"] for key, passed in manifest["checks"].items()]
        inspection_path = directory / 'inspection.json'
        if inspection_path.is_file():
            name = str(inspection_path.relative_to(project))
            verify_files(project, {name: manifest['files'][name]})
            for image_path in read_json(inspection_path).get('overlay_paths', []):
                relative = str(project_file(project, image_path).relative_to(project))
                check['views'].append({'id': '叠图 · ' + Path(image_path).stem,
                    'render': asset(check, image_path, '原图与模型各 50% · 同相机叠图', manifest['files'][relative])})
        for name, label in (("editability.json", "编辑检查报告"), ("metrics.json", "图像比较指标"), ("inventory.json", "物体覆盖报告")):
            report_path = directory / name
            if report_path.is_file():
                document(check, report_path, label)
        edit_path = directory / "editability.json"
        if edit_path.is_file():
            edit = read_json(project_file(project, edit_path))
            for index, image_path in enumerate(edit.get("inspection_paths", []), 1):
                check["views"].append({"id": f"物体隐藏检查 · {index}", "render": asset(
                    check, image_path, "隐藏前 / 隐藏后", manifest["files"].get(image_path))})
        review_path = directory / "review.json"
        entry = add(review_path if review_path.is_file() else path, "reviewing", f"审核批次 · 第 {number} 轮审核",
                    run=directory.parent.name, actor=state.get("reviewer"))
        # The run's status holds the latest thread; do not attribute it to old rounds.
        if entry["actor"]:
            entry["actor"] = {key: value for key, value in entry["actor"].items() if key != "thread_id"}
        entry["candidate_id"] = manifest["candidate_id"]
        build(entry, receipt, scene)
        for source in manifest.get("source_observations", []):
            entry["views"].append({"id": source["view_id"], "reference": asset(entry, source["path"], "原始证据",
                                    manifest["files"].get(source["path"]))})
        entry["summary"] = "冻结候选已准备，尚无完整审核结论。"
        if review_path.is_file():
            result = read_json(project_file(project, review_path))
            if result["candidate_id"] != manifest["candidate_id"]:
                raise ValueError("review belongs to another candidate")
            entry.update(summary="审核记录覆盖下列视角与对象。选择问题查看具体证据和返工目标；完整审核结论保留在原始记录中。",
                         issues=result["issues"], verdict=result["verdict"],
                         dimensions=result["dimensions"])
            entry["facts"] += [["已检查视角", str(len(result["reviewed_views"]))],
                               ["已检查对象", str(len(result["checked_objects"]))],
                               ["已解决旧问题", str(len(result["resolved_issue_ids"]))]]
            document(entry, review_path, "审核 Agent 完整结论")
        document(entry, path, "审核输入记录")

    for path in sorted((project / "reconstruction").glob("*/round_*/candidate.json")):
        collect(path, review)
    return entries, errors


def reconstruction_history(project):
    entries, errors = _catalog(project)
    return {"entries": [{k: v for k, v in entry.items() if k != "_assets"} for entry in entries], "errors": errors}


def history_asset(project, entry_id, asset_id):
    entries, _ = _catalog(project)
    entry = next((item for item in entries if item["id"] == entry_id), None)
    if entry is None or asset_id not in entry["_assets"]:
        raise ValueError("unknown stage artifact")
    path, digest = entry["_assets"][asset_id]
    if digest and file_hash(path) != digest:
        raise ValueError("stage artifact changed after its recorded build")
    return path
