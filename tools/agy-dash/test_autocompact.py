"""Pruebas unitarias del prototipo de compactación del hook de AGY."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agy_dash import autocompact as ac  # noqa: E402


class AutoCompactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.transcript = self.root / "transcript.jsonl"
        self.heartbeats = self.root / "heartbeats"
        self.heartbeats.mkdir()
        self.paths_patch = mock.patch.object(ac._paths, "HEARTBEATS_DIR", self.heartbeats)
        self.paths_patch.start()
        self.addCleanup(self.paths_patch.stop)
        self.compacted = self.root / "compacted"
        cpatch = mock.patch.object(ac._paths, "COMPACTED_DIR", self.compacted)
        cpatch.start()
        self.addCleanup(cpatch.stop)
        self.env = {"AGY_DASH_MACHINE_COMPACT": "1"}
        self.payload = {
            "conversationId": "conv-123",
            "transcriptPath": str(self.transcript),
            "modelName": "gemini-test",
            "invocationNum": 0,
        }
        self.spawn = mock.Mock()
        self.measure = mock.Mock(return_value={"used_tokens": 250000})

    def write_transcript(self, *events: dict | str) -> None:
        lines = [event if isinstance(event, str) else json.dumps(event) for event in events]
        self.transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def decide(self, *, used_tokens: int = 250000, **kwargs: object) -> dict:
        self.measure.return_value = {"used_tokens": used_tokens}
        return ac.decide(
            self.payload,
            env=self.env,
            measure=self.measure,
            spawn=self.spawn,
            now=1000,
            **kwargs,
        )

    def test_on_by_default_and_off_with_zero(self) -> None:
        # 2026-11-27: on by default like claude-dash; AGY_DASH_MACHINE_COMPACT=0 is the opt-out
        self.write_transcript({"type": "USER_INPUT", "content": "hola"})
        self.assertEqual(ac.decide(self.payload, env={}, measure=self.measure, spawn=self.spawn,
                                   now=1000)["terminationBehavior"], "terminate")
        self.spawn.reset_mock()
        self.measure.reset_mock()
        (self.heartbeats / "conv-123.autocompact").unlink()
        self.assertEqual(
            ac.decide(
                self.payload,
                env={"AGY_DASH_MACHINE_COMPACT": "0"},
                measure=self.measure,
                spawn=self.spawn,
                now=1000,
            ),
            {},
        )
        self.measure.assert_not_called()
        self.spawn.assert_not_called()

    def test_threshold_below_and_at_or_above(self) -> None:
        self.write_transcript({"type": "USER_INPUT", "content": "sigue"})
        self.assertEqual(self.decide(used_tokens=249999), {})
        self.spawn.assert_not_called()

        result = self.decide(used_tokens=250000)
        self.assertEqual(result["terminationBehavior"], "terminate")
        self.assertEqual(len(self.spawn.call_args_list), 1)

    def test_threshold_can_be_configured_above_and_below(self) -> None:
        self.write_transcript({"type": "USER_INPUT", "content": "sigue"})
        self.env["AGY_DASH_AUTO_COMPACT_TOKENS"] = "300000"
        self.assertEqual(self.decide(used_tokens=299999), {})
        self.env["AGY_DASH_AUTO_COMPACT_TOKENS"] = "200000"
        self.assertEqual(self.decide(used_tokens=200001)["terminationBehavior"], "terminate")

    def test_compact_command_fires_below_threshold_and_keeps_followup(self) -> None:
        self.write_transcript(
            {
                "type": "USER_INPUT",
                "content": "<USER_REQUEST>\n/compactnvidia continúa con el resumen\n</USER_REQUEST>",
            }
        )
        result = self.decide(used_tokens=12)
        self.assertEqual(result["terminationBehavior"], "terminate")
        self.assertEqual(self.spawn.call_args.args[0][-2:], ["--resume-prompt", "continúa con el resumen"])

    def test_invocation_three_does_not_fire(self) -> None:
        self.write_transcript({"type": "USER_INPUT", "content": "/compactnvidia"})
        self.payload["invocationNum"] = 1  # after a tool call, mid-turn (verified 2026-11-18)
        self.assertEqual(self.decide(used_tokens=500000), {})
        self.measure.assert_not_called()
        self.spawn.assert_not_called()

    def test_antidebounce_suppresses_for_600_seconds(self) -> None:
        self.write_transcript({"type": "USER_INPUT", "content": "sigue"})
        (self.heartbeats / "conv-123.autocompact").write_text("500", encoding="utf-8")
        self.assertEqual(self.decide(used_tokens=500000), {})
        self.measure.assert_not_called()
        self.spawn.assert_not_called()

    def test_corrupt_transcript_lines_are_ignored(self) -> None:
        self.write_transcript(
            "{bad json",
            {"type": "USER_INPUT", "content": "primero"},
            "not json either",
            {"type": "USER_INPUT", "content": "<USER_REQUEST>último texto</USER_REQUEST>"},
        )
        self.assertEqual(ac.last_user_input(self.transcript), "último texto")

    def test_invalid_hook_json_returns_empty_object(self) -> None:
        self.assertEqual(ac.hook_main("{no-json", env=self.env), "{}")

    def test_spawn_receives_exact_argv(self) -> None:
        self.write_transcript({"type": "USER_INPUT", "content": "mensaje de seguimiento"})
        with mock.patch.object(ac._paths, "SELF_PATH", self.root / "agy_dashboard.py"):
            self.assertEqual(self.decide(used_tokens=250000)["terminationBehavior"], "terminate")
        self.assertEqual(
            self.spawn.call_args.args[0],
            [
                sys.executable,
                str(self.root / "agy_dashboard.py"),
                "self-compact",
                "conv-123",
                "--hook-cut",
                "--resume-prompt",
                "mensaje de seguimiento",
            ],
        )

    def test_prompt_while_compacting_is_cut_and_queued(self) -> None:
        self.payload["lastUserInput"] = "y luego corre los tests"
        out = self.decide(used_tokens=10, running=lambda cid: True)
        self.assertEqual(out["terminationBehavior"], "terminate")
        self.assertIn("en cola", out["injectSteps"][0]["errorMessage"]["userMessage"])
        self.spawn.assert_not_called()
        self.measure.assert_not_called()
        self.assertEqual(ac.drain_queue("conv-123"), ["y luego corre los tests"])
        self.assertEqual(ac.drain_queue("conv-123"), [])  # consumed

    def test_repeated_compact_command_while_running_is_not_queued(self) -> None:
        self.payload["lastUserInput"] = "/compactnvidia"
        out = self.decide(used_tokens=10, running=lambda cid: True)
        self.assertIn("ya está en curso", out["injectSteps"][0]["errorMessage"]["userMessage"])
        self.assertEqual(ac.drain_queue("conv-123"), [])

    def test_hook_refire_storm_after_trigger_queues_nothing_and_stays_silent(self) -> None:
        # 2026-11-27: after the terminate, AGY re-fired PreInvocation every ~140 ms with the same
        # lastUserInput; each call queued the prompt again and printed one warning line (56 in 8 s).
        self.payload["lastUserInput"] = "si pulamoslo"
        self.spawn.return_value = mock.Mock(pid=4242)
        first = self.decide(used_tokens=300000)
        self.assertIn("compactando con NVIDIA", first["injectSteps"][0]["errorMessage"]["userMessage"])
        for _ in range(ac._SILENT_REPEATS):
            out = self.decide(used_tokens=300000, running=lambda cid: True)
            self.assertEqual(out, {"terminationBehavior": "terminate"})
        # Esc never arrived (tab outside the dash): fall back to the visible error AGY gives up on
        late = self.decide(used_tokens=300000, running=lambda cid: True)
        self.assertIn("en cola", late["injectSteps"][0]["errorMessage"]["userMessage"])
        self.spawn.assert_called_once()
        self.assertEqual(ac.drain_queue("conv-123"), [])  # already travels as --resume-prompt

    def test_new_prompt_while_compacting_is_queued_once_then_silent(self) -> None:
        self.payload["lastUserInput"] = "y luego corre los tests"
        first = self.decide(used_tokens=10, running=lambda cid: True)
        self.assertIn("en cola", first["injectSteps"][0]["errorMessage"]["userMessage"])
        again = self.decide(used_tokens=10, running=lambda cid: True)
        self.assertEqual(again, {"terminationBehavior": "terminate"})
        self.assertEqual(ac.drain_queue("conv-123"), ["y luego corre los tests"])

    def test_drain_dedupes_legacy_queue_with_repeats(self) -> None:
        f = ac.queue_file("conv-123")
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("".join(json.dumps({"at": i, "text": t}) + "\n"
                             for i, t in enumerate(["a", "a", "b", " a ", "b"])), encoding="utf-8")
        self.assertEqual(ac.drain_queue("conv-123"), ["a", "b"])

    def test_worker_pid_is_recorded_and_liveness_checks_argv(self) -> None:
        self.write_transcript({"type": "USER_INPUT", "content": "sigue"})
        self.spawn.return_value = mock.Mock(pid=4242)
        self.decide(used_tokens=300000)
        self.assertEqual(json.loads(ac.running_file("conv-123").read_text())["pid"], 4242)
        proc = self.root / "proc"
        (proc / "4242").mkdir(parents=True)
        (proc / "4242" / "cmdline").write_bytes(b"python3\0agy_dashboard.py\0self-compact\0conv-123\0")
        self.assertTrue(ac.compaction_running("conv-123", proc_root=proc))
        (proc / "4242" / "cmdline").write_bytes(b"bash\0")  # pid recycled by something else
        self.assertFalse(ac.compaction_running("conv-123", proc_root=proc))

    def test_threshold_zero_disables_auto_but_not_the_command(self) -> None:
        self.env["AGY_DASH_AUTO_COMPACT_TOKENS"] = "0"
        self.payload["lastUserInput"] = "sigue"
        self.assertEqual(self.decide(used_tokens=900000), {})
        self.payload["lastUserInput"] = "/compactnvidia"
        self.assertEqual(self.decide(used_tokens=900000)["terminationBehavior"], "terminate")


if __name__ == "__main__":
    unittest.main()


class HeartbeatHookWiringTests(unittest.TestCase):
    """cmd_heartbeat_hook must defer to autocompact.decide and print its verdict."""

    def _run(self, decision: dict) -> str:
        import argparse
        import io
        import agy_dash.cli as cli
        payload = json.dumps({"conversationId": "c1", "hookEventName": "PreInvocation"})
        out = io.StringIO()
        with mock.patch.object(cli._autocompact, "decide", return_value=decision) as dec, \
             mock.patch("sys.stdin", io.StringIO(payload)), \
             mock.patch.object(cli._compact, "evaluate_context_heartbeat", return_value={"should_notify": False}), \
             mock.patch("sys.stdout", out):
            rc = cli.cmd_heartbeat_hook(argparse.Namespace(status=False, force=False))
        self.assertEqual(rc, 0)
        dec.assert_called_once()
        return out.getvalue().strip()

    def test_terminate_verdict_is_printed(self) -> None:
        verdict = {"injectSteps": [{"errorMessage": {"userMessage": "x"}}], "terminationBehavior": "terminate"}
        self.assertEqual(json.loads(self._run(verdict)), verdict)

    def test_empty_verdict_falls_through_to_heartbeat(self) -> None:
        self.assertEqual(self._run({}), "{}")


class LastUserInputPayloadTests(unittest.TestCase):
    def test_payload_last_user_input_wins_over_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            hb = Path(td) / "hb"
            spawned = []
            with mock.patch.object(ac._paths, "HEARTBEATS_DIR", hb):
                out = ac.decide(
                    {"conversationId": "c9", "invocationNum": 0, "lastUserInput": "/compactnvidia sigue con X",
                     "transcriptPath": str(Path(td) / "missing.jsonl")},
                    env={"AGY_DASH_MACHINE_COMPACT": "1"},
                    measure=lambda *a, **k: {"used_tokens": 10},
                    spawn=spawned.append,
                    now=1000.0,
                )
            self.assertEqual(out.get("terminationBehavior"), "terminate")
            self.assertEqual(spawned[0][-1], "sigue con X")
            self.assertIn("/compactnvidia", out["injectSteps"][0]["errorMessage"]["userMessage"])


class NvapiFallbackTests(unittest.TestCase):
    """2026-11-27: 550B timed out and every fallback was end-of-life (HTTP 410); the error blamed
    the 550B with the 70B's 410 and the Nemotron 3.5 fallbacks need thinking off."""

    def _run(self, opener):
        from agy_dash import compact as cp
        sess = mock.Mock(conversation_id="c1", title="T", workspace="/tmp", step_count=1)
        cfg = {"api_key": "k", "base_url": "https://x.invalid/v1", "model": "nvidia/nemotron-3-ultra-550b-a55b"}
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        with mock.patch.object(cp, "load_nvapi_config", return_value=cfg), \
             mock.patch.object(cp, "_shells_now", return_value=""), \
             mock.patch.object(cp, "build_compaction_digest", return_value=("digest", 400)):
            return cp.compact_conversation_with_nvapi(sess, handoffs_dir=Path(td.name), opener=opener)

    def test_every_candidate_failure_is_named(self) -> None:
        import urllib.error
        seen = []

        def opener(req, timeout=0):
            seen.append(json.loads(req.data)["model"])
            if len(seen) == 1:
                raise urllib.error.URLError(TimeoutError("timed out"))
            raise urllib.error.HTTPError(req.full_url, 410, "Gone", {}, None)
        ok, msg, _ = self._run(opener)
        self.assertFalse(ok)
        self.assertEqual(len(seen), 3)
        self.assertNotIn("nemotron-70b", " ".join(seen))
        self.assertIn("nemotron-3-ultra-550b-a55b: timed out", msg)
        self.assertIn("lightning-30b-a3b: HTTP 410", msg)

    def test_fallback_answers_with_thinking_off(self) -> None:
        import io
        import urllib.error
        bodies = []

        def opener(req, timeout=0):
            body = json.loads(req.data)
            bodies.append(body)
            if len(bodies) == 1:
                raise urllib.error.URLError(TimeoutError("timed out"))
            resp = mock.MagicMock()
            resp.__enter__.return_value = io.BytesIO(b'{"choices":[{"message":{"content":"## 1. OBJ"}}]}')
            return resp
        ok, msg, hfile = self._run(opener)
        self.assertTrue(ok, msg)
        self.assertNotIn("chat_template_kwargs", bodies[0])
        self.assertEqual(bodies[1]["chat_template_kwargs"], {"enable_thinking": False})


