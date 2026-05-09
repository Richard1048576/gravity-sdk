"""Periodic metrics collector for pfn_chain pressure tests.

Polls 5 nodes every 2s and writes one jsonl line per poll containing:
- aptos_consensus_* (from inspection_port)
- eth_blockNumber (from rpc_port)
- txpool_status (from rpc_port)

Stops on SIGTERM/SIGINT.

Usage:
    pfn_pressure_sidecar.py --output /tmp/.../sidecar_metrics.jsonl
"""
import argparse
import json
import signal
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Per spec §1
NODES = [
    {"id": "node1", "rpc": 18545, "consensus_metrics": 10002},
    {"id": "vfn1",  "rpc": 18546, "consensus_metrics": 10003},
    {"id": "pfn1",  "rpc": 18547, "consensus_metrics": 10004},
    {"id": "pfn2",  "rpc": 18548, "consensus_metrics": 10005},
    {"id": "pfn3",  "rpc": 18549, "consensus_metrics": 10006},
]

CONSENSUS_KEYS = (
    "aptos_consensus_epoch",
    "aptos_consensus_current_round",
    "aptos_consensus_last_committed_round",
    "aptos_consensus_proposals_count",
)

POLL_INTERVAL = 2.0
HTTP_TIMEOUT = 1.5

_stop = False


def parse_consensus_metrics(text: str) -> dict:
    """Extract bare aptos_consensus_* counter values (no label variants)."""
    out = {}
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 2:
            continue
        key, value = parts
        if key in CONSENSUS_KEYS:
            try:
                out[key] = float(value)
            except ValueError:
                pass
    return out


def http_get(url: str) -> str:
    with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT) as r:
        return r.read().decode()


def http_post_json(url: str, payload: dict) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        return json.loads(r.read())


def fetch_node(node: dict) -> dict:
    snap = {"id": node["id"]}

    try:
        text = http_get(f"http://127.0.0.1:{node['consensus_metrics']}/metrics")
        snap["consensus"] = parse_consensus_metrics(text)
    except Exception as e:
        snap["consensus_error"] = str(e)[:200]

    rpc_url = f"http://127.0.0.1:{node['rpc']}"
    try:
        r = http_post_json(rpc_url, {
            "jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1,
        })
        snap["block_number"] = int(r["result"], 16)
    except Exception as e:
        snap["block_number_error"] = str(e)[:200]

    try:
        r = http_post_json(rpc_url, {
            "jsonrpc": "2.0", "method": "txpool_status", "params": [], "id": 1,
        })
        result = r.get("result", {})
        snap["txpool_pending"] = int(result.get("pending", "0x0"), 16)
        snap["txpool_queued"] = int(result.get("queued", "0x0"), 16)
    except Exception as e:
        snap["txpool_error"] = str(e)[:200]

    return snap


def poll_all() -> list:
    with ThreadPoolExecutor(max_workers=len(NODES)) as ex:
        return list(ex.map(fetch_node, NODES))


def handle_signal(_signum, _frame):
    global _stop
    _stop = True


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--output", required=True)
    args = ap.parse_args()

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    print(f"sidecar: writing to {out_path}", file=sys.stderr, flush=True)

    with out_path.open("a") as f:
        while not _stop:
            t0 = time.time()
            record = {"ts": t0, "nodes": poll_all()}
            f.write(json.dumps(record) + "\n")
            f.flush()
            elapsed = time.time() - t0
            time.sleep(max(0.0, POLL_INTERVAL - elapsed))

    print("sidecar: stopped cleanly", file=sys.stderr)


if __name__ == "__main__":
    main()
