"""Eval report writer + baseline diff.

The runners (run_local.py / run_agentcore.py) build a `report` dict and
hand it here. We:

  1. Load runs/baseline.json (if it exists).
  2. Compare row-by-row: which rows changed PASS↔FAIL since baseline?
  3. Print a colored diff. Regressions in red, improvements in green.
  4. Return regression count so the runner can exit non-zero on regression.

Baseline philosophy: the baseline is whatever the most recently-committed
green run was. If today's run is fully green, you'd run the runner with
`--save-baseline` to make today's results the new reference.

This file is deliberately small. Most of the eval logic lives in judge.py
and the runners; this is just the diffing surface.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = ROOT / "evals" / "runs"
BASELINE_PATH = RUNS_DIR / "baseline.json"


# ANSI color escapes — fall back to no-color if stdout isn't a TTY.
def _supports_color() -> bool:
    return sys.stdout.isatty()

_GREEN = "\033[32m" if _supports_color() else ""
_RED   = "\033[31m" if _supports_color() else ""
_GREY  = "\033[90m" if _supports_color() else ""
_BOLD  = "\033[1m"  if _supports_color() else ""
_RST   = "\033[0m"  if _supports_color() else ""


def diff_against_baseline(report: dict) -> int:
    """Compare `report` (the just-completed run) against runs/baseline.json.

    Prints a per-row diff. Returns the number of regressions (PASS→FAIL).
    A run is "green" iff regressions == 0; the runner exits non-zero
    otherwise.

    If no baseline exists yet, prints a friendly message and returns 0.
    """
    if not BASELINE_PATH.exists():
        print(f"\n{_GREY}(no baseline yet at {BASELINE_PATH} — run with "
              f"--save-baseline once you have a green run){_RST}")
        return 0

    baseline = json.loads(BASELINE_PATH.read_text())
    base_results = {r["id"]: r for r in baseline.get("results", [])}
    cur_results  = {r["id"]: r for r in report.get("results", [])}

    regressions = []
    improvements = []
    new_rows = []
    dropped_rows = []
    unchanged_pass = 0
    unchanged_fail = 0

    for rid, cur in cur_results.items():
        base = base_results.get(rid)
        if base is None:
            new_rows.append(rid)
            continue
        b_overall = base.get("overall", "FAIL")
        c_overall = cur.get("overall", "FAIL")
        if b_overall == "PASS" and c_overall == "FAIL":
            regressions.append((rid, cur))
        elif b_overall == "FAIL" and c_overall == "PASS":
            improvements.append(rid)
        elif b_overall == "PASS":
            unchanged_pass += 1
        else:
            unchanged_fail += 1

    for rid in base_results:
        if rid not in cur_results:
            dropped_rows.append(rid)

    # Print diff
    print(f"\n{_BOLD}=== Diff vs. baseline ==={_RST}")
    print(f"  baseline timestamp: {baseline.get('timestamp', '?')}")
    print(f"  current  timestamp: {report.get('timestamp', '?')}")
    print()
    print(f"  {_GREEN}Improvements (FAIL→PASS): {len(improvements)}{_RST}")
    for rid in improvements:
        print(f"    {_GREEN}↑ {rid}{_RST}")
    print()
    print(f"  {_RED}Regressions (PASS→FAIL): {len(regressions)}{_RST}")
    for rid, cur in regressions:
        # Also surface the first failing criterion to make triage faster.
        verdicts = cur.get("verdicts", {})
        first_fail = next(
            (f"{name}: {v.get('reason', '')}"
             for name, v in verdicts.items()
             if name != "overall" and v.get("verdict") == "FAIL"),
            "(no per-criterion failures captured)",
        )
        print(f"    {_RED}↓ {rid}{_RST} — {first_fail}")
    print()
    print(f"  {_GREY}Unchanged: {unchanged_pass} PASS, {unchanged_fail} FAIL{_RST}")
    if new_rows:
        print(f"  {_GREY}New rows (not in baseline): {len(new_rows)} → {new_rows}{_RST}")
    if dropped_rows:
        print(f"  {_GREY}Rows in baseline but not in current run: {dropped_rows}{_RST}")

    # Verdict
    print()
    if regressions:
        print(f"{_RED}{_BOLD}✗ {len(regressions)} regression(s) — DO NOT SHIP{_RST}")
    elif improvements:
        print(f"{_GREEN}{_BOLD}✓ {len(improvements)} improvement(s), no regressions — "
              f"if this is the new reference, rerun with --save-baseline{_RST}")
    else:
        print(f"{_GREY}✓ no change vs. baseline{_RST}")

    return len(regressions)


# ---------------------------------------------------------------------------
# Standalone CLI: re-diff an existing report against baseline without rerunning
# ---------------------------------------------------------------------------

def main():
    """Usage: python -m evals.report [path/to/report.json]

    If no path is given, diff the most recent runs/local-*.json against
    runs/baseline.json. Useful when you've already run the suite and just
    want to re-print the diff (e.g. after pulling a new baseline)."""
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("report", nargs="?",
                        help="path to a report JSON. Defaults to the most recent local-*.json.")
    args = parser.parse_args()

    if args.report:
        path = Path(args.report)
    else:
        candidates = sorted(RUNS_DIR.glob("local-*.json"))
        if not candidates:
            raise SystemExit("no runs/local-*.json found and no path given")
        path = candidates[-1]
        print(f"Re-diffing {path}")

    report = json.loads(path.read_text())
    regs = diff_against_baseline(report)
    sys.exit(1 if regs else 0)


if __name__ == "__main__":
    main()
