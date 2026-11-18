from __future__ import annotations

import curses
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agy_dash.tui as tui  # noqa: E402


class DashboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stdscr = mock.Mock()
        self.dashboard = tui.Dashboard(self.stdscr)

    def test_run_curses_tui_builds_dashboard_and_runs_it(self) -> None:
        with mock.patch.object(tui, "Dashboard") as dashboard_cls:
            tui.run_curses_tui(self.stdscr)

        dashboard_cls.assert_called_once_with(self.stdscr)
        dashboard_cls.return_value.run.assert_called_once_with()

    def test_handle_key_q_r_and_arrows(self) -> None:
        self.assertEqual(self.dashboard.mode, "sessions")
        self.assertEqual(self.dashboard.cursor, 0)

        with mock.patch.object(tui._tmuxctl, "tmux_cmd") as tmux_cmd:
            self.dashboard.handle_key(ord("q"))
            tmux_cmd.assert_called_once_with("next-window")
            tmux_cmd.reset_mock()

            self.dashboard.handle_key(ord("r"))
            tmux_cmd.assert_called_once_with("refresh-client", "-S")

        self.assertTrue(self.dashboard.force_scan)
        self.assertEqual(self.dashboard.cached_preview_sig, (-1, -1))
        self.assertEqual(self.dashboard.mode, "sessions")

        self.dashboard.force_scan = False
        self.dashboard.handle_key(curses.KEY_DOWN)
        self.assertEqual(self.dashboard.cursor, 1)
        self.dashboard.handle_key(curses.KEY_UP)
        self.assertEqual(self.dashboard.cursor, 0)
        self.dashboard.handle_key(curses.KEY_UP)
        self.assertEqual(self.dashboard.cursor, 0)


if __name__ == "__main__":
    unittest.main()
