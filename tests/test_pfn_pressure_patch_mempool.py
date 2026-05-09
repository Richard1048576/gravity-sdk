"""Unit tests for the mempool yaml patcher."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from pfn_pressure_patch_mempool import patch_yaml, MEMPOOL_PATCH


def test_patch_adds_missing_keys(tmp_path):
    yaml_path = tmp_path / "validator.yaml"
    yaml_path.write_text(
        "mempool:\n"
        "  capacity_per_user: 20000\n"
        "logger:\n"
        "  level: INFO\n"
    )

    changed = patch_yaml(yaml_path)
    assert changed is True

    content = yaml_path.read_text()
    assert "shared_mempool_max_concurrent_inbound_syncs: 16" in content
    assert "shared_mempool_batch_size: 1000" in content
    assert "max_broadcasts_per_peer: 50" in content
    assert "capacity_per_user: 20000" in content    # preserved


def test_patch_is_idempotent(tmp_path):
    yaml_path = tmp_path / "validator.yaml"
    yaml_path.write_text("mempool:\n  capacity_per_user: 20000\n")

    patch_yaml(yaml_path)
    changed = patch_yaml(yaml_path)
    assert changed is False


def test_patch_creates_mempool_block_if_missing(tmp_path):
    yaml_path = tmp_path / "validator.yaml"
    yaml_path.write_text("logger:\n  level: INFO\n")

    changed = patch_yaml(yaml_path)
    assert changed is True

    content = yaml_path.read_text()
    assert "mempool:" in content
    assert "shared_mempool_max_concurrent_inbound_syncs: 16" in content
