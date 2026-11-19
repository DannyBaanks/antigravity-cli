#!/usr/bin/env python3
"""Regression tests for the EGM goldfish review fixes (2026-11-19, round 1 on agy_dashboard.py)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agy_dash import accounts, compact, paths, procs, sessions, tmuxctl  # noqa: E402


def _sess(**kw) -> sessions.AgySession:
    base = dict(conversation_id="c" * 36, title="t", preview="", step_count=1, last_modified="",
                workspace="/tmp", db_status="CASCADE_RUN_STATUS_IDLE", not_fully_idle=False)
    base.update(kw)
    return sessions.AgySession(**base)


class ProcSafety(unittest.TestCase):
    def test_proc_is_agy_requires_exact_basename(self) -> None:
        for argv, ok in ((b"/home/x/.local/bin/agy\0", True), (b"python3\0/x/agy_dashboard.py\0tui\0", False),
                         (b"/x/agy-fast-core\0scan\0", False), (b"/x/agy-dash\0", False)):
            with mock.patch.object(Path, "read_bytes", return_value=argv):
                self.assertEqual(procs._proc_is_agy(1234), ok, argv)

    def test_terminate_refuses_unknown_start_time(self) -> None:
        p = procs.ProcMemInfo(pid=os.getpid() + 999999, start_ticks=0)
        with mock.patch.object(procs, "proc_start_ticks", return_value=42), \
             mock.patch.object(procs, "is_ancestor_pid", return_value=False), \
             mock.patch.object(procs.os, "kill") as kill:
            ok, why = procs.terminate_verified(p)
        self.assertFalse(ok)
        self.assertIn("desconocida", why)
        kill.assert_not_called()

    def test_failed_scan_is_flagged_not_empty_success(self) -> None:
        with mock.patch.object(procs, "ensure_zig_core", return_value=True), \
             mock.patch.object(procs._tmuxctl, "_get_tmux_dash_server_pids", return_value=set()), \
             mock.patch.object(procs.subprocess, "run", side_effect=subprocess.TimeoutExpired("x", 1.5)):
            _, _, by_conv = procs.zig_scan()
        self.assertEqual(by_conv, {})
        self.assertTrue(procs.scan_failed())
        ok, msg = sessions.hibernate_session(_sess(proc=procs.ProcMemInfo(pid=999999, start_ticks=1)))
        self.assertFalse(ok)
        self.assertIn("escaneo", msg)
        procs._mark_scan(False)

    def test_zig_build_failure_is_negatively_cached(self) -> None:
        with mock.patch.object(procs._paths, "ZIG_CORE_BIN", Path("/nonexistent/agy-fast-core")), \
             mock.patch.object(procs.shutil, "which", return_value="/usr/bin/zig"), \
             mock.patch.object(Path, "is_file", return_value=True), \
             mock.patch.object(procs.os, "access", return_value=False), \
             mock.patch.object(procs.subprocess, "run", side_effect=OSError("boom")) as run:
            procs._ZIG_BUILD_FAILED_AT = 0.0
            self.assertFalse(procs.ensure_zig_core())
            self.assertFalse(procs.ensure_zig_core())
        self.assertEqual(run.call_count, 1)
        procs._ZIG_BUILD_FAILED_AT = 0.0


class SessionState(unittest.TestCase):
    def test_unknown_db_row_is_never_hibernatable(self) -> None:
        s = _sess(db_status=sessions.DB_STATUS_UNKNOWN, not_fully_idle=True,
                  proc=procs.ProcMemInfo(pid=999999, start_ticks=1))
        with mock.patch.object(procs, "is_ancestor_pid", return_value=False):
            self.assertFalse(s.can_hibernate)
            self.assertEqual(s.state_label, "UNKNOWN")

    def test_strip_controls_removes_escape_sequences(self) -> None:
        dirty = "ok\x1b]52;c;ZXZpbA==\x07 fin\x9b31m"
        self.assertEqual(sessions.strip_controls(dirty), "ok]52;c;ZXZpbA== fin31m")

    def test_close_stale_dash_duplicate_never_mid_turn(self) -> None:
        ext = procs.ProcMemInfo(pid=1, active_children=["bash"], in_dash=False, start_ticks=1)
        dash = procs.ProcMemInfo(pid=2, in_dash=True, start_ticks=1)
        s = _sess(proc=ext, extra_procs=[dash], transcript_busy=True)
        with mock.patch.object(sessions, "refresh_session", return_value=s), \
             mock.patch.object(procs, "terminate_verified") as term:
            self.assertEqual(sessions.close_stale_dash_duplicates(s), 0)
        term.assert_not_called()


class Tabs(unittest.TestCase):
    def test_user_tab_titles_never_carry_the_dash_marker(self) -> None:
        self.assertNotIn("DASH", tmuxctl._short_tab_title("AGY DASH fix"))
        name = tmuxctl.format_led_tab_title(_sess(title="DASH work"), 0, "editing DASH", busy=True)
        self.assertNotIn("DASH", name)


class Quota(unittest.TestCase):
    def test_missing_remaining_fraction_is_unknown_not_zero(self) -> None:
        raw = json.dumps({"status": "SUCCESS", "command": {"data": {"groups": [
            {"name": "Gemini", "buckets": [{"id": "a", "remaining_fraction": None}, {"id": "b"},
                                            {"id": "c", "remaining_fraction": 0.5}]}]}}})
        parsed = accounts.parse_usage_output(raw)
        self.assertEqual([b["pct"] for b in parsed["buckets"]], [50.0])
        self.assertEqual(parsed["min_pct"], 50.0)

    def test_registry_update_does_not_resurrect_removed_slot(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            reg_file = Path(td) / "accounts.json"
            accounts.save_accounts_registry({"accounts": {"primary": {"slot": "primary", "mode": "system"}}},
                                            registry_path=reg_file)

            def _m(reg: dict) -> None:
                if "gone" in reg["accounts"]:
                    reg["accounts"]["gone"]["authenticated"] = False

            reg = accounts.update_accounts_registry(_m, registry_path=reg_file)
            self.assertNotIn("gone", reg["accounts"])
            self.assertEqual(list(Path(td).glob("*.tmp")), [])


class Nvapi(unittest.TestCase):
    def test_save_model_does_not_persist_env_key(self) -> None:
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"NVIDIA_API_KEY": "nvapi-envsecret"}):
            cfg = Path(td) / "nv.json"
            compact.save_nvapi_config(model="x/model", config_path=cfg)
            self.assertNotIn("nvapi-envsecret", cfg.read_text())
            self.assertEqual(compact.load_nvapi_config(config_path=cfg)["api_key"], "nvapi-envsecret")

    def test_base_url_must_be_https(self) -> None:
        self.assertTrue(compact._nvapi_url_ok("https://integrate.api.nvidia.com/v1"))
        self.assertFalse(compact._nvapi_url_ok("http://evil.example/v1"))
        self.assertFalse(compact._nvapi_url_ok("file:///etc/passwd"))


class Rescue(unittest.TestCase):
    def test_diverged_items_include_home_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "slot1" / "home"
            (home / ".cache").mkdir(parents=True)
            (home / ".config" / "gh").mkdir(parents=True)
            found = {p.name for p in accounts.diverged_shared_items("slot1", accounts_dir=Path(td))}
            self.assertTrue({".cache", "gh"} <= found)


class AtomicWrite(unittest.TestCase):
    def test_atomic_write_sets_mode_and_leaves_no_tmp(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "x.json"
            paths.atomic_write_text(f, "{}", mode=0o600)
            self.assertEqual(f.read_text(), "{}")
            self.assertEqual(f.stat().st_mode & 0o777, 0o600)
            self.assertEqual([p.name for p in Path(td).iterdir()], ["x.json"])


if __name__ == "__main__":
    unittest.main()
