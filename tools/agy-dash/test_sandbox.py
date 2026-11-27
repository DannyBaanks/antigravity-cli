"""Shell sandbox tests for agy-dash: PreToolUse hook wrapping (AGY & Claude shapes),
jobs that outlive their wrapper, swarm-ext worker sandboxing, and compaction glue."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

from agy_dash import compact, paths, procs, sandbox, sessions

SID = "93516b12-c0ce-4f11-a5c4-0f7e0ee07855"
BIN = str(paths.SELF_PATH)


def _agy_payload(cmd: str, *, wait_ms: int = 500, is_daemon: bool = False) -> dict:
    return {
        "conversationId": SID,
        "hookEventName": "PreToolUse",
        "toolCall": {
            "id": "toolu_01",
            "name": "run_command",
            "args": {
                "CommandLine": cmd,
                "WaitMsBeforeAsync": wait_ms,
                "IsDaemon": is_daemon,
                "toolSummary": "Background task",
            },
        },
    }


def _claude_payload(cmd: str, bg: bool = True) -> dict:
    return {
        "session_id": SID,
        "tool_name": "Bash",
        "tool_input": {
            "command": cmd,
            "run_in_background": bg,
            "description": "d",
        },
    }


def test_agy_hook_wraps_background_and_swarm_commands(monkeypatch):
    monkeypatch.delenv("AGY_DASH_SANDBOX", raising=False)
    out = sandbox.hook_rewrite(_agy_payload("sleep 5 && echo 'hi'", wait_ms=500))
    assert out is not None
    assert out["decision"] == "ask"
    rewritten = out["overwrite"]["CommandLine"]
    assert rewritten == f"agy-dash sbx run --sid {SID} -- 'sleep 5 && echo '\"'\"'hi'\"'\"''"

    # Foreground normal command (WaitMsBeforeAsync=5000) is left alone
    assert sandbox.hook_rewrite(_agy_payload("ls -la", wait_ms=5000)) is None

    # Daemon command is always wrapped even with WaitMsBeforeAsync=5000
    daemon_out = sandbox.hook_rewrite(_agy_payload("./server", wait_ms=5000, is_daemon=True))
    assert daemon_out is not None and "agy-dash sbx run" in daemon_out["overwrite"]["CommandLine"]

    # External swarm worker (`swarm-ext run`) is always wrapped even with WaitMsBeforeAsync=5000
    swarm_out = sandbox.hook_rewrite(
        _agy_payload("swarm-ext run --worker w1 --model nvapi:nemotron 'review'", wait_ms=5000)
    )
    assert swarm_out is not None and "agy-dash sbx run" in swarm_out["overwrite"]["CommandLine"]

    # Already wrapped -> no loop
    assert sandbox.hook_rewrite(_agy_payload(rewritten, wait_ms=500)) is None

    # Disabled via env -> None
    monkeypatch.setenv("AGY_DASH_SANDBOX", "0")
    assert sandbox.hook_rewrite(_agy_payload("sleep 5", wait_ms=500)) is None


def test_claude_compatible_hook_wraps_only_background_commands(monkeypatch):
    monkeypatch.delenv("AGY_DASH_SANDBOX", raising=False)
    out = sandbox.hook_rewrite(_claude_payload("sleep 5 && echo 'hi'"))
    new = out["hookSpecificOutput"]["updatedInput"]
    assert new["command"] == f"agy-dash sbx run --sid {SID} -- 'sleep 5 && echo '\"'\"'hi'\"'\"''"
    assert new["run_in_background"] is True and new["description"] == "d"
    assert "permissionDecision" not in out["hookSpecificOutput"]
    assert sandbox.hook_rewrite(_claude_payload("ls", bg=False)) is None
    assert sandbox.hook_rewrite(_claude_payload(new["command"])) is None


def test_job_survives_its_wrapper_and_can_be_reattached(tmp_path):
    sbx_dir = tmp_path / "sandbox"
    env = {**os.environ, "AGY_DASH_SANDBOX_DIR": str(sbx_dir)}
    w = subprocess.Popen(
        [
            sys.executable,
            BIN,
            "sbx",
            "run",
            "--sid",
            SID,
            "--",
            "echo uno; sleep 1.5; echo dos; exit 3",
        ],
        env=env,
        stdout=subprocess.PIPE,
    )
    time.sleep(0.8)
    w.terminate()
    w.wait(5)
    job_files = list(sbx_dir.glob("*.json"))
    assert len(job_files) == 1
    job = json.loads(job_files[0].read_text(encoding="utf-8"))
    assert job["status"] == "running"
    r = subprocess.run(
        [sys.executable, BIN, "sbx", "wait", job["id"], "--from-start"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert r.returncode == 3 and "uno" in r.stdout and "dos" in r.stdout
    job = json.loads((sbx_dir / f"{job['id']}.json").read_text(encoding="utf-8"))
    assert job["status"] == "failed" and job["rc"] == 3


def test_sandbox_shells_do_not_block_compaction(monkeypatch):
    pinfo = procs.ProcMemInfo(pid=1234, active_children=["python3(5555)"])
    monkeypatch.setattr(
        procs,
        "shells_of",
        lambda pid, active_children=None, proc_root=Path("/proc"): [
            procs.ShellProc(5555, 60.0, f"agy-dash sbx run --sid {SID} -- 'make'")
        ],
    )
    assert not procs.has_blocking_children(pinfo)

    # Viewer (`sbx wait` / `follow`) also does not block compaction
    monkeypatch.setattr(
        procs,
        "shells_of",
        lambda pid, active_children=None, proc_root=Path("/proc"): [
            procs.ShellProc(5556, 30.0, "rtk agy-dash sbx wait 1127-1")
        ],
    )
    assert not procs.has_blocking_children(pinfo)

    # Non-sandbox shell DOES block compaction
    monkeypatch.setattr(
        procs,
        "shells_of",
        lambda pid, active_children=None, proc_root=Path("/proc"): [
            procs.ShellProc(5557, 30.0, "pytest -q")
        ],
    )
    assert procs.has_blocking_children(pinfo)


def test_digest_and_continue_prompt_mention_the_shells(tmp_path, monkeypatch):
    sbx_dir = tmp_path / "sandbox"
    monkeypatch.setattr(paths, "SANDBOX_DIR", sbx_dir)
    monkeypatch.delenv("AGY_DASH_SANDBOX_DIR", raising=False)
    now = time.time()
    sandbox.save_job(
        "J1",
        {
            "id": "J1",
            "sid": SID,
            "cmd": "zig build test",
            "status": "running",
            "pid": os.getpid(),
            "started": now - 90,
            "log": "/x/J1.log",
        },
        sbx_dir,
    )
    block = sandbox.describe_for_digest(
        SID, ["until ! pgrep -f x; do sleep 3; done"], now=now, sandbox_dir=sbx_dir
    )
    assert "sbx wait J1" in block and "zig build test" in block and "MURIÓ" in block and "pgrep" in block

    tfile = tmp_path / "transcript.jsonl"
    tfile.write_text(
        json.dumps({"step_index": 1, "type": "USER_INPUT", "content": "hola"}) + "\n",
        encoding="utf-8",
    )
    with mock.patch.object(sessions, "transcript_path_for", return_value=tfile):
        digest, _ = compact.build_compaction_digest(SID, "T", "/w", shells=block)
    assert "## SHELLS EN 2º PLANO" in digest and "sbx wait J1" in digest
    assert "sbx wait <id>" in compact.SYSTEM_PROMPT
    cp = compact.continue_prompt({"message": "m", "shells_block": block}, "sigue")
    assert "reengancha" in cp and "sbx wait J1" in cp and cp.endswith("sigue")


def test_hook_leaves_follow_alone_but_not_chains():
    assert sandbox.hook_rewrite(_agy_payload("agy-dash follow egm-1-synth --wait 120", wait_ms=500)) is None
    assert sandbox.hook_rewrite(_agy_payload("rtk agy-dash follow w1", wait_ms=500)) is None
    assert sandbox.hook_rewrite(_agy_payload("agy-dash sbx wait 1127-0001", wait_ms=500)) is None
    # Anything chained after follow could be long-lived work: still sandboxed
    assert sandbox.hook_rewrite(_agy_payload("agy-dash follow w1; sleep 999", wait_ms=500)) is not None
    assert sandbox.hook_rewrite(_agy_payload("agy-dash follow w1 && make", wait_ms=500)) is not None


def test_sbx_hook_cli_returns_neutral_ask_for_foreground_and_overwrite_for_bg():
    fg = subprocess.run(
        [sys.executable, BIN, "sbx-hook"],
        input=json.dumps(_agy_payload("echo hola", wait_ms=5000)),
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert fg.returncode == 0
    assert json.loads(fg.stdout) == {"decision": "ask"}

    bg = subprocess.run(
        [sys.executable, BIN, "sbx-hook"],
        input=json.dumps(_agy_payload("sleep 3", wait_ms=500)),
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert bg.returncode == 0
    parsed = json.loads(bg.stdout)
    assert parsed["decision"] == "ask"
    assert "agy-dash sbx run" in parsed["overwrite"]["CommandLine"]

