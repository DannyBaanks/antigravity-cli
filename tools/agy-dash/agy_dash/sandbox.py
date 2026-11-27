"""Shell sandbox for AGY (agy-dash): background commands and swarm workers that survive closing/compacting the session.

When AGY respawns a pane (`tmux respawn-pane -k`) during in-place NVIDIA compaction, any child process
inside AGY's process tree is killed. The PreToolUse hook (`agy-dash sbx-hook`) wraps background
`run_command` calls (WaitMsBeforeAsync <= 1000, IsDaemon=true, or `swarm-ext run` workers) into
`agy-dash sbx run --sid <conv_id> -- '<cmd>'`:

  * the real command runs under a detached runner (`setsid -f` -> reparented to systemd --user),
    outside AGY's tree, with its output in `<SANDBOX_DIR>/<id>.log` and its exit code in `<id>.json`;
  * the wrapper that AGY sees follows that log and exits with the job's code, so `run_command`
    behaves normally. Killing the wrapper on compaction leaves the job running.

After compaction, the handoff and continuation boot prompt list every running sandbox job so the
resumed session re-attaches with `agy-dash sbx wait <id>`.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import paths as _paths
from . import procs as _procs

WRAP_PREFIX = "agy-dash sbx run "
_WRAPPED_RE = re.compile(
    r"(?:^|\s|/)(?:agy-dash|agy_dashboard\.py|claude-dash)\s+sbx\s+run\b"
)
_VIEWER_RE = re.compile(
    r"(?:^|\s|/)(?:agy-dash|agy_dashboard\.py|claude-dash)\s+(?:follow|sbx\s+(?:wait|tail))\b"
)
_CHAIN_OPS = ("&&", "||", ";", "|", "`", "$(", "\n")
NO_WRAP_PREFIXES = (
    "agy-dash follow ",
    "rtk agy-dash follow ",
    "claude-dash follow ",
    "rtk claude-dash follow ",
)


def fmt_dur(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def sbx_dir(sandbox_dir: Path | None = None) -> Path:
    if sandbox_dir is not None:
        return Path(sandbox_dir)
    env_dir = (os.environ.get("AGY_DASH_SANDBOX_DIR") or "").strip()
    if env_dir:
        return Path(env_dir)
    return _paths.SANDBOX_DIR


def job_file(jid: str, sandbox_dir: Path | None = None) -> Path:
    return sbx_dir(sandbox_dir) / f"{jid}.json"


def log_file(jid: str, sandbox_dir: Path | None = None) -> Path:
    return sbx_dir(sandbox_dir) / f"{jid}.log"


def new_id() -> str:
    return time.strftime("%m%d-%H%M%S") + f"-{os.getpid() % 10000:04d}"


def load_job(jid: str, sandbox_dir: Path | None = None) -> dict:
    p = job_file(jid, sandbox_dir)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_job(jid: str, job: dict, sandbox_dir: Path | None = None) -> None:
    p = job_file(jid, sandbox_dir)
    _paths.atomic_write_text(p, json.dumps(job, indent=2, ensure_ascii=False), mode=0o600)


def alive(job: dict, proc_root: Path = Path("/proc")) -> bool:
    """Running = its runner still exists with the recorded start time (no PID reuse)."""
    pid = int(job.get("pid") or 0)
    if job.get("status") != "running" or pid <= 0:
        return False
    ident = _procs.proc_identity(pid, proc_root)
    if ident is None:
        return False
    recorded = str(job.get("proc_start") or "")
    return not recorded or ident[1] == recorded


def jobs(
    sid: str = "",
    *,
    include_done_s: float = 3600.0,
    now: float | None = None,
    sandbox_dir: Path | None = None,
    proc_root: Path = Path("/proc"),
) -> list[dict]:
    """Jobs of `sid` (all when empty): running ones, plus those that ended within `include_done_s`."""
    now = time.time() if now is None else now
    sdir = sbx_dir(sandbox_dir)
    out: list[dict] = []
    if not sdir.is_dir():
        return out
    for f in sorted(sdir.glob("*.json")):
        try:
            j = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(j, dict) or not j:
            continue
        jsid = str(j.get("sid") or "")
        if sid and jsid != sid and not jsid.startswith(sid) and not sid.startswith(jsid or "__none__"):
            continue
        if j.get("status") == "running" and not alive(j, proc_root) and j.get("pid"):
            j["status"] = "lost"  # runner vanished without writing its code (reboot, SIGKILL)
        if j.get("status") != "running" and now - float(j.get("ended") or j.get("created") or 0) > include_done_s:
            continue
        out.append(j)
    return out


def launch(sid: str, command: str, cwd: str, *, sandbox_dir: Path | None = None) -> dict:
    """Start the detached runner; returns the job record (status starting/running)."""
    sdir = sbx_dir(sandbox_dir)
    sdir.mkdir(parents=True, exist_ok=True)
    jid = new_id()
    job = {
        "id": jid,
        "sid": sid,
        "cmd": command,
        "cwd": cwd,
        "created": time.time(),
        "status": "starting",
        "log": str(log_file(jid, sdir)),
    }
    save_job(jid, job, sdir)
    runner = [sys.executable, str(_paths.SELF_PATH), "sbx", "_exec", jid]
    setsid = "/usr/bin/setsid" if os.path.exists("/usr/bin/setsid") else ""
    argv = [setsid, "-f", *runner] if setsid else runner
    env = os.environ.copy()
    env["AGY_DASH_SANDBOX_DIR"] = str(sdir)
    subprocess.Popen(
        argv,
        cwd=cwd if (cwd and Path(cwd).is_dir()) else None,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    return job


def run_job(jid: str, *, sandbox_dir: Path | None = None) -> int:
    """The detached runner: run the command, stream into the log, record exit code."""
    sdir = sbx_dir(sandbox_dir)
    job = load_job(jid, sdir)
    if not job:
        return 2
    me = os.getpid()
    ident = _procs.proc_identity(me)
    lf = log_file(jid, sdir)
    lf.parent.mkdir(parents=True, exist_ok=True)
    cwd = job.get("cwd") or None
    if cwd and not Path(cwd).is_dir():
        cwd = None
    with open(lf, "ab", buffering=0) as log:
        p = subprocess.Popen(
            ["bash", "-c", job["cmd"]],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        job.update(
            status="running",
            pid=me,
            proc_start=ident[1] if ident else "",
            child_pid=p.pid,
            started=time.time(),
        )
        save_job(jid, job, sdir)

        def forward(signum, _frame):
            try:
                os.killpg(p.pid, signum)
            except ProcessLookupError:
                pass

        signal.signal(signal.SIGTERM, forward)
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        rc = p.wait()
    job = load_job(jid, sdir) or job
    job.update(status="done" if rc == 0 else "failed", rc=rc, ended=time.time())
    save_job(jid, job, sdir)
    return rc


def follow(
    jid: str,
    *,
    out=None,
    from_start: bool = True,
    poll_s: float = 0.25,
    timeout_s: float = 0.0,
    sandbox_dir: Path | None = None,
) -> int:
    """Copy the job's log to `out` as it grows, until the job ends; returns its exit code
    (124 when `timeout_s` runs out first, 125 when the runner vanished)."""
    sdir = sbx_dir(sandbox_dir)
    out = out or sys.stdout.buffer
    lf = log_file(jid, sdir)
    pos = 0
    if not from_start and lf.exists():  # re-attach: last ~4 KB for context, from a line start
        size = lf.stat().st_size
        if size > 4096:
            with open(lf, "rb") as f:
                f.seek(size - 4096)
                head = f.read(4096)
            nl = head.find(b"\n")
            pos = size - 4096 + (nl + 1 if nl >= 0 else 0)
    t0 = time.time()
    seen_running = False
    while True:
        job = load_job(jid, sdir)
        try:
            with open(lf, "rb") as f:
                f.seek(pos)
                chunk = f.read()
        except OSError:
            chunk = b""
        if chunk:
            pos += len(chunk)
            out.write(chunk)
            out.flush()
        st = job.get("status")
        if st in ("done", "failed"):
            return int(job.get("rc") or 0)
        if st == "running":
            seen_running = True
            if not alive(job):
                return 125
        elif seen_running or time.time() - t0 > 20:
            return 125  # never started / runner gone
        if timeout_s and time.time() - t0 > timeout_s:
            return 124
        time.sleep(poll_s)


def kill(jid: str, *, sandbox_dir: Path | None = None) -> tuple[bool, str]:
    sdir = sbx_dir(sandbox_dir)
    job = load_job(jid, sdir)
    if not alive(job):
        return False, f"{jid}: no está corriendo ({job.get('status', 'no existe')})"
    os.kill(int(job["pid"]), signal.SIGTERM)  # runner forwards it to the job's process group
    return True, f"{jid}: SIGTERM enviado"


# ------------------------------------------------------------------ hook + compaction glue


def is_wrapped(command: str) -> bool:
    c = command.lstrip()
    if c.startswith(("agy-dash sbx ", "rtk agy-dash sbx ", "claude-dash sbx ", "rtk claude-dash sbx ")):
        return True
    return bool(_WRAPPED_RE.search(c))


def is_viewer(command: str) -> bool:
    """A read-only watcher (`agy-dash follow <worker>` / `sbx wait|tail`): it holds no work, the
    worker it watches runs in the sandbox, and it can be re-attached after the relaunch. It must not
    hold a compaction back."""
    c = command.lstrip()
    if c.startswith("rtk "):
        c = c[4:].lstrip()
    if any(op in c for op in _CHAIN_OPS):
        return False
    if c.startswith((
        "agy-dash follow ",
        "agy-dash sbx wait ",
        "agy-dash sbx tail ",
        "claude-dash follow ",
        "claude-dash sbx wait ",
        "claude-dash sbx tail ",
    )):
        return True
    return bool(_VIEWER_RE.search(c))


def blocks_close(command: str) -> bool:
    """True when closing AGY would kill real work in this shell."""
    return not (is_wrapped(command) or is_viewer(command))


def display_command(command: str) -> str:
    """'🛡 <inner command>' for a sandbox wrapper, the command itself otherwise."""
    if not is_wrapped(command):
        return command
    try:
        parts = shlex.split(command)
        return "🛡 " + " ".join(parts[parts.index("--") + 1 :])
    except (ValueError, IndexError):
        return "🛡 " + command


def wrap_command(sid: str, command: str) -> str:
    return f"{WRAP_PREFIX}--sid {sid} -- {shlex.quote(command)}"


def is_exempt(command: str) -> bool:
    c = command.lstrip()
    if is_viewer(c):
        return True
    return c.startswith(NO_WRAP_PREFIXES) and not any(op in c for op in _CHAIN_OPS)


def _is_swarm_worker_cmd(command: str) -> bool:
    """External swarm workers (`swarm-ext run ...`) must always run in the sandbox so they survive compaction."""
    return bool(re.search(r"(?:^|\s|/)swarm-ext\s+run\b", command))


def _is_agy_background_call(args: dict, command: str) -> bool:
    if bool(args.get("IsDaemon")) or bool(args.get("run_in_background")):
        return True
    if _is_swarm_worker_cmd(command):
        return True
    wait_ms = args.get("WaitMsBeforeAsync")
    if wait_ms is not None:
        try:
            max_bg_ms = int(os.environ.get("AGY_DASH_SBX_BG_WAIT_MS", "1000"))
            if int(wait_ms) <= max_bg_ms:
                return True
        except (TypeError, ValueError):
            pass
    return False


def hook_rewrite(payload: dict) -> dict | None:
    """PreToolUse(run_command / Bash): background commands & swarm-ext workers go through the sandbox.
    Returns None when the call should be left alone."""
    if os.environ.get("AGY_DASH_SANDBOX", "1") == "0":
        return None
    if not isinstance(payload, dict):
        return None

    # 1. Native AGY PreToolUse contract (`toolCall: {name: "run_command", args: {...}}`)
    tc = payload.get("toolCall")
    if isinstance(tc, dict):
        if tc.get("name") != "run_command":
            return None
        args = tc.get("args") or {}
        cmd = str(args.get("CommandLine") or "")
        if not cmd.strip() or is_wrapped(cmd) or is_exempt(cmd):
            return None
        if not _is_agy_background_call(args, cmd):
            return None
        sid = str(payload.get("conversationId") or payload.get("session_id") or "")
        return {
            "decision": "ask",
            "reason": "agy-dash: shell en sandbox (sobrevive a compactar)",
            "overwrite": {"CommandLine": wrap_command(sid, cmd)},
        }

    # 2. Claude-Code-compatible PreToolUse contract (`tool_name: "Bash", tool_input: {...}`)
    if payload.get("tool_name") == "Bash":
        ti = payload.get("tool_input") or {}
        cmd = str(ti.get("command") or "")
        bg = bool(ti.get("run_in_background")) or _is_swarm_worker_cmd(cmd)
        if not bg or not cmd.strip() or is_wrapped(cmd) or is_exempt(cmd):
            return None
        sid = str(payload.get("session_id") or payload.get("conversationId") or "")
        new = {**ti, "command": wrap_command(sid, cmd)}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecisionReason": "agy-dash: shell en sandbox (sobrevive a compactar)",
                "updatedInput": new,
            }
        }

    return None


def describe_for_digest(
    sid: str,
    lost: list[str] | None = None,
    *,
    now: float | None = None,
    sandbox_dir: Path | None = None,
    proc_root: Path = Path("/proc"),
) -> str:
    """Block for the Nemotron digest / handoff / resume prompt. '' when there is nothing to tell."""
    now = time.time() if now is None else now
    lines: list[str] = []
    for j in jobs(sid, now=now, sandbox_dir=sandbox_dir, proc_root=proc_root):
        cmd = " ".join(str(j.get("cmd", "")).split())[:300]
        if j.get("status") == "running":
            lines.append(
                f"- SIGUE CORRIENDO en sandbox `{j['id']}` (hace {fmt_dur(now - float(j.get('started') or now))}): "
                f"`{cmd}` · log {j.get('log')} · reengánchalo en 2º plano (`run_command` con `WaitMsBeforeAsync=500`): "
                f"`agy-dash sbx wait {j['id']}`"
            )
        else:
            lines.append(
                f"- terminó en sandbox `{j['id']}` (rc={j.get('rc', '?')}, {j.get('status')}): `{cmd}` · "
                f"log {j.get('log')} (léelo con `agy-dash sbx tail {j['id']}`)"
            )
    for sh in lost or []:
        lines.append(
            f"- MURIÓ al compactar (no estaba en sandbox): `{' '.join(sh.split())[:300]}` · "
            f"relánzalo solo si sigue haciendo falta"
        )
    return "\n".join(lines)
