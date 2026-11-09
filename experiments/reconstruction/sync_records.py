"""Mirror allowlisted experiment records over SSH; never copy agent homes."""
import argparse
import fcntl
import io
from pathlib import Path
import shlex
import subprocess
import tarfile
import time

from common import write


def sync(remote, source, destination):
    # Emit regular files only. Resolve paths before reading to reject symlinks
    # into sessions, credentials or files outside the experiment root.
    code = f"""import pathlib,sys,tarfile
root=pathlib.Path({str(source)!r}).resolve()
patterns=('protocol.json','registered_protocol.json','inputs.json','initialization_summary.json',
          'preparation_deviations.json','workflow_incident.json','execution_gate.json','launch_hold.json',
          'continuation_manifest.json','excluded_feedback_attempts.json',
          'STATUS.md','progress.json','MONITOR.md','monitor.json',
          'monitor_actions.jsonl','automatic_completion.json','results.csv','results.json',
          'evaluation_errors.json','runs/*/spec.json','runs/*/status.json',
          'runs/*/resume.json','runs/*/initial_modeler_usage.json','runs/*/source_calls/*/usage.json',
          'runs/*/planner_usage.json','runs/*/calls/*/usage.json',
          'runs/*/checkpoints/*/frozen.json','evaluation_results/*/*/*metrics.json',
          'evaluation_results/*/*/complete.json','evaluation_results/*/*/error.json',
          'evaluation_results/*/*/target_ownership.json','evaluation_results/*/*/target_ownership.jpg')
with tarfile.open(fileobj=sys.stdout.buffer,mode='w|') as archive:
 for pattern in patterns:
  for path in sorted(root.glob(pattern)):
   if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(root):
    archive.add(path,arcname=str(path.relative_to(root)),recursive=False)
"""
    argv = ["ssh", "-F", str(Path.home()/".ssh/config"), "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
            remote, "python3 -c " + shlex.quote(code)]
    result = subprocess.run(argv, capture_output=True, timeout=180, check=True)
    with tarfile.open(fileobj=io.BytesIO(result.stdout)) as archive:
        for entry in archive:
            relative = Path(entry.name)
            if not entry.isfile() or relative.is_absolute() or ".." in relative.parts:
                raise ValueError("Unexpected record archive member")
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + ".sync")
            temporary.write_bytes(archive.extractfile(entry).read())
            temporary.replace(target)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--remote", required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--interval", type=int, default=300)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    args.destination.mkdir(parents=True, exist_ok=True)
    with (args.destination / "sync.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        while True:
            try:
                sync(args.remote, args.source, args.destination)
                write(args.destination / "sync_status.json", {"updated_at": time.time(), "status": "ok"})
            except (subprocess.SubprocessError, OSError) as error:
                write(args.destination / "sync_status.json", {"updated_at": time.time(), "status": "error", "error": str(error)})
                if args.once:
                    raise
            if args.once or (args.destination / "automatic_completion.json").exists():
                break
            time.sleep(args.interval)


if __name__ == "__main__":
    main()
