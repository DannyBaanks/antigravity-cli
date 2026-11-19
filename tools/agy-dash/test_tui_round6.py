"""EGM ronda 6 sobre agy_dash/tui.py (+ accounts.baton_pass_session): un test por hallazgo arreglado."""
from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agy_dash.accounts as accounts  # noqa: E402
import agy_dash.procs as procs  # noqa: E402
import agy_dash.tui as tui  # noqa: E402
from test_tui import FakeWin  # noqa: E402
from test_tui_round2 import _sess  # noqa: E402


class Round6(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="agy-dash-r6-"))
        for p in (mock.patch.object(tui.curses, "color_pair", side_effect=lambda n: n << 8),
                  mock.patch.object(tui.curses, "flushinp"),
                  mock.patch.object(tui._compact, "list_compacted_vault", return_value=[]),
                  mock.patch.object(tui, "CRASH_LOG", self.tmp / "tui-errors.log"),
                  mock.patch.dict(procs.SCAN_STATE, {"failed": False, "reason": ""})):
            p.start()
            self.addCleanup(p.stop)
        self.d = tui.Dashboard(FakeWin(30, 120))

    # (#4) el modal guarda la identidad; el estado de procesos sale del último escaneo
    def test_modal_open_full_uses_fresh_session(self) -> None:
        old = _sess(1)
        fresh = _sess(1)
        fresh.proc = procs.ProcMemInfo(pid=4242, pss_kb=1, start_ticks=1)
        self.d.sessions = [fresh]
        self.d.pending_compact_sess = old
        with mock.patch.object(tui._tmuxctl, "open_session_in_dashboard_tab", return_value="ok") as op:
            self.d.handle_key(ord("o"))
        self.assertIs(op.call_args.args[0], fresh)

    def test_modal_target_falls_back_to_snapshot_when_gone(self) -> None:
        old = _sess(1)
        self.d.sessions = []
        self.d.pending_compact_sess = old
        with mock.patch.object(tui._tmuxctl, "open_session_in_dashboard_tab", return_value="ok") as op:
            self.d.handle_key(ord("o"))
        self.assertIs(op.call_args.args[0], old)

    # (#3/#19) terminal bajo: el aviso nombra [b] y [k] y no pisa status_banner
    def test_short_terminal_hint_is_local_and_complete(self) -> None:
        win = FakeWin(18, 200)
        d = tui.Dashboard(win)
        s = _sess(1)
        d.sessions = [s]
        d.pending_compact_sess = s
        before = d.status_banner
        with mock.patch.object(tui.Dashboard, "_prepare_frame", lambda self, now: None):
            d.draw(0.0)
        banner = "".join(t for y, _, t in win.writes if y == win.h - 2)
        self.assertIn("[b]", banner)
        self.assertIn("[k]", banner)
        self.assertEqual(d.status_banner, before)

    # (#5) Baton Pass no mata copias si el re-escaneo de refresh_session falló
    def test_baton_pass_aborts_when_refresh_scan_failed(self) -> None:
        s = _sess(1)
        s.proc = procs.ProcMemInfo(pid=4243, pss_kb=1, start_ticks=1)
        pf = {"ok": True, "blockers": [], "target_slot": "pro2", "model_display": "m", "model_slug": "m"}

        def failed_refresh(cid):
            procs.SCAN_STATE["failed"] = True
            procs.SCAN_STATE["reason"] = "boom"
            return s
        with mock.patch.object(accounts, "baton_preflight", return_value=pf), \
             mock.patch.object(accounts._sessions, "refresh_session", side_effect=failed_refresh), \
             mock.patch.object(accounts._procs, "terminate_verified") as kill:
            ok, msg = accounts.baton_pass_session(s, target_slot="pro2")
        self.assertFalse(ok)
        self.assertIn("escaneo", msg)
        kill.assert_not_called()

    # (#6) varios resultados de compactación en el mismo frame se resumen, no se pisan
    def test_bulk_compaction_results_are_summarized(self) -> None:
        a, b, c = _sess(1), _sess(2), _sess(3)
        self.d.compact_results.extend([
            (a, True, "ok a", self.tmp / "a.md", False),
            (b, False, "falló b", None, False),
            (c, True, "ok c", self.tmp / "c.md", False),
        ])
        self.d._process_compact_results()
        self.assertIn("2 ok", self.d.status_banner)
        self.assertIn("1 fallos", self.d.status_banner)
        self.assertIn("falló b", self.d.status_banner)

    def test_single_compaction_result_keeps_its_message(self) -> None:
        self.d.compact_results.append((_sess(1), False, "falló solo", None, False))
        self.d._process_compact_results()
        self.assertEqual(self.d.status_banner, "❌ falló solo")

    # (#15) una copia externa nueva cambia la clave del preflight memorizado
    def test_preflight_key_includes_process_pids(self) -> None:
        s = _sess(1)
        self.d.sel_for_alert = s
        self.d.quota_alert = {"target_slot": "pro2"}
        self.d.model_disp = "m"
        with mock.patch.object(tui._accounts, "baton_preflight", return_value={"ok": True}) as pf:
            self.d._preflight_ok(100.0)
            s.extra_procs.append(procs.ProcMemInfo(pid=4244, pss_kb=1, start_ticks=1))
            self.d._preflight_ok(100.1)
        self.assertEqual(pf.call_count, 2)

    # (#16) [M] no apila un segundo popup mientras el primero sigue abierto
    def test_model_board_single_popup(self) -> None:
        release = threading.Event()
        with mock.patch.object(tui.shutil, "which", return_value="/bin/swarm-ext"), \
             mock.patch.object(tui._tmuxctl, "tmux_cmd", side_effect=lambda *a, **k: release.wait(2)) as tc:
            first = tui.open_model_board()
            second = tui.open_model_board()
            release.set()
            for _ in range(100):
                if not tui._BOARD_OPEN.locked():
                    break
                threading.Event().wait(0.01)
        self.assertIn("abierto (popup", first)
        self.assertIn("ya está abierto", second)
        self.assertEqual(tc.call_count, 1)
        self.assertFalse(tui._BOARD_OPEN.locked())

    # (#14) el banner de "nueva" dice el GOMEMLIMIT real
    def test_new_tab_banner_reports_effective_gomemlimit(self) -> None:
        with mock.patch.dict(tui.os.environ, {"GOMEMLIMIT": "321MiB"}), \
             mock.patch.object(tui._cli, "cmd_open_new_tab"):
            msg = self.d.execute_action("new")
        self.assertIn("321MiB", msg)


if __name__ == "__main__":
    unittest.main()
