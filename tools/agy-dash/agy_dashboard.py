#!/usr/bin/env python3
"""agy_dashboard.py (agy-dash) — Hybrid Zig+Python Single-Window Command Center for AGY.

Architecture (informed by REA inspection of Grok `workspace.db` & `xai-grok-pager`,
Codex `app-server`, and AGY `conversations/*.db`):
- Zig Fast Core (`tools/agy_limits/agy-fast-core`, Zig 0.16 ReleaseFast):
  * `scan`: scans `/proc`, `smaps_rollup`, `presence/*.lock`, child trees, and KSM in ~7 ms.
  * `statusbar`: renders tmux top bar natively in Zig.
  * `tail-lines <transcript.jsonl> <N>`: reverse-seeks the last 256 KB in <0.2 ms.
  * `ksm-exec <agy> ...`: sets `prctl(PR_SET_MEMORY_MERGE, 1)` and `execvp`s `agy`.
- Python Command Center (`agy_dashboard.py`):
  * Dual-pane `0:⚡DASH` with clickable Mouse + Keyboard Command Center buttons on the right.
  * Real-time updates (~0.5s stat()-gated polling + manual `[r] Actualizar` button/key).
  * `<3 MB` Real-Time Live Mirror tabs for sessions running in external consoles (zero
    `⚠ Conversation already open` duplicates) + Safe Auto-Hibernate to `0 MB RAM`.
"""
from __future__ import annotations

import argparse
import ctypes
import curses
import datetime as dt
import glob
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import textwrap
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

HOME = Path.home()
SCRIPT_DIR = Path(__file__).resolve().parent
ZIG_CORE_DIR = SCRIPT_DIR / "zig_core"
ZIG_CORE_BIN = SCRIPT_DIR / "agy-fast-core"

AGY_DATA_DIR = HOME / ".gemini" / "antigravity-cli"
SUMMARIES_DB = AGY_DATA_DIR / "conversation_summaries.db"
BRAIN_DIR = AGY_DATA_DIR / "brain"
PRESENCE_DIR = AGY_DATA_DIR / "presence"
HIBERNATED_FILE = AGY_DATA_DIR / "dashboard_hibernated.json"
TMUX_CONF = AGY_DATA_DIR / "agy-dash.tmux.conf"
TMUX_SOCKET = "agy-dash"
TMUX_SESSION = "agy"

AGY_BIN = os.environ.get("AGY_BIN", str(HOME / ".local" / "bin" / "agy"))
DEFAULT_GOGC = os.environ.get("AGY_GOGC", "35")
DEFAULT_GOMEMLIMIT = os.environ.get("AGY_GOMEMLIMIT", "180MiB")
DEFAULT_IDLE_HIBERNATE_SEC = int(os.environ.get("AGY_IDLE_HIBERNATE_SEC", "180"))

PR_SET_MEMORY_MERGE = 67


def ensure_zig_core() -> bool:
    """Return True if `agy-fast-core` exists, compiling via `zig build` if missing."""
    if ZIG_CORE_BIN.is_file() and os.access(ZIG_CORE_BIN, os.X_OK):
        return True
    if not shutil.which("zig") or not (ZIG_CORE_DIR / "build.zig").is_file():
        return False
    try:
        cp = subprocess.run(
            ["zig", "build", "-Drelease"],
            cwd=str(ZIG_CORE_DIR),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=90,
        )
        built = ZIG_CORE_DIR / "zig-out" / "bin" / "agy-fast-core"
        if cp.returncode == 0 and built.is_file():
            tmp = ZIG_CORE_BIN.with_suffix(".tmp")
            shutil.copy2(built, tmp)
            os.chmod(tmp, 0o755)
            os.replace(tmp, ZIG_CORE_BIN)
            return True
    except Exception:
        pass
    return False


def enable_ksm_memory_merge() -> bool:
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        return libc.prctl(PR_SET_MEMORY_MERGE, 1, 0, 0, 0) == 0
    except Exception:
        return False


@dataclass
class ProcMemInfo:
    pid: int
    ppid: int = 0
    tty: str = "?"
    rss_kb: int = 0
    pss_kb: int = 0
    pss_anon_kb: int = 0
    shared_clean_kb: int = 0
    ksm_kb: int = 0
    swap_kb: int = 0
    active_children: list[str] = field(default_factory=list)
    in_dash: bool = False


@dataclass
class AgySession:
    conversation_id: str
    title: str
    preview: str
    step_count: int
    last_modified: str
    workspace: str
    db_status: str
    not_fully_idle: bool
    proc: ProcMemInfo | None = None
    extra_procs: list[ProcMemInfo] = field(default_factory=list)
    hibernated: bool = False
    hibernated_saved_kb: int = 0
    dash_win_idx: str | None = None
    dash_win_name: str = ""

    @property
    def state_label(self) -> str:
        if self.proc is not None:
            if self.db_status == "CASCADE_RUN_STATUS_RUNNING":
                return "RUNNING"
            if self.not_fully_idle or self.proc.active_children:
                return "BG-TASK"
            return "IDLE"
        if self.hibernated:
            return "HIBERNATED"
        return "SAVED"

    @property
    def location_label(self) -> str:
        if self.dash_win_idx is not None and self.proc is not None:
            if self.dash_win_name.startswith("👁"):
                return f"live:{self.dash_win_idx}"
            if self.extra_procs:
                return f"tab:{self.dash_win_idx}+ext"
            return f"tab:{self.dash_win_idx}"
        if self.proc is not None:
            return f"ext:{self.proc.tty}"
        if self.hibernated:
            return f"💤tab:{self.dash_win_idx or '-'}"
        return "disco"

    @property
    def can_hibernate(self) -> bool:
        if self.proc is None:
            return False
        if is_ancestor_pid(self.proc.pid):
            return False
        if self.db_status == "CASCADE_RUN_STATUS_RUNNING":
            return False
        if self.not_fully_idle or self.proc.active_children:
            return False
        return True

    @property
    def external_busy(self) -> bool:
        """True if session is running externally with active work or background tasks."""
        if self.proc is None or self.proc.in_dash:
            return False
        return (
            self.db_status == "CASCADE_RUN_STATUS_RUNNING"
            or self.not_fully_idle
            or bool(self.proc.active_children)
        )


def is_ancestor_pid(target_pid: int) -> bool:
    cur = os.getpid()
    seen: set[int] = set()
    while cur > 1 and cur not in seen:
        if cur == target_pid:
            return True
        seen.add(cur)
        try:
            stat_text = Path(f"/proc/{cur}/stat").read_text()
            after_comm = stat_text.rsplit(")", 1)[1].split()
            cur = int(after_comm[1])
        except Exception:
            break
    return False


def transcript_path_for(conv_id: str) -> Path:
    return BRAIN_DIR / conv_id / ".system_generated" / "logs" / "transcript.jsonl"


def transcript_sig(conv_id: str) -> tuple[int, int]:
    p = transcript_path_for(conv_id)
    try:
        st = p.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return (0, 0)


