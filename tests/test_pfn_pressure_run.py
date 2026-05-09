"""Unit tests for orchestrator pure-logic helpers."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from pfn_pressure_run import parse_bench_log, decide_best_senders, BenchResult


SAMPLE_BENCH_LOG = """\
2026-05-09T10:00:00.000000Z  INFO gravity_bench: Starting in recovery mode...
2026-05-09T10:00:01.000000Z  INFO gravity_bench: bench erc20 transfer started
2026-05-09T10:00:11.000000Z  INFO  TPS: 4523.4 | Avg Latency: 152.0ms | Success%: 99.5% | Pending Txns: 1.2K | Pool Pending: 5K | Pool Queued: 0 | Timed Out Txns: 12
2026-05-09T10:00:21.000000Z  INFO  TPS: 5012.7 | Avg Latency: 145.3ms | Success%: 99.8% | Pending Txns: 1.5K | Pool Pending: 6K | Pool Queued: 0 | Timed Out Txns: 18
2026-05-09T10:00:31.000000Z  INFO  TPS: 5234.1 | Avg Latency: 148.1ms | Success%: 99.9% | Pending Txns: 1.6K | Pool Pending: 7K | Pool Queued: 0 | Timed Out Txns: 20
"""


def test_parse_bench_log_picks_last_steady_sample():
    result = parse_bench_log(SAMPLE_BENCH_LOG)
    assert result.tps == pytest.approx(5234.1)
    assert result.success_pct == pytest.approx(99.9)
    assert result.timed_out == 20


def test_parse_bench_log_handles_no_samples():
    assert parse_bench_log("only header lines\n") is None


def test_decide_best_senders_picks_highest_tps_above_threshold():
    runs = [
        BenchResult(senders=100, tps=4500.0, success_pct=99.5, timed_out=10),
        BenchResult(senders=200, tps=7800.0, success_pct=98.0, timed_out=20),
        BenchResult(senders=400, tps=8000.0, success_pct=92.0, timed_out=200),
    ]
    chosen = decide_best_senders(runs, success_threshold=95.0)
    assert chosen.senders == 200      # 400 fails the threshold


def test_decide_best_senders_returns_none_if_all_below_threshold():
    runs = [
        BenchResult(senders=100, tps=4000.0, success_pct=80.0, timed_out=500),
        BenchResult(senders=200, tps=5000.0, success_pct=85.0, timed_out=600),
    ]
    assert decide_best_senders(runs, success_threshold=95.0) is None
