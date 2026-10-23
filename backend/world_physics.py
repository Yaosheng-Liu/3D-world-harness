"""Bounded, headless MuJoCo runs from the editable world specification.

Collision primitives determine mass distribution; visual geometry never enters
the solver. These checks establish simulation health, not real-world calibration.
"""

from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path
import xml.etree.ElementTree as ET

from world_model import WorldError, scene_hash, validate_scene


TIMESTEP = 0.002
PENETRATION_LIMIT = 0.01
JOINT_TOLERANCE = {"hinge": 0.02, "slide": 0.002}


def _numbers(values) -> str:
    return " ".join(format(float(value), ".12g") for value in values)


def _volume(shape: dict) -> float:
    if shape["type"] == "box":
        return math.prod(shape["size"])
    if shape["type"] == "sphere":
        return 4 * math.pi * shape["radius"] ** 3 / 3
    return math.pi * shape["radius"] ** 2 * shape["depth"]


def export_mjcf(scene: dict, project_dir: Path | None = None) -> str:
    """Export collision-only MJCF; dimensions are full extents in source JSON."""
    validate_scene(scene, project_dir)
    root = ET.Element("mujoco", model=scene["scene_id"])
    ET.SubElement(root, "compiler", angle="radian", eulerseq="XYZ", inertiafromgeom="true")
    option = ET.SubElement(root, "option", timestep=str(TIMESTEP), gravity="0 0 -9.81",
                           integrator="implicitfast")
    # Cabinet parts must collide with their parents; never conceal numerical failure.
    ET.SubElement(option, "flag", filterparent="disable", autoreset="disable")
    default = ET.SubElement(root, "default")
    ET.SubElement(default, "geom", solref="0.005 1", solimp="0.95 0.99 0.001", condim="3")
    worldbody = ET.SubElement(root, "worldbody")
    children: dict[str | None, list[dict]] = {}
    for obj in scene["objects"]:
        children.setdefault(obj["parent_id"], []).append(obj)

    def add_body(obj: dict, parent: ET.Element) -> None:
        body = ET.SubElement(parent, "body", name=obj["id"], pos=_numbers(obj["position"]),
                             euler=_numbers(obj["rotation"]))
        physics = obj["physics"]
        if obj.get("joint"):
            joint = obj["joint"]
            ET.SubElement(body, "joint", name=obj["id"], type=joint["type"],
                          pos=_numbers(joint["position"]), axis=_numbers(joint["axis"]),
                          limited="true", range=_numbers(joint["range"]),
                          damping=str(joint["damping"]), solreflimit="0.005 1")
        elif physics["body_type"] == "dynamic":
            ET.SubElement(body, "freejoint", name=obj["id"])
        try:
            volumes = [_volume(shape) for shape in obj["collision"]]
        except OverflowError as error:
            raise WorldError(f"{obj['id']}: collision dimensions exceed the solver's numeric range") from error
        total_volume = sum(volumes)
        if not math.isfinite(total_volume) or total_volume <= 0:
            raise WorldError(f"{obj['id']}: collision volumes must be positive and finite")
        for index, (shape, volume) in enumerate(zip(obj["collision"], volumes)):
            kind = shape["type"]
            size = ([dimension / 2 for dimension in shape["size"]] if kind == "box" else
                    [shape["radius"]] if kind == "sphere" else
                    [shape["radius"], shape["depth"] / 2])
            ET.SubElement(body, "geom", name=f"{obj['id']}__collision_{index}", type=kind,
                          size=_numbers(size), pos=_numbers(shape.get("position", [0, 0, 0])),
                          euler=_numbers(shape.get("rotation", [0, 0, 0])),
                          mass=str(physics["mass"] * (volume / total_volume)),
                          friction=_numbers([physics["friction"], 0, 0]))
        for child in children.get(obj["id"], []):
            add_body(child, body)

    for obj in children.get(None, []):
        add_body(obj, worldbody)
    ET.indent(root)
    return ET.tostring(root, encoding="unicode") + "\n"


