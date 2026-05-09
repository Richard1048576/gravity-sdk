"""Aggregate Phase 1 / Phase 2 bench results into a comparison summary md.

Usage:
    pfn_pressure_aggregate.py /tmp/pfn_pressure_results_<TS>/
"""
import argparse
import json
import sys
from pathlib import Path
from typing import Dict, Any

TARGETS_ORDER = ("node1", "vfn1", "pfn1", "pfn3")


def build_summary_table(summary: dict) -> str:
    """Render a markdown comparison table from phase_summary.json content."""
    senders = summary["senders"]
    p1 = summary.get("phase1", {})
    p2 = summary.get("phase2", {})
    lines = []
    lines.append(f"# pfn_chain VFN Pressure Test — Summary")
    lines.append("")
    lines.append(f"**Sender concurrency**: {senders}  ")
    lines.append(f"**Target tps (input)**: 8000  ")
    lines.append("")
    lines.append("## TPS comparison (Phase 1 default vs Phase 2 mempool patch)")
    lines.append("")
    lines.append("| target | phase1 tps | phase2 tps | uplift |")
    lines.append("|---|---|---|---|")
    for tid in TARGETS_ORDER:
        r1 = p1.get(tid, {})
        r2 = p2.get(tid, {})
        t1 = r1.get("tps")
        t2 = r2.get("tps")
        if t1 is not None and t2 is not None and t1 > 0:
            ratio = t2 / t1
            uplift = f"x{ratio:.2f}"
        else:
            uplift = "-"
        # Pad short ids for alignment
        padded = tid.ljust(5)
        c1 = f"{t1:.1f}" if t1 is not None else "-"
        c2 = f"{t2:.1f}" if t2 is not None else "-"
        lines.append(f"| {padded} | {c1} | {c2} | {uplift} |")
    lines.append("")
    lines.append("## Hypothesis verdict (per spec §5.2)")
    lines.append("")
    p1_node1 = p1.get("node1", {}).get("tps")
    p1_vfn1 = p1.get("vfn1", {}).get("tps")
    if p1_node1 and p1_vfn1:
        ratio = p1_vfn1 / p1_node1
        if p1_node1 >= 7200 and p1_vfn1 <= 6500 and ratio < 0.85:
            verdict = "✅ Phase 1 confirms the assumption: vfn1 << node1 under default config"
        elif ratio > 0.9:
            verdict = "❌ Phase 1 does NOT reproduce the gap — investigate sender count / target_tps"
        else:
            verdict = "⚠ Phase 1 partial — gap exists but smaller than mainnet"
        lines.append(f"- Phase 1: {verdict}")
    p2_vfn1 = p2.get("vfn1", {}).get("tps")
    if p1_vfn1 and p2_vfn1:
        uplift = (p2_vfn1 / p1_vfn1) - 1
        if p2_vfn1 >= 7000 and uplift >= 0.25:
            verdict = "✅ Phase 2 confirms the fix: vfn1 reaches near-validator throughput"
        elif uplift < 0.1:
            verdict = "❌ Phase 2 patch ineffective — other bottleneck dominates"
        else:
            verdict = f"⚠ Phase 2 partial — vfn1 gained {uplift:.1%} but not all the way"
        lines.append(f"- Phase 2: {verdict}")
    lines.append("")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("results_dir")
    args = ap.parse_args()

    results_dir = Path(args.results_dir)
    summary_path = results_dir / "phase_summary.json"
    if not summary_path.exists():
        print(f"ERROR: {summary_path} not found", file=sys.stderr)
        sys.exit(1)

    summary = json.loads(summary_path.read_text())
    md = build_summary_table(summary)
    out = results_dir / "summary.md"
    out.write_text(md)
    print(f"Wrote {out}")
    print(md)


if __name__ == "__main__":
    main()
