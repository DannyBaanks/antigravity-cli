"""Tests for the non-programmer-friendly UX improvements in agy_dash.tui."""
from __future__ import annotations

import curses
import datetime as dt
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agy_dash.tui as tui  # noqa: E402
from agy_dash import procs, sessions  # noqa: E402
from test_tui import FakeWin  # noqa: E402


def _make_session(
    cid: str,
    title: str,
    *,
    live: bool = False,
    running: bool = False,
    hibernated: bool = False,
    workspace: str = "/home/danny/Development/ISyCo",
    last_modified_ts: float = 0.0,
) -> sessions.AgySession:
    proc = (
        procs.ProcMemInfo(pid=1234, ppid=1, tty="pts/1", rss_kb=100_000, pss_kb=80_000, in_dash=True)
        if live
        else None
    )
    return sessions.AgySession(
        conversation_id=cid,
        title=title,
        preview="",
        step_count=12,
        last_modified="",
        workspace=workspace,
        db_status="CASCADE_RUN_STATUS_RUNNING" if running else "CASCADE_RUN_STATUS_IDLE",
        not_fully_idle=False,
        proc=proc,
        hibernated=hibernated,
        dash_win_idx="1" if live else None,
        last_modified_ts=last_modified_ts,
    )


class FriendlyHelpersTests(unittest.TestCase):
    def test_format_relative_age(self) -> None:
        now = dt.datetime(2026, 11, 25, 12, 0, 0).timestamp()
        self.assertEqual(tui.format_relative_age(0.0, now), "-")
        self.assertEqual(tui.format_relative_age(now - 20, now), "ahora")
        self.assertEqual(tui.format_relative_age(now - 15 * 60, now), "hace 15m")
        self.assertEqual(tui.format_relative_age(now - 3 * 3600, now), "hace 3h")
        self.assertEqual(tui.format_relative_age(now - 30 * 3600, now), "ayer")
        self.assertEqual(tui.format_relative_age(now - 4 * 86400, now), "hace 4d")
        old = dt.datetime(2026, 11, 10, 9, 0, 0).timestamp()
        self.assertEqual(tui.format_relative_age(old, now), "10 nov")

    def test_friendly_state_workspace_and_tools(self) -> None:
        self.assertEqual(tui.friendly_state_label("RUNNING"), "● Activo")
        self.assertEqual(tui.friendly_state_label("IDLE"), "○ En espera")
        self.assertEqual(tui.friendly_state_label("SAVED"), "📁 Guardado")
        self.assertEqual(tui.friendly_workspace("cwd"), "-")
        self.assertEqual(tui.friendly_workspace("/home/danny/ISyCo"), "ISyCo")
        self.assertIn("Editando", tui.friendly_tool_activity(["edit_file(/tmp/a.py)"]))
        self.assertIn("Revisando", tui.friendly_tool_activity(["read_file(/tmp/a.py)"]))
        self.assertIn("tarea", tui.friendly_tool_activity(["run_command(ls)"]))


