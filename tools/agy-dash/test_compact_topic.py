"""Compacted sessions get a title that says what they work on (2026-11-27)."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agy_dash import compact as cp  # noqa: E402
from agy_dash import sessions as ss  # noqa: E402
from agy_dash import tmuxctl as tc  # noqa: E402


def test_title_line_is_taken_and_removed() -> None:
    topic, rest = cp.split_topic("TÍTULO: **Hook autocompact sin duplicados**\n\n## 1. OBJETIVO\nx")
    assert topic == "Hook autocompact sin duplicados"
    assert "TÍTULO" not in rest and rest.startswith("## 1. OBJETIVO")


def test_fallback_is_the_objective_sentence() -> None:
    summary = ("# CONTEXTO\n\n## 1. OBJETIVO Y PETICIONES DEL USUARIO\n"
               "**Meta principal**: Conectar experimentos Riemann con Alpöge sin atribuir nada. Más.\n")
    topic, rest = cp.split_topic(summary)
    assert topic == "Conectar experimentos Riemann con Alpöge sin"
    assert rest == summary


def test_generic_titles() -> None:
    for t in ("Sesión 46960a2e", "🗜Sesión-46960a2e", "🗜Retomar-Context", "Sesión activa c0ffee12",
              "Retomar Contexto De Sesión", "Load Compacted Context Session", ""):
        assert cp.is_generic_title(t), t
    for t in ("malbolge", "Arreglar Conector Claude Y Pruebas Adversariales", "🧪e2e-compact"):
        assert not cp.is_generic_title(t), t


def _sess(title: str) -> ss.AgySession:
    return ss.AgySession(conversation_id="46960a2e-dd00", title=title, preview="", step_count=0,
                         last_modified="", workspace="/tmp", db_status="", not_fully_idle=False)


def test_adopt_topic_only_replaces_generic_titles(tmp_path: Path) -> None:
    with mock.patch.object(cp._paths, "CUSTOM_TITLES_FILE", tmp_path / "t.json"):
        s = _sess("Sesión 46960a2e")
        cp.adopt_topic(s, "Nombres de sesiones compactadas")
        assert s.title == "Nombres de sesiones compactadas"
        assert ss.load_custom_titles(tmp_path / "t.json")["46960a2e-dd00"] == s.title
        manual = _sess("malbolge")
        cp.adopt_topic(manual, "Otra cosa")
        assert manual.title == "malbolge"


def test_topic_round_trips_through_handoff_header(tmp_path: Path) -> None:
    h = tmp_path / "h.md"
    h.write_text("# 🗜️ Handoff\n- **Conversación original:** `x` (y)\n- **Tema:** Fix hook storm\n", encoding="utf-8")
    assert cp.topic_from_handoff(h) == "Fix hook storm"
    assert cp.topic_from_handoff(tmp_path / "missing.md") == ""


def test_led_rename_of_compacted_tab_saves_full_title(tmp_path: Path) -> None:
    with mock.patch.object(tc._paths, "COMPACTED_DIR", tmp_path), \
         mock.patch.object(tc, "tmux_cmd"), \
         mock.patch.object(tc, "session_live_activity", return_value=(False, "")):
        tab = tc._short_tab_title("Nombres de sesiones compactadas", prefix="🗜")
        tc.remember_tab_title(tab, "Nombres de sesiones compactadas")
        s = _sess("Retomar Contexto De Sesión")
        s.dash_win_idx, s.dash_win_name = "2", tab
        renames = tc.sync_led_tmux_tabs([s], 0, {"2": "old"}, {})
        assert renames == [("46960a2e-dd00", "Nombres de sesiones compactadas")]
        assert tc.take_tab_title(tab) == ""  # consumed


def _cp(rc: int = 0, out: str = ""):
    import subprocess
    return subprocess.CompletedProcess([], rc, out, "")


def test_interrupt_cut_turn_presses_esc_only_on_the_bound_busy_tab() -> None:
    calls = []

    def tmux(*a, **k):
        calls.append(a)
        return _cp(0, "46960a2e-dd00\n") if a[0] == "display-message" else _cp()
    kw = dict(settle_s=0, tmux=tmux, sleep=lambda s: None)
    assert cp.interrupt_cut_turn("46960a2e-dd00", "1", is_active=lambda c: True, **kw)
    assert calls[-1] == ("send-keys", "-t", f"{cp._paths.TMUX_SESSION}:1", "Escape")
    calls.clear()
    assert not cp.interrupt_cut_turn("46960a2e-dd00", "1", is_active=lambda c: False, **kw)  # idle
    assert not cp.interrupt_cut_turn("otra-conv", "1", is_active=lambda c: True, **kw)  # tab rebound
    assert not cp.interrupt_cut_turn("46960a2e-dd00", "", is_active=lambda c: True, **kw)  # no tab
    assert not [c for c in calls if c[0] == "send-keys"]
