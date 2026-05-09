"""pfn_chain VFN pressure test orchestrator.

Runs Phase 0 (sender sanity) → Phase 1 (default config 4-target) →
Phase 2 (mempool patch + 4-target rerun) → Phase 3 (aggregate).

Designed to run on zz@192.168.1.226. See spec
docs/superpowers/specs/2026-05-09-pfn-chain-vfn-pressure-test-design.md
for full design.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class BenchResult:
    senders: int
    tps: float
    success_pct: float
    timed_out: int


# Match a sample line with TPS / Success% / Timed Out Txns
_BENCH_SAMPLE_RE = re.compile(
    r"TPS:\s*([0-9.]+).*?Success%:\s*([0-9.]+)%.*?Timed Out Txns:\s*([0-9]+)"
)


def parse_bench_log(log_text: str, senders: int = 0) -> Optional[BenchResult]:
    """Pull the LAST sample line from a gravity_bench log.

    Returns None if no sample found (bench died early or log truncated).
    """
    last = None
    for m in _BENCH_SAMPLE_RE.finditer(log_text):
        last = m
    if last is None:
        return None
    return BenchResult(
        senders=senders,
        tps=float(last.group(1)),
        success_pct=float(last.group(2)),
        timed_out=int(last.group(3)),
    )


def decide_best_senders(
    runs: list[BenchResult], *, success_threshold: float = 95.0
) -> Optional[BenchResult]:
    """Pick the run with highest TPS among those passing success threshold."""
    eligible = [r for r in runs if r.success_pct >= success_threshold]
    if not eligible:
        return None
    return max(eligible, key=lambda r: r.tps)


import argparse
import json
import logging
import os
import shutil
import signal
import string
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Tuple
from urllib import request as urlreq

LOG = logging.getLogger("pfn_pressure")

# Layout — these are absolute paths on 226. Make them parameterizable just in case.
SDK_ROOT = Path(os.environ.get("SDK_ROOT", "/home/zz/Gravity/gravity-sdk"))
BENCH_DIR = Path(os.environ.get("BENCH_DIR", "/home/zz/Gravity/gravity_bench"))
BENCH_BIN = BENCH_DIR / "target" / "release" / "gravity_bench"
CLUSTER_TOML = SDK_ROOT / "gravity_e2e" / "cluster_test_cases" / "pfn_chain" / "cluster.toml"
BENCH_TEMPLATE = SDK_ROOT / "gravity_e2e" / "cluster_test_cases" / "pfn_chain" / "bench_config_pfn_chain.toml.template"
CLUSTER_BASE_DIR = Path("/tmp/gravity-cluster-pfn-chain")  # from cluster.toml's base_dir
GEN_REPORT_SH = SDK_ROOT / ".agents" / "benchmark" / "gen_report.sh"

# Bench targets (id, rpc_port) — order matters per spec §3 Phase 1
TARGETS = [
    ("node1", 18545),
    ("vfn1",  18546),
    ("pfn1",  18547),
    ("pfn3",  18549),
]
# All 5 nodes (TARGETS + pfn2) — used for health/drain checks
TARGETS_ALL = TARGETS + [("pfn2", 18548)]

SENDERS_CANDIDATES = (100, 200, 400)
SUCCESS_THRESHOLD = 95.0
PHASE0_DURATION = 60
PHASE1_DURATION = 300
PHASE2_DURATION = 300
COOLDOWN = 30


def render_bench_config(rpc_url: str, num_senders: int, duration_secs: int, dest: Path) -> None:
    """Render the bench config template into `dest`."""
    tpl = BENCH_TEMPLATE.read_text()
    rendered = string.Template(tpl).substitute(
        RPC_URL=rpc_url,
        NUM_SENDERS=num_senders,
        DURATION_SECS=duration_secs,
    )
    dest.write_text(rendered)


def http_post_json(url: str, payload: dict, timeout: float = 2.0) -> dict:
    req = urlreq.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urlreq.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def get_block_number(rpc_url: str) -> Optional[int]:
    try:
        r = http_post_json(rpc_url, {
            "jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1,
        })
        return int(r["result"], 16)
    except Exception:
        return None


def get_txpool_pending(rpc_url: str) -> Optional[int]:
    try:
        r = http_post_json(rpc_url, {
            "jsonrpc": "2.0", "method": "txpool_status", "params": [], "id": 1,
        })
        return int(r.get("result", {}).get("pending", "0x0"), 16)
    except Exception:
        return None


def wait_for_cluster_health(timeout: int = 120) -> bool:
    """Wait until 5 nodes return non-null block_number with max-min < 50."""
    LOG.info("Waiting for cluster health (timeout %ds)...", timeout)
    deadline = time.time() + timeout
    while time.time() < deadline:
        heights = [get_block_number(f"http://127.0.0.1:{p}") for _, p in TARGETS_ALL]
        if all(h is not None for h in heights) and (max(heights) - min(heights)) < 50:
            LOG.info("Cluster healthy: heights=%s", heights)
            return True
        time.sleep(3)
    LOG.error("Cluster never reached healthy state in %ds", timeout)
    return False


def wait_for_mempool_drain(timeout: int = 60) -> None:
    """Wait until all 5 nodes have txpool_pending < 1000, or timeout."""
    LOG.info("Waiting for mempool drain (timeout %ds)...", timeout)
    deadline = time.time() + timeout
    while time.time() < deadline:
        pendings = [get_txpool_pending(f"http://127.0.0.1:{p}") for _, p in TARGETS_ALL]
        if all(p is not None and p < 1000 for p in pendings):
            LOG.info("Mempool drained: pending=%s", pendings)
            return
        time.sleep(3)
    LOG.warning("Mempool drain timed out, continuing anyway")


CLUSTER_DIR = SDK_ROOT / "cluster"
GENESIS_TOML = CLUSTER_TOML.parent / "genesis.toml"
ARTIFACTS_DIR = CLUSTER_TOML.parent / "artifacts"


def _cluster_env() -> dict:
    """Env dict for cluster shell scripts — sets the per-test artifacts dir."""
    env = os.environ.copy()
    env["GRAVITY_ARTIFACTS_DIR"] = str(ARTIFACTS_DIR)
    return env


def cluster_init() -> None:
    """init.sh <genesis.toml> + genesis.sh <genesis.toml>.

    Skipped if artifacts already cached (runner.py convention).
    """
    if (ARTIFACTS_DIR / "genesis.json").exists():
        LOG.info("Artifacts already at %s — skipping init/genesis", ARTIFACTS_DIR)
        return
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    env = _cluster_env()
    LOG.info("Running init.sh...")
    subprocess.run(
        ["bash", str(CLUSTER_DIR / "init.sh"), str(GENESIS_TOML)],
        cwd=CLUSTER_DIR, env=env, check=True,
    )
    LOG.info("Running genesis.sh...")
    subprocess.run(
        ["bash", str(CLUSTER_DIR / "genesis.sh"), str(GENESIS_TOML)],
        cwd=CLUSTER_DIR, env=env, check=True,
    )


def cluster_deploy() -> None:
    LOG.info("Running deploy.sh...")
    subprocess.run(
        ["bash", str(CLUSTER_DIR / "deploy.sh"), str(CLUSTER_TOML)],
        cwd=CLUSTER_DIR, env=_cluster_env(), check=True,
    )


def cluster_start() -> None:
    LOG.info("Running start.sh...")
    subprocess.run(
        ["bash", str(CLUSTER_DIR / "start.sh"), "--config", str(CLUSTER_TOML)],
        cwd=CLUSTER_DIR, env=_cluster_env(), check=True,
    )


def cluster_stop() -> None:
    LOG.info("Running stop.sh...")
    subprocess.run(
        ["bash", str(CLUSTER_DIR / "stop.sh"), "--config", str(CLUSTER_TOML)],
        cwd=CLUSTER_DIR, env=_cluster_env(), check=False,    # idempotent
    )
    # Wait for all nodes to be off the wire before next start (avoid port conflicts)
    deadline = time.time() + 30
    while time.time() < deadline:
        if subprocess.run(["pgrep", "-f", "gravity_node"],
                         stdout=subprocess.DEVNULL).returncode != 0:
            return
        time.sleep(1)
    LOG.warning("gravity_node still running after stop.sh — may cause port conflict")


def patch_all_yamls() -> None:
    """Apply mempool patch to all 5 per-node yaml configs."""
    yamls = [
        CLUSTER_BASE_DIR / "node1" / "config" / "validator.yaml",
        CLUSTER_BASE_DIR / "vfn1"  / "config" / "validator_full_node.yaml",
        CLUSTER_BASE_DIR / "pfn1"  / "config" / "public_full_node.yaml",
        CLUSTER_BASE_DIR / "pfn2"  / "config" / "public_full_node.yaml",
        CLUSTER_BASE_DIR / "pfn3"  / "config" / "public_full_node.yaml",
    ]
    for y in yamls:
        if not y.exists():
            raise FileNotFoundError(f"Expected yaml not found: {y}")
    cmd = [sys.executable, str(SDK_ROOT / "scripts" / "pfn_pressure_patch_mempool.py"), *map(str, yamls)]
    LOG.info("Patching yamls: %s", cmd)
    subprocess.run(cmd, check=True)


def start_sidecar(out_path: Path) -> subprocess.Popen:
    """Spawn the sidecar collector, return Popen for later termination."""
    LOG.info("Starting sidecar → %s", out_path)
    return subprocess.Popen(
        [sys.executable, str(SDK_ROOT / "scripts" / "pfn_pressure_sidecar.py"),
         "--output", str(out_path)],
    )


def stop_sidecar(proc: subprocess.Popen) -> None:
    LOG.info("Stopping sidecar (PID=%s)...", proc.pid)
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def run_bench(rpc_url: str, num_senders: int, duration_secs: int, log_path: Path) -> Optional[BenchResult]:
    """Render config, run gravity_bench, capture log, parse last sample."""
    cfg = BENCH_DIR / "bench_config_pfn_chain.toml"
    render_bench_config(rpc_url, num_senders, duration_secs, cfg)
    LOG.info("bench: rpc=%s senders=%d duration=%ds → %s",
             rpc_url, num_senders, duration_secs, log_path)
    with log_path.open("w") as f:
        rc = subprocess.call(
            [str(BENCH_BIN), "--config", str(cfg)],
            cwd=BENCH_DIR, stdout=f, stderr=subprocess.STDOUT,
        )
    LOG.info("bench done: rc=%d log=%s", rc, log_path)
    return parse_bench_log(log_path.read_text(), senders=num_senders)


def phase0(results_dir: Path) -> Optional[BenchResult]:
    """3 short runs against vfn1 with different num_senders."""
    LOG.info("=== PHASE 0: senders sanity ===")
    runs = []
    for s in SENDERS_CANDIDATES:
        ts = int(time.time())
        log = results_dir / f"bench_phase0_senders{s}_{ts}.log"
        result = run_bench("http://127.0.0.1:18546", s, PHASE0_DURATION, log)
        if result:
            LOG.info("phase0 senders=%d → tps=%.1f success=%.1f%% timed_out=%d",
                     s, result.tps, result.success_pct, result.timed_out)
            runs.append(result)
        else:
            LOG.warning("phase0 senders=%d → bench produced no parseable output", s)
        wait_for_mempool_drain()
        time.sleep(COOLDOWN)
    chosen = decide_best_senders(runs, success_threshold=SUCCESS_THRESHOLD)
    if chosen is None:
        LOG.error("PHASE 0 FAILED: no run passed success threshold %.1f%%", SUCCESS_THRESHOLD)
        return None
    LOG.info("PHASE 0 winner: senders=%d (tps=%.1f, success=%.1f%%)",
             chosen.senders, chosen.tps, chosen.success_pct)
    return chosen


def phase_n_targets(phase_label: str, num_senders: int, duration_secs: int,
                    results_dir: Path) -> dict:
    """Run the 4-target comparison. Returns {target_id: BenchResult}."""
    LOG.info("=== PHASE %s: 4-target comparison (senders=%d) ===", phase_label, num_senders)
    out = {}
    for tid, port in TARGETS:
        if not wait_for_cluster_health(timeout=60):
            LOG.error("Cluster unhealthy before %s/%s — aborting", phase_label, tid)
            break
        ts = int(time.time())
        log = results_dir / f"bench_{phase_label}_{tid}_{ts}.log"
        result = run_bench(f"http://127.0.0.1:{port}", num_senders, duration_secs, log)
        if result:
            LOG.info("%s/%s → tps=%.1f success=%.1f%% timed_out=%d",
                     phase_label, tid, result.tps, result.success_pct, result.timed_out)
            out[tid] = result
        else:
            LOG.warning("%s/%s → bench produced no parseable output", phase_label, tid)
        wait_for_mempool_drain()
        time.sleep(COOLDOWN)
    return out


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    ap = argparse.ArgumentParser()
    ap.add_argument("--results-dir", default=f"/tmp/pfn_pressure_results_{int(time.time())}")
    ap.add_argument("--skip-phase0", type=int, default=0,
                    help="If set, use this num_senders and skip Phase 0")
    ap.add_argument("--skip-deploy", action="store_true",
                    help="Skip make deploy_start (cluster already running)")
    args = ap.parse_args()

    results_dir = Path(args.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)
    LOG.info("Results dir: %s", results_dir)

    sidecar_log = results_dir / "sidecar_metrics.jsonl"
    sidecar = start_sidecar(sidecar_log)

    try:
        if not args.skip_deploy:
            cluster_init()
            cluster_deploy()
            cluster_start()

        if not wait_for_cluster_health(timeout=180):
            raise RuntimeError("Cluster never came up")

        # Phase 0
        if args.skip_phase0 > 0:
            chosen_senders = args.skip_phase0
            LOG.info("Skipping Phase 0; using num_senders=%d", chosen_senders)
        else:
            best = phase0(results_dir)
            if best is None:
                LOG.error("Aborting: Phase 0 found no usable senders config")
                return 2
            chosen_senders = best.senders

        # Phase 1: default config baseline
        phase1_results = phase_n_targets("phase1", chosen_senders, PHASE1_DURATION, results_dir)

        # Phase 2: stop, patch yamls, restart, rerun 4 targets
        LOG.info("Stopping cluster for yaml patch...")
        cluster_stop()
        patch_all_yamls()
        LOG.info("Restarting cluster post-patch...")
        cluster_start()
        if not wait_for_cluster_health(timeout=180):
            raise RuntimeError("Cluster did not return healthy after patch")

        phase2_results = phase_n_targets("phase2", chosen_senders, PHASE2_DURATION, results_dir)

        # Save phase results as a compact json for the aggregator
        (results_dir / "phase_summary.json").write_text(json.dumps({
            "senders": chosen_senders,
            "phase1": {k: v.__dict__ for k, v in phase1_results.items()},
            "phase2": {k: v.__dict__ for k, v in phase2_results.items()},
        }, indent=2))
        LOG.info("Wrote phase_summary.json")

    finally:
        stop_sidecar(sidecar)
        # Don't auto-stop the cluster — leave it for inspection
        LOG.info("Done. Run scripts/pfn_pressure_aggregate.py %s for the final report.", results_dir)


if __name__ == "__main__":
    sys.exit(main() or 0)