class FriendlyDashboardTests(unittest.TestCase):
    def setUp(self) -> None:
        for p in (
            mock.patch.object(tui.curses, "color_pair", side_effect=lambda n: n << 8),
            mock.patch.object(tui.curses, "flushinp"),
            mock.patch.object(tui.Dashboard, "_prepare_frame", lambda self, now: None),
            mock.patch.object(tui._compact, "list_compacted_vault", return_value=[]),
            mock.patch.object(tui._sessions, "transcript_sig", return_value=(0, 0)),
            mock.patch.object(tui._sessions, "zig_tail_transcript", return_value=[]),
            mock.patch.object(tui._compact, "load_nvapi_config", return_value={}),
        ):
            p.start()
            self.addCleanup(p.stop)

    def test_default_is_friendly_and_T_toggles_technical_mode(self) -> None:
        win = FakeWin(30, 130)
        d = tui.Dashboard(win)
        now = dt.datetime(2026, 11, 25, 12, 0, 0).timestamp()
        d.sessions = [
            _make_session("s1", "Mi Plática Activa", live=True, running=True, last_modified_ts=now - 10),
            _make_session("s2", "Mi Plática Guardada", live=False, last_modified_ts=now - 3600),
        ]
        d.live = [d.sessions[0]]
        with mock.patch.object(d, "cached_context", return_value={"level": "COMPACT_URGENT", "pct": 34.0, "used_tokens": 340_000}):
            self.assertFalse(d.tech_mode)
            d.draw(now=now)
            friendly_text = "\n".join(t for _, _, t in win.writes)
            self.assertIn("CONVERSACIÓN", friendly_text)
            self.assertIn("● Activo", friendly_text)
            self.assertIn("📁 Guardado", friendly_text)
            self.assertIn("Abiertos ahora (1)", friendly_text)
            self.assertIn("Guardados recientes (1)", friendly_text)
            self.assertNotIn("PSS", friendly_text)
            self.assertNotIn("COMPACT_URGENT", friendly_text)
            self.assertIn("ctx 34%", friendly_text)

            # Toggle to technical mode with T
            d.handle_key(ord("T"))
            self.assertTrue(d.tech_mode)
            d.draw(now=now)
            tech_text = "\n".join(t for _, _, t in win.writes)
            self.assertIn("PSS", tech_text)
            self.assertIn("COMPACT_URGENT", tech_text)
            self.assertIn("TÉCNICO", tech_text)

    def test_preview_uses_local_time_and_human_tool_labels_in_friendly_mode(self) -> None:
        win = FakeWin(30, 130)
        d = tui.Dashboard(win)
        now = dt.datetime(2026, 11, 25, 12, 0, 0).timestamp()
        s = _make_session("s1", "Chat De Prueba", live=True, running=True, last_modified_ts=now - 5)
        d.sessions = [s]
        d.cached_preview_cid = "s1"
        d.cached_preview_sig = (0, 0)
        d.cached_turns = [
            {"idx": 10, "role": "USER", "status": "DONE", "content": "hola ayuda", "tools": [], "ts": "17:55:00", "ts_local": "11:55"},
            {"idx": 11, "role": "AGY", "status": "DONE", "content": "", "tools": ["edit_file(/tmp/secret.py)"], "ts": "17:55:10", "ts_local": "11:55"},
            {"idx": 12, "role": "TOOL", "status": "DONE", "content": "Created At: ... task id: 1234", "tools": [], "ts": "17:55:20", "ts_local": "11:55"},
        ]
        with mock.patch.object(d, "cached_context", return_value={"level": "OK", "pct": 10.0, "used_tokens": 5000}):
            d.draw(now=now)
            out = "\n".join(t for _, _, t in win.writes)
            self.assertIn("[11:55] Tú: hola ayuda", out)
            self.assertIn("Editando archivos", out)
            self.assertIn("Ejecutando una tarea en segundo plano", out)
            self.assertNotIn("/tmp/secret.py", out)
            self.assertNotIn("#10", out)

    def test_search_filter_and_mouse_click_with_section_dividers(self) -> None:
        win = FakeWin(30, 130)
        d = tui.Dashboard(win)
        now = dt.datetime(2026, 11, 25, 12, 0, 0).timestamp()
        d.sessions = [
            _make_session("a1", "Riemann Discovery", live=True, running=True, last_modified_ts=now - 10),
            _make_session("s1", "Arreglar CI Móvil", live=False, last_modified_ts=now - 100),
            _make_session("s2", "Revisión Cámaras", live=False, last_modified_ts=now - 200),
        ]
        with mock.patch.object(d, "cached_context", return_value={"level": "OK", "pct": 10.0, "used_tokens": 5000}):
            d.draw(now=now)
            # Row 6 = header "Abiertos ahora (1)", row 7 = a1 (idx 0), row 8 = header "Guardados recientes (2)", row 9 = s1 (idx 1)
            self.assertNotIn(6, d.row_y_to_session_idx)
            self.assertEqual(d.row_y_to_session_idx[7], 0)
            self.assertNotIn(8, d.row_y_to_session_idx)
            self.assertEqual(d.row_y_to_session_idx[9], 1)

            # Clicking header row 8 does NOT change cursor; clicking row 9 selects s1 (idx 1)
            with mock.patch.object(tui.curses, "getmouse", return_value=(0, 5, 8, 0, curses.BUTTON1_CLICKED)):
                d.handle_mouse()
            self.assertEqual(d.cursor, 0)
            with mock.patch.object(tui.curses, "getmouse", return_value=(0, 5, 9, 0, curses.BUTTON1_CLICKED)):
                d.handle_mouse()
            self.assertEqual(d.cursor, 1)

            # Search with `/` filters visible sessions; empty Enter clears filter
            with mock.patch.object(tui, "_prompt_curses_text", return_value="cámaras"):
                d.handle_key(ord("/"))
            self.assertEqual(d.search_query, "cámaras")
            self.assertEqual([s.conversation_id for s in d._visible_sessions()], ["s2"])
            self.assertEqual(d.current_session().conversation_id, "s2")

            with mock.patch.object(tui, "_prompt_curses_text", return_value=""):
                d.handle_key(ord("/"))
            self.assertEqual(d.search_query, "")
            self.assertEqual(len(d._visible_sessions()), 3)


if __name__ == "__main__":
    unittest.main()
