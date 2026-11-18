#!/usr/bin/env python3
"""Unit tests for tools/agy_limits/agy_dashboard.py and agy-fast-core."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agy_dashboard as ad  # noqa: E402


class AgyDashboardTests(unittest.TestCase):
    def test_zig_scan_returns_valid_structure(self) -> None:
        self.assertTrue(ad.ensure_zig_core(), "agy-fast-core must build/exist")
        scan_us, ksm, by_conv = ad.zig_scan()
        self.assertGreater(scan_us, 0)
        self.assertIn("pages_sharing", ksm)
        self.assertIsInstance(by_conv, dict)

    def test_zig_tail_transcript_parses_user_and_planner_turns(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            orig_brain = ad.BRAIN_DIR
            try:
                ad.BRAIN_DIR = Path(td)
                cid = "test-conv-1234"
                log_dir = Path(td) / cid / ".system_generated" / "logs"
                log_dir.mkdir(parents=True, exist_ok=True)
                lines = [
                    json.dumps({
                        "step_index": 1,
                        "type": "USER_INPUT",
                        "status": "DONE",
                        "created_at": "2026-11-17T23:00:00Z",
                        "content": "<USER_REQUEST>\nhola mundo\n</USER_REQUEST>",
                    }),
                    json.dumps({
                        "step_index": 2,
                        "type": "PLANNER_RESPONSE",
                        "status": "DONE",
                        "created_at": "2026-11-17T23:00:05Z",
                        "content": "Listo",
                        "tool_calls": [{"name": "run_command", "arguments": {"toolSummary": "Check"}}],
                    }),
                ]
                (log_dir / "transcript.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
                turns = ad.zig_tail_transcript(cid, max_turns=5)
                self.assertEqual(len(turns), 2)
                self.assertEqual(turns[0]["role"], "USER")
                self.assertEqual(turns[0]["content"], "hola mundo")
                self.assertEqual(turns[1]["role"], "AGY")
                self.assertEqual(turns[1]["tools"], ["run_command(Check)"])
            finally:
                ad.BRAIN_DIR = orig_brain

    def test_command_button_hit_test_and_external_busy_guard(self) -> None:
        btns = ad.build_command_buttons(60)
        self.assertEqual(ad.hit_test_button(btns, 90, 5, 93, 5), "refresh")
        self.assertIsNone(ad.hit_test_button(btns, 90, 5, 10, 5))

        busy_ext = ad.AgySession(
            conversation_id="abc",
            title="Test",
            preview="",
            step_count=10,
            last_modified="2026-11-17",
            workspace="/tmp",
            db_status="CASCADE_RUN_STATUS_IDLE",
            not_fully_idle=True,
            proc=ad.ProcMemInfo(pid=999999, tty="pts/1", in_dash=False, active_children=["bash(1)"]),
        )
        self.assertTrue(busy_ext.external_busy)
        self.assertFalse(busy_ext.can_hibernate)


if __name__ == "__main__":
    unittest.main()
