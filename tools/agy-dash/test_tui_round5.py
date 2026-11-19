"""EGM ronda 5 sobre agy_dash/tui.py: un test por hallazgo de código arreglado."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agy_dash.procs as procs  # noqa: E402
import agy_dash.tui as tui  # noqa: E402
from test_tui import FakeWin  # noqa: E402
from test_tui_round2 import _sess  # noqa: E402


class _SyncThread:
    def __init__(self, target, daemon=None):
        self.target = target

    def start(self):
        self.target()


class Round5(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="agy-dash-r5-"))
        for p in (mock.patch.object(tui.curses, "color_pair", side_effect=lambda n: n << 8),
                  mock.patch.object(tui.curses, "flushinp"),
                  mock.patch.object(tui._compact, "list_compacted_vault", return_value=[]),
                  mock.patch.object(tui, "CRASH_LOG", self.tmp / "tui-errors.log"),
                  mock.patch.dict(procs.SCAN_STATE, {"failed": False, "reason": ""})):
            p.start()
            self.addCleanup(p.stop)
        self.d = tui.Dashboard(FakeWin(30, 120))

    # (escalado) el popup del ranking bloquea a propósito: sin el límite de 2 s de tmux_cmd
    def test_model_board_popup_has_no_timeout(self) -> None:
        with mock.patch.object(tui.shutil, "which", return_value="/bin/swarm-ext"), \
             mock.patch.object(tui.threading, "Thread", _SyncThread), \
             mock.patch.object(tui._tmuxctl, "tmux_cmd") as tc:
            tui.open_model_board()
        self.assertEqual(tc.call_args.args[0], "display-popup")
        self.assertIn("timeout", tc.call_args.kwargs)
        self.assertIsNone(tc.call_args.kwargs["timeout"])

    # la alerta de cuota no relee registro/settings en cada frame
    def test_preflight_memoized_within_ttl(self) -> None:
        self.d.sel_for_alert = _sess(1)
        self.d.quota_alert = {"target_slot": "pro2"}
        self.d.model_disp = "m"
        with mock.patch.object(tui._accounts, "baton_preflight", return_value={"ok": True}) as pf:
            self.assertTrue(self.d._preflight_ok(100.0))
            self.assertTrue(self.d._preflight_ok(100.5))
            self.assertEqual(pf.call_count, 1)
            self.d._preflight_ok(100.0 + tui.Dashboard.PREFLIGHT_TTL_SEC + 0.01)
            self.assertEqual(pf.call_count, 2)

    def test_preflight_recomputed_when_target_changes(self) -> None:
        self.d.sel_for_alert = _sess(1)
        self.d.model_disp = "m"
        with mock.patch.object(tui._accounts, "baton_preflight", return_value={"ok": False}) as pf:
            self.d.quota_alert = {"target_slot": "pro2"}
            self.assertFalse(self.d._preflight_ok(100.0))
            self.d.quota_alert = {"target_slot": "pro3"}
            self.d._preflight_ok(100.1)
        self.assertEqual(pf.call_count, 2)
        self.assertEqual(pf.call_args.kwargs["target_slot"], "pro3")

    # la vista muerta pending_blocked desaparece: toda vista de teclas es un modo o un CONFIRM_SPECS
    def test_every_key_view_is_mode_or_confirm_kind(self) -> None:
        modes = {"sessions", "accounts", "vault", "memory"}
        for view in tui.KEYMAP:
            self.assertTrue(view in modes or view in tui.CONFIRM_SPECS, view)
        self.assertNotIn("pending_blocked", tui.KEYMAP)
        self.assertNotIn("pending_blocked", tui.Dashboard.FOOTER_VARIANTS)
        self.assertNotIn("pending_blocked", tui.Dashboard.HELP_LINES)

    def test_view_name_is_confirm_kind_even_with_blocked_payload(self) -> None:
        self.d.mode = "accounts"
        self.d.confirm = tui.Confirm("pending_delete", "accounts", {"blocked_reason": "x"}, False, "")
        self.assertEqual(self.d._view_name(), "pending_delete")

    # curs_set sin soporte en la terminal no tumba el arranque
    def test_configure_curses_tolerates_curs_set_error(self) -> None:
        class Reached(Exception):
            pass
        with mock.patch.object(tui.curses, "curs_set", side_effect=tui.curses.error("no")), \
             mock.patch.object(tui.curses, "use_default_colors", side_effect=Reached):
            with self.assertRaises(Reached):  # pasó de curs_set al siguiente paso sin propagar
                self.d._configure_curses()

if __name__ == "__main__":
    unittest.main()
