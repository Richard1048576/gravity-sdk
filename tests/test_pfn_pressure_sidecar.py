"""Unit tests for the sidecar consensus-metrics parser."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from pfn_pressure_sidecar import parse_consensus_metrics


def test_parse_consensus_metrics_extracts_keys():
    text = (
        "# HELP aptos_consensus_epoch Current epoch\n"
        "# TYPE aptos_consensus_epoch gauge\n"
        "aptos_consensus_epoch 42\n"
        "aptos_consensus_current_round 1234\n"
        "aptos_consensus_last_committed_round 1230\n"
        "aptos_consensus_proposals_count 567\n"
        "some_other_metric 999\n"
    )
    result = parse_consensus_metrics(text)
    assert result == {
        "aptos_consensus_epoch": 42.0,
        "aptos_consensus_current_round": 1234.0,
        "aptos_consensus_last_committed_round": 1230.0,
        "aptos_consensus_proposals_count": 567.0,
    }


def test_parse_consensus_metrics_handles_empty():
    assert parse_consensus_metrics("# only comments\n") == {}


def test_parse_consensus_metrics_skips_labeled_variants():
    """aptos_consensus_proposals_count{role="validator"} should be skipped — we only want the bare counter."""
    text = (
        "aptos_consensus_proposals_count 567\n"
        'aptos_consensus_proposals_count{role="validator"} 100\n'
    )
    result = parse_consensus_metrics(text)
    assert result["aptos_consensus_proposals_count"] == 567.0
