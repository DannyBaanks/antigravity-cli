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
        self.assertTrue(any(b.action_id == "baton" for b in btns))
        self.assertTrue(any(b.action_id == "accounts" for b in btns))
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

    def test_isolated_home_shares_conversations_and_isolates_auth(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            base_home = root / "real_home"
            agy_data = base_home / ".gemini" / "antigravity-cli"
            (agy_data / "conversations").mkdir(parents=True)
            (agy_data / "brain").mkdir(parents=True)
            (agy_data / "settings.json").write_text('{"model":"Gemini 4 Argon (Medium)"}', encoding="utf-8")
            (base_home / ".gitconfig").write_text("[user]\n\tname = Test\n", encoding="utf-8")

            slot_home = ad.ensure_isolated_home(
                "pro2",
                base_home=base_home,
                agy_data_dir=agy_data,
                accounts_dir=agy_data / "accounts",
            )
            self.assertTrue((slot_home / ".config" / "antigravity").is_dir())
            self.assertFalse((slot_home / ".config" / "antigravity").is_symlink())
            self.assertTrue((slot_home.parent / "bin" / "xdg-open").is_file())
            self.assertTrue((slot_home / ".gemini" / "antigravity-cli" / "conversations").is_symlink())
            self.assertTrue((slot_home / ".gemini" / "antigravity-cli" / "settings.json").is_symlink())
            self.assertTrue((slot_home / ".gitconfig").is_symlink())
            self.assertEqual((slot_home.stat().st_mode & 0o777), 0o700)

    def test_usage_parser_alert_and_baton_target_selection(self) -> None:
        sample = json.dumps({
            "status": "SUCCESS",
            "command": {
                "data": {
                    "groups": [
                        {
                            "name": "Gemini Models",
                            "buckets": [{"id": "gemini-5h", "name": "Five Hour", "window": "5h", "remaining_fraction": 0.65, "reset_time": "2026-11-18T09:00:00Z"}],
                        },
                        {
                            "name": "Gemini 4 Argon",
                            "buckets": [{"id": "gemini-argon-wtu-weekly", "name": "Argon Weekly", "window": "weekly", "remaining_fraction": 0.01, "reset_time": "2026-11-25T00:00:00Z"}],
                        },
                    ]
                }
            },
        })
        parsed = ad.parse_usage_output(sample)
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["families"]["argon"], 1.0)
        self.assertEqual(parsed["families"]["gemini"], 65.0)

        reg = {
            "active_slot": "primary",
            "accounts": {
                "primary": {"slot": "primary", "mode": "system", "email": "a@example.com", "authenticated": True, "quota": parsed},
                "pro2": {
                    "slot": "pro2",
                    "mode": "isolated",
                    "email": "b@example.com",
                    "authenticated": True,
                    "quota": {"ok": True, "families": {"argon": 92.0, "gemini": 88.0}, "min_pct": 88.0, "buckets": []},
                },
            },
        }
        alert = ad.compute_quota_alert(reg, active_slot="primary", model_name="Gemini 4 Argon (Medium)")
        self.assertIsNotNone(alert)
        self.assertEqual(alert["level"], "CRITICAL")
        self.assertEqual(alert["target_slot"], "pro2")
        self.assertEqual(ad.choose_baton_target(reg, "primary", "Gemini 4 Argon (Medium)"), "pro2")
        self.assertEqual(ad.resolve_model_arg("Gemini 4 Argon (Medium)")[1], "gemini-4-argon-medium")

    def test_led_tab_title_spinner_and_marquee(self) -> None:
        sess = ad.AgySession(
            conversation_id="cid-1",
            title="Orquestacion Piton Wave",
            preview="",
            step_count=5,
            last_modified="ahora",
            workspace="/tmp",
            db_status="CASCADE_RUN_STATUS_RUNNING",
            not_fully_idle=False,
            proc=ad.ProcMemInfo(pid=123, in_dash=True),
            dash_win_idx="1",
            dash_win_name="●Orquestacion",
        )
        t0 = ad.format_led_tab_title(sess, 0, "Editing files...", True, width=16)
        t1 = ad.format_led_tab_title(sess, 1, "Editing files...", True, width=16)
        self.assertTrue(t0.startswith("⠋"))
        self.assertTrue(t1.startswith("⠙"))
        self.assertNotEqual(t0, t1)

        idle_sess = ad.AgySession(
            conversation_id="cid-2",
            title="Idle Session",
            preview="",
            step_count=2,
            last_modified="ahora",
            workspace="/tmp",
            db_status="CASCADE_RUN_STATUS_IDLE",
            not_fully_idle=False,
            proc=ad.ProcMemInfo(pid=124, in_dash=True),
            dash_win_idx="2",
            dash_win_name="●Idle",
        )
        self.assertTrue(ad.format_led_tab_title(idle_sess, 3, "IDLE", False).startswith("●"))

    def test_activity_label_sanitizes_tool_logs_and_tmux_conf_has_close_and_freeze(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            orig_brain = ad.BRAIN_DIR
            orig_conf = ad.TMUX_CONF
            try:
                ad.BRAIN_DIR = Path(td)
                cid = "conv-act-1"
                log_dir = Path(td) / cid / ".system_generated" / "logs"
                log_dir.mkdir(parents=True, exist_ok=True)
                (log_dir / "transcript.jsonl").write_text(
                    json.dumps({
                        "step_index": 1,
                        "type": "GENERIC",
                        "content": "Created At: 2026-11-18T00:00:00Z Tool is running as a background task",
                    }) + "\n",
                    encoding="utf-8",
                )
                sess = ad.AgySession(
                    conversation_id=cid,
                    title="Demo",
                    preview="",
                    step_count=1,
                    last_modified="ahora",
                    workspace="/tmp",
                    db_status="CASCADE_RUN_STATUS_RUNNING",
                    not_fully_idle=True,
                    proc=ad.ProcMemInfo(pid=555, in_dash=True),
                    dash_win_idx="1",
                    dash_win_name="●Demo",
                )
                busy, lbl = ad.session_live_activity(sess, {})
                self.assertTrue(busy)
                self.assertEqual(lbl, "Task running...")

                ad.TMUX_CONF = Path(td) / "agy-dash.tmux.conf"
                ad.write_tmux_config(Path(__file__).resolve())
                conf_txt = ad.TMUX_CONF.read_text(encoding="utf-8")
                self.assertIn("MouseDrag1Pane", conf_txt)
                self.assertIn("copy-pipe-no-clear" if "copy-pipe-no-clear" in conf_txt else "copy-selection-no-clear", conf_txt)
                self.assertIn("range=user|x_#{window_index}", conf_txt)
                self.assertIn("close-tab", conf_txt)
            finally:
                ad.BRAIN_DIR = orig_brain
                ad.TMUX_CONF = orig_conf


if __name__ == "__main__":
    unittest.main()
