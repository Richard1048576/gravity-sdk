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
