"""Revert data/ files whose only change is a timestamp (fetched_at / issued_at),
so daily runs without a new PortWatch day produce no commit."""
import json, subprocess, sys
VOLATILE = {"fetched_at", "issued_at"}
def strip(o):
    if isinstance(o, dict):
        return {k: strip(v) for k, v in o.items() if k not in VOLATILE}
    if isinstance(o, list):
        return [strip(v) for v in o]
    return o
changed = subprocess.run(["git", "diff", "--name-only", "--", "data"], capture_output=True, text=True, check=True).stdout.split()
for f in changed:
    if not f.endswith(".json"):
        continue
    old = subprocess.run(["git", "show", f"HEAD:{f}"], capture_output=True, text=True)
    if old.returncode != 0:
        continue
    try:
        same = strip(json.loads(old.stdout)) == strip(json.load(open(f, encoding="utf-8")))
    except Exception:
        continue
    if same:
        subprocess.run(["git", "checkout", "--", f], check=True)
        print("timestamp-only change reverted:", f)
