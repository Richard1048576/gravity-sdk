"""Patch mempool block in cluster-generated per-node yaml files.

Idempotent: re-running gives same result. Uses pyyaml (no comment preservation,
which is fine since target yamls are auto-generated each `make deploy`).

Usage:
    pfn_pressure_patch_mempool.py YAML_FILE [YAML_FILE ...]
"""
import argparse
import sys
from pathlib import Path

import yaml

# Mempool patch values (see spec §0.1 — colleague-proposed fix)
MEMPOOL_PATCH = {
    "shared_mempool_max_concurrent_inbound_syncs": 16,
    "shared_mempool_batch_size": 1000,
    "max_broadcasts_per_peer": 50,
}


def patch_yaml(path: Path) -> bool:
    """Apply MEMPOOL_PATCH to mempool block at `path`. Returns True if changed."""
    with path.open() as f:
        doc = yaml.safe_load(f) or {}

    if "mempool" not in doc:
        doc["mempool"] = {}

    changed = False
    for key, value in MEMPOOL_PATCH.items():
        if doc["mempool"].get(key) != value:
            doc["mempool"][key] = value
            changed = True

    if changed:
        with path.open("w") as f:
            yaml.safe_dump(doc, f, sort_keys=False, default_flow_style=False)

    return changed


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("yaml_files", nargs="+", help="yaml files to patch")
    args = ap.parse_args()

    for path_str in args.yaml_files:
        path = Path(path_str)
        if not path.exists():
            print(f"ERROR: {path} does not exist", file=sys.stderr)
            sys.exit(1)
        changed = patch_yaml(path)
        status = "PATCHED" if changed else "ALREADY OK"
        print(f"{status}: {path}")


if __name__ == "__main__":
    main()