class InPlaceRelaunchTests(unittest.TestCase):
    """compact.respawn_compacted_session_in_place: queue delivered, no duplicate, relaunch verified."""

    def setUp(self) -> None:
        from agy_dash import compact as cp
        from agy_dash import sessions as ss
        self.cp = cp
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.root = Path(td.name)
        for name, val in (("COMPACTED_DIR", self.root / "compacted"), ("SELF_PATH", self.root / "agy_dashboard.py")):
            pt = mock.patch.object(cp._paths, name, val)
            pt.start()
            self.addCleanup(pt.stop)
        pt = mock.patch.object(cp._accounts, "load_accounts_registry", return_value={})
        pt.start()
        self.addCleanup(pt.stop)
        self.sess = ss.AgySession(conversation_id="abcdef12-0000", title="T", preview="", step_count=0,
                                  last_modified="", workspace=str(self.root), db_status="", not_fully_idle=False)
        self.calls: list[tuple] = []

    def _tmux(self, rc_respawn: int = 0):
        import subprocess

        def tmux(*args, **k):
            self.calls.append(args)
            return subprocess.CompletedProcess(args, rc_respawn if args[0] == "respawn-pane" else 0, "", "")
        return mock.patch.object(self.cp._tmuxctl, "tmux_cmd", side_effect=tmux)

    def test_queued_prompts_go_into_the_boot_prompt_and_relaunch_is_verified(self) -> None:
        ac.enqueue("abcdef12-0000", "corre los tests", 1.0)
        verify = mock.Mock(return_value=(True, ""))
        with self._tmux():
            msg = self.cp.respawn_compacted_session_in_place(self.sess, self.root / "h.md", resume_prompt="sigue",
                                                            win_idx="3", verify=verify)
        self.assertIn("continuada in-place", msg)
        verify.assert_called_once_with("agy:3")
        cmd = [c for c in self.calls if c[0] == "respawn-pane"][0][-1]
        self.assertIn("corre los tests", cmd)
        self.assertEqual(ac.drain_queue("abcdef12-0000"), [])

    def test_dead_pane_after_relaunch_is_reported_not_called_continued(self) -> None:
        with self._tmux():
            msg = self.cp.respawn_compacted_session_in_place(
                self.sess, self.root / "h.md", win_idx="3", verify=lambda t: (False, "agy salió al arrancar (código 1)"))
        self.assertIn("agy salió al arrancar", msg)
        self.assertIn("--respawn-only", msg)
        with self._tmux(rc_respawn=1):
            msg = self.cp.respawn_compacted_session_in_place(self.sess, self.root / "h.md", win_idx="3",
                                                            verify=lambda t: (True, ""))
        self.assertIn("tmux no pudo relanzar", msg)

    def test_outside_dash_with_agy_alive_does_not_open_a_duplicate(self) -> None:
        live = mock.Mock(proc=object())
        with self._tmux(), mock.patch.object(self.cp._tmuxctl, "_find_tmux_window_by_conv", return_value=None), \
             mock.patch.object(self.cp._sessions, "refresh_session", return_value=live):
            msg = self.cp.respawn_compacted_session_in_place(self.sess, self.root / "h.md")
        self.assertIn("fuera del dash", msg)
        self.assertFalse([c for c in self.calls if c[0] in ("new-window", "respawn-pane")])

    def test_await_pane_alive(self) -> None:
        import subprocess
        t = iter([0.0, 0.0, 9.0])
        alive = lambda *a, **k: subprocess.CompletedProcess(a, 0, "0 0\n", "")
        self.assertEqual(self.cp.await_pane_alive("agy:1", 8.0, tmux=alive, clock=lambda: next(t),
                                                  sleep=lambda s: None), (True, ""))
        dead = lambda *a, **k: subprocess.CompletedProcess(a, 0, "1 2\n", "")
        self.assertEqual(self.cp.await_pane_alive("agy:1", tmux=dead)[0], False)
        gone = lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "can't find window")
        self.assertIn("se cerró", self.cp.await_pane_alive("agy:1", tmux=gone)[1])


