#!/usr/bin/env python3
"""agy_dashboard.py (agy-dash) — Hybrid Zig+Python Single-Window Command Center for AGY.

Architecture (informed by REA inspection of Grok `workspace.db` & `xai-grok-pager`,
Codex `app-server`, and AGY `conversations/*.db`):
- Zig Fast Core (`tools/agy_limits/agy-fast-core`, Zig 0.16 ReleaseFast):
  * `scan`: scans `/proc`, `smaps_rollup`, `presence/*.lock`, child trees, and KSM in ~7 ms.
  * `statusbar`: renders tmux top bar natively in Zig.
  * `tail-lines <transcript.jsonl> <N>`: reverse-seeks the last 256 KB in <0.2 ms.
  * `ksm-exec <agy> ...`: sets `prctl(PR_SET_MEMORY_MERGE, 1)` and `execvp`s `agy`.
- Python Command Center (`agy_dashboard.py`):
  * Dual-pane `0:⚡DASH` with clickable Mouse + Keyboard Command Center buttons on the right.
  * Real-time updates (~0.5s stat()-gated polling + manual `[r] Actualizar` button/key).
  * `<3 MB` Real-Time Live Mirror tabs for sessions running in external consoles (zero
    `⚠ Conversation already open` duplicates) + Safe Auto-Hibernate to `0 MB RAM`.
"""
from __future__ import annotations

# Shim: the code lives in the agy_dash package next to this file. This module forwards every
# attribute read/write/delete to the owning submodule, so `ad.BRAIN_DIR = x` and
# `mock.patch.object(ad, "refresh_session")` keep reaching all callers. Tabs, hooks,
# ~/.local/bin/agy-dash and agy-limited still exec this file (paths.SELF_PATH).
import importlib
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agy_dash  # noqa: E402

_MODS = {m: importlib.import_module(f"agy_dash.{m}") for m in set(agy_dash.OWNER.values())}
_OWNER = {name: _MODS[m] for name, m in agy_dash.OWNER.items()}


class _ForwardingModule(types.ModuleType):
    def __getattr__(self, name: str):
        mod = _OWNER.get(name)
        if mod is None:
            raise AttributeError(f"module {self.__name__!r} has no attribute {name!r}")
        return getattr(mod, name)

    def __setattr__(self, name: str, value) -> None:
        mod = _OWNER.get(name)
        if mod is not None:
            setattr(mod, name, value)
        else:
            super().__setattr__(name, value)

    def __delattr__(self, name: str) -> None:
        mod = _OWNER.get(name)
        if mod is not None and name not in self.__dict__:
            delattr(mod, name)
        else:
            super().__delattr__(name)

    def __dir__(self):
        return sorted(set(super().__dir__()) | set(_OWNER))


sys.modules[__name__].__class__ = _ForwardingModule

if __name__ == "__main__":
    raise SystemExit(_OWNER["main"].main())
