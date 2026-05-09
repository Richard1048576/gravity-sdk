"""Unit tests for the aggregate-report parser."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from pfn_pressure_aggregate import build_summary_table


def test_build_summary_table_simple_case():
    summary = {
        "senders": 200,
        "phase1": {
            "node1": {"senders": 200, "tps": 8050.0, "success_pct": 99.5, "timed_out": 30},
            "vfn1":  {"senders": 200, "tps": 5500.0, "success_pct": 99.0, "timed_out": 50},
        },
        "phase2": {
            "node1": {"senders": 200, "tps": 8100.0, "success_pct": 99.7, "timed_out": 25},
            "vfn1":  {"senders": 200, "tps": 7800.0, "success_pct": 99.6, "timed_out": 28},
        },
    }
    md = build_summary_table(summary)
    # Check key facts present
    assert "| node1 | 8050.0 | 8100.0 |" in md
    assert "| vfn1  | 5500.0 | 7800.0 |" in md
    assert "x1.42" in md or "1.42x" in md   # vfn1 uplift ratio
