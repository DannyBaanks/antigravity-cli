"""EGM ronda 4 sobre agy_dash/tui.py (+ tmuxctl/sessions/cli): un test por hallazgo arreglado."""
from __future__ import annotations

import argparse
import inspect
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agy_dash.cli as cli  # noqa: E402
import agy_dash.procs as procs  # noqa: E402
import agy_dash.sessions as sessions  # noqa: E402
import agy_dash.tmuxctl as tmuxctl  # noqa: E402
import agy_dash.tui as tui  # noqa: E402
from test_tui import FakeWin  # noqa: E402
from test_tui_round2 import _sess  # noqa: E402


def _live(i: int, pss_kb: int = 0) -> sessions.AgySession:
    s = _sess(i)
    s.proc = procs.ProcMemInfo(pid=900000 + i, pss_kb=pss_kb, start_ticks=1)
    return s


class Round4(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="agy-dash-r4-"))
        for p in (mock.patch.object(tui.curses, "color_pair", side_effect=lambda n: n << 8),
                  mock.patch.object(tui.curses, "flushinp"),
                  mock.patch.object(tui._compact, "list_compacted_vault", return_value=[]),
                  mock.patch.object(tui, "CRASH_LOG", self.tmp / "tui-errors.log"),
                  mock.patch.dict(procs.SCAN_STATE, {"failed": False, "reason": ""})):
            p.start()
            self.addCleanup(p.stop)
        self.d = tui.Dashboard(FakeWin(30, 120))

    # (1) tmux colgado: tmux_cmd vuelve con rc 124 en vez de bloquear el escáner...
    def test_tmux_cmd_timeout_returns_124_and_counts(self) -> None:
        before = tmuxctl.TMUX_STATE["timeouts"]
        with mock.patch.object(tmuxctl.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired(["tmux"], 2.0)) as run:
            cp = tmuxctl.tmux_cmd("list-windows", capture=True)
        self.assertEqual(cp.returncode, 124)
        self.assertEqual(run.call_args.kwargs["timeout"], tmuxctl.TMUX_TIMEOUT_SEC)
        self.assertEqual(tmuxctl.TMUX_STATE["timeouts"], before + 1)

    # ...y load_sessions marca el escaneo como fallido (lo que bloquea matar/abrir)
    def test_tmux_timeout_during_scan_marks_scan_failed(self) -> None:
        with mock.patch.object(sessions._procs, "zig_scan", return_value=(0, {}, {})), \
             mock.patch.object(tmuxctl.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired(["tmux"], 2.0)), \
             mock.patch.object(sessions, "_all_summaries_dbs", return_value=[]), \
             mock.patch.object(sessions, "load_hibernated_registry", return_value={}), \
             mock.patch.object(sessions, "load_custom_titles", return_value={}):
            sessions.load_sessions(limit=5)
        self.assertTrue(procs.scan_failed())
        self.assertIn("tmux", str(procs.SCAN_STATE["reason"]))

    # (2) la causa del fallo del escaneo llega al banner
    def test_scan_reason_reaches_scan_error(self) -> None:
        def fake_load(limit):
            procs._mark_scan(True, "agy-fast-core scan rc=3")
            return (0, {}, [_sess(1)])
        with mock.patch.object(tui._sessions, "load_sessions", side_effect=fake_load):
            self.d._scan_once()
        self.d._adopt_scan_snapshot()
        self.assertTrue(self.d.scan_failed)
        self.assertEqual(self.d.scan_error, "agy-fast-core scan rc=3")
        with mock.patch.object(tui._sessions, "load_sessions", return_value=(0, {}, [_sess(1)])):
            procs._mark_scan(False)
            self.d._scan_once()
        self.d._adopt_scan_snapshot()
        self.assertEqual((self.d.scan_failed, self.d.scan_error), (False, ""))

    # (3) el hilo LED no muta las sesiones ni escribe el título: lo devuelve...
    def test_led_sync_returns_renames_without_mutating(self) -> None:
        s = _sess(1)
        s.dash_win_idx, s.dash_win_name = "3", "mi-nombre"
        with mock.patch.object(tmuxctl._sessions, "save_custom_title") as save, \
             mock.patch.object(tmuxctl, "tmux_cmd") as tc, \
             mock.patch.object(tmuxctl, "session_live_activity", return_value=(False, "IDLE")):
            renames = tmuxctl.sync_led_tmux_tabs([s], 0, {"3": "●s1"}, {})
        save.assert_not_called()
        self.assertEqual(s.title, "s1")
        self.assertEqual(renames, [(s.conversation_id, "mi-nombre")])
        self.assertIn("●mi-nombre", tc.call_args.args)  # la pestaña ya muestra el título nuevo

    # ...y el hilo principal lo aplica a las sesiones ACTUALES (un escaneo pudo reemplazarlas)
    def test_led_renames_apply_on_main_thread_to_current_sessions(self) -> None:
        old, new = _sess(1), _sess(1)
        self.d.sessions = [new]
        with mock.patch.object(tui._tmuxctl, "sync_led_tmux_tabs",
                               return_value=[(old.conversation_id, "renombrada")]):
            self.d._led_work([old], 0)
        self.assertEqual(new.title, "s1")  # el hilo no la tocó
        with mock.patch.object(tui._sessions, "save_custom_title", side_effect=lambda c, t: t) as save:
            self.d._apply_led_renames()
        save.assert_called_once_with(old.conversation_id, "renombrada")
        self.assertEqual(new.title, "renombrada")
        self.assertEqual(old.title, "s1")

    # (4) hibernar en lote: UN escaneo fresco para todo el lote; MB de los objetos frescos
    def test_hibernate_batch_scans_once_and_counts_fresh_memory(self) -> None:
        stale = [_live(1, pss_kb=1024), _live(2, pss_kb=1024), _live(3, pss_kb=1024)]
        fresh = [_live(1, pss_kb=4096), _live(2, pss_kb=8192)]  # la 3 ya no está en RAM
        stubbed = []
        with mock.patch.object(sessions, "load_sessions", return_value=(0, {}, fresh)) as ls, \
             mock.patch.object(sessions, "hibernate_session", return_value=(True, "ok")) as hs:
            count, freed, results = sessions.hibernate_batch(stale, stubbed.append)
        self.assertEqual(ls.call_count, 1)
        self.assertEqual([c.args[0] for c in hs.call_args_list], fresh)
        self.assertTrue(all(c.kwargs == {"fresh": False} for c in hs.call_args_list))
        self.assertEqual((count, freed), (2, 4096 + 8192))
        self.assertEqual(stubbed, fresh)
        self.assertFalse(results[2][0])

    def test_tui_hib_all_and_auto_hibernate_use_one_batch(self) -> None:
        with mock.patch.object(tui._sessions, "hibernate_all_idle", return_value=(2, 3 << 20, [])) as ha:
            msg = self.d.execute_action("hib_all")
        ha.assert_called_once()
        self.assertIn("3072 MB", msg)
        a, b = _live(1), _live(2)
        for s in (a, b):
            s.dash_win_idx = "1"
        self.d.sessions = [a, b]
        self.d.idle_seen_since = {a.conversation_id: 0.0, b.conversation_id: 0.0}
        self.d.last_auto_check = 0
        with mock.patch.object(tui._paths, "DEFAULT_IDLE_HIBERNATE_SEC", 5), \
             mock.patch.object(tui._sessions, "hibernate_batch",
                               return_value=(1, 0, [(True, "Hibernada a"), (False, "no")])) as hb, \
             mock.patch.object(tui._sessions, "hibernate_session") as hs:
            self.d._periodic_idle_check(100.0)
        hb.assert_called_once()
        hs.assert_not_called()
        self.assertEqual(hb.call_args.args[0], [a, b])
        self.assertNotIn(a.conversation_id, self.d.idle_seen_since)
        self.assertIn(b.conversation_id, self.d.idle_seen_since)

    def test_cli_hibernate_all_idle_uses_shared_helper(self) -> None:
        s = _live(1)
        with mock.patch.object(cli._sessions, "load_sessions", return_value=(0, {}, [s])), \
             mock.patch.object(cli._sessions, "deduplicate_session_procs", return_value=0), \
             mock.patch.object(cli._sessions, "close_stale_dash_duplicates", return_value=0), \
             mock.patch.object(type(s), "can_hibernate", new_callable=mock.PropertyMock, return_value=True), \
             mock.patch.object(cli._sessions, "hibernate_all_idle", return_value=(1, 2048, [(True, "ok")])) as ha, \
             mock.patch("builtins.print"):
            rc = cli.cmd_hibernate(argparse.Namespace(target="all-idle"))
        self.assertEqual(rc, 0)
        ha.assert_called_once()

    # (5) la vista previa nunca espera al subproceso: lo lee un hilo
    def test_preview_reads_transcript_off_the_ui_thread(self) -> None:
        gate = threading.Event()
        done = threading.Event()

        def slow_tail(cid, max_turns):
            gate.wait(5)
            done.set()
            return [{"role": "user", "text": "hola"}]
        with mock.patch.object(tui._sessions, "zig_tail_transcript", side_effect=slow_tail):
            self.d._refresh_preview("c1", (1, 1), 5)  # vuelve sin esperar
            self.assertEqual(self.d.cached_turns, [])
            self.assertTrue(self.d.preview_busy)
            self.d._refresh_preview("c1", (2, 2), 5)  # ya hay uno en curso: no lanza otro
            gate.set()
            self.assertTrue(done.wait(5))
            for _ in range(100):
                if not self.d.preview_busy:
                    break
                threading.Event().wait(0.01)
            with mock.patch.object(threading, "Thread") as th:
                self.d._refresh_preview("c1", (1, 1), 5)
            th.assert_not_called()  # misma firma: no relee
        self.assertEqual(self.d.cached_turns, [{"role": "user", "text": "hola"}])
        self.d._refresh_preview("otra", (1, 1), 5)
        self.assertEqual(self.d.cached_turns, [])  # nunca se ven turnos de otra conversación

    # (6) nits
    def test_stale_handle_account_key_reference_is_gone(self) -> None:
        self.assertNotIn("_handle_account_key", inspect.getsource(tui))

    def test_background_error_keeps_open_confirmation(self) -> None:
        self.d.sessions = [_sess(1)]
        self.d._ask("dedup_confirm", True, "¿cerrar?")
        with mock.patch.object(self.d, "_advance_background", side_effect=RuntimeError("quota")), \
             mock.patch.object(self.d, "draw"), mock.patch.object(tui.time, "sleep"):
            self.d._tick()
        self.assertTrue(self.d.pending_dedup)
        self.assertIn("error interno", self.d.status_banner)
        with mock.patch.object(tui.time, "sleep"):
            try:
                raise RuntimeError("draw")
            except RuntimeError as exc:
                self.d._report_crash(exc)
        self.assertFalse(self.d.pending_dedup)

    def test_prompt_waits_for_second_escape_byte(self) -> None:
        win = FakeWin(10, 80)
        timeouts: list[int] = []
        keys = iter(["\x1b", "a", "\n"])
        win.get_wch = lambda: next(keys)
        win.timeout = timeouts.append
        win.nodelay = lambda flag: None
        seq = iter([ord("["), ord("A"), -1])
        win.getch = lambda: next(seq)
        with mock.patch.object(tui.curses, "curs_set"), mock.patch.object(tui.curses, "noecho"):
            out = tui._prompt_curses_text(win, "x")
        self.assertIn(tui.ESC_SEQ_WAIT_MS, timeouts)
        self.assertGreaterEqual(tui.ESC_SEQ_WAIT_MS, 30)
        self.assertEqual(out, "a")  # la flecha no canceló el prompt

    def test_help_separates_h_from_hibernate_all(self) -> None:
        keys = [k for k, _ in self.d.HELP_LINES["sessions"]]
        self.assertIn("h", keys)
        self.assertIn("H / a", keys)

    def test_footer_uses_first_fit(self) -> None:
        with mock.patch.object(tui, "first_fit", wraps=tui.first_fit) as ff:
            text, hits = self.d._footer("sessions", 40)
        ff.assert_called_once()
        self.assertLessEqual(tui.text_cols(text), 40)
        self.assertTrue(all(x1 < 40 for _, x1, _ in hits))

    def test_ask_rejects_key_view_only_kinds(self) -> None:
        with self.assertRaises(ValueError):
            self.d._ask("pending_blocked", {}, "x")
        self.assertIsNone(self.d.confirm)

    # Grupo B: las cachés por conversación se podan con cada escaneo
    def test_caches_pruned_to_present_sessions(self) -> None:
        self.d.ctx_cache = {"gone": ((0, 0), {}), _sess(1).conversation_id: ((0, 0), {})}
        self.d.activity_cache = {"gone": ((0, 0), "")}
        sessions._TRANSCRIPT_BUSY_CACHE["gone"] = ((0, 0), 0.0, False)
        self.d.scan_box = {"data": (0, {}, [_sess(1)]), "failed": False, "error": "",
                           "ver": self.d.scan_box.get("ver", 0) + 1}
        self.d._adopt_scan_snapshot()
        self.assertEqual(list(self.d.ctx_cache), [_sess(1).conversation_id])
        self.assertEqual(self.d.activity_cache, {})
        self.assertNotIn("gone", sessions._TRANSCRIPT_BUSY_CACHE)


if __name__ == "__main__":
    unittest.main()
