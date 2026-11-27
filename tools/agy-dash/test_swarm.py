"""Tests for agy_dash.swarm (ranked catalog, ping badges, pick fallback, active model persistence)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agy_dash import swarm  # noqa: E402


def _fake_ext(tmp_path: Path, catalog_rows: list[dict], ping: list[dict] | None = None) -> tuple[str, Path]:
    log = tmp_path / "argv.log"
    cat = tmp_path / "cat.json"
    cat.write_text(json.dumps(catalog_rows), encoding="utf-8")
    pong = tmp_path / "ping.json"
    pong.write_text(json.dumps(ping if ping is not None else []), encoding="utf-8")
    exe = tmp_path / "swarm-ext"
    exe.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> "{log}"\n'
        f'case "$1" in catalog) cat "{cat}";; ping) cat "{pong}";; esac\n',
        encoding="utf-8",
    )
    exe.chmod(0o755)
    return str(exe), log


ROWS = [
    {"provider": "agy", "spec": "agy:gemini-4-neon-medium", "label": "[AGY] Gemini 4 Neon (Medium)", "rank": None, "recommended": False},
    {"provider": "nvapi", "spec": "nvapi:n2", "label": "[NVAPI] n2", "rank": 2, "recommended": False,
     "score": 0.4, "stats": {"last_ping_ok": True, "latency_s": 3.0}},
    {"provider": "openisy", "spec": "openisy:opencode/big-pickle", "label": "[OpenCode Zen] big-pickle · GRATIS",
     "rank": 1, "recommended": True, "score": 0.9, "free": True,
     "stats": {"last_ping_ok": True, "latency_s": 15.6}},
]


def test_catalog_fallback_without_swarm_ext() -> None:
    rows, note = swarm.catalog(swarm_ext="")
    assert rows and all(r["provider"] == "agy" for r in rows)
    assert "swarm-ext" in note


def test_catalog_asks_all_providers_and_rank(tmp_path: Path) -> None:
    exe, log = _fake_ext(tmp_path, ROWS)
    rows, note = swarm.catalog(swarm_ext=exe, rank="fix a backend bug")
    argv = log.read_text(encoding="utf-8")
    assert note == ""
    assert "--providers agy,nvapi,codex,claude,opencode,freellm,qwen" in argv
    assert "--rank fix a backend bug" in argv
    assert [r["spec"] for r in rows] == [
        "openisy:opencode/big-pickle",
        "nvapi:n2",
        "agy:gemini-4-neon-medium",
    ]


def test_badge() -> None:
    b = swarm.badge(ROWS[2])
    assert b.startswith("★ ✓") and "15.6s" in b and "0.90" in b
    assert swarm.badge({"provider": "agy", "stats": {}}).startswith("  ·")
    assert swarm.badge({"provider": "agy", "_pinging": True})[2] == "…"
    assert swarm.badge({"provider": "agy", "stats": {"last_ping_ok": False}})[2] == "✗"


def test_ping_row_and_apply(tmp_path: Path) -> None:
    exe, log = _fake_ext(tmp_path, ROWS, ping=[{"spec": "agy:a", "ok": True, "latency_s": 2.5, "err": ""}])
    r = dict(ROWS[0])
    res = swarm.ping_row(r, exe, timeout=5)
    assert res["ok"] is True and "ping --json --timeout 5 agy:gemini-4-neon-medium" in log.read_text(encoding="utf-8")
    swarm.apply_ping(r, res)
    assert r["stats"]["last_ping_ok"] is True and r["stats"]["latency_s"] == 2.5


def test_pick_ranks_each_worker_and_fallback_mentions_via(tmp_path: Path, monkeypatch) -> None:
    exe, log = _fake_ext(tmp_path, ROWS)
    monkeypatch.delenv("TMUX", raising=False)
    code, text = swarm.pick(
        [{"name": "api", "desc": "backend endpoint"}, {"name": "ui", "desc": "frontend page"}],
        popup=True,
        swarm_ext=exe,
        state_dir=tmp_path / "state",
    )
    assert code == swarm.EXIT_NO_POPUP
    argv = log.read_text(encoding="utf-8")
    assert "--rank backend endpoint" in argv and "--rank frontend page" in argv
    assert "--- api" in text and "--- ui" in text and "--via" in text
    assert "★" in text


def test_set_active_model_preserves_settings(tmp_path: Path) -> None:
    sf = tmp_path / "settings.json"
    sf.write_text(json.dumps({"verbosity": "normal", "model": "Old Model"}), encoding="utf-8")
    ok, disp = swarm.set_active_model("[AGY] Gemini 4 Hydrogen (Medium)", agy_data_dir=tmp_path)
    assert ok is True
    assert disp == "Gemini 4 Hydrogen (Medium)"
    saved = json.loads(sf.read_text(encoding="utf-8"))
    assert saved["model"] == "Gemini 4 Hydrogen (Medium)"
    assert saved["verbosity"] == "normal"


def test_swarm_picker_has_no_raw_addnstr() -> None:
    src = (Path(__file__).resolve().parent / "agy_dash" / "swarm.py").read_text(encoding="utf-8")
    assert "addnstr(" not in src and "addstr(" not in src