def _bounded_number(value, label: str, lower: float, upper: float) -> float:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not lower <= value <= upper):
        raise WorldError(f"{label} must be a finite number between {lower} and {upper}")
    return float(value)


def _validate_action(action: dict | None, objects: dict, duration: float) -> dict | None:
    if action is None:
        return None
    if not isinstance(action, dict) or action.get("type") not in ("impulse", "force", "joint_force"):
        raise WorldError("action.type must be impulse, force, or joint_force")
    kind = action["type"]
    fields = {"type", "object_id", "force" if kind == "joint_force" else "vector"}
    if kind != "impulse":
        fields.add("duration_sec")
    if set(action) != fields:
        raise WorldError(f"{kind} action requires exactly {', '.join(sorted(fields))}")
    obj = objects.get(action["object_id"]) if isinstance(action["object_id"], str) else None
    if obj is None or obj["physics"]["body_type"] != "dynamic":
        raise WorldError("action.object_id must identify a dynamic object")
    if kind == "joint_force":
        if not obj.get("joint"):
            raise WorldError("joint_force requires an object with a hinge or slide joint")
        _bounded_number(action["force"], "action.force", -10000, 10000)
    else:
        vector = action["vector"]
        if not isinstance(vector, list) or len(vector) != 3:
            raise WorldError("action.vector must contain three world-space components")
        for component in vector:
            _bounded_number(component, "action.vector component", -10000, 10000)
    if kind != "impulse":
        _bounded_number(action["duration_sec"], "action.duration_sec", TIMESTEP, duration)
    return dict(action)


