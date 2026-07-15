"""Shared utility functions for the agent package.

Extracted to avoid duplication between nodes_memory.py and nodes_listen.py.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)


def allowed_regulation_tokens() -> str:
    """Return the comma-joined regulation enum for the fact extractor prompts.

    Sourced from mcp_server/data/fsi_compliance.json keys at module load so the
    extractor enum and the answerable set stay in lockstep. Falls back to the
    canonical 7 if the data file isn't readable from the runtime path.
    """
    canonical = ["SOX", "PCI_DSS", "FDIC", "OCC", "GLBA", "FFIEC", "FINRA_17a-4"]
    try:
        candidates = [
            Path(__file__).resolve().parent.parent / "mcp_server" / "data" / "fsi_compliance.json",
            Path("/var/task") / "data" / "fsi_compliance.json",
            Path("/var/task") / "fsi_compliance.json",
        ]
        for p in candidates:
            if p.is_file():
                with p.open() as f:
                    data = json.load(f)
                regs = list((data.get("regulations") or {}).keys())
                if regs:
                    return ", ".join(regs)
                break
    except Exception:
        pass
    return ", ".join(canonical)


def expand_numeric_shorthand(s: str) -> str:
    """Expand K/M/B shorthand in a string: '40k' → '40000', '1.5M' → '1500000'."""
    def _repl(m):
        num = float(m.group(1))
        mult = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}[m.group(2).lower()]
        return str(int(num * mult))
    return re.sub(r"(\d+(?:\.\d+)?)\s*([kmb])\b", _repl, s, flags=re.IGNORECASE)


def normalize_for_quote_match(s: str) -> str:
    """Normalize a string for quote/value grounding comparison.

    Lowercase, collapse whitespace, strip quotes, expand numeric shorthand,
    drop common separators.
    """
    if not s:
        return ""
    s = s.strip().lower().strip("\"'")
    s = expand_numeric_shorthand(s)
    s = re.sub(r"[,.\-_/()\[\]]", "", s)
    return re.sub(r"\s+", " ", s).strip()
