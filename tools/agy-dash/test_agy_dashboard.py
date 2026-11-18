#!/usr/bin/env python3
"""Unit tests for tools/agy_limits/agy_dashboard.py and agy-fast-core."""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

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
                self.assertIn("{ run-shell", conf_txt)
                self.assertIn("#[bg=#{?#{m:*DASH*,#{window_name}},#50fa7b,#e94560}", conf_txt)
            finally:
                ad.BRAIN_DIR = orig_brain
                ad.TMUX_CONF = orig_conf

    def test_remove_account_slot_cleans_refs_and_protects_primary(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            base_home = root / "real_home"
            agy_data = base_home / ".gemini" / "antigravity-cli"
            shared_conv = agy_data / "conversations"
            shared_conv.mkdir(parents=True)
            sentinel = shared_conv / "keep_me.pb"
            sentinel.write_text("SHARED_STATE", encoding="utf-8")
            (agy_data / "brain").mkdir(parents=True)
            (base_home / ".gitconfig").write_text("[user]\n\tname = Safe\n", encoding="utf-8")

            accounts_dir = agy_data / "accounts"
            reg_path = accounts_dir / "registry.json"
            p3_home = ad.ensure_isolated_home("pro3", base_home=base_home, agy_data_dir=agy_data, accounts_dir=accounts_dir)
            p4_home = ad.ensure_isolated_home("pro4", base_home=base_home, agy_data_dir=agy_data, accounts_dir=accounts_dir)
            (p4_home / ".config" / "antigravity" / "local_only.txt").write_text("PRO4_ONLY", encoding="utf-8")

            reg_data = {
                "active_slot": "pro4",
                "conv_accounts": {"cid-1": "pro4", "cid-2": "pro3"},
                "accounts": {
                    "primary": {"slot": "primary", "mode": "system", "email": "a@example.com", "authenticated": True},
                    "pro3": {"slot": "pro3", "mode": "isolated", "email": "b@example.com", "authenticated": True},
                    "pro4": {"slot": "pro4", "mode": "isolated", "email": "b@example.com", "authenticated": True},
                },
            }
            ad.save_accounts_registry(reg_data, registry_path=reg_path)

            info = ad.inspect_account_removal("pro4", registry_path=reg_path, accounts_dir=accounts_dir, base_home=base_home)
            self.assertEqual(info["duplicate_of"], ["pro3"])
            self.assertEqual(len(info["associated_sessions"]), 1)
            self.assertEqual(info["blocked_reason"], "")

            ok_unconf, _ = ad.remove_account_slot("pro4", confirmed=False, registry_path=reg_path, accounts_dir=accounts_dir, base_home=base_home)
            self.assertFalse(ok_unconf)
            ok_pri, _ = ad.remove_account_slot("primary", confirmed=True, registry_path=reg_path, accounts_dir=accounts_dir, base_home=base_home)
            self.assertFalse(ok_pri)

            ok_p4, _ = ad.remove_account_slot("pro4", confirmed=True, registry_path=reg_path, accounts_dir=accounts_dir, base_home=base_home)
            self.assertTrue(ok_p4)
            after = ad.load_accounts_registry(registry_path=reg_path, base_home=base_home)
            self.assertNotIn("pro4", after["accounts"])
            self.assertEqual(after["active_slot"], "pro3")
            self.assertEqual(after["conv_accounts"]["cid-1"], "pro3")
            self.assertFalse((accounts_dir / "pro4").exists())
            self.assertTrue(sentinel.is_file())
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "SHARED_STATE")
            self.assertTrue((p3_home / ".config" / "antigravity").is_dir())

    def test_baton_preflight_verifies_isolation_quota_and_no_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            base_home = root / "real_home"
            agy_data = base_home / ".gemini" / "antigravity-cli"
            (agy_data / "conversations").mkdir(parents=True)
            (agy_data / "brain").mkdir(parents=True)
            (agy_data / "conversations" / "cid-hyd.pb").write_text("pb", encoding="utf-8")
            (agy_data / "settings.json").write_text('{"model":"Gemini 4 Hydrogen (High)"}', encoding="utf-8")
            accounts_dir = agy_data / "accounts"
            reg_path = accounts_dir / "registry.json"
            ad.ensure_isolated_home("pro3", base_home=base_home, agy_data_dir=agy_data, accounts_dir=accounts_dir)
            ad.ensure_isolated_home("pro_dup", base_home=base_home, agy_data_dir=agy_data, accounts_dir=accounts_dir)

            reg = {
                "active_slot": "primary",
                "conv_accounts": {"cid-hyd": "primary"},
                "accounts": {
                    "primary": {
                        "slot": "primary",
                        "mode": "system",
                        "email": "a@example.com",
                        "authenticated": True,
                        "quota": {"ok": True, "families": {"gemini": 2.0, "argon": 1.0}, "min_pct": 1.0, "buckets": []},
                    },
                    "pro3": {
                        "slot": "pro3",
                        "mode": "isolated",
                        "email": "b@example.com",
                        "authenticated": True,
                        "quota": {"ok": True, "families": {"gemini": 98.0, "argon": 61.0}, "min_pct": 61.0, "buckets": []},
                    },
                    "pro_dup": {
                        "slot": "pro_dup",
                        "mode": "isolated",
                        "email": "a@example.com",
                        "authenticated": True,
                        "quota": {"ok": True, "families": {"gemini": 99.0}, "min_pct": 99.0, "buckets": []},
                    },
                },
            }
            ad.save_accounts_registry(reg, registry_path=reg_path)

            idle_sess = ad.AgySession(
                conversation_id="cid-hyd",
                title="Hydrogen Task",
                preview="",
                step_count=42,
                last_modified="ahora",
                workspace=str(root),
                db_status="CASCADE_RUN_STATUS_IDLE",
                not_fully_idle=False,
                proc=ad.ProcMemInfo(pid=999999, tty="pts/2", in_dash=True),
            )
            pf_ok = ad.baton_preflight(
                idle_sess,
                registry_path=reg_path,
                accounts_dir=accounts_dir,
                agy_data_dir=agy_data,
                base_home=base_home,
            )
            self.assertTrue(pf_ok["ok"], pf_ok["blockers"])
            self.assertEqual(pf_ok["target_slot"], "pro3")
            self.assertTrue(all(pf_ok["checks"].values()))

            pf_same = ad.baton_preflight(
                idle_sess,
                target_slot="pro_dup",
                registry_path=reg_path,
                accounts_dir=accounts_dir,
                agy_data_dir=agy_data,
                base_home=base_home,
            )
            self.assertFalse(pf_same["ok"])
            self.assertFalse(pf_same["checks"]["distinct_identity"])

            busy_sess = ad.AgySession(
                conversation_id="cid-hyd",
                title="Hydrogen Busy",
                preview="",
                step_count=42,
                last_modified="ahora",
                workspace=str(root),
                db_status="CASCADE_RUN_STATUS_RUNNING",
                not_fully_idle=True,
                proc=ad.ProcMemInfo(pid=999999, tty="pts/2", in_dash=True),
            )
            pf_busy = ad.baton_preflight(
                busy_sess,
                registry_path=reg_path,
                accounts_dir=accounts_dir,
                agy_data_dir=agy_data,
                base_home=base_home,
            )
            self.assertFalse(pf_busy["ok"])
            self.assertFalse(pf_busy["checks"]["no_duplicate_operation"])

    def test_transcript_busy_prevents_false_hibernate_and_default_auto_hib_disabled(self) -> None:
        self.assertEqual(ad.DEFAULT_IDLE_HIBERNATE_SEC, 0)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            logs_dir = root / "brain" / "cid-work" / ".system_generated" / "logs"
            logs_dir.mkdir(parents=True)
            tp = logs_dir / "transcript.jsonl"
            tp.write_text(
                '{"step_index":1,"type":"PLANNER_RESPONSE","content":"","tool_calls":[{"name":"read_file"}]}\n',
                encoding="utf-8",
            )
            orig_brain = ad.BRAIN_DIR
            try:
                ad.BRAIN_DIR = root / "brain"
                self.assertTrue(ad.is_transcript_turn_active("cid-work", now=time.time()))
            finally:
                ad.BRAIN_DIR = orig_brain

            sess = ad.AgySession(
                conversation_id="cid-work",
                title="Working Agent",
                preview="",
                step_count=50,
                last_modified="ahora",
                workspace=str(root),
                db_status="CASCADE_RUN_STATUS_IDLE",
                not_fully_idle=False,
                proc=ad.ProcMemInfo(pid=12345, tty="pts/3", in_dash=True, active_children=[]),
                transcript_busy=True,
            )
            self.assertEqual(sess.state_label, "RUNNING")
            self.assertFalse(sess.can_hibernate)

    def test_persistent_custom_titles_and_tmux_rename_adoption(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            titles_file = root / "custom_titles.json"
            saved = ad.save_custom_title("cid-malb", "malbolge", titles_path=titles_file)
            self.assertEqual(saved, "malbolge")
            loaded = ad.load_custom_titles(titles_path=titles_file)
            self.assertEqual(loaded.get("cid-malb"), "malbolge")
            self.assertFalse(ad._is_led_generated_name("malbolge"))
            self.assertTrue(ad._is_led_generated_name("⠋⠁·malbolge- · Running ×"))
            self.assertTrue(ad._is_led_generated_name("●malbolge ×"))

    def test_nvapi_compactor_saves_key_and_compacts_heavy_conversation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg_path = root / "nvapi.json"
            handoffs_dir = root / "handoffs"
            logs_dir = root / "brain" / "cid-heavy" / ".system_generated" / "logs"
            logs_dir.mkdir(parents=True)
            tp = logs_dir / "transcript.jsonl"
            lines = [
                f'{{"step_index":{i},"type":"USER_INPUT","content":"Paso {i}: optimizar el motor Zig y Baton Pass."}}'
                for i in range(180)
            ]
            tp.write_text("\n".join(lines) + "\n", encoding="utf-8")

            ad.save_nvapi_config(api_key="nvapi-testSecretKey1234567890", cfg_path=cfg_path)
            self.assertEqual(cfg_path.stat().st_mode & 0o777, 0o600)
            cfg = ad.load_nvapi_config(cfg_path=cfg_path)
            self.assertEqual(cfg["api_key"], "nvapi-testSecretKey1234567890")
            self.assertIn("...", ad.mask_api_key(cfg["api_key"]))

            orig_brain = ad.BRAIN_DIR
            try:
                ad.BRAIN_DIR = root / "brain"
                heavy_sess = ad.AgySession(
                    conversation_id="cid-heavy",
                    title="Heavy Session",
                    preview="Contexto largo",
                    step_count=180,
                    last_modified="hace 5m",
                    workspace=str(root),
                    db_status="CASCADE_RUN_STATUS_IDLE",
                    not_fully_idle=False,
                    proc=None,
                )
                offer, est_tok, _ = ad.should_offer_compaction(heavy_sess)
                self.assertTrue(offer)
                self.assertGreater(est_tok, 1000)

                class _FakeResp:
                    def __enter__(self):
                        return self

                    def __exit__(self, *args):
                        return False

                    def read(self) -> bytes:
                        return b'{"choices":[{"message":{"content":"## 1. Objetivo\\nContinuar sin gastar tokens."}}]}'

                ok, msg, hfile = ad.compact_conversation_with_nvapi(
                    heavy_sess,
                    cfg_path=cfg_path,
                    handoffs_dir=handoffs_dir,
                    opener=lambda req, timeout=90.0: _FakeResp(),
                )
                self.assertTrue(ok, msg)
                self.assertIsNotNone(hfile)
                assert hfile is not None
                self.assertTrue(hfile.is_file())
                self.assertIn("Continuar sin gastar tokens", hfile.read_text(encoding="utf-8"))

                # Vault listing, metadata parsing, lookup & cleanup
                self.assertEqual(ad.get_vault_handoff("cid-heavy", handoffs_dir=handoffs_dir), hfile)
                vault = ad.list_compacted_vault(handoffs_dir=handoffs_dir)
                self.assertEqual(len(vault), 1)
                self.assertEqual(vault[0]["title"], "Heavy Session")
                self.assertEqual(vault[0]["conv_id"], "cid-heavy")
                self.assertEqual(vault[0]["workspace"], str(root))
                self.assertTrue(ad.delete_vault_handoff(hfile))
                self.assertIsNone(ad.get_vault_handoff("cid-heavy", handoffs_dir=handoffs_dir))
            finally:
                ad.BRAIN_DIR = orig_brain

    def test_command_buttons_include_vault_and_no_hover_mousemask(self) -> None:
        btns = ad.build_command_buttons(48)
        ids = {b.action_id for b in btns}
        self.assertIn("vault", ids)
        self.assertIn("vault_compact", ids)
        self.assertIn("compact", ids)
        here = Path(ad.__file__).resolve().parent  # shim + agy_dash package (split 2026-11-18)
        src = "".join(p.read_text(encoding="utf-8") for p in [here / "agy_dashboard.py", *sorted((here / "agy_dash").glob("*.py"))])
        self.assertNotIn("curses.REPORT_MOUSE_POSITION", src)
        self.assertIn("handoff.load", src)

    def test_context_heartbeat_5min_cooldown_and_ranges(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tfile = root / "transcript.jsonl"
            hb_dir = root / "heartbeats"

            tfile.write_text(
                '{"step_index":1,"type":"PLANNER_RESPONSE","input_tokens":1500,"cache_read_tokens":165000,"output_tokens":400,"model":"gemini-4-hydrogen-high"}\n',
                encoding="utf-8",
            )
            m = ad.measure_session_context("cid-hb-1", transcript_path=tfile)
            self.assertEqual(m["source"], "transcript_usage")
            self.assertEqual(m["used_tokens"], 166900)
            self.assertEqual(m["level"], "COMPACT_RECOMMENDED")

            # First heartbeat fires immediately
            hb1 = ad.evaluate_context_heartbeat(
                "cid-hb-1",
                transcript_path=tfile,
                cwd=str(root),
                interval_sec=300,
                now=1000.0,
                heartbeats_dir=hb_dir,
            )
            self.assertTrue(hb1["should_notify"])
            self.assertIn("handoff.self_compact", hb1["notice"])
            self.assertIn("166,900", hb1["notice"])

            # Within 5 minutes (at +120s) at same level -> suppressed (0 noise)
            hb2 = ad.evaluate_context_heartbeat(
                "cid-hb-1",
                transcript_path=tfile,
                cwd=str(root),
                interval_sec=300,
                now=1120.0,
                heartbeats_dir=hb_dir,
            )
            self.assertFalse(hb2["should_notify"])

            # After 5 minutes (+305s) -> fires again
            hb3 = ad.evaluate_context_heartbeat(
                "cid-hb-1",
                transcript_path=tfile,
                cwd=str(root),
                interval_sec=300,
                now=1305.0,
                heartbeats_dir=hb_dir,
            )
            self.assertTrue(hb3["should_notify"])

            # Escalation to COMPACT_URGENT fires even before 5m cooldown
            tfile.write_text(
                '{"step_index":2,"type":"PLANNER_RESPONSE","input_tokens":2000,"cache_read_tokens":360000,"output_tokens":500,"model":"gemini-4-hydrogen-high"}\n',
                encoding="utf-8",
            )
            hb4 = ad.evaluate_context_heartbeat(
                "cid-hb-1",
                transcript_path=tfile,
                cwd=str(root),
                interval_sec=300,
                now=1320.0,
                heartbeats_dir=hb_dir,
            )
            self.assertTrue(hb4["should_notify"])
            self.assertEqual(hb4["level"], "COMPACT_URGENT")

    def test_prompt_curses_text_cancels_on_esc(self) -> None:
        class _FakeScr:
            def __init__(self, keys):
                self._keys = list(keys)
                self._nodelay = False

            def getmaxyx(self):
                return (30, 120)

            def timeout(self, _ms):
                pass

            def nodelay(self, flag):
                self._nodelay = flag

            def move(self, _y, _x):
                pass

            def clrtoeol(self):
                pass

            def addstr(self, _y, _x, _s, _attr=0):
                pass

            def refresh(self):
                pass

            def get_wch(self):
                return self._keys.pop(0)

            def getch(self):
                if self._nodelay:
                    return -1
                return self._keys.pop(0) if self._keys else -1

        with mock.patch("curses.noecho"), mock.patch("curses.curs_set"), mock.patch("curses.color_pair", return_value=0):
            # Esc cancels even if user typed partial text first
            res_esc = ad._prompt_curses_text(_FakeScr(["a", "b", "\x1b"]), "Key", "fallback")
            self.assertEqual(res_esc, "")

            # Enter confirms typed text
            res_ok = ad._prompt_curses_text(_FakeScr(["n", "v", "\n"]), "Key", "fallback")
            self.assertEqual(res_ok, "nv")


class SafetyFixesTests(unittest.TestCase):
    """Audit 2026-11-18: kills are verified, snapshots re-checked, purges rescue data, self is unambiguous."""

    def _fake_agy(self, td: str):
        import subprocess
        script = Path(td) / "agy"
        script.write_text("#!/bin/sh\nwhile :; do sleep 0.1; done\n")
        script.chmod(0o755)
        proc = subprocess.Popen([str(script)])
        self.addCleanup(lambda: (proc.poll() is None and proc.kill(), proc.wait()))
        for _ in range(50):
            if ad._proc_is_agy(proc.pid):
                break
            time.sleep(0.02)
        return proc

    def test_terminate_verified_kills_the_same_agy_and_waits(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            proc = self._fake_agy(td)
            info = ad.ProcMemInfo(pid=proc.pid, start_ticks=ad.proc_start_ticks(proc.pid))
            ok, why = ad.terminate_verified(info, timeout_s=5)
            self.assertTrue(ok, why)
            self.assertIsNotNone(proc.wait(timeout=2))

    def test_terminate_verified_refuses_recycled_pid_non_agy_and_self(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            proc = self._fake_agy(td)
            stale = ad.ProcMemInfo(pid=proc.pid, start_ticks=ad.proc_start_ticks(proc.pid) + 1)
            ok, why = ad.terminate_verified(stale)
            self.assertFalse(ok)
            self.assertIn("reutilizado", why)
            self.assertIsNone(proc.poll(), "a recycled PID must never be signalled")
        import subprocess
        other = subprocess.Popen(["sleep", "30"])
        self.addCleanup(lambda: (other.kill(), other.wait()))
        ok, why = ad.terminate_verified(ad.ProcMemInfo(pid=other.pid, start_ticks=ad.proc_start_ticks(other.pid)))
        self.assertFalse(ok)
        self.assertIn("no es agy", why)
        import os
        ok, _ = ad.terminate_verified(ad.ProcMemInfo(pid=os.getpid()))
        self.assertFalse(ok)

    def _sess(self, **kw) -> "ad.AgySession":
        base = dict(conversation_id="c" * 36, title="T", preview="", step_count=1, last_modified="",
                    workspace="/w", db_status="CASCADE_RUN_STATUS_IDLE", not_fully_idle=False,
                    proc=ad.ProcMemInfo(pid=999999, in_dash=True))
        base.update(kw)
        return ad.AgySession(**base)

    def test_hibernate_rechecks_fresh_state_before_killing(self) -> None:
        stale_idle = self._sess()
        now_busy = self._sess(db_status="CASCADE_RUN_STATUS_RUNNING")
        with mock.patch.object(ad, "refresh_session", return_value=now_busy), \
                mock.patch.object(ad, "terminate_verified") as kill:
            ok, msg = ad.hibernate_session(stale_idle)
        self.assertFalse(ok)
        kill.assert_not_called()

    def test_hibernate_does_not_register_when_a_process_survives(self) -> None:
        s = self._sess()
        with mock.patch.object(ad, "refresh_session", return_value=s), \
                mock.patch.object(ad, "terminate_verified", return_value=(False, "sigue vivo")), \
                mock.patch.object(ad, "save_hibernated_registry") as save:
            ok, msg = ad.hibernate_session(s)
        self.assertFalse(ok)
        self.assertIn("sigue vivo", msg)
        save.assert_not_called()

    def test_dedup_leaves_busy_conversations_alone(self) -> None:
        ext = ad.ProcMemInfo(pid=999998, in_dash=False)
        busy = self._sess(extra_procs=[ext], transcript_busy=True)
        with mock.patch.object(ad, "refresh_session", return_value=busy), \
                mock.patch.object(ad, "terminate_verified") as kill:
            self.assertEqual(ad.deduplicate_session_procs(busy), 0)
        kill.assert_not_called()

    def test_detect_caller_uses_own_pane_and_refuses_ambiguous_workspace(self) -> None:
        a = self._sess(conversation_id="a" * 36, proc=ad.ProcMemInfo(pid=999997), workspace=str(Path.cwd()))
        b = self._sess(conversation_id="b" * 36, proc=ad.ProcMemInfo(pid=999996), workspace=str(Path.cwd()))
        env = {k: v for k, v in __import__("os").environ.items() if k not in ("AGY_CONVERSATION_ID", "TMUX", "TMUX_PANE")}
        with mock.patch.dict("os.environ", env, clear=True):
            self.assertIsNone(ad.detect_caller_conversation_id([a, b]))
            self.assertEqual(ad.detect_caller_conversation_id([a]), a.conversation_id)
        env.update(TMUX="/tmp/tmux-1/agy-dash,1,0", TMUX_PANE="%7")
        with mock.patch.dict("os.environ", env, clear=True), mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="b" * 36 + "\n")
            self.assertEqual(ad.detect_caller_conversation_id([a, b]), "b" * 36)
            self.assertIn("%7", run.call_args[0][0])

    def test_remove_account_rescues_diverged_shared_db(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            accts = Path(td) / "accounts"
            reg = accts / "registry.json"
            data = Path(td) / "data"
            data.mkdir()
            ad.ensure_isolated_home("pro3", base_home=Path(td), agy_data_dir=data, accounts_dir=accts)
            db = accts / "pro3" / "home" / ".gemini" / "antigravity-cli" / "conversation_summaries.db"
            if db.is_symlink():
                db.unlink()
            db.write_bytes(b"real summaries")  # AGY rewrote the shared symlink as a real file
            reg.write_text(json.dumps({"accounts": {"pro3": {"mode": "isolated", "email": "x@y"}},
                                       "active_slot": "primary", "conv_accounts": {}}))
            with mock.patch.object(ad, "_active_pids_for_slot", return_value=[]):
                ok, msg = ad.remove_account_slot("pro3", confirmed=True, registry_path=reg,
                                                 accounts_dir=accts, base_home=Path(td))
            self.assertTrue(ok, msg)
            rescued = list((accts / ".rescued").glob("pro3-*/.gemini/antigravity-cli/conversation_summaries.db"))
            self.assertEqual(len(rescued), 1)
            self.assertEqual(rescued[0].read_bytes(), b"real summaries")
            self.assertIn("rescatados", msg)

    def test_tui_has_no_undefined_spinner_name(self) -> None:
        src = Path(ad.__file__).read_text()
        self.assertNotIn("_LED_SPINNER_FRAMES", src)


if __name__ == "__main__":
    unittest.main()
