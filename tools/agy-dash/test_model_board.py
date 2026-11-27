"""M key / 🏆 button: swarm-ext board (model ranking shared with claude-dash) in a tmux popup."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agy_dash.tui as tui  # noqa: E402


class ModelBoardTests(unittest.TestCase):
    def test_button_fits_above_inspector(self) -> None:
        btns = {b.action_id: b for b in tui.build_command_buttons(80)}
        self.assertEqual(btns["ranking"].hotkey, "M")
        # the inspector starts at panel_btn_y + 9: buttons must stay in rows 0..8
        self.assertTrue(all(b.row_offset <= 8 for b in btns.values()))
        cells = [(b.row_offset, b.col_offset) for b in btns.values()]
        self.assertEqual(len(cells), len(set(cells)))

    def test_board_command(self) -> None:
        self.assertEqual(tui.model_board_command("/x/swarm-ext"),
                         ["/x/swarm-ext", "board", "--providers", "agy,nvapi,codex,opencode,freellm,qwen"])
        with mock.patch.object(tui.shutil, "which", return_value=None):
            self.assertIsNone(tui.model_board_command())
            self.assertIn("swarm-ext", tui.open_model_board())

    def test_open_board_runs_display_popup(self) -> None:
        with mock.patch.object(tui.shutil, "which", return_value="/x/swarm-ext"), \
             mock.patch.object(tui._tmuxctl, "tmux_cmd") as tmux_cmd:
            msg = tui.open_model_board()
            for t in list(tui.threading.enumerate()):
                if t is not tui.threading.current_thread() and t.daemon:
                    t.join(timeout=2)
        self.assertIn("Ranking", msg)
        args = tmux_cmd.call_args.args
        self.assertEqual(args[0], "display-popup")
        self.assertIn("/x/swarm-ext board --providers agy,nvapi,codex,opencode,freellm,qwen", args[-1])

    def test_key_M_in_dashboard(self) -> None:
        dash = tui.Dashboard(mock.Mock())
        with mock.patch.object(tui, "open_model_picker", return_value="ok-picker") as op:
            dash.handle_key(ord("M"))
        op.assert_called_once_with(dash)
        self.assertEqual(dash.status_banner, "ok-picker")

    def test_open_picker_runs_display_popup_and_updates_model(self) -> None:
        dash = tui.Dashboard(mock.Mock())
        dash.model_disp = "Gemini 4 Hydrogen (Medium)"
        with mock.patch.object(tui._swarm, "catalog", return_value=([{"provider": "agy", "spec": "agy:gemini-4-neon-medium", "label": "[AGY] Gemini 4 Neon (Medium)"}], "")), \
             mock.patch.object(tui._swarm, "_swarm_ext", return_value="/x/swarm-ext"), \
             mock.patch.object(tui._tmuxctl, "tmux_cmd") as tmux_cmd, \
             mock.patch.object(tui._swarm, "set_active_model", return_value=(True, "Gemini 4 Neon (Medium)")):
            msg = tui.open_model_picker(dash)
            for t in list(tui.threading.enumerate()):
                if t is not tui.threading.current_thread() and t.daemon:
                    t.join(timeout=2)
        self.assertIn("Selector de modelos", msg)
        args = tmux_cmd.call_args.args
        self.assertEqual(args[0], "display-popup")
        self.assertIn("swarm pick-ui", args[-1])


if __name__ == "__main__":
    unittest.main()
