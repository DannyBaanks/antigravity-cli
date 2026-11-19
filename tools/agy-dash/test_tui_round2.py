"""EGM ronda 2 sobre agy_dash/tui.py: un test por hallazgo arreglado."""
from __future__ import annotations

import curses
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agy_dash.tui as tui  # noqa: E402
from agy_dash import sessions as _sessions  # noqa: E402
from test_tui import FakeWin  # noqa: E402


def _sess(i: int) -> _sessions.AgySession:
    return _sessions.AgySession(conversation_id=f"{i:08x}-c", title=f"s{i}", preview="", step_count=1,
                                last_modified="", workspace="/w", db_status="CASCADE_RUN_STATUS_IDLE",
                                not_fully_idle=False)


class Round2(unittest.TestCase):
    def setUp(self) -> None:
        for p in (mock.patch.object(tui.curses, "color_pair", side_effect=lambda n: n << 8),
                  mock.patch.object(tui.curses, "flushinp"),
                  mock.patch.object(tui._compact, "list_compacted_vault", return_value=[])):
            p.start()
            self.addCleanup(p.stop)
        self.d = tui.Dashboard(FakeWin(30, 120))

    def _mouse(self, x: int, y: int, bstate: int = curses.BUTTON1_CLICKED):
        with mock.patch.object(tui.curses, "getmouse", return_value=(0, x, y, 0, bstate)):
            return self.d.handle_mouse()

    # Vista memoria: Esc/m vuelven, u va a cuentas (antes Esc saltaba de ventana tmux)
    def test_memory_esc_returns_and_u_opens_accounts(self) -> None:
        self.d.mode = "memory"
        with mock.patch.object(tui._tmuxctl, "tmux_cmd") as tmux:
            self.d.handle_key(27)
        tmux.assert_not_called()
        self.assertEqual(self.d.mode, "sessions")
        self.d.mode = "memory"
        with mock.patch.object(self.d, "trigger_quota_poll"):
            self.d.handle_key(ord("u"))
        self.assertEqual(self.d.mode, "accounts")

    # Backspace (127) nunca borra; x pide [S]
    def test_vault_delete_needs_confirmation_and_backspace_does_nothing(self) -> None:
        self.d.mode = "vault"
        self.d.vault_entries = [{"path": "/tmp/x.md", "title": "t", "short_id": "a"}]
        with mock.patch.object(tui._compact, "delete_vault_handoff", return_value=True) as rm:
            self.d.handle_key(127)
            rm.assert_not_called()
            self.d.handle_key(ord("x"))
            rm.assert_not_called()
            self.assertEqual(self.d._view_name(), "vault_confirm")
            self.d.handle_key(ord("n"))
            rm.assert_not_called()
            self.d.handle_key(ord("x"))
            self.d.handle_key(ord("S"))
            rm.assert_called_once()

    def test_lowercase_s_does_not_confirm_account_deletion(self) -> None:
        self.d.mode = "accounts"
        self.d.pending_delete_info = {"slot": "pro2", "blocked_reason": ""}
        with mock.patch.object(tui._accounts, "remove_account_slot", return_value=(True, "ok")) as rm:
            self.d.handle_key(ord("s"))
        rm.assert_not_called()

    # Botones que el dibujo no pintó no se disparan
    def test_hidden_button_is_not_clickable(self) -> None:
        btns = tui.build_command_buttons(40)
        last = btns[-1]
        y = 6 + last.row_offset
        self.assertEqual(tui.hit_test_button(btns, 0, 6, 2 + last.col_offset, y), last.action_id)
        self.assertIsNone(tui.hit_test_button(btns, 0, 6, 2 + last.col_offset, y, max_row=y))

    # Con la ayuda abierta un clic la cierra y no actúa debajo
    def test_click_under_help_closes_it_without_acting(self) -> None:
        self.d.show_help = True
        self.d.show_cmd_panel = True
        self.d.panel_x, self.d.list_avail = 0, 20
        self.d.cmd_buttons = tui.build_command_buttons(40)
        with mock.patch.object(self.d, "execute_action") as ex:
            self._mouse(4, 6)
        ex.assert_not_called()
        self.assertFalse(self.d.show_help)

    def test_click_under_compaction_modal_does_nothing(self) -> None:
        self.d.pending_compact_sess = _sess(1)
        self.d.show_cmd_panel = True
        self.d.panel_x, self.d.list_avail = 0, 20
        self.d.cmd_buttons = tui.build_command_buttons(40)
        with mock.patch.object(self.d, "execute_action") as ex:
            self._mouse(4, 6)
        ex.assert_not_called()

    # Clic en el Baúl con la lista desplazada selecciona la fila pintada
    def test_vault_click_respects_scroll_offset(self) -> None:
        self.d.mode = "vault"
        self.d.max_y = 15  # 6 filas visibles
        self.d.vault_entries = [{"path": f"/v{i}", "title": f"t{i}", "short_id": str(i)} for i in range(20)]
        self.d.cursor = 10  # start = 5
        self._mouse(1, 6)
        self.assertEqual(self.d.cursor, 5)

    def test_wheel_down_is_clamped(self) -> None:
        self.d.sessions = [_sess(1), _sess(2)]
        for _ in range(5):
            self._mouse(1, 7, curses.BUTTON5_PRESSED)
        self.assertEqual(self.d.cursor, 1)

    # Índice guardado fuera de rango tras encoger la lista: sin IndexError
    def test_baton_with_stale_saved_cursor_does_not_crash(self) -> None:
        self.d.mode = "accounts"
        self.d.saved_session_cursor = 9
        self.d.sessions = [_sess(1)]
        self.assertIsNone(self.d.current_session())
        self.assertEqual(self.d.execute_action("baton"), "")

    def test_scan_failed_blocks_reload_and_vault_open(self) -> None:
        self.d.scan_failed = True
        self.d.sessions = [_sess(1)]
        for act in ("reload", "open_vault"):
            self.assertIn("bloqueados", self.d.execute_action(act))
        self.assertIn("bloqueados", self.d._open_vault_entry({"path": "/x"}))

    def test_finished_compaction_does_not_open_tab_when_scan_failed(self) -> None:
        self.d.scan_failed = True
        self.d.compact_results.append((_sess(1), True, "ok", Path("/h.md"), True))
        with mock.patch.object(tui._compact, "open_compacted_session_in_tab") as op:
            self.d._process_compact_results()
        op.assert_not_called()
        self.assertIn("Baúl", self.d.status_banner)

    # La reserva del cid impide dos compactaciones de la misma sesión
    def test_compaction_reserves_before_prompting_for_key(self) -> None:
        s = _sess(1)
        seen: list[str] = []

        def prompt(*a, **k):
            seen.append(self.d.start_async_compaction(s))  # segundo disparo mientras se pide la clave
            return ""

        with mock.patch.object(tui._compact, "load_nvapi_config", return_value={}), \
             mock.patch.object(tui, "_prompt_curses_text", side_effect=prompt):
            first = self.d.start_async_compaction(s)
        self.assertIn("ya se está compactando", seen[0])
        self.assertIn("cancelada", first)
        self.assertEqual(self.d.compact_jobs, {})  # cancelar libera la reserva

    def test_api_key_prompt_is_masked(self) -> None:
        with mock.patch.object(tui._compact, "load_nvapi_config", return_value={}), \
             mock.patch.object(tui, "_prompt_curses_text", return_value="") as pr:
            self.d.execute_action("nvapi_key")
        self.assertTrue(pr.call_args.kwargs.get("secret"))

    def test_bulk_compact_asks_first_and_skips_existing_handoffs(self) -> None:
        self.d.mode = "vault"
        a, b = _sess(1), _sess(2)
        self.d.sessions = [a, b]
        with mock.patch.object(tui._compact, "should_offer_compaction", return_value=(True, 1, 1)), \
             mock.patch.object(tui._compact, "get_vault_handoff", side_effect=lambda cid: Path("/h") if cid == a.conversation_id else None), \
             mock.patch.object(self.d, "_start_compaction", return_value=("started", "")) as st:
            self.d.handle_key(ord("A"))
            st.assert_not_called()
            self.assertEqual(self.d._view_name(), "bulk_confirm")
            self.d.handle_key(ord("A"))
        st.assert_called_once()
        self.assertIs(st.call_args.args[0], b)

    # Cuota que falla: no se reintenta en cada frame
    def test_failing_quota_worker_sets_last_poll(self) -> None:
        with mock.patch.object(tui._accounts, "load_accounts_registry", side_effect=OSError("x")):
            self.d._quota_worker()
        self.assertGreater(self.d.quota_worker_state["last_poll"], 0)
        self.assertFalse(self.d.quota_worker_state["running"])
        self.assertIn("OSError", self.d.quota_worker_state["msg"])

    # Escáner que falla: visible y bloquea acciones; no tumba el arranque
    def test_scanner_error_marks_scan_failed(self) -> None:
        with mock.patch.object(tui._sessions, "load_sessions", side_effect=RuntimeError("db locked")), \
             mock.patch.object(tui.threading, "Thread"):
            self.d._start_scanner()
        self.d._adopt_scan_snapshot()
        self.assertTrue(self.d.scan_failed)
        self.assertIn("db locked", self.d.scan_error)

    def test_a_crashing_frame_does_not_kill_the_dashboard(self) -> None:
        calls = {"n": 0}

        def tick():
            calls["n"] += 1
            if calls["n"] == 1:
                raise KeyError("level")
            raise SystemExit

        with mock.patch.object(self.d, "_configure_curses"), mock.patch.object(self.d, "_start_scanner"), \
             mock.patch.object(tui._accounts, "load_accounts_registry", return_value={}), \
             mock.patch.object(self.d, "_tick", side_effect=tick), mock.patch.object(tui.time, "sleep"), \
             mock.patch.object(tui, "CRASH_LOG", Path("/tmp/claude-agy-dash-test-crash.log")):
            with self.assertRaises(SystemExit):
                self.d.run()
        self.assertEqual(calls["n"], 2)
        self.assertIn("error interno", self.d.status_banner)

    def test_duplicate_notice_shows_even_with_a_banner(self) -> None:
        s = _sess(1)
        s.extra_procs = [mock.Mock(pss_kb=2048)]
        self.d.sessions = [s]
        self.d.last_auto_check = 0
        self.d._periodic_idle_check(100.0)
        self.assertIn("2 veces", self.d.dup_notice)

    def test_narrow_table_keeps_title_and_drops_ram_columns(self) -> None:
        win = FakeWin(24, 60)
        d = tui.Dashboard(win)
        d.max_y, d.max_x = 24, 60
        d._compute_layout()
        s = _sess(1)
        s.title = "Título largo de sesión que no cabe"
        d.sessions = [s]
        d._draw_session_list()
        hdr = next(t for y, _, t in win.writes if y == 4)
        self.assertNotIn("PSS", hdr)
        row = next(t for y, _, t in win.writes if y == 6)
        self.assertIn("Título largo", row)


if __name__ == "__main__":
    unittest.main()
