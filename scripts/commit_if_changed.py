"""Revert data/ files whose only change is a timestamp (fetched_at / issued_at)
or float rounding noise (<= 1e-9 relative, e.g. numpy differing between a
Windows dev box and the Linux runner), so daily runs without new data produce
no commit."""
import json, math, subprocess, sys
VOLATILE = {"fetched_at", "issued_at"}

def same(a, b):
    if isinstance(a, dict) and isinstance(b, dict):
        ka = {k for k in a if k not in VOLATILE}
        kb = {k for k in b if k not in VOLATILE}
        return ka == kb and all(same(a[k], b[k]) for k in ka)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    if isinstance(a, float) or isinstance(b, float):
        if isinstance(a, (int, float)) and isinstance(b, (int, float)) \
                and not isinstance(a, bool) and not isinstance(b, bool):
            return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12)
        return False
    return a == b

if __name__ == "__main__":
    changed = subprocess.run(["git", "diff", "--name-only", "--", "data"], capture_output=True, text=True, check=True).stdout.split()
    for f in changed:
        if not f.endswith(".json"):
            continue
        old = subprocess.run(["git", "show", f"HEAD:{f}"], capture_output=True, text=True)
        if old.returncode != 0:
            continue
        try:
            unchanged = same(json.loads(old.stdout), json.load(open(f, encoding="utf-8")))
        except Exception:
            continue
        if unchanged:
            subprocess.run(["git", "checkout", "--", f], check=True)
            print("noise-only change reverted:", f)