def zig_scan() -> tuple[int, dict[str, int], dict[str, list[ProcMemInfo]]]:
    """Call `agy-fast-core scan` (Zig) and parse its JSON in Python."""
    dash_server_pids = _get_tmux_dash_server_pids()
    if ensure_zig_core():
        try:
            cp = subprocess.run(
                [str(ZIG_CORE_BIN), "scan"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=1.5,
            )
            if cp.returncode == 0 and cp.stdout.strip():
                data = json.loads(cp.stdout)
                scan_us = int(data.get("scan_us", 0))
                ksm = data.get("ksm", {"run": 1, "pages_shared": 0, "pages_sharing": 0, "pages_scanned": 0})
                by_conv: dict[str, list[ProcMemInfo]] = {}
                for p in data.get("procs", []):
                    cid = p.get("conv_id") or ""
                    if not cid:
                        continue
                    kids = [
                        c for c in p.get("children", [])
                        if not c.startswith("agy-fast-core(")
                    ]
                    ppid = int(p.get("ppid", 0))
                    pinfo = ProcMemInfo(
                        pid=int(p["pid"]),
                        ppid=ppid,
                        tty=str(p.get("tty", "?")),
                        rss_kb=int(p.get("rss_kb", 0)),
                        pss_kb=int(p.get("pss_kb", 0)),
                        pss_anon_kb=int(p.get("pss_anon_kb", 0)),
                        shared_clean_kb=int(p.get("shared_clean_kb", 0)),
                        ksm_kb=int(p.get("ksm_kb", 0)),
                        swap_kb=int(p.get("swap_kb", 0)),
                        active_children=kids,
                        in_dash=(ppid in dash_server_pids),
                    )
                    by_conv.setdefault(cid, []).append(pinfo)
                return scan_us, ksm, by_conv
        except Exception:
            pass
    return 0, {"run": 1, "pages_shared": 0, "pages_sharing": 0, "pages_scanned": 0}, {}


def zig_tail_transcript(conv_id: str, max_turns: int = 12, include_tools: bool = True) -> list[dict]:
    """Use `agy-fast-core tail-lines` (Zig) to read the tail of transcript.jsonl in <1 ms and parse in Python."""
    tpath = transcript_path_for(conv_id)
    if not tpath.is_file():
        return []
    raw_lines: list[str] = []
    if ensure_zig_core():
        try:
            cp = subprocess.run(
                [str(ZIG_CORE_BIN), "tail-lines", str(tpath), str(max_turns * 4)],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=0.8,
            )
            if cp.returncode == 0:
                raw_lines = [l for l in cp.stdout.splitlines() if l.strip()]
        except Exception:
            pass
    if not raw_lines:
        try:
            with tpath.open("rb") as f:
                f.seek(0, os.SEEK_END)
                end = f.tell()
                pos = max(0, end - 131072)
                f.seek(pos, os.SEEK_SET)
                chunk = f.read().decode("utf-8", errors="replace")
                lines = chunk.splitlines()
                if pos > 0 and lines:
                    lines = lines[1:]
                raw_lines = lines[-(max_turns * 4) :]
        except Exception:
            return []

    parsed: list[dict] = []
    for line in raw_lines:
        try:
            obj = json.loads(line)
        except Exception:
            continue
        stype = obj.get("type", "")
        if stype not in ("USER_INPUT", "PLANNER_RESPONSE", "GENERIC"):
            continue
        if stype == "GENERIC" and not include_tools:
            continue
        content = (obj.get("content") or "").strip()
        if "<USER_REQUEST>" in content and "</USER_REQUEST>" in content:
            m = re.search(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", content, re.DOTALL)
            if m:
                content = m.group(1).strip()
        tools = []
        for tc in obj.get("tool_calls") or []:
            if isinstance(tc, dict):
                tname = tc.get("name") or tc.get("tool_name") or "tool"
                args = tc.get("arguments") or tc.get("args") or {}
                summary = ""
                if isinstance(args, dict):
                    summary = str(args.get("toolSummary") or args.get("Path") or args.get("CommandLine", ""))[:45]
                tools.append(f"{tname}({summary})" if summary else tname)
        if stype == "GENERIC":
            # Only surface compact tool-running / error status lines from GENERIC
            if "Tool is running as a background task" in content:
                role = "TOOL"
                content = re.sub(r"\s+", " ", content)[:140]
            else:
                continue
        else:
            role = "USER" if stype == "USER_INPUT" else "AGY"
        if not content and not tools:
            continue
        parsed.append(
            {
                "idx": obj.get("step_index", 0),
                "role": role,
                "status": obj.get("status", "DONE"),
                "content": re.sub(r"\s+", " ", content)[:260],
                "tools": tools[:4],
                "ts": str(obj.get("created_at", ""))[11:19],
            }
        )
    return parsed[-max_turns:]


def print_instant_zig_preview(conv_id: str, title: str, workspace: str) -> None:
    """Print <4ms conversation preview before `agy --conversation` finishes loading its 14MB DB."""
    turns = zig_tail_transcript(conv_id, max_turns=8)
    print("\033[2J\033[H", end="")
    print("\033[1;36m╔══════════════════════════════════════════════════════════════════════════════════════════╗\033[0m")
    print(f"\033[1;36m║\033[0m ⚡ \033[1;32mVISTA INSTANTÁNEA ZIG (<4 ms)\033[0m — \033[1;37m{title[:54]:<54}\033[0m \033[1;36m║\033[0m")
    print(f"\033[1;36m║\033[0m 📂 Workspace: {workspace[:74]:<74} \033[1;36m║\033[0m")
    print("\033[1;36m╚══════════════════════════════════════════════════════════════════════════════════════════╝\033[0m")
    if turns:
        print("\033[0;37m  Últimos turnos de la conversación (leídos por `agy-fast-core` desde `transcript.jsonl`):\033[0m\n")
        for t in turns:
            if t["role"] == "USER":
                print(f"  \033[1;33m👤 [{t['ts']} #{t['idx']}] TÚ:\033[0m {t['content'][:160]}")
            elif t["role"] == "TOOL":
                print(f"  \033[1;35m⏳ [{t['ts']} #{t['idx']}] TASK:\033[0m {t['content'][:140]}")
            else:
                tools_s = f" \033[0;35m[🔧 {', '.join(t['tools'])}]\033[0m" if t["tools"] else ""
                body_s = t["content"][:160] if t["content"] else "(ejecutando herramientas...)"
                print(f"  \033[1;32m🤖 [{t['ts']} #{t['idx']}] AGY:\033[0m {body_s}{tools_s}")
        print()
    print("\033[1;35m  ⏳ Sincronizando motor TUI interactivo de AGY (KSM + GOMEMLIMIT=180MiB)...\033[0m", flush=True)


def _get_tmux_dash_server_pids() -> set[int]:
    pids: set[int] = set()
    for stat_p in glob.glob("/proc/[0-9]*/cmdline"):
        try:
            raw = Path(stat_p).read_bytes().decode("utf-8", errors="replace")
            if "tmux" in raw and TMUX_SOCKET in raw:
                pids.add(int(stat_p.split("/")[2]))
        except Exception:
            pass
    return pids


def _get_dash_window_map() -> dict[str, tuple[str, str]]:
    """Return mapping conv_id -> (window_index, window_name) inside tmux -L agy-dash."""
    out: dict[str, tuple[str, str]] = {}
    cp = tmux_cmd(
        "list-windows",
        "-t",
        TMUX_SESSION,
        "-F",
        "#{window_index}|#{window_name}|#{@agy_conv_id}",
        capture=True,
    )
    if cp.returncode == 0 and cp.stdout:
        for line in cp.stdout.splitlines():
            parts = line.split("|", 2)
            if len(parts) == 3 and parts[2].strip():
                out[parts[2].strip()] = (parts[0].strip(), parts[1].strip())
    return out


def load_hibernated_registry() -> dict[str, dict]:
    if not HIBERNATED_FILE.is_file():
        return {}
    try:
        return json.loads(HIBERNATED_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_hibernated_registry(reg: dict[str, dict]) -> None:
    HIBERNATED_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = HIBERNATED_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, HIBERNATED_FILE)


def parse_workspace_uri(raw_uris: str) -> str:
    try:
        arr = json.loads(raw_uris)
        if isinstance(arr, list) and arr:
            uri = str(arr[0])
            if uri.startswith("file://"):
                return urllib.parse.unquote(uri[7:])
            return uri
    except Exception:
        pass
    return str(Path.cwd())


def load_sessions(limit: int = 40) -> tuple[int, dict[str, int], list[AgySession]]:
    scan_us, ksm_stats, live_by_conv = zig_scan()
    win_map = _get_dash_window_map()
    hib_reg = load_hibernated_registry()

    dirty_hib = False
    for cid in list(hib_reg.keys()):
        if cid in live_by_conv:
            old_pid = hib_reg[cid].get("hibernated_pid")
            live_pids = {p.pid for p in live_by_conv[cid]}
            if old_pid is None or any(lp != old_pid for lp in live_pids):
                del hib_reg[cid]
                dirty_hib = True
    if dirty_hib:
        save_hibernated_registry(hib_reg)

    rows_by_id: dict[str, AgySession] = {}
    if SUMMARIES_DB.is_file():
        try:
            uri = f"file:{SUMMARIES_DB}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=2.0)
            cur = conn.execute(
                """
                SELECT conversation_id, title, preview, step_count,
                       last_modified_time, workspace_uris, status, not_fully_idle
                FROM conversation_summaries
                WHERE nesting_depth = 0 AND killed = 0
                ORDER BY last_modified_time DESC
                LIMIT ?
                """,
                (max(limit, 80),),
            )
            for cid, title, preview, steps, lastmod, ws_uris, status, not_idle in cur.fetchall():
                procs = live_by_conv.get(cid, [])
                procs_sorted = sorted(
                    procs,
                    key=lambda p: (not p.in_dash, len(p.active_children) == 0, p.pid),
                )
                primary = procs_sorted[0] if procs_sorted else None
                extras = procs_sorted[1:] if len(procs_sorted) > 1 else []
                if primary and extras:
                    for ep in extras:
                        for kc in ep.active_children:
                            if kc not in primary.active_children:
                                primary.active_children.append(kc)

                hib_info = hib_reg.get(cid)
                win_idx, win_name = win_map.get(cid, (None, ""))
                clean_title = (title or "").strip() or f"Sesión {cid[:8]}"
                clean_prev = re.sub(r"\s+", " ", (preview or "").strip())[:100]
                rows_by_id[cid] = AgySession(
                    conversation_id=cid,
                    title=clean_title,
                    preview=clean_prev,
                    step_count=int(steps or 0),
                    last_modified=str(lastmod or "")[:19],
                    workspace=parse_workspace_uri(ws_uris),
                    db_status=str(status or ""),
                    not_fully_idle=bool(not_idle),
                    proc=primary,
                    extra_procs=extras,
                    hibernated=(hib_info is not None and primary is None),
                    hibernated_saved_kb=int((hib_info or {}).get("saved_pss_kb", 250_000)),
                    dash_win_idx=win_idx,
                    dash_win_name=win_name,
                )
            conn.close()
        except Exception:
            pass

    for cid, procs in live_by_conv.items():
        if cid not in rows_by_id and procs:
            win_idx, win_name = win_map.get(cid, (None, ""))
            rows_by_id[cid] = AgySession(
                conversation_id=cid,
                title=f"Sesión activa {cid[:8]}",
                preview="",
                step_count=0,
                last_modified="ahora",
                workspace=str(Path.cwd()),
                db_status="CASCADE_RUN_STATUS_IDLE",
                not_fully_idle=False,
                proc=procs[0],
                extra_procs=procs[1:],
                dash_win_idx=win_idx,
                dash_win_name=win_name,
            )

    def sort_key(s: AgySession) -> tuple[int, str]:
        if s.proc is not None:
            rank = 0 if s.state_label == "RUNNING" else (1 if s.state_label == "BG-TASK" else 2)
            return (rank, s.last_modified)
        if s.hibernated:
            return (3, s.last_modified)
        return (4, "")

    live_and_hib = [s for s in rows_by_id.values() if s.proc is not None or s.hibernated]
    saved = [s for s in rows_by_id.values() if s.proc is None and not s.hibernated]
    live_and_hib.sort(key=sort_key)
    return scan_us, ksm_stats, (live_and_hib + saved)[:limit]


def hibernate_session(session: AgySession) -> tuple[bool, str]:
    if session.proc is None:
        return False, f"La sesión {session.conversation_id[:8]} no está en RAM."
    if not session.can_hibernate:
        reason = (
            "es la sesión actual"
            if is_ancestor_pid(session.proc.pid)
            else f"estado={session.state_label} (tiene turno o tarea activa)"
        )
        return False, f"No se hiberna {session.conversation_id[:8]} ({session.title}): {reason}."

    all_procs = [session.proc] + list(session.extra_procs)
    saved_pss = sum(p.pss_kb + p.swap_kb for p in all_procs)
    pids = [p.pid for p in all_procs if not is_ancestor_pid(p.pid)]
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass

    for _ in range(40):
        if all(not Path(f"/proc/{pid}").exists() for pid in pids):
            break
        time.sleep(0.1)

    hib_reg = load_hibernated_registry()
    hib_reg[session.conversation_id] = {
        "conversation_id": session.conversation_id,
        "title": session.title,
        "workspace": session.workspace,
        "step_count": session.step_count,
        "saved_pss_kb": saved_pss,
        "hibernated_pid": pids[0] if pids else 0,
        "hibernated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    save_hibernated_registry(hib_reg)
    return True, (
        f"Hibernada '{session.title}' ({session.conversation_id[:8]}) -> "
        f"liberados {saved_pss // 1024} MB RAM (ahora 0 MB)"
    )


def deduplicate_session_procs(session: AgySession) -> int:
    """Close idle duplicate copies when a session already has a primary process."""
    freed_kb = 0
    for ep in list(session.extra_procs):
        if not ep.in_dash and not ep.active_children and not is_ancestor_pid(ep.pid):
            try:
                os.kill(ep.pid, signal.SIGTERM)
                freed_kb += ep.pss_kb + ep.swap_kb
            except OSError:
                pass
    return freed_kb


def close_stale_dash_duplicates(session: AgySession) -> int:
    """Close a stale in-dash duplicate `agy` when external `ext:pts/*` holds active work."""
    freed_kb = 0
    all_procs = ([session.proc] if session.proc else []) + list(session.extra_procs)
    has_busy_ext = any((not p.in_dash) and p.active_children for p in all_procs)
    if not has_busy_ext:
        return 0
    for p in all_procs:
        if p.in_dash and not p.active_children and not is_ancestor_pid(p.pid):
            try:
                os.kill(p.pid, signal.SIGTERM)
                freed_kb += p.pss_kb + p.swap_kb
            except OSError:
                pass
    return freed_kb


def build_agy_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("GOGC", DEFAULT_GOGC)
    env.setdefault("GOMEMLIMIT", DEFAULT_GOMEMLIMIT)
    return env


def exec_agy_optimized(extra_args: list[str], cwd: str | None = None, preview_conv: str = "", preview_title: str = "") -> int:
    enable_ksm_memory_merge()
    env = build_agy_env()
    target_cwd = cwd if (cwd and Path(cwd).is_dir()) else str(Path.cwd())
    os.chdir(target_cwd)

    if preview_conv:
        print_instant_zig_preview(preview_conv, preview_title or preview_conv[:8], target_cwd)

    if ensure_zig_core():
        cmd = [str(ZIG_CORE_BIN), "ksm-exec", AGY_BIN] + extra_args
        os.execvpe(str(ZIG_CORE_BIN), cmd, env)
    cmd = [AGY_BIN] + extra_args
    os.execvpe(AGY_BIN, cmd, env)
    return 0


# ============================================================================
# Tmux Single-Window Multiplexer & Real-Time Live Mirror
# ============================================================================

def sh_quote(s: str) -> str:
    return "'" + s.replace("'", "'\"'\"'") + "'"


def write_tmux_config(self_path: Path) -> None:
    TMUX_CONF.parent.mkdir(parents=True, exist_ok=True)
    if ensure_zig_core():
        status_cmd = f"{ZIG_CORE_BIN} statusbar"
    else:
        status_cmd = f"python3 {self_path} statusbar"

    conf = f"""\
# Auto-generated by agy-dash (Zig+Python Single-Window Command Center)
set -g default-terminal "tmux-256color"
set -ga terminal-overrides ",*256col*:Tc"
set -g mouse on
set -g history-limit 50000
set -g escape-time 10
set -g focus-events on
set -g set-clipboard on
set -g allow-passthrough on
set -g base-index 0
setw -g pane-base-index 0
set -g renumber-windows on

# Top dashboard bar powered by Zig `agy-fast-core statusbar` (<7ms)
set -g status-position top
set -g status-interval 2
set -g status-style "bg=#14171f,fg=#d8dee9"
set -g status-left-length 32
set -g status-right-length 78
set -g status-left "#[bg=#e94560,fg=#ffffff,bold] ⚡ AGY DASH #[bg=#14171f,fg=#e94560] "
set -g status-right "#[fg=#414650]│ #[fg=#8be9fd]#({status_cmd}) #[fg=#414650]│ #[fg=#f1fa8c]Alt+0:Dash Alt+1..9:Tab Alt+N:New "

setw -g window-status-format "#[fg=#6272a4] #I:#W "
setw -g window-status-current-format "#[bg=#282a36,fg=#50fa7b,bold] ▸ #I:#W #[default]"

bind -n M-0 select-window -t :0
bind -n M-d select-window -t :0
bind -n M-1 select-window -t :1
bind -n M-2 select-window -t :2
bind -n M-3 select-window -t :3
bind -n M-4 select-window -t :4
bind -n M-5 select-window -t :5
bind -n M-6 select-window -t :6
bind -n M-7 select-window -t :7
bind -n M-8 select-window -t :8
bind -n M-9 select-window -t :9
bind -n M-Left previous-window
bind -n M-Right next-window
bind -n M-n run-shell "python3 {self_path} open-new-tab"
bind -n M-h run-shell "python3 {self_path} hibernate all-idle"
"""
    TMUX_CONF.write_text(conf, encoding="utf-8")


def tmux_cmd(*args: str, capture: bool = False) -> subprocess.CompletedProcess:
    if capture:
        return subprocess.run(
            ["tmux", "-L", TMUX_SOCKET, "-f", str(TMUX_CONF), *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
        )
    return subprocess.run(
        ["tmux", "-L", TMUX_SOCKET, "-f", str(TMUX_CONF), *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )


def ensure_dash_window_zero() -> None:
    self_path = Path(__file__).resolve()
    cp = tmux_cmd("list-windows", "-t", TMUX_SESSION, "-F", "#{window_index}|#{window_name}", capture=True)
    if cp.returncode != 0:
        return
    for line in cp.stdout.splitlines():
        parts = line.split("|")
        if len(parts) >= 2 and "DASH" in parts[1]:
            return
    tui_cmd = f"exec python3 {sh_quote(str(self_path))} tui"
    tmux_cmd("new-window", "-d", "-b", "-t", f"{TMUX_SESSION}:0", "-n", "⚡DASH", tui_cmd)


def _short_tab_title(title: str, prefix: str = "") -> str:
    clean = re.sub(r"[^A-Za-z0-9áéíóúÁÉÍÓÚñÑ_-]+", "-", title.strip()).strip("-")
    clean = clean[:15] or "session"
    return f"{prefix}{clean}"


def _find_tmux_window_by_conv(conv_id: str) -> str | None:
    info = _get_dash_window_map().get(conv_id)
    return info[0] if info else None


def _replace_tmux_pane_with_wake_stub(session: AgySession) -> None:
    win_idx = _find_tmux_window_by_conv(session.conversation_id)
    if not win_idx:
        return
    self_path = Path(__file__).resolve()
    tab_name = _short_tab_title(session.title, prefix="💤")
    stub_cmd = (
        f"exec python3 {sh_quote(str(self_path))} wake-stub "
        f"{sh_quote(session.conversation_id)} --cwd {sh_quote(session.workspace)} "
        f"--title {sh_quote(session.title)}"
    )
    tmux_cmd("respawn-pane", "-k", "-t", f"{TMUX_SESSION}:{win_idx}.0", stub_cmd)
    tmux_cmd("rename-window", "-t", f"{TMUX_SESSION}:{win_idx}", tab_name)


def open_session_in_dashboard_tab(
    session: AgySession,
    *,
    switch_to: bool = True,
    as_stub: bool = False,
    force_live_mirror: bool = False,
    force_interactive: bool = False,
) -> str:
    self_path = Path(__file__).resolve()
    existing_idx = _find_tmux_window_by_conv(session.conversation_id)
    cwd = session.workspace if Path(session.workspace).is_dir() else str(Path.cwd())

    # If external session is busy (RUNNING / BG-TASK) and user didn't force interactive duplicate,
    # open a <3MB Real-Time Zig Live Mirror instead of spawning a stale duplicate `agy`!
    use_mirror = force_live_mirror or (
        (not as_stub) and (not force_interactive) and session.external_busy
    )

    migrated_ext = False
    if not as_stub and not use_mirror and session.proc is not None and not session.proc.in_dash:
        if not session.proc.active_children and session.db_status != "CASCADE_RUN_STATUS_RUNNING" and not is_ancestor_pid(session.proc.pid):
            try:
                os.kill(session.proc.pid, signal.SIGTERM)
                migrated_ext = True
            except OSError:
                pass

    if as_stub:
        tab_name = _short_tab_title(session.title, prefix="💤")
        cmd = (
            f"exec python3 {sh_quote(str(self_path))} wake-stub "
            f"{sh_quote(session.conversation_id)} --cwd {sh_quote(cwd)} "
            f"--title {sh_quote(session.title)}"
        )
    elif use_mirror:
        tab_name = _short_tab_title(session.title, prefix="👁")
        cmd = (
            f"exec python3 {sh_quote(str(self_path))} mirror "
            f"{sh_quote(session.conversation_id)} --cwd {sh_quote(cwd)} "
            f"--title {sh_quote(session.title)}"
        )
    else:
        tab_name = _short_tab_title(session.title, prefix="●")
        cmd = (
            f"exec python3 {sh_quote(str(self_path))} run "
            f"--conversation {sh_quote(session.conversation_id)} --cwd {sh_quote(cwd)} "
            f"--title {sh_quote(session.title)}"
        )

    if existing_idx is not None:
        if force_interactive or force_live_mirror or (
            not as_stub and (session.proc is None or not session.proc.in_dash or (use_mirror and not session.dash_win_name.startswith("👁")))
        ):
            reg = load_hibernated_registry()
            if session.conversation_id in reg:
                del reg[session.conversation_id]
                save_hibernated_registry(reg)
            tmux_cmd("respawn-pane", "-k", "-t", f"{TMUX_SESSION}:{existing_idx}.0", cmd)
            tmux_cmd("rename-window", "-t", f"{TMUX_SESSION}:{existing_idx}", tab_name)
        if switch_to:
            tmux_cmd("select-window", "-t", f"{TMUX_SESSION}:{existing_idx}")
        if use_mirror:
            return f"Espejo en vivo tiempo real abierto en pestaña #{existing_idx} (<3MB, sin duplicar AGY)."
        return f"Pestaña #{existing_idx} ('{session.title}') lista."

    args = ["new-window", "-t", TMUX_SESSION, "-n", tab_name, "-c", cwd]
    if not switch_to:
        args.insert(1, "-d")
    args.append(cmd)
    tmux_cmd(*args)
    cp = tmux_cmd("list-windows", "-t", TMUX_SESSION, "-F", "#{window_index}", capture=True)
    if cp.returncode == 0 and cp.stdout and cp.stdout.strip():
        last_win = cp.stdout.strip().splitlines()[-1]
        tmux_cmd("set-window-option", "-t", f"{TMUX_SESSION}:{last_win}", "@agy_conv_id", session.conversation_id)
    if use_mirror:
        return f"Espejo en vivo tiempo real ('{session.title}') — actualiza en <0.4s desde {session.proc.tty if session.proc else 'ext'}."
    if migrated_ext:
        return f"Migrada '{session.title}' desde consola externa al Dashboard."
    return f"Abierta '{session.title}' en pestaña del Dashboard."


def reload_session_tab(session: AgySession) -> str:
    """Respawn an open dashboard tab cleanly to sync latest disk state."""
    existing_idx = _find_tmux_window_by_conv(session.conversation_id)
    if existing_idx is None:
        return open_session_in_dashboard_tab(session, switch_to=True)
    if session.external_busy:
        return open_session_in_dashboard_tab(session, switch_to=True, force_live_mirror=True)
    return open_session_in_dashboard_tab(session, switch_to=True, force_interactive=True)


# ============================================================================
# Real-Time Live Mirror View (`agy-dash mirror <conv_id>`)
# ============================================================================

def latest_active_task_log(conv_id: str, max_lines: int = 4) -> tuple[str, tuple[int, int], list[str]]:
    """Return (filename, (mtime_ns, size), tail_lines) for the newest task-*.log of conv_id."""
    tasks_dir = BRAIN_DIR / conv_id / ".system_generated" / "tasks"
    if not tasks_dir.is_dir():
        return "", (0, 0), []
    newest_path: Path | None = None
    newest_mtime = 0
    newest_size = 0
    try:
        for p in tasks_dir.glob("task-*.log"):
            st = p.stat()
            if st.st_mtime_ns > newest_mtime:
                newest_mtime = st.st_mtime_ns
                newest_size = st.st_size
                newest_path = p
    except OSError:
        return "", (0, 0), []
    if newest_path is None:
        return "", (0, 0), []
    try:
        with newest_path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            end = f.tell()
            pos = max(0, end - 8192)
            f.seek(pos, os.SEEK_SET)
            raw = f.read().decode("utf-8", errors="replace")
        # Normalize carriage returns from progress bars / pytest
        lines = [l.strip() for l in raw.replace("\r", "\n").splitlines() if l.strip()]
        return newest_path.name, (newest_mtime, newest_size), lines[-max_lines:]
    except OSError:
        return newest_path.name, (newest_mtime, newest_size), []


def run_live_mirror_curses(stdscr, conv_id: str, cwd: str, title: str) -> str:
    curses.curs_set(0)
    curses.use_default_colors()
    curses.mousemask(curses.ALL_MOUSE_EVENTS)
    if curses.has_colors():
        curses.init_pair(1, curses.COLOR_WHITE, curses.COLOR_RED)
        curses.init_pair(2, curses.COLOR_CYAN, -1)
        curses.init_pair(3, curses.COLOR_GREEN, -1)
        curses.init_pair(4, curses.COLOR_YELLOW, -1)
        curses.init_pair(5, curses.COLOR_MAGENTA, -1)
        curses.init_pair(7, curses.COLOR_WHITE, curses.COLOR_BLUE)

    stdscr.timeout(400)
    last_sig = (-1, -1)
    last_task_sig = (-1, -1)
    last_scan = 0.0
    turns: list[dict] = []
    task_name = ""
    task_lines: list[str] = []
    sess: AgySession | None = None
    last_update_str = time.strftime("%H:%M:%S")
    status_msg = "En vivo (auto-refresco 0.4s). [r] Actualizar  [↑/↓] Scroll  [m] Migrar  [o] Abrir AGY  [d] ⚡DASH"
    force_refresh = True
    scroll_offset = 0

    while True:
        now = time.time()
        sig = transcript_sig(conv_id)
        if force_refresh or sig != last_sig:
            last_sig = sig
            turns = zig_tail_transcript(conv_id, max_turns=40, include_tools=True)
            last_update_str = time.strftime("%H:%M:%S")
        tname, tsig, tlines = latest_active_task_log(conv_id, max_lines=4)
        if force_refresh or tsig != last_task_sig:
            last_task_sig = tsig
            task_name = tname
            task_lines = tlines
            last_update_str = time.strftime("%H:%M:%S")
        if force_refresh or (now - last_scan) >= 1.5:
            last_scan = now
            _, _, sessions = load_sessions(limit=30)
            sess = next((s for s in sessions if s.conversation_id == conv_id), None)
        force_refresh = False

        stdscr.erase()
        max_y, max_x = stdscr.getmaxyx()

        st_lbl = sess.state_label if sess else "DISK"
        loc_lbl = sess.location_label if sess else "disco"
        kids = ", ".join(sess.proc.active_children) if (sess and sess.proc and sess.proc.active_children) else "ninguno"
        pss_s = f"{sess.proc.pss_kb // 1024}MB" if (sess and sess.proc) else "0MB"

        hdr = f" 👁️ ESPEJO EN VIVO ZIG (<3MB RAM) — {title[:42]} ({conv_id[:8]}) — Sync {last_update_str} "
        stdscr.addstr(0, 0, hdr[: max_x - 1].ljust(max_x - 1), curses.color_pair(1) | curses.A_BOLD)
        info = f" Estado: {st_lbl} | Origen: {loc_lbl} | RAM AGY: {pss_s} | Hijos activos: {kids} | Ruta: {cwd}"
        stdscr.addstr(1, 0, info[: max_x - 1], curses.color_pair(2) | curses.A_BOLD)
        stdscr.addstr(2, 0, "─" * (max_x - 1))

        # Show live background task box when a recent task-*.log exists
        task_age_s = int(max(0, now - (last_task_sig[0] / 1e9))) if last_task_sig[0] else 999999
        show_task_box = bool(task_name and task_lines and (task_age_s <= 180 or (sess and sess.external_busy)))
        task_box_h = (2 + min(4, len(task_lines))) if (show_task_box and max_y >= 16) else 0

        body_rows = max(1, max_y - 6 - task_box_h)
        wrap_w = max(40, max_x - 4)
        lines_to_draw: list[tuple[str, int]] = []
        for t in turns:
            if t["role"] == "USER":
                prefix = f" 👤 [{t['ts']} #{t['idx']}] TÚ: "
                attr = curses.color_pair(4) | curses.A_BOLD
                raw_txt = t["content"]
            elif t["role"] == "TOOL":
                prefix = f" ⏳ [{t['ts']} #{t['idx']}] "
                attr = curses.color_pair(5)
                raw_txt = t["content"]
            else:
                tools_s = f" [🔧 {', '.join(t['tools'])}]" if t["tools"] else ""
                body_s = t["content"] or "(ejecutando herramientas...)"
                prefix = f" 🤖 [{t['ts']} #{t['idx']}] AGY: "
                attr = curses.color_pair(3)
                raw_txt = body_s + tools_s

            wrapped = textwrap.wrap(raw_txt, width=max(20, wrap_w - len(prefix))) or [""]
            lines_to_draw.append((prefix + wrapped[0], attr))
            indent = " " * min(len(prefix), 12)
            for cont in wrapped[1:]:
                lines_to_draw.append((indent + cont, attr))

        max_scroll = max(0, len(lines_to_draw) - body_rows)
        scroll_offset = max(0, min(scroll_offset, max_scroll))
        end_idx = len(lines_to_draw) - scroll_offset
        start_idx = max(0, end_idx - body_rows)
        for i, (txt, attr) in enumerate(lines_to_draw[start_idx:end_idx]):
            stdscr.addstr(3 + i, 0, txt[: max_x - 1], attr)

        if show_task_box and task_box_h > 0:
            ty = max_y - 3 - task_box_h
            stdscr.addstr(ty, 0, "─" * (max_x - 1))
            thdr = f" ⚡ TAREA ACTIVA EN VIVO ({task_name} · actualizado hace {task_age_s}s):"
            stdscr.addstr(ty + 1, 0, thdr[: max_x - 1], curses.color_pair(5) | curses.A_BOLD)
            for ti, tline in enumerate(task_lines[-4:]):
                if ty + 2 + ti < max_y - 3:
                    stdscr.addstr(ty + 2 + ti, 0, f"   {tline}"[: max_x - 1], curses.A_NORMAL)

        stdscr.addstr(max_y - 3, 0, "─" * (max_x - 1))
        stdscr.addstr(max_y - 2, 0, f" {status_msg}"[: max_x - 1], curses.color_pair(4))
        bar = " [r] 🔄 Actualizar ahora   [m] 🔀 Migrar aquí   [o] 🚀 Abrir AGY interactivo   [d/q] ⬅ Volver a 0:⚡DASH "
        stdscr.addstr(max_y - 1, 0, bar[: max_x - 1].ljust(max_x - 1), curses.color_pair(7) | curses.A_BOLD)
        stdscr.refresh()

        ch = stdscr.getch()
        if ch == -1:
            continue
        if ch == curses.KEY_MOUSE:
            try:
                _, mx, my, _, _ = curses.getmouse()
            except curses.error:
                continue
            if my == max_y - 1:
                if mx < 25:
                    ch = ord("r")
                elif mx < 46:
                    ch = ord("m")
                elif mx < 78:
                    ch = ord("o")
                else:
                    ch = ord("d")
            else:
                continue

        if ch == ord("r"):
            force_refresh = True
            scroll_offset = 0
            tmux_cmd("refresh-client", "-S")
            status_msg = f"✅ Actualizado a las {time.strftime('%H:%M:%S')} (transcript + {task_name or 'estado'})."
        elif ch in (curses.KEY_UP, ord("k")):
            scroll_offset += 2
        elif ch in (curses.KEY_DOWN, ord("j")):
            scroll_offset = max(0, scroll_offset - 2)
        elif ch == curses.KEY_PPAGE:
            scroll_offset += max(4, body_rows // 2)
        elif ch == curses.KEY_NPAGE:
            scroll_offset = max(0, scroll_offset - max(4, body_rows // 2))
        elif ch in (ord("d"), ord("q"), 27):
            tmux_cmd("select-window", "-t", f"{TMUX_SESSION}:0")
        elif ch == ord("m"):
            if sess and not sess.external_busy:
                return "migrate"
            kids_s = ", ".join(sess.proc.active_children) if (sess and sess.proc and sess.proc.active_children) else "turno activo"
            status_msg = f"⚠️ No se puede migrar aún: '{kids_s}' activo en {loc_lbl}. Pulsa [o] para abrir copia interactiva."
        elif ch == ord("o"):
            return "interactive"


def cmd_live_mirror(args: argparse.Namespace) -> int:
    cid = args.conversation_id
    cwd = args.cwd or str(Path.cwd())
    title = args.title or f"Sesión {cid[:8]}"
    action = curses.wrapper(lambda stdscr: run_live_mirror_curses(stdscr, cid, cwd, title))
    if action in ("migrate", "interactive"):
        _, _, sessions = load_sessions(limit=40)
        sess = next((s for s in sessions if s.conversation_id == cid), None)
        if action == "migrate" and sess and sess.proc and not sess.proc.in_dash and not sess.external_busy:
            try:
                os.kill(sess.proc.pid, signal.SIGTERM)
            except OSError:
                pass
        return exec_agy_optimized(["--conversation", cid], cwd=cwd, preview_conv=cid, preview_title=title)
    return 0


# ============================================================================
# CLI Subcommands: status, statusbar, hibernate, wake-stub, open-new-tab
# ============================================================================

def cmd_status(_args: argparse.Namespace) -> int:
    scan_us, ksm_sys, sessions = load_sessions(limit=25)
    live = [s for s in sessions if s.proc is not None]
    hib = [s for s in sessions if s.hibernated]
    idle_reclaimable = [s for s in live if s.can_hibernate]

    all_live_procs = [p for s in live for p in ([s.proc] if s.proc else []) + s.extra_procs]
    total_pss_kb = sum(p.pss_kb for p in all_live_procs)
    total_anon_kb = sum(p.pss_anon_kb for p in all_live_procs)
    total_shared_kb = max((p.shared_clean_kb for p in all_live_procs), default=0)
    total_ksm_kb = sum(p.ksm_kb for p in all_live_procs)
    total_swap_kb = sum(p.swap_kb for p in all_live_procs)
    reclaim_kb = sum((s.proc.pss_kb + s.proc.swap_kb) for s in idle_reclaimable if s.proc)
    hib_saved_kb = sum(s.hibernated_saved_kb for s in hib)

    print("╔══════════════════════════════════════════════════════════════════════════════════════════════╗")
    print("║  ⚡ AGY MULTI-SESSION DASHBOARD (Motor Zig + Parser Python — Estilo Codex / Grok)            ║")
    print("╚══════════════════════════════════════════════════════════════════════════════════════════════╝")
    print(
        f"  Escaneo Zig     : {scan_us / 1000.0:.2f} ms (`agy-fast-core scan`) | "
        f"Sesiones en RAM: {len(live)} activas ({len(all_live_procs)} procs) | {len(hib)} hibernadas (0 MB, ahorrados ~{hib_saved_kb // 1024} MB)"
    )
    print(
        f"  Consumo actual  : PSS={total_pss_kb // 1024} MB (Anon={total_anon_kb // 1024} MB) | "
        f"Shared={total_shared_kb // 1024} MB + KSM={total_ksm_kb // 1024} MB ({ksm_sys.get('pages_sharing', 0)} págs) | Swap={total_swap_kb // 1024} MB"
    )
    if idle_reclaimable:
        print(
            f"  💡 Ahorro disponible: {len(idle_reclaimable)} sesión(es) IDLE reclamables (~{reclaim_kb // 1024} MB con `agy-dash hibernate all-idle`)"
        )
    print()
    print(
        f"  {'ESTADO':<11} {'PID':<8} {'UBICACIÓN':<11} {'PSS':>6} {'SHR+KSM':>8} {'SWAP':>6}  {'ID':<9} {'PASOS':>5}  {'TÍTULO / WORKSPACE'}"
    )
    print("  " + "─" * 92)
    for s in sessions[:14]:
        if s.proc:
            pid_s = str(s.proc.pid)
            pss_s = f"{s.proc.pss_kb // 1024}M"
            shr_s = f"{(s.proc.shared_clean_kb + s.proc.ksm_kb) // 1024}M"
            swp_s = f"{s.proc.swap_kb // 1024}M"
        elif s.hibernated:
            pid_s = "💤0MB"
            pss_s = "0M"
            shr_s = f"-{s.hibernated_saved_kb // 1024}M"
            swp_s = "0M"
        else:
            pid_s = "-"
            pss_s = "0M"
            shr_s = "-"
            swp_s = "0M"
        ws_short = os.path.basename(s.workspace.rstrip("/")) or s.workspace
        mark = " *" if (s.proc and is_ancestor_pid(s.proc.pid)) else ""
        print(
            f"  {s.state_label:<11} {pid_s:<8} {s.location_label:<11} {pss_s:>6} {shr_s:>8} {swp_s:>6}  "
            f"{s.conversation_id[:8]:<9} {s.step_count:>5}  {s.title[:34]} ({ws_short}){mark}"
        )
    print()
    print("  Comandos: `agy-dash` (Dashboard 1 ventana) | `agy-dash hibernate all-idle` | `isymem list`")
    return 0


def cmd_statusbar(_args: argparse.Namespace) -> int:
    if ensure_zig_core():
        os.execv(str(ZIG_CORE_BIN), [str(ZIG_CORE_BIN), "statusbar"])
    _, _, sessions = load_sessions(limit=20)
    live = [s for s in sessions if s.proc is not None]
    hib = [s for s in sessions if s.hibernated]
    pss_mb = sum(s.proc.pss_kb for s in live if s.proc) // 1024
    print(f"RAM:{pss_mb}M PSS · {len(live)}🔥 {len(hib)}💤")
    return 0


def cmd_hibernate(args: argparse.Namespace) -> int:
    _, _, sessions = load_sessions(limit=60)
    target = args.target.strip()
    if target in ("all-idle", "idle", "all"):
        dedup_kb = 0
        for s in sessions:
            dedup_kb += deduplicate_session_procs(s)
            dedup_kb += close_stale_dash_duplicates(s)
        candidates = [s for s in sessions if s.can_hibernate]
        if not candidates and dedup_kb == 0:
            print("No hay sesiones AGY ociosas (IDLE con not_fully_idle=0) fuera de la sesión actual.")
            return 0
        total_freed = dedup_kb
        for s in candidates:
            saved = (s.proc.pss_kb + s.proc.swap_kb) if s.proc else 0
            ok, msg = hibernate_session(s)
            print(("  [OK] " if ok else "  [--] ") + msg)
            if ok:
                total_freed += saved
                _replace_tmux_pane_with_wake_stub(s)
        print(f"Total liberado: {total_freed // 1024} MB RAM.")
        return 0

    for s in sessions:
        if (
            s.conversation_id.startswith(target)
            or (s.proc and str(s.proc.pid) == target)
            or target.lower() in s.title.lower()
        ):
            ok, msg = hibernate_session(s)
            print(msg)
            if ok:
                _replace_tmux_pane_with_wake_stub(s)
            return 0 if ok else 1

    print(f"No se encontró sesión coincidente con '{target}'.", file=sys.stderr)
    return 1


def cmd_wake_stub(args: argparse.Namespace) -> int:
    cid = args.conversation_id
    cwd = args.cwd or str(Path.cwd())
    title = args.title or f"Sesión {cid[:8]}"

    print_instant_zig_preview(cid, title, cwd)
    print("\n\033[1;33m  💤 SESIÓN EN MODO HIBERNADO (0 MB RAM en uso — ~250-369 MB ahorrados)\033[0m")
    print("  \033[1;32m[ENTER]\033[0m Despertar motor interactivo AGY aquí mismo  |  \033[1;36m[d]\033[0m Ir a 0:⚡DASH  |  \033[1;31m[q]\033[0m Cerrar pestaña")
    try:
        ans = input("\n  Acción [ENTER / d / q]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return 0
    if ans == "q":
        return 0
    if ans == "d":
        tmux_cmd("select-window", "-t", f"{TMUX_SESSION}:0")
        return cmd_wake_stub(args)

    reg = load_hibernated_registry()
    if cid in reg:
        del reg[cid]
        save_hibernated_registry(reg)
    return exec_agy_optimized(["--conversation", cid], cwd=cwd, preview_conv=cid, preview_title=title)


def cmd_open_new_tab(args: argparse.Namespace) -> int:
    self_path = Path(__file__).resolve()
    cwd = getattr(args, "cwd", None) or str(Path.cwd())
    cmd = f"exec python3 {sh_quote(str(self_path))} run --cwd {sh_quote(cwd)}"
    tmux_cmd("new-window", "-t", TMUX_SESSION, "-n", "●nueva", "-c", cwd, cmd)
    return 0


# ============================================================================
# Dual-Pane Interactive Command Center (`0:⚡DASH`)
# ============================================================================

@dataclass(frozen=True)
class CommandButton:
    action_id: str
    hotkey: str
    label: str
    row_offset: int
    col_offset: int
    width: int


def build_command_buttons(panel_w: int) -> list[CommandButton]:
    """Layout clickable Command Center buttons inside the right-hand panel."""
    half = max(18, (panel_w - 4) // 2)
    return [
        CommandButton("refresh", "r", "[r] 🔄 Actualizar", 0, 2, half),
        CommandButton("open", "↵", "[↵] 🚀 Abrir / Ir", 0, 2 + half + 1, half),
        CommandButton("mirror", "v", "[v] 👁 Espejo Vivo", 1, 2, half),
        CommandButton("reload", "R", "[R] ♻ Recargar Tab", 1, 2 + half + 1, half),
        CommandButton("new", "n", "[n] ➕ Nueva Sesión", 2, 2, half),
        CommandButton("hibernate", "h", "[h] 💤 Hibernar 0MB", 2, 2 + half + 1, half),
        CommandButton("hib_all", "H", "[H] ⚡ Hibernar IDLE", 3, 2, half),
        CommandButton("dedup", "x", "[x] 🧹 Deduplicar", 3, 2 + half + 1, half),
        CommandButton("memory", "m", "[m] 🧠 Memoria Shared", 4, 2, half * 2 + 1),
    ]


def hit_test_button(
    buttons: list[CommandButton],
    panel_x: int,
    panel_y: int,
    mouse_x: int,
    mouse_y: int,
) -> str | None:
    for b in buttons:
        bx0 = panel_x + b.col_offset
        bx1 = bx0 + b.width - 1
        by = panel_y + b.row_offset
        if mouse_y == by and bx0 <= mouse_x <= bx1:
            return b.action_id
    return None


def _ram_bar(ratio: float, width: int = 20) -> str:
    clamped = max(0.0, min(1.0, ratio))
    filled = int(round(clamped * width))
    return "█" * filled + "░" * (width - filled)


def run_curses_tui(stdscr) -> None:
    curses.curs_set(0)
    curses.use_default_colors()
    curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)
    if curses.has_colors():
        curses.init_pair(1, curses.COLOR_WHITE, curses.COLOR_RED)      # Header badge
        curses.init_pair(2, curses.COLOR_CYAN, -1)                     # Accent cyan
        curses.init_pair(3, curses.COLOR_GREEN, -1)                    # RUNNING / AGY
        curses.init_pair(4, curses.COLOR_YELLOW, -1)                   # BG-TASK / USER
        curses.init_pair(5, curses.COLOR_MAGENTA, -1)                  # HIBERNATED / Tools
        curses.init_pair(6, curses.COLOR_BLACK, curses.COLOR_CYAN)     # Selected row
        curses.init_pair(7, curses.COLOR_WHITE, curses.COLOR_BLUE)     # Button surface

    stdscr.timeout(500)
    cursor = 0
    mode = "sessions"
    status_banner = "Listo — Clic o teclas: [r] Actualizar | [Enter] Abrir/Espejo | [R] Recargar Tab | [h/H] Hibernar | [m] Memoria"
    last_scan_ts = 0.0
    last_auto_check = time.time()
    idle_seen_since: dict[str, float] = {}
    cached_preview_cid = ""
    cached_preview_sig = (-1, -1)
    cached_turns: list[dict] = []
    last_refresh_clock = time.strftime("%H:%M:%S")
    scan_us = 0
    ksm: dict[str, int] = {"run": 1, "pages_shared": 0, "pages_sharing": 0, "pages_scanned": 0}
    sessions: list[AgySession] = []
    last_db_sig = (0, 0)
    force_scan = True

    def db_wal_sig() -> tuple[int, int]:
        try:
            s1 = SUMMARIES_DB.stat().st_mtime_ns if SUMMARIES_DB.is_file() else 0
            wal = Path(str(SUMMARIES_DB) + "-wal")
            s2 = wal.stat().st_mtime_ns if wal.is_file() else 0
            return (s1, s2)
        except OSError:
            return (0, 0)

    def execute_action(action: str) -> str:
        nonlocal mode, cursor, force_scan, cached_preview_sig
        if action == "refresh":
            force_scan = True
            cached_preview_sig = (-1, -1)
            tmux_cmd("refresh-client", "-S")
            return f"Actualizado en vivo a las {time.strftime('%H:%M:%S')} (Zig scan + DB + Vista)."
        if action == "memory":
            mode = "memory" if mode == "sessions" else "sessions"
            cursor = 0
            return "Vista: Memoria Compartida Multi-Harness (isymem)" if mode == "memory" else "Vista: Command Center AGY"
        if mode != "sessions":
            return ""
        sel = sessions[cursor] if (sessions and 0 <= cursor < len(sessions)) else None
        if action == "open" and sel:
            return open_session_in_dashboard_tab(sel, switch_to=True)
        if action == "mirror" and sel:
            return open_session_in_dashboard_tab(sel, switch_to=True, force_live_mirror=True)
        if action == "reload" and sel:
            return reload_session_tab(sel)
        if action == "new":
            cmd_open_new_tab(argparse.Namespace(cwd=str(Path.cwd())))
            return "Creada nueva pestaña AGY optimizada (KSM + GOMEMLIMIT=180MiB)."
        if action == "hibernate" and sel:
            ok, msg = hibernate_session(sel)
            if ok:
                _replace_tmux_pane_with_wake_stub(sel)
            force_scan = True
            return msg
        if action == "hib_all":
            freed = 0
            count = 0
            for s in sessions:
                freed += deduplicate_session_procs(s)
                freed += close_stale_dash_duplicates(s)
                if s.can_hibernate:
                    saved = (s.proc.pss_kb + s.proc.swap_kb) if s.proc else 0
                    ok, _ = hibernate_session(s)
                    if ok:
                        freed += saved
                        count += 1
                        open_session_in_dashboard_tab(s, switch_to=False, as_stub=True)
            force_scan = True
            return (
                f"Adoptadas/hibernadas {count} sesiones y deduplicadas copias -> liberados {freed // 1024} MB."
                if (count or freed)
                else "No hay sesiones IDLE o copias duplicadas que limpiar."
            )
        if action == "dedup":
            freed = 0
            for s in sessions:
                freed += deduplicate_session_procs(s)
                freed += close_stale_dash_duplicates(s)
            force_scan = True
            return (
                f"Deduplicadas copias redundantes -> liberados {freed // 1024} MB RAM."
                if freed
                else "No hay copias duplicadas inactivas para cerrar."
            )
        return ""

    while True:
        now = time.time()
        cur_db_sig = db_wal_sig()
        if force_scan or cur_db_sig != last_db_sig or (now - last_scan_ts) >= 1.2:
            last_db_sig = cur_db_sig
            last_scan_ts = now
            force_scan = False
            scan_us, ksm, sessions = load_sessions(limit=35)
            last_refresh_clock = time.strftime("%H:%M:%S")

        if now - last_auto_check >= 10.0:
            last_auto_check = now
            active_idle_ids: set[str] = set()
            for s in sessions:
                if s.extra_procs:
                    freed_dup = deduplicate_session_procs(s)
                    if freed_dup > 0:
                        status_banner = f"Auto-dedup '{s.title}' -> +{freed_dup // 1024} MB libres."
                if s.can_hibernate and s.proc is not None and s.dash_win_idx is not None:
                    active_idle_ids.add(s.conversation_id)
                    first_t = idle_seen_since.setdefault(s.conversation_id, now)
                    if DEFAULT_IDLE_HIBERNATE_SEC > 0 and (now - first_t) >= DEFAULT_IDLE_HIBERNATE_SEC:
                        ok, msg = hibernate_session(s)
                        if ok:
                            _replace_tmux_pane_with_wake_stub(s)
                            status_banner = f"Auto-Hibernate: {msg}"
                            idle_seen_since.pop(s.conversation_id, None)
            for cid in list(idle_seen_since.keys()):
                if cid not in active_idle_ids:
                    del idle_seen_since[cid]

        stdscr.erase()
        max_y, max_x = stdscr.getmaxyx()

        live = [s for s in sessions if s.proc is not None]
        hib = [s for s in sessions if s.hibernated]
        all_procs = [p for s in live for p in ([s.proc] if s.proc else []) + s.extra_procs]
        total_pss_mb = sum(p.pss_kb for p in all_procs) // 1024
        total_shr_mb = max((p.shared_clean_kb for p in all_procs), default=0) // 1024
        total_ksm_mb = sum(p.ksm_kb for p in all_procs) // 1024
        hib_saved_mb = sum(s.hibernated_saved_kb for s in hib) // 1024

        hdr = (
            f" ⚡ AGY COMMAND CENTER (Zig {scan_us / 1000.0:.1f}ms + Python · Sync {last_refresh_clock}) — "
            f"{len(live)} activas · {len(hib)} hibernadas (0MB) "
        )
        stdscr.addstr(0, 0, hdr[: max_x - 1].ljust(max_x - 1), curses.color_pair(1) | curses.A_BOLD)

        ram_line = (
            f" RAM: {total_pss_mb} MB PSS | Shared: {total_shr_mb} MB + KSM: {total_ksm_mb} MB "
            f"({ksm.get('pages_sharing', 0)} págs) | Ahorrado (💤): {hib_saved_mb} MB"
        )
        stdscr.addstr(1, 0, ram_line[: max_x - 1], curses.color_pair(2) | curses.A_BOLD)
        stdscr.addstr(2, 0, "─" * (max_x - 1))

        # Layout split: Left Session List + Right Command Center Panel (if width >= 116)
        show_cmd_panel = (mode == "sessions" and max_x >= 116)
        left_w = min(92, max_x - 46) if show_cmd_panel else (max_x - 1)
        panel_x = left_w + 1
        panel_w = max_x - panel_x - 1
        panel_btn_y = 5
        cmd_buttons = build_command_buttons(panel_w) if show_cmd_panel else []
        list_start_y = 5
        list_avail = 0

        if mode == "sessions":
            if sessions:
                cursor = max(0, min(cursor, len(sessions) - 1))

            preview_height = max(7, min(12, max_y // 3)) if max_y >= 20 else 0
            list_avail = max(3, max_y - 8 - preview_height)

            col_hdr = f" {'ESTADO':<10} {'UBICACIÓN':<10} {'PSS':>6} {'SHR+KSM':>7} {'PASOS':>5}  {'TÍTULO DE SESIÓN':<28} {'WS'}"
            stdscr.addstr(3, 0, col_hdr[:left_w], curses.A_BOLD)
            stdscr.addstr(4, 0, "─" * left_w)

            start_idx = max(0, cursor - list_avail + 1) if cursor >= list_avail else 0
            for idx, s in enumerate(sessions[start_idx : start_idx + list_avail], start=start_idx):
                row_y = list_start_y + (idx - start_idx)
                if row_y >= list_start_y + list_avail:
                    break
                st = s.state_label
                if s.proc:
                    pss_s = f"{s.proc.pss_kb // 1024}M"
                    shr_s = f"{(s.proc.shared_clean_kb + s.proc.ksm_kb) // 1024}M"
                elif s.hibernated:
                    pss_s = "0M"
                    shr_s = f"-{s.hibernated_saved_kb // 1024}M"
                else:
                    pss_s = "0M"
                    shr_s = "-"

                ws_name = (os.path.basename(s.workspace.rstrip("/")) or s.workspace)[:12]
                line = (
                    f" {st:<10} {s.location_label:<10} {pss_s:>6} {shr_s:>7} {s.step_count:>5}  "
                    f"{s.title[:28]:<28} {ws_name}"
                )
                attr = curses.A_NORMAL
                if idx == cursor:
                    attr = curses.color_pair(6) | curses.A_BOLD
                elif st == "RUNNING":
                    attr = curses.color_pair(3) | curses.A_BOLD
                elif st == "BG-TASK":
                    attr = curses.color_pair(4)
                elif st == "HIBERNATED":
                    attr = curses.color_pair(5)
                stdscr.addstr(row_y, 0, line[:left_w].ljust(left_w), attr)

            # Right-hand Command Center Panel (Buttons + Inspector + RAM Gauge)
            if show_cmd_panel:
                for ry in range(3, 5 + list_avail):
                    stdscr.addstr(ry, left_w, "│")
                stdscr.addstr(3, panel_x, " 🎛️  CENTRO DE COMANDO (Clic o Tecla)"[:panel_w], curses.color_pair(2) | curses.A_BOLD)
                stdscr.addstr(4, panel_x, "─" * panel_w)
                for b in cmd_buttons:
                    by = panel_btn_y + b.row_offset
                    bx = panel_x + b.col_offset
                    if by < 5 + list_avail and bx + b.width < max_x:
                        btn_txt = f" {b.label} ".ljust(b.width)[: b.width]
                        stdscr.addstr(by, bx, btn_txt, curses.color_pair(7) | curses.A_BOLD)

                insp_y = panel_btn_y + 6
                if insp_y < 5 + list_avail and sessions and 0 <= cursor < len(sessions):
                    sel = sessions[cursor]
                    stdscr.addstr(insp_y - 1, panel_x, "─" * panel_w)
                    t_sig = transcript_sig(sel.conversation_id)
                    age_s = int(max(0, now - (t_sig[0] / 1e9))) if t_sig[0] else -1
                    age_txt = f"hace {age_s}s" if age_s >= 0 else "sin log"
                    kids_txt = ", ".join(sel.proc.active_children) if (sel.proc and sel.proc.active_children) else "ninguno"
                    pid_txt = str(sel.proc.pid) if sel.proc else "-"
                    extra_txt = f" (+{len(sel.extra_procs)} dup)" if sel.extra_procs else ""
                    meta_lines = [
                        (f" 📌 {sel.title[:panel_w - 4]}", curses.A_BOLD),
                        (f" ID: {sel.conversation_id[:18]}  PID: {pid_txt}{extra_txt}  [{sel.location_label}]", curses.A_NORMAL),
                        (f" Log: {t_sig[1] // 1024} KB ({age_txt}) | Hijos: {kids_txt[:panel_w - 28]}", curses.color_pair(4) if kids_txt != "ninguno" else curses.A_NORMAL),
                        (f" RAM: {_ram_bar(min(1.0, total_pss_mb / 2048.0), 18)} {total_pss_mb}M (KSM {total_ksm_mb}M)", curses.color_pair(3)),
                    ]
                    for mi, (ml, ma) in enumerate(meta_lines):
                        if insp_y + mi < 5 + list_avail:
                            stdscr.addstr(insp_y + mi, panel_x, ml[:panel_w], ma)

            # Bottom Real-Time Zig Live Conversation Preview!
            if preview_height > 0 and sessions and 0 <= cursor < len(sessions):
                sel = sessions[cursor]
                cur_sig = transcript_sig(sel.conversation_id)
                if sel.conversation_id != cached_preview_cid or cur_sig != cached_preview_sig:
                    cached_preview_cid = sel.conversation_id
                    cached_preview_sig = cur_sig
                    cached_turns = zig_tail_transcript(sel.conversation_id, max_turns=preview_height - 2)

                py = max_y - 3 - preview_height
                stdscr.addstr(py, 0, "─" * (max_x - 1))
                age_s = int(max(0, now - (cur_sig[0] / 1e9))) if cur_sig[0] else -1
                age_lbl = f"hace {age_s}s" if age_s >= 0 else "sin log"
                phdr = (
                    f" ⚡ VISTA EN VIVO ZIG (<4ms · {age_lbl}): {sel.title[:38]} ({sel.conversation_id[:8]}) "
                    f"[{sel.state_label} · {sel.location_label}]"
                )
                stdscr.addstr(py + 1, 0, phdr[: max_x - 1], curses.color_pair(2) | curses.A_BOLD)
                if not cached_turns:
                    stdscr.addstr(py + 2, 0, "   (Sin transcript.jsonl local o conversación recién creada)"[: max_x - 1])
                else:
                    for tidx, t in enumerate(cached_turns[-(preview_height - 2) :]):
                        ry = py + 2 + tidx
                        if ry >= max_y - 3:
                            break
                        if t["role"] == "USER":
                            txt = f"  👤 [{t['ts']} #{t['idx']}] TÚ: {t['content']}"
                            stdscr.addstr(ry, 0, txt[: max_x - 1], curses.color_pair(4))
                        elif t["role"] == "TOOL":
                            txt = f"  ⏳ [{t['ts']} #{t['idx']}] {t['content']}"
                            stdscr.addstr(ry, 0, txt[: max_x - 1], curses.color_pair(5))
                        else:
                            tools_s = f" [🔧 {', '.join(t['tools'])}]" if t["tools"] else ""
                            body_s = t["content"] or "(ejecutando herramientas...)"
                            txt = f"  🤖 [{t['ts']} #{t['idx']}] AGY: {body_s}{tools_s}"
                            stdscr.addstr(ry, 0, txt[: max_x - 1], curses.color_pair(3))
        else:
            mem_candidates = [
                Path(os.environ["ISYMEM_DIR"]) / "MEMORY.md" if os.environ.get("ISYMEM_DIR") else None,
                Path.cwd() / "docs" / "agent" / "memory" / "MEMORY.md",
                HOME / ".config" / "isyco-memory" / "memory" / "MEMORY.md",
            ]
            mem_idx = next((p for p in mem_candidates if p is not None and p.is_file()), Path.cwd() / "docs" / "agent" / "memory" / "MEMORY.md")
            mem_lines = (
                [l for l in mem_idx.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()]
                if mem_idx.is_file()
                else ["(MEMORY.md not found — run `isymem setup` in your workspace)"]
            )
            stdscr.addstr(
                3,
                0,
                f" 🧠 MEMORIA COMPARTIDA MULTI-HARNESS ({len(mem_lines)} entradas en docs/agent/memory/MEMORY.md — m:Volver)"[
                    : max_x - 1
                ],
                curses.color_pair(3) | curses.A_BOLD,
            )
            stdscr.addstr(4, 0, "─" * (max_x - 1))
            cursor = max(0, min(cursor, max(0, len(mem_lines) - 1)))
            avail_rows = max(1, max_y - 8)
            start_idx = max(0, cursor - avail_rows + 1) if cursor >= avail_rows else 0
            for idx, mline in enumerate(mem_lines[start_idx : start_idx + avail_rows], start=start_idx):
                row_y = 5 + (idx - start_idx)
                if row_y >= max_y - 3:
                    break
                attr = (curses.color_pair(6) | curses.A_BOLD) if idx == cursor else curses.A_NORMAL
                stdscr.addstr(row_y, 0, f" {mline}"[: max_x - 1].ljust(max_x - 1), attr)

        if max_y >= 3:
            stdscr.addstr(max_y - 3, 0, "─" * (max_x - 1))
            stdscr.addstr(max_y - 2, 0, f" {status_banner}"[: max_x - 1], curses.color_pair(4))
            keys_help = (
                " [r] Actualizar  [Enter] Abrir/Espejo  [v] Espejo Vivo  [R] Recargar Tab  "
                "[n] Nueva  [h/H] Hibernar  [x] Deduplicar  [m] Memoria  [Tab] Pestaña "
            )
            stdscr.addstr(max_y - 1, 0, keys_help[: max_x - 1], curses.A_REVERSE)

        stdscr.refresh()
        ch = stdscr.getch()
        if ch == -1:
            continue
        if ch == curses.KEY_MOUSE:
            try:
                _, mx, my, _, bstate = curses.getmouse()
            except curses.error:
                continue
            if show_cmd_panel and mx >= panel_x:
                act = hit_test_button(cmd_buttons, panel_x, panel_btn_y, mx, my)
                if act:
                    res = execute_action(act)
                    if res:
                        status_banner = res
            elif mode == "sessions" and list_start_y <= my < list_start_y + list_avail and mx < left_w:
                clicked_idx = (max(0, cursor - list_avail + 1) if cursor >= list_avail else 0) + (my - list_start_y)
                if 0 <= clicked_idx < len(sessions):
                    if (bstate & curses.BUTTON1_DOUBLE_CLICKED) or clicked_idx == cursor:
                        cursor = clicked_idx
                        if bstate & curses.BUTTON1_DOUBLE_CLICKED:
                            status_banner = execute_action("open")
                    else:
                        cursor = clicked_idx
            continue

        if ch in (ord("q"), 27, 9):
            tmux_cmd("next-window")
        elif ch == ord("D"):
            tmux_cmd("detach-client")
        elif ch in (curses.KEY_UP, ord("k")):
            cursor = max(0, cursor - 1)
        elif ch in (curses.KEY_DOWN, ord("j")):
            cursor += 1
        elif ch == ord("r"):
            status_banner = execute_action("refresh")
        elif ch == ord("m"):
            status_banner = execute_action("memory")
        elif mode == "sessions" and ch in (10, 13, curses.KEY_ENTER):
            status_banner = execute_action("open")
        elif mode == "sessions" and ch == ord("v"):
            status_banner = execute_action("mirror")
        elif mode == "sessions" and ch == ord("R"):
            status_banner = execute_action("reload")
        elif mode == "sessions" and ch == ord("n"):
            status_banner = execute_action("new")
        elif mode == "sessions" and ch == ord("h"):
            status_banner = execute_action("hibernate")
        elif mode == "sessions" and ch in (ord("H"), ord("a")):
            status_banner = execute_action("hib_all")
        elif mode == "sessions" and ch == ord("x"):
            status_banner = execute_action("dedup")


def cmd_tui(_args: argparse.Namespace) -> int:
    curses.wrapper(run_curses_tui)
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    if not shutil.which("tmux"):
        return cmd_tui(args)

    self_path = Path(__file__).resolve()
    write_tmux_config(self_path)

    if TMUX_SOCKET in os.environ.get("TMUX", ""):
        ensure_dash_window_zero()
        tmux_cmd("select-window", "-t", f"{TMUX_SESSION}:0")
        return 0

    has = tmux_cmd("has-session", "-t", TMUX_SESSION, capture=True)
    if has.returncode != 0:
        tui_cmd = f"exec python3 {sh_quote(str(self_path))} tui"
        tmux_cmd("new-session", "-d", "-s", TMUX_SESSION, "-n", "⚡DASH", tui_cmd)
        for s in load_sessions(limit=20)[2]:
            if s.hibernated:
                open_session_in_dashboard_tab(s, switch_to=False, as_stub=True)
        tmux_cmd("select-window", "-t", f"{TMUX_SESSION}:0")
    else:
        ensure_dash_window_zero()
        tmux_cmd("source-file", str(TMUX_CONF))

    if getattr(args, "detach", False) or not sys.stdin.isatty():
        print(f"Dashboard listo en servidor tmux `{TMUX_SOCKET}` (sesión `{TMUX_SESSION}`).")
        return 0

    os.execvp("tmux", ["tmux", "-L", TMUX_SOCKET, "-f", str(TMUX_CONF), "attach-session", "-t", TMUX_SESSION])
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="agy-dash",
        description="Hybrid Zig+Python Single-Window Command Center & RAM Optimizer for AGY",
    )
    ap.add_argument("--detach", action="store_true", help="Ensure dashboard session without attaching")
    sub = ap.add_subparsers(dest="cmd")

    p_st = sub.add_parser("status", help="Show live RAM, KSM sharing, and session status (via Zig agy-fast-core)")
    p_st.set_defaults(fn=cmd_status)

    p_sb = sub.add_parser("statusbar", help="Compact status string for tmux top bar")
    p_sb.set_defaults(fn=cmd_statusbar)

    p_hib = sub.add_parser("hibernate", help="Hibernate an idle session (or `all-idle`) to 0 MB RAM")
    p_hib.add_argument("target", nargs="?", default="all-idle")
    p_hib.set_defaults(fn=cmd_hibernate)

    p_tui = sub.add_parser("tui", help="Run the interactive curses Command Center panel")
    p_tui.set_defaults(fn=cmd_tui)

    p_mir = sub.add_parser("mirror", help="Open real-time <3MB Zig tail live mirror for a conversation")
    p_mir.add_argument("conversation_id")
    p_mir.add_argument("--cwd", default="")
    p_mir.add_argument("--title", default="")
    p_mir.set_defaults(fn=cmd_live_mirror)

    p_ws = sub.add_parser("wake-stub", help="Internal wake stub for hibernated dashboard tabs")
    p_ws.add_argument("conversation_id")
    p_ws.add_argument("--cwd", default="")
    p_ws.add_argument("--title", default="")
    p_ws.set_defaults(fn=cmd_wake_stub)

    p_new = sub.add_parser("open-new-tab", help="Open a new optimized AGY session tab")
    p_new.add_argument("--cwd", default="")
    p_new.set_defaults(fn=cmd_open_new_tab)

    return ap


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] == "run":
        conv = ""
        cwd = ""
        title = ""
        rest: list[str] = []
        argv = sys.argv[2:]
        i = 0
        while i < len(argv):
            a = argv[i]
            if a == "--":
                rest.extend(argv[i + 1 :])
                break
            elif a == "--conversation" and i + 1 < len(argv):
                conv = argv[i + 1]
                i += 2
            elif a == "--cwd" and i + 1 < len(argv):
                cwd = argv[i + 1]
                i += 2
            elif a == "--title" and i + 1 < len(argv):
                title = argv[i + 1]
                i += 2
            else:
                rest.extend(argv[i:])
                break
        extra = (["--conversation", conv] if conv else []) + rest
        return exec_agy_optimized(extra, cwd=cwd or None, preview_conv=conv, preview_title=title)

    ap = build_parser()
    args = ap.parse_args()
    if not getattr(args, "cmd", None):
        return cmd_dashboard(args)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
