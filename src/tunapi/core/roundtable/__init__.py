"""Roundtable: sequential multi-agent opinion collection in a thread.

Package split out of the former single-module ``core/roundtable.py``; the
public API is re-exported here so existing imports keep working.
"""

from __future__ import annotations

from .commands import handle_rt, parse_followup_args, parse_rt_args
from .orchestrator import run_followup_round, run_roundtable
from .prompt import _MAX_ANSWER_LENGTH, _build_round_prompt
from .session import RoundtableBridgeCfg, RoundtableSession, RoundtableStore

__all__ = [
    "RoundtableBridgeCfg",
    "RoundtableSession",
    "RoundtableStore",
    "parse_rt_args",
    "parse_followup_args",
    "handle_rt",
    "run_roundtable",
    "run_followup_round",
    "_build_round_prompt",
    "_MAX_ANSWER_LENGTH",
]