class HookTerminationIdleTests(unittest.TestCase):
    """After a hook terminates a turn (USER_INPUT + ERROR_MESSAGE DONE), the session is idle."""

    def test_error_message_done_ends_turn_instead_of_600s_user_input_wait(self) -> None:
        from agy_dash import sessions as ss

        with tempfile.TemporaryDirectory() as td:
            tpath = Path(td) / "transcript.jsonl"
            tpath.write_text(
                "\n".join([
                    json.dumps({"step_index": 0, "type": "USER_INPUT", "status": "DONE", "content": "/compactnvidia"}),
                    json.dumps({"step_index": 1, "type": "ERROR_MESSAGE", "status": "DONE", "error": "compactando"}),
                ]) + "\n",
                encoding="utf-8",
            )
            mtime = tpath.stat().st_mtime
            ss._TRANSCRIPT_BUSY_CACHE.pop("conv-err-done", None)
            with mock.patch.object(ss, "transcript_path_for", return_value=tpath):
                # Within max_recent_sec (3s) -> still settling
                self.assertTrue(ss.is_transcript_turn_active("conv-err-done", max_recent_sec=3.0, now=mtime + 1.0))
                # Past max_recent_sec (e.g. 5s, NOT 600s!) -> idle immediately
                self.assertFalse(ss.is_transcript_turn_active("conv-err-done", max_recent_sec=3.0, now=mtime + 5.0))

    def test_user_input_without_error_message_stays_active(self) -> None:
        from agy_dash import sessions as ss

        with tempfile.TemporaryDirectory() as td:
            tpath = Path(td) / "transcript.jsonl"
            tpath.write_text(
                json.dumps({"step_index": 0, "type": "USER_INPUT", "status": "DONE", "content": "trabaja"}) + "\n",
                encoding="utf-8",
            )
            mtime = tpath.stat().st_mtime
            ss._TRANSCRIPT_BUSY_CACHE.pop("conv-user-only", None)
            with mock.patch.object(ss, "transcript_path_for", return_value=tpath):
                self.assertTrue(ss.is_transcript_turn_active("conv-user-only", max_recent_sec=3.0, now=mtime + 10.0))

    def test_spawn_detached_writes_worker_output_to_log(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cdir = Path(td) / "compacted"
            with mock.patch.object(ac._paths, "COMPACTED_DIR", cdir):
                proc = ac._spawn_detached([sys.executable, "-c", "print('worker-ok')"])
                proc.wait(timeout=5)
            self.assertIn("worker-ok", (cdir / ".last_self_compact.log").read_text(encoding="utf-8"))

    def test_decide_forwards_workspace_cwd_to_self_compact(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            hb = Path(td) / "hb"
            spawned = []
            with mock.patch.object(ac._paths, "HEARTBEATS_DIR", hb), \
                 mock.patch.object(ac._paths, "COMPACTED_DIR", Path(td) / "compacted"):
                out = ac.decide(
                    {
                        "conversationId": "c-ws",
                        "invocationNum": 0,
                        "lastUserInput": "/compactnvidia sigue",
                        "workspacePaths": ["/home/danny/Development/ISyCo"],
                    },
                    env={"AGY_DASH_MACHINE_COMPACT": "1"},
                    measure=lambda *a, **k: {"used_tokens": 10},
                    spawn=spawned.append,
                    now=1000.0,
                )
            self.assertEqual(out.get("terminationBehavior"), "terminate")
            self.assertIn("--cwd", spawned[0])
            self.assertEqual(spawned[0][spawned[0].index("--cwd") + 1], "/home/danny/Development/ISyCo")
