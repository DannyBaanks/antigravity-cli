"""Tests for CDMX (America/Mexico_City) timezone formatting across agy-dash."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agy_dashboard as ad  # noqa: E402
from agy_dash import paths, sessions, tui  # noqa: E402
from test_tui import FakeWin  # noqa: E402


class CdmxTimezoneTests(unittest.TestCase):
    def test_format_display_iso_converts_utc_to_cdmx_including_day_rollover(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AGY_TZ", None)
            self.assertEqual(
                paths.format_display_iso("2026-11-26T21:49:17Z"),
                "2026-11-26 15:49:17 CDMX",
            )
            self.assertEqual(
                paths.format_display_iso("2026-12-03T08:16:02Z"),
                "2026-12-03 02:16:02 CDMX",
            )
            self.assertEqual(
                paths.format_display_iso("2026-11-26T23:16:02Z"),
                "2026-11-26 17:16:02 CDMX",
            )
            # 03:42:44 UTC on Dec 2 is 21:42:44 on Dec 1 in CDMX (UTC-6)
            self.assertEqual(
                paths.format_display_iso("2026-12-02T03:42:44Z"),
                "2026-12-01 21:42:44 CDMX",
            )
            self.assertEqual(paths.format_display_iso("", empty="nunca"), "nunca")
            self.assertEqual(paths.format_display_iso(""), "")
            self.assertEqual(paths.format_display_iso("not-a-date"), "not-a-date")

    def test_agy_tz_env_override_and_shim_exports(self) -> None:
        with mock.patch.dict(os.environ, {"AGY_TZ": "UTC"}):
            self.assertEqual(
                ad.format_display_iso("2026-11-26T21:49:17Z"),
                "2026-11-26 21:49:17 UTC",
            )
            self.assertEqual(ad.display_tz_label(), "UTC")

    def test_account_details_renders_cdmx_and_hides_empty_reset(self) -> None:
        win = FakeWin(25, 130)
        with (
            mock.patch.object(tui.curses, "color_pair", side_effect=lambda n: n << 8),
            mock.patch.object(tui.curses, "flushinp"),
            mock.patch.object(tui.Dashboard, "_prepare_frame", lambda self, now: None),
        ):
            d = tui.Dashboard(win)
            d.mode = "accounts"
            d.reg = {
                "active_slot": "primary",
                "accounts": {
                    "pro3": {
                        "slot": "pro3",
                        "mode": "isolated",
                        "email": "danielbaanksyy@gmail.com",
                        "authenticated": True,
                        "updated_at": "2026-11-26T21:49:17Z",
                        "quota": {
                            "ok": True,
                            "families": {"gemini": 0.0, "claude_gpt": 0.0},
                            "buckets": [
                                {
                                    "group": "Gemini Models",
                                    "name": "Weekly Limit Remaining",
                                    "pct": 62.0,
                                    "reset_time": "2026-12-03T08:16:02Z",
                                },
                                {
                                    "group": "Gemini Models",
                                    "name": "Five Hour Limit Remaining",
                                    "pct": 0.0,
                                    "reset_time": "2026-11-26T23:16:02Z",
                                },
                                {
                                    "group": "Claude and GPT models",
                                    "name": "Weekly Limit Remaining",
                                    "pct": 0.0,
                                    "reset_time": "2026-12-02T03:42:44Z",
                                },
                                {
                                    "group": "Claude and GPT models",
                                    "name": "Five Hour Limit Remaining",
                                    "pct": 0.0,
                                    "reset_time": "",
                                },
                            ],
                        },
                    }
                },
            }
            d.cursor = 0
            d.draw(now=1795729890.0)
            rendered = "\n".join(t for _, _, t in win.writes)
            self.assertIn("actualizado: 2026-11-26 15:49:17 CDMX", rendered)
            self.assertIn("(reset 2026-12-03 02:16:02 CDMX)", rendered)
            self.assertIn("(reset 2026-11-26 17:16:02 CDMX)", rendered)
            self.assertIn("(reset 2026-12-01 21:42:44 CDMX)", rendered)
            self.assertNotIn("(reset )", rendered)
            self.assertNotIn("2026-11-26T21:49:17Z", rendered)
            self.assertIn("CDMX", d.last_refresh_clock)

    def test_zig_tail_transcript_uses_cdmx_for_ts_and_ts_local(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            orig_brain = ad.BRAIN_DIR
            try:
                ad.BRAIN_DIR = Path(td)
                cid = "tz-conv-1"
                log_dir = Path(td) / cid / ".system_generated" / "logs"
                log_dir.mkdir(parents=True, exist_ok=True)
                line = json.dumps({
                    "step_index": 1,
                    "type": "USER_INPUT",
                    "status": "DONE",
                    "created_at": "2026-11-26T23:16:02Z",
                    "content": "<USER_REQUEST>\nhola cdmx\n</USER_REQUEST>",
                })
                (log_dir / "transcript.jsonl").write_text(line + "\n", encoding="utf-8")
                with mock.patch.dict(os.environ, {}, clear=False):
                    os.environ.pop("AGY_TZ", None)
                    turns = sessions.zig_tail_transcript(cid, max_turns=5)
                self.assertEqual(len(turns), 1)
                self.assertEqual(turns[0]["ts"], "17:16:02")
                self.assertEqual(turns[0]["ts_local"], "17:16")
            finally:
                ad.BRAIN_DIR = orig_brain


if __name__ == "__main__":
    unittest.main()
