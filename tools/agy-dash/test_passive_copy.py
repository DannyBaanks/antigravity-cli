"""Wheel/drag copy-mode is PASSIVE: menu keys leave the scroll and reach AGY (2026-11-18 picker freeze)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agy_dash import paths as _paths  # noqa: E402
from agy_dash import tmuxctl  # noqa: E402


class PassiveCopyTests(unittest.TestCase):
    def test_bindings(self) -> None:
        b = tmuxctl.passive_copy_bindings("send-keys -X copy-selection-and-cancel")
        for k in ("Up", "Down", "Left", "Right", "Enter", "Tab"):
            self.assertIn(f"bind -T copy-mode-vi {k} if -F '#{{@passive_copy}}' "
                          f"{{ send-keys -X cancel ; send-keys {k} }}", b)
        self.assertIn("{ send-keys -X cursor-up }", b)  # active copy-mode unchanged
        self.assertIn("{ send-keys -X copy-selection-and-cancel }", b)
        esc = [line for line in b.splitlines() if " Escape " in line][0]
        self.assertNotIn("send-keys Escape", esc)  # never cancels AGY's menu

    def test_conf_marks_wheel_drag_passive_and_alt_c_active(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(_paths, "TMUX_CONF", Path(d) / "t.conf"):
            tmuxctl.write_tmux_config(Path("agy_dashboard.py"))
            conf = (Path(d) / "t.conf").read_text()
        wheel = [line for line in conf.splitlines() if line.startswith("bind -n WheelUpPane")][0]
        drag = [line for line in conf.splitlines() if line.startswith("bind -n MouseDrag1Pane")][0]
        altc = [line for line in conf.splitlines() if line.startswith("bind -n M-c")][0]
        self.assertIn("set -p @passive_copy 1", wheel)
        self.assertIn("set -p @passive_copy 1", drag)
        self.assertIn("set -p @passive_copy 0", altc)
        self.assertIn("bind -T copy-mode-vi Down if -F", conf)


if __name__ == "__main__":
    unittest.main()
