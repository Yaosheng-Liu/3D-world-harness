#!/usr/bin/env python3
"""Run Blender on an explicitly configured free GPU; retain per-invocation device logs."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def main():
    config = json.loads(Path(os.environ["WORLDHARNESS_BLENDER_CONFIG"]).read_text())
    lock_dir = Path(config["lock_dir"])
    lock_dir.mkdir(parents=True, exist_ok=True)
    while True:
        for device in config["devices"]:
            lock = (lock_dir / f"gpu{device}.lock").open("a")
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                lock.close()
                continue
            env = dict(os.environ, CUDA_VISIBLE_DEVICES=str(device),
                       WORLDHARNESS_CYCLES_DEVICE=config["backend"], OMP_NUM_THREADS="4", OPENBLAS_NUM_THREADS="4")
            print(json.dumps({"host": os.uname().nodename, "gpu": device, "cycles_backend": config["backend"],
                              "blender": config["blender"], "started_at": time.time()}), flush=True)
            return subprocess.call([config["blender"], "--threads", "4", *sys.argv[1:]], env=env)
        time.sleep(2)


if __name__ == "__main__":
    raise SystemExit(main())