def simulate(scene: dict, *, action: dict | None = None, duration_sec: float = 2,
             sample_hz: float = 30, output_dir: Path | None = None,
             project_dir: Path | None = None) -> dict:
    """Run gravity plus one optional load at t=0, with fixed 2 ms integration.

    Impulse units are N*s; force units are N in world coordinates at the body's
    centre of mass. Joint force means N*m for hinges, N for sliders. Duration and
    force windows round up to an integration step; actual timestamps are saved.
    Output poses are world-space metres, Z up, with wxyz quaternions.
    """
    duration = _bounded_number(duration_sec, "duration_sec", TIMESTEP, 10)
    sample_hz = _bounded_number(sample_hz, "sample_hz", 1, 60)
    xml = export_mjcf(scene, project_dir)
    objects = {obj["id"]: obj for obj in scene["objects"]}
    action = _validate_action(action, objects, duration)
    try:
        import mujoco
        import numpy as np
    except ImportError as error:
        raise WorldError("MuJoCo is required: pip install -r backend/requirements-world.txt") from error
    try:
        model = mujoco.MjModel.from_xml_string(xml)
    except ValueError as error:
        raise WorldError(f"MuJoCo rejected the collision model: {error}") from error
    data = mujoco.MjData(model)
    mujoco.mj_forward(model, data)
    body_ids = {name: model.body(name).id for name in objects}
    joint_ids = {name: model.joint(name).id for name, obj in objects.items() if obj.get("joint")}
    identity = {"scene_id": scene["scene_id"], "revision": scene["revision"],
                "scene_hash": scene_hash(scene), "engine": {"name": "mujoco", "version": mujoco.__version__},
                "created_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    trajectory = {**identity, "timestamps": [], "poses": {name: [] for name in objects},
                  "joints": {name: [] for name in joint_ids}}
    max_penetration = 0.0
    worst_contact = None
    joint_excess = {name: 0.0 for name in joint_ids}
    finite = True
    peak_velocity = 0.0

    def inspect_state() -> bool:
        nonlocal max_penetration, worst_contact, finite, peak_velocity
        finite = all(np.isfinite(array).all() for array in
                     (data.qpos, data.qvel, data.qacc, data.xpos, data.xquat, data.time))
        if not finite:
            return False
        peak_velocity = max(peak_velocity, float(np.max(np.abs(data.qvel), initial=0)))
        for contact in data.contact[:data.ncon]:
            penetration = max(0.0, -float(contact.dist))
            if penetration > max_penetration:
                max_penetration = penetration
                names = [mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_BODY,
                                         int(model.geom_bodyid[geom])) for geom in (contact.geom1, contact.geom2)]
                worst_contact = {"object_ids": names, "time_sec": round(float(data.time), 9)}
        for name, jid in joint_ids.items():
            value = float(data.qpos[model.jnt_qposadr[jid]])
            low, high = objects[name]["joint"]["range"]
            joint_excess[name] = max(joint_excess[name], low - value, value - high)
        return True

    def sample() -> None:
        trajectory["timestamps"].append(round(float(data.time), 9))
        for name, bid in body_ids.items():
            trajectory["poses"][name].append({"position": data.xpos[bid].tolist(),
                                               "quaternion": data.xquat[bid].tolist()})
        for name, jid in joint_ids.items():
            trajectory["joints"][name].append(float(data.qpos[model.jnt_qposadr[jid]]))

    steps = math.ceil(duration / TIMESTEP)
    action_steps = (1 if action and action["type"] == "impulse" else
                    math.ceil(action["duration_sec"] / TIMESTEP) if action else 0)
    next_sample = 1 / sample_hz
    if inspect_state():
        sample()
    for step in range(steps):
        if not finite:
            break
        data.xfrc_applied[:] = 0
        data.qfrc_applied[:] = 0
        if action and step < action_steps:
            name = action["object_id"]
            if action["type"] == "joint_force":
                data.qfrc_applied[model.jnt_dofadr[joint_ids[name]]] = action["force"]
            else:
                data.xfrc_applied[body_ids[name], :3] = np.array(action["vector"]) / (
                    TIMESTEP if action["type"] == "impulse" else 1)
        mujoco.mj_step(model, data)
        # mj_step integrates qpos after computing xpos; refresh before recording.
        mujoco.mj_forward(model, data)
        if not inspect_state():
            break
        if data.time + 1e-9 >= next_sample or step == steps - 1:
            sample()
            next_sample += 1 / sample_hz

    warnings = {mujoco.mjtWarning(index).name: int(warning.number)
                for index, warning in enumerate(data.warning) if warning.number}
    limits = {name: {"max_excess": excess,
                     "tolerance": JOINT_TOLERANCE[objects[name]["joint"]["type"]],
                     "unit": "rad" if objects[name]["joint"]["type"] == "hinge" else "m"}
              for name, excess in joint_excess.items()}
    checks = {
        "finite_state": {"passed": finite},
        "numerical_stability": {"passed": not warnings and peak_velocity < 1000,
                                "warnings": warnings, "peak_generalized_speed": peak_velocity},
        "penetration": {"passed": max_penetration <= PENETRATION_LIMIT,
                        "max_depth_m": max_penetration, "tolerance_m": PENETRATION_LIMIT,
                        "worst_contact": worst_contact},
        "joint_limits": {"passed": all(item["max_excess"] <= item["tolerance"] for item in limits.values()),
                         "joints": limits},
    }
    report = {**identity, "passed": all(check["passed"] for check in checks.values()),
              "checks": checks, "action": action, "timestep_sec": TIMESTEP,
              "requested_duration_sec": duration, "duration_sec": round(float(data.time), 9),
              "sample_hz": sample_hz, "real_world_calibrated": False,
              "limitations": ["Numerical smoke checks; no measured-motion calibration.",
                              "Mass is distributed uniformly over collision primitive volumes; inertia is estimated.",
                              "Soft, critically damped contacts; friction is sliding only, with no restitution calibration."]}
    result = {"trajectory": trajectory, "report": report}
    if output_dir is not None:
        directory = Path(output_dir)
        directory.mkdir(parents=True, exist_ok=True)
        paths = {"mjcf": directory / "world.xml", "trajectory": directory / "trajectory.json",
                 "report": directory / "report.json"}
        paths["mjcf"].write_text(xml, encoding="utf-8")
        for key in ("trajectory", "report"):
            paths[key].write_text(json.dumps(result[key], indent=2, allow_nan=False) + "\n", encoding="utf-8")
        result["artifacts"] = {key: str(path) for key, path in paths.items()}
    return result
