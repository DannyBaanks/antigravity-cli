"""EGM ronda 3 sobre agy_dash/tui.py: un test por hallazgo arreglado."""
from __future__ import annotations

import curses
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agy_dash.tui as tui  # noqa: E402
from test_tui import FakeWin  # noqa: E402
from test_tui_round2 import _sess  # noqa: E402


class Round3(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="agy-dash-r3-"))
        for p in (mock.patch.object(tui.curses, "color_pair", side_effect=lambda n: n << 8),
                  mock.patch.object(tui.curses, "flushinp"),
                  mock.patch.object(tui._compact, "list_compacted_vault", return_value=[]),
                  mock.patch.object(tui, "CRASH_LOG", self.tmp / "tui-errors.log")):
            p.start()
            self.addCleanup(p.stop)
        self.d = tui.Dashboard(FakeWin(30, 120))

    def _mouse(self, x: int, y: int, bstate: int):
        with mock.patch.object(tui.curses, "getmouse", return_value=(0, x, y, 0, bstate)):
            return self.d.handle_mouse()

    def _publish(self, sessions) -> None:
        self.d.scan_box = {"data": (0, {}, sessions), "failed": False, "error": "",
                           "ver": self.d.scan_box.get("ver", 0) + 1}
        self.d._adopt_scan_snapshot()

    # Blocker: Baton Pass mata el proceso de la sesión; con el escaneo fallido no corre
    def test_baton_is_blocked_while_scan_failed_in_both_views(self) -> None:
        self.d.sessions = [_sess(1)]
        self.d.scan_failed = True
        with mock.patch.object(tui._accounts, "baton_pass_session") as bp:
            self.assertEqual(self.d.execute_action("baton"), self.d.SCAN_BLOCKED_MSG)
            self.d.mode = "accounts"
            self.d.reg = {"accounts": {"primary": {}, "pro2": {}}}
            self.d.cursor = 1
            self.d.handle_key(ord("B"))
        bp.assert_not_called()
        self.assertEqual(self.d.status_banner, self.d.SCAN_BLOCKED_MSG)

    def test_account_removal_is_blocked_while_scan_failed(self) -> None:
        self.d.mode = "accounts"
        self.d.reg = {"accounts": {"pro2": {}}}
        self.d.pending_delete_info = {"slot": "pro2", "blocked_reason": ""}
        self.d.scan_failed = True
        with mock.patch.object(tui._accounts, "remove_account_slot") as rm:
            self.d.handle_key(ord("S"))
        rm.assert_not_called()
        self.assertIsNone(self.d.pending_delete_info)

    # El modal actúa sobre la sesión que muestra aunque el cursor se haya movido
    def test_compaction_modal_targets_its_own_session(self) -> None:
        a, b = _sess(1), _sess(2)
        self.d.sessions = [a, b]
        self.d.pending_compact_sess = a
        self.d.cursor = 1
        with mock.patch.object(tui._tmuxctl, "open_session_in_dashboard_tab", return_value="") as op:
            self.d.handle_key(ord("o"))
        self.assertIs(op.call_args.args[0], a)

    def test_wheel_does_not_move_cursor_under_a_confirmation(self) -> None:
        self.d.sessions = [_sess(1), _sess(2)]
        self.d.pending_compact_sess = self.d.sessions[0]
        self._mouse(5, 8, getattr(curses, "BUTTON5_PRESSED", 0) or 1 << 21)
        self.assertEqual(self.d.cursor, 0)

    # La selección sigue a la conversación cuando el escaneo reordena la lista
    def test_cursor_follows_conversation_across_resort(self) -> None:
        a, b, c = _sess(1), _sess(2), _sess(3)
        self.d.scan_box = {"ver": 0}
        self._publish([a, b, c])
        self.d.cursor = 1  # b
        self._publish([c, a, b])
        self.assertEqual(self.d.sessions[self.d.cursor].conversation_id, b.conversation_id)

    # Una excepción tras reservar no deja la sesión "compactándose" para siempre
    def test_reservation_is_released_when_config_load_raises(self) -> None:
        with mock.patch.object(tui._compact, "load_nvapi_config", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                self.d._start_compaction(_sess(1))
        self.assertEqual(self.d.compact_jobs, {})

    # Un dibujo que falla no bloquea las teclas, y tras varios fallos se vuelve a sesiones
    def test_failing_draw_still_reads_keys_and_falls_back_to_sessions(self) -> None:
        self.d.mode = "vault"
        keys = iter([ord("V"), -1, -1])
        with mock.patch.object(self.d, "draw", side_effect=KeyError("x")), \
             mock.patch.object(self.d, "_advance_background"), mock.patch.object(tui.time, "sleep"), \
             mock.patch.object(self.d.stdscr, "getch", side_effect=lambda: next(keys)):
            self.d._tick()
            self.assertEqual(self.d.mode, "sessions")  # la V llegó
            self.d.mode = "memory"
            self.d._tick()
            self.d._tick()
        self.assertEqual(self.d.mode, "sessions")
        self.assertIn("error interno", self.d.status_banner)

    def test_crash_log_writes_a_repeated_traceback_once(self) -> None:
        for _ in range(3):
            try:
                raise KeyError("same")
            except KeyError as exc:
                with mock.patch.object(tui.time, "sleep"):
                    self.d._report_crash(exc)
        log = (self.tmp / "tui-errors.log").read_text(encoding="utf-8")
        self.assertEqual(log.count("\n--- ") + log.startswith("--- "), 1)

    # Activar cuenta: leer-modificar-escribir con lock, no la copia del frame
    def test_set_default_account_uses_locked_update(self) -> None:
        self.d.mode = "accounts"
        self.d.reg = {"accounts": {"primary": {}, "pro2": {}}}
        self.d.cursor = 1
        with mock.patch.object(tui._accounts, "update_accounts_registry", return_value={"active_slot": "pro2"}) as up, \
             mock.patch.object(tui._accounts, "save_accounts_registry") as save:
            self.d.handle_key(ord("s"))
        save.assert_not_called()
        reg = {"active_slot": "primary"}
        up.call_args.args[0](reg)
        self.assertEqual(reg["active_slot"], "pro2")

    # H no mata copias duplicadas; x pide confirmación
    def test_hibernate_all_does_not_close_duplicates(self) -> None:
        s = _sess(1)
        s.extra_procs = [mock.Mock(pss_kb=1)]
        self.d.sessions = [s]
        with mock.patch.object(tui._sessions, "deduplicate_session_procs") as dd, \
             mock.patch.object(tui._sessions, "close_stale_dash_duplicates") as cs, \
             mock.patch.object(tui._sessions, "hibernate_session", return_value=(False, "")):
            self.d.handle_key(ord("H"))
            dd.assert_not_called()
            cs.assert_not_called()
            self.d.handle_key(ord("x"))
            self.assertEqual(self.d._view_name(), "dedup_confirm")
            dd.assert_not_called()
            self.d.handle_key(ord("n"))
            dd.assert_not_called()
            self.assertFalse(self.d.pending_dedup)

    # Un lote no lanza más de COMPACT_PARALLEL llamadas a NVIDIA a la vez
    def test_compaction_concurrency_is_capped(self) -> None:
        live = {"now": 0, "max": 0}
        lock = threading.Lock()

        def slow(_s):
            with lock:
                live["now"] += 1
                live["max"] = max(live["max"], live["now"])
            time.sleep(0.05)
            with lock:
                live["now"] -= 1
            return True, "ok", None

        with mock.patch.object(tui._compact, "load_nvapi_config", return_value={"api_key": "nvapi-x"}), \
             mock.patch.object(tui._compact, "compact_conversation_with_nvapi", side_effect=slow):
            self.d._bulk_compact([_sess(i) for i in range(6)])
            deadline = time.time() + 3
            while time.time() < deadline and len(self.d.compact_results) < 6:
                time.sleep(0.01)
        self.assertEqual(len(self.d.compact_results), 6)
        self.assertLessEqual(live["max"], tui.COMPACT_PARALLEL)

    # El hilo LED no deja escapar excepciones (romperían la pantalla) y respeta un renombrado
    def test_led_worker_catches_errors_and_forgets_renamed_tabs(self) -> None:
        self.d.last_tab_names = {"a": "old"}
        self.d.forget_tab_names = {"a"}
        with mock.patch.object(tui._tmuxctl, "sync_led_tmux_tabs", side_effect=RuntimeError("tmux")):
            self.d._led_work([], 0)
        self.assertFalse(self.d.led_worker_busy["running"])
        self.assertIn("tmux", (self.tmp / "tui-errors.log").read_text(encoding="utf-8"))
        with mock.patch.object(tui._tmuxctl, "sync_led_tmux_tabs"):
            self.d.forget_tab_names = {"a"}
            self.d._led_work([], 0)
        self.assertNotIn("a", self.d.last_tab_names)

    # Terminal bajo: el modal no cabe, pero sus teclas se anuncian en el banner
    def test_modal_without_room_is_announced_in_banner(self) -> None:
        win = FakeWin(18, 80)
        d = tui.Dashboard(win)
        s = _sess(1)
        d.sessions = [s]
        d.pending_compact_sess = s
        with mock.patch.object(tui.Dashboard, "_prepare_frame", lambda self, now: None):
            d.draw(0.0)
        banner = "".join(t for y, _, t in win.writes if y == win.h - 2)
        self.assertIn("[c] compactar", banner)  # se dibuja en el banner (ronda 6: sin pisar status_banner)

    def test_footer_q_no_longer_says_salir(self) -> None:
        for variants in tui.Dashboard.FOOTER_VARIANTS.values():
            for v in variants:
                self.assertNotIn(("q", "salir"), v)

    def test_bulk_confirmation_accepts_lowercase(self) -> None:
        self.d.mode = "vault"
        self.d.pending_bulk_compact = [_sess(1)]
        with mock.patch.object(self.d, "_bulk_compact", return_value="ok") as bc:
            self.d.handle_key(ord("a"))
        bc.assert_called_once()

    def test_vault_recompact_matches_full_conversation_id(self) -> None:
        self.d.mode = "vault"
        near = _sess(1)
        near.conversation_id = "abc12345-other"
        self.d.sessions = [near]
        self.d.vault_entries = [{"short_id": "abc12345", "conv_id": "abc12345-real", "title": "t",
                                 "workspace": "/w", "path": "/h.md"}]
        with mock.patch.object(self.d, "_start_compaction", return_value=("started", "")) as st:
            self.d.handle_key(ord("r"))
        self.assertEqual(st.call_args.args[0].conversation_id, "abc12345-real")

    def test_nvapi_key_needs_prefix(self) -> None:
        with mock.patch.object(tui._compact, "load_nvapi_config", return_value={}), \
             mock.patch.object(tui, "_prompt_curses_text", return_value="x" * 40), \
             mock.patch.object(tui._compact, "save_nvapi_config") as save:
            msg = self.d.execute_action("nvapi_key")
        save.assert_not_called()
        self.assertIn("inválido", msg)

    # El registro de cuentas solo se relee cuando cambia su mtime
    def test_registry_is_cached_by_mtime(self) -> None:
        reg_file = self.tmp / "accounts.json"
        reg_file.write_text("{}", encoding="utf-8")
        with mock.patch.object(tui._paths, "ACCOUNTS_REGISTRY", reg_file), \
             mock.patch.object(tui._accounts, "load_accounts_registry", return_value={}) as ld, \
             mock.patch.object(tui._accounts, "resolve_model_arg", return_value=("m", "m")), \
             mock.patch.object(tui._accounts, "compute_quota_alert", return_value=None):
            self.d._prepare_frame(0.0)
            self.d._prepare_frame(0.0)
            self.assertEqual(ld.call_count, 1)
            reg_file.write_text('{"a": 1}', encoding="utf-8")
            import os
            os.utime(reg_file, ns=(1, 10**18))
            self.d._prepare_frame(0.0)
        self.assertEqual(ld.call_count, 2)


if __name__ == "__main__":
    unittest.main()
