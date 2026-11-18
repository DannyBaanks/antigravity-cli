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
import threading
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
ACCOUNTS_DIR = AGY_DATA_DIR / "accounts"
ACCOUNTS_REGISTRY = ACCOUNTS_DIR / "registry.json"
TMUX_CONF = AGY_DATA_DIR / "agy-dash.tmux.conf"
TMUX_SOCKET = "agy-dash"
TMUX_SESSION = "agy"

AGY_BIN = os.environ.get("AGY_BIN", str(HOME / ".local" / "bin" / "agy"))
DEFAULT_GOGC = os.environ.get("AGY_GOGC", "35")
DEFAULT_GOMEMLIMIT = os.environ.get("AGY_GOMEMLIMIT", "180MiB")
DEFAULT_IDLE_HIBERNATE_SEC = int(os.environ.get("AGY_IDLE_HIBERNATE_SEC", "180"))
QUOTA_WARN_PCT = float(os.environ.get("AGY_QUOTA_WARN_PCT", "15"))
QUOTA_CRIT_PCT = float(os.environ.get("AGY_QUOTA_CRIT_PCT", "5"))
QUOTA_POLL_INTERVAL_SEC = float(os.environ.get("AGY_QUOTA_POLL_SEC", "120"))

MODEL_DISPLAY_TO_SLUG: dict[str, str] = {
    "Gemini 4 Argon (Medium)": "gemini-4-argon-medium",
    "Gemini 4 Argon (High)": "gemini-4-argon-high",
    "Gemini 4 Argon (Low)": "gemini-4-argon-low",
    "Gemini 4 Hydrogen (High)": "gemini-4-hydrogen-high",
    "Gemini 4 Hydrogen (XHigh)": "gemini-4-hydrogen-xhigh",
    "Gemini 4 Hydrogen (Medium)": "gemini-4-hydrogen-medium",
    "Gemini 4 Hydrogen (Low)": "gemini-4-hydrogen-low",
    "Orchestrator (Argon)": "gemini-pro-orchestrator-g1-pro",
    "Gemini 3.8 Flash (High)": "gemini-3.8-flash-high",
    "Gemini 3.8 Flash (Medium)": "gemini-3.8-flash-medium",
    "Gemini 3.8 Flash (Low)": "gemini-3.8-flash-low",
    "Claude Opus 5.5 (Low)": "claude-opus-5-5-low",
    "Claude Opus 5.5 (Medium)": "claude-opus-5-5-medium",
    "Claude Opus 5.5 (High)": "claude-opus-5-5-high",
    "Claude Sonnet 5.5 (Low)": "claude-sonnet-5-5-low",
    "Claude Sonnet 5.5 (Medium)": "claude-sonnet-5-5-medium",
    "Claude Sonnet 5.5 (High)": "claude-sonnet-5-5-high",
    "GPT-OSS 120B (Medium)": "gpt-oss-120b-medium",
}

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
            if "👁" in self.dash_win_name:
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


# ============================================================================
# Multi-Account Vault, Quota Monitor, and Baton Pass
# ============================================================================

_SHARED_AGY_DATA_ITEMS = (
    "conversations",
    "brain",
    "conversation_summaries.db",
    "conversation_summaries.db-shm",
    "conversation_summaries.db-wal",
    "settings.json",
    "mcp",
    "builtin",
    "implicit",
    "presence",
    "annotations",
    "jetski_state.pbtxt",
    "history.jsonl",
    "installation_id",
)

_SHARED_GEMINI_ITEMS = (
    "config",
    "GEMINI.md",
    "settings.json",
    "projects.json",
    "trustedFolders.json",
    "antigravity",
)

_SHARED_HOME_ITEMS = (
    ".ssh",
    ".gitconfig",
    ".git-credentials",
    ".local",
    ".cargo",
    ".rustup",
    ".nvm",
    ".npm",
    ".bun",
    ".cache",
)

_EMAIL_RE = re.compile(r"authenticated successfully as ([^\s,;]+@[^\s,;]+)")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def validate_slot_name(slot: str) -> str:
    """Normalize and validate an account slot identifier."""
    clean = re.sub(r"[^a-zA-Z0-9_-]+", "-", slot.strip().lower()).strip("-")
    if not clean or len(clean) > 24:
        raise ValueError(f"Invalid account slot name: {slot!r}")
    return clean


def _ensure_symlink(link_path: Path, target: Path) -> None:
    """Create a symlink at link_path -> target when target exists and no conflict exists."""
    if not target.exists():
        return
    if link_path.is_symlink():
        try:
            if link_path.resolve(strict=False) == target.resolve(strict=False):
                return
            link_path.unlink()
        except OSError:
            return
    elif link_path.exists():
        return
    try:
        link_path.symlink_to(target)
    except OSError:
        pass


def ensure_isolated_home(
    slot: str,
    *,
    base_home: Path | None = None,
    agy_data_dir: Path | None = None,
    accounts_dir: Path | None = None,
) -> Path:
    """Build an isolated per-account HOME that shares conversations/MCPs but isolates OAuth tokens."""
    safe_slot = validate_slot_name(slot)
    real_home = base_home or HOME
    real_agy_data = agy_data_dir or AGY_DATA_DIR
    acct_root = accounts_dir or ACCOUNTS_DIR

    slot_dir = acct_root / safe_slot
    slot_home = slot_dir / "home"
    slot_cfg = slot_home / ".config"
    slot_cfg_agy = slot_cfg / "antigravity"
    slot_gemini = slot_home / ".gemini"
    slot_agy_data = slot_gemini / "antigravity-cli"

    for d in (acct_root, slot_dir, slot_home, slot_cfg, slot_cfg_agy, slot_gemini, slot_agy_data):
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass

    for name in _SHARED_AGY_DATA_ITEMS:
        _ensure_symlink(slot_agy_data / name, real_agy_data / name)

    real_gemini = real_home / ".gemini"
    for name in _SHARED_GEMINI_ITEMS:
        _ensure_symlink(slot_gemini / name, real_gemini / name)

    for name in _SHARED_HOME_ITEMS:
        _ensure_symlink(slot_home / name, real_home / name)

    _ensure_symlink(slot_cfg / "gh", real_home / ".config" / "gh")
    return slot_home


def extract_email_from_cli_log(home_dir: Path) -> str:
    """Extract the most recent authenticated Google email from cli.log without touching credentials."""
    log_path = home_dir / ".gemini" / "antigravity-cli" / "cli.log"
    if not log_path.exists():
        return ""
    try:
        with log_path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 65536), os.SEEK_SET)
            text = f.read().decode("utf-8", errors="replace")
        matches = _EMAIL_RE.findall(text)
        return matches[-1] if matches else ""
    except OSError:
        return ""


def load_accounts_registry(registry_path: Path | None = None, base_home: Path | None = None) -> dict:
    """Load non-secret account registry, seeding `primary` (system keyring) if absent."""
    reg_file = registry_path or ACCOUNTS_REGISTRY
    real_home = base_home or HOME
    data: dict = {}
    if reg_file.is_file():
        try:
            data = json.loads(reg_file.read_text(encoding="utf-8"))
        except Exception:
            data = {}

    if not isinstance(data, dict):
        data = {}
    accounts = data.setdefault("accounts", {})
    if not isinstance(accounts, dict):
        accounts = {}
        data["accounts"] = accounts

    if "primary" not in accounts:
        accounts["primary"] = {
            "slot": "primary",
            "mode": "system",
            "label": "Principal (Keyring)",
            "email": extract_email_from_cli_log(real_home),
            "authenticated": True,
            "quota": {},
            "updated_at": "",
        }
    elif not accounts["primary"].get("email"):
        detected = extract_email_from_cli_log(real_home)
        if detected:
            accounts["primary"]["email"] = detected

    if not data.get("active_slot") or data["active_slot"] not in accounts:
        data["active_slot"] = "primary"
    if not isinstance(data.get("conv_accounts"), dict):
        data["conv_accounts"] = {}
    return data


def save_accounts_registry(data: dict, registry_path: Path | None = None) -> None:
    """Persist account metadata (never tokens/secrets) atomically with 0600 permissions."""
    reg_file = registry_path or ACCOUNTS_REGISTRY
    reg_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(reg_file.parent, 0o700)
    except OSError:
        pass
    tmp = reg_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, reg_file)


def model_to_quota_family(model_name: str) -> str:
    """Map an AGY model display name or slug to its `/usage` quota group family."""
    low = model_name.lower()
    if "argon" in low or "orchestrator" in low:
        return "argon"
    if any(tok in low for tok in ("claude", "opus", "sonnet", "gpt", "3p")):
        return "claude_gpt"
    return "gemini"


def _bucket_family(group_name: str, bucket_id: str) -> str:
    low = f"{group_name} {bucket_id}".lower()
    if "argon" in low:
        return "argon"
    if "claude" in low or "gpt" in low or "3p" in low:
        return "claude_gpt"
    return "gemini"


def resolve_model_arg(explicit_model: str = "", agy_data_dir: Path | None = None) -> tuple[str, str]:
    """Return (display_name, cli_slug) using explicit_model or settings.json."""
    chosen = explicit_model.strip()
    if not chosen:
        settings_path = (agy_data_dir or AGY_DATA_DIR) / "settings.json"
        if settings_path.is_file():
            try:
                cfg = json.loads(settings_path.read_text(encoding="utf-8"))
                chosen = str(cfg.get("model", "")).strip()
            except Exception:
                chosen = ""
    if not chosen:
        return ("", "")
    if chosen in MODEL_DISPLAY_TO_SLUG:
        return (chosen, MODEL_DISPLAY_TO_SLUG[chosen])
    slug_to_disp = {v: k for k, v in MODEL_DISPLAY_TO_SLUG.items()}
    if chosen in slug_to_disp:
        return (slug_to_disp[chosen], chosen)
    for disp, slug in MODEL_DISPLAY_TO_SLUG.items():
        if disp.lower() == chosen.lower() or slug.lower() == chosen.lower():
            return (disp, slug)
    slug = re.sub(r"[^a-z0-9.]+", "-", chosen.lower()).strip("-")
    return (chosen, slug)


def parse_usage_output(raw_stdout: str) -> dict:
    """Parse `agy -p '/usage' --output-format json` into normalized quota percentages."""
    text = raw_stdout.strip()
    if not text:
        return {"ok": False, "error": "empty output", "buckets": [], "families": {}, "min_pct": None}

    data = None
    for line in [text] + text.splitlines():
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                data = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
    if not isinstance(data, dict):
        return {"ok": False, "error": "invalid JSON", "buckets": [], "families": {}, "min_pct": None}

    if data.get("status") != "SUCCESS":
        return {"ok": False, "error": str(data.get("status", "ERROR")), "buckets": [], "families": {}, "min_pct": None}

    buckets: list[dict] = []
    groups = data.get("command", {}).get("data", {}).get("groups", [])
    if isinstance(groups, list) and groups:
        for grp in groups:
            gname = str(grp.get("name", ""))
            for b in grp.get("buckets", []) or []:
                frac = float(b.get("remaining_fraction", 0.0))
                pct = round(max(0.0, min(100.0, frac * 100.0)), 1)
                bid = str(b.get("id", ""))
                buckets.append({
                    "group": gname,
                    "id": bid,
                    "name": str(b.get("name", bid)),
                    "window": str(b.get("window", "")),
                    "family": _bucket_family(gname, bid),
                    "pct": pct,
                    "reset_time": str(b.get("reset_time", "")),
                })
    else:
        for line in str(data.get("response", "")).splitlines():
            parts = line.split("\t")
            if len(parts) >= 4 and parts[2].endswith("%"):
                try:
                    pct = float(parts[2].rstrip("%"))
                except ValueError:
                    continue
                buckets.append({
                    "group": parts[0],
                    "id": parts[1].lower().replace(" ", "-"),
                    "name": parts[1],
                    "window": "5h" if "five" in parts[1].lower() else "weekly",
                    "family": _bucket_family(parts[0], parts[1]),
                    "pct": round(pct, 1),
                    "reset_time": parts[3],
                })

    families: dict[str, float] = {}
    for b in buckets:
        fam = b["family"]
        if fam not in families or b["pct"] < families[fam]:
            families[fam] = b["pct"]
    min_pct = min((b["pct"] for b in buckets), default=None)
    return {"ok": bool(buckets), "buckets": buckets, "families": families, "min_pct": min_pct}


def build_agy_env(account_slot: str | None = None, *, registry_path: Path | None = None) -> dict[str, str]:
    """Build execution env for `agy`, isolating HOME+XDG+DBUS when running an isolated slot."""
    env = os.environ.copy()
    env.setdefault("GOGC", DEFAULT_GOGC)
    env.setdefault("GOMEMLIMIT", DEFAULT_GOMEMLIMIT)

    reg = load_accounts_registry(registry_path=registry_path)
    slot = account_slot or reg.get("active_slot", "primary")
    acct = reg.get("accounts", {}).get(slot, {})
    mode = acct.get("mode", "system" if slot == "primary" else "isolated")
    if mode == "isolated":
        slot_home = ensure_isolated_home(slot)
        env["HOME"] = str(slot_home)
        env["XDG_CONFIG_HOME"] = str(slot_home / ".config")
        env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/nonexistent"
        env["AGY_ACCOUNT_SLOT"] = slot
    else:
        env["AGY_ACCOUNT_SLOT"] = slot
    return env


def refresh_account_quota(slot: str, timeout_sec: float = 15.0) -> tuple[bool, str]:
    """Query `/usage --output-format json` for one account slot without prompting interactively."""
    reg = load_accounts_registry()
    if slot not in reg["accounts"]:
        return False, f"Cuenta '{slot}' no existe en el registro."

    env = build_agy_env(slot)
    home_for_log = Path(env.get("HOME", str(HOME)))
    try:
        proc = subprocess.Popen(
            [AGY_BIN, "-p", "/usage", "--output-format", "json"],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + timeout_sec
        stdout_text = ""
        stderr_text = ""
        while True:
            rem = deadline - time.monotonic()
            if rem <= 0:
                proc.kill()
                stdout_text, stderr_text = proc.communicate()
                if "Authentication required" in stderr_text or "https://accounts.google.com/o/oauth2" in stderr_text:
                    reg["accounts"][slot]["authenticated"] = False
                    save_accounts_registry(reg)
                    return False, f"[{slot}] Sin sesión iniciada (requiere login OAuth)."
                return False, f"[{slot}] Timeout consultando /usage."
            try:
                stdout_text, stderr_text = proc.communicate(timeout=min(0.6, rem))
                break
            except subprocess.TimeoutExpired:
                log_path = home_for_log / ".gemini" / "antigravity-cli" / "cli.log"
                if log_path.is_file() and (time.time() - log_path.stat().st_mtime) < 15:
                    try:
                        with log_path.open("rb") as lf:
                            lf.seek(0, os.SEEK_END)
                            lf.seek(max(0, lf.tell() - 8192), os.SEEK_SET)
                            tail = lf.read().decode("utf-8", errors="replace")
                        if "starting OAuth flow" in tail and proc.poll() is None:
                            proc.kill()
                            stdout_text, stderr_text = proc.communicate()
                            reg["accounts"][slot]["authenticated"] = False
                            save_accounts_registry(reg)
                            return False, f"[{slot}] Sin sesión iniciada (requiere login OAuth)."
                    except OSError:
                        pass
                continue
    except OSError as e:
        return False, f"[{slot}] Error ejecutando agy: {e}"

    if "Authentication required" in stderr_text:
        reg["accounts"][slot]["authenticated"] = False
        save_accounts_registry(reg)
        return False, f"[{slot}] Sin sesión iniciada (requiere login OAuth)."

    parsed = parse_usage_output(stdout_text)
    if not parsed["ok"]:
        return False, f"[{slot}] /usage falló: {parsed.get('error', stderr_text[:100])}"

    email = extract_email_from_cli_log(home_for_log)
    reg = load_accounts_registry()
    acct = reg["accounts"].setdefault(slot, {"slot": slot, "mode": "isolated" if slot != "primary" else "system"})
    if email:
        acct["email"] = email
    acct["authenticated"] = True
    acct["quota"] = parsed
    acct["updated_at"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    save_accounts_registry(reg)
    return True, f"[{slot}] Cuota actualizada (mín {parsed['min_pct']}%)."


def compute_quota_alert(
    reg: dict,
    *,
    active_slot: str | None = None,
    model_name: str = "",
    warn_pct: float = QUOTA_WARN_PCT,
    crit_pct: float = QUOTA_CRIT_PCT,
) -> dict | None:
    """Return a structured alert dict when the active account runs low on any or current model quota."""
    accounts = reg.get("accounts", {})
    slot = active_slot or reg.get("active_slot", "primary")
    cur = accounts.get(slot)
    if not cur or not cur.get("quota", {}).get("ok"):
        return None

    buckets = cur["quota"].get("buckets", [])
    if not buckets:
        return None

    target_family = model_to_quota_family(model_name) if model_name else ""
    fam_buckets = [b for b in buckets if b.get("family") == target_family] if target_family else []
    candidates = fam_buckets or buckets
    worst_model = min(candidates, key=lambda b: float(b.get("pct", 100.0)))
    worst_any = min(buckets, key=lambda b: float(b.get("pct", 100.0)))
    worst = worst_model if float(worst_model["pct"]) <= warn_pct else worst_any

    pct = float(worst.get("pct", 100.0))
    if pct > warn_pct:
        return None

    check_fam = str(worst.get("family", target_family or "gemini"))
    best_target = ""
    best_target_email = ""
    best_target_pct = -1.0
    for sname, sdata in accounts.items():
        if sname == slot or not sdata.get("authenticated", True):
            continue
        q = sdata.get("quota", {})
        if not q.get("ok"):
            if not best_target:
                best_target = sname
                best_target_email = sdata.get("email", "")
            continue
        other_pct = float(q.get("families", {}).get(check_fam, q.get("min_pct") or 100.0))
        if other_pct > best_target_pct:
            best_target = sname
            best_target_email = sdata.get("email", "")
            best_target_pct = other_pct

    return {
        "level": "CRITICAL" if pct <= crit_pct else "WARN",
        "active_slot": slot,
        "active_email": cur.get("email", ""),
        "bucket_name": worst.get("name", worst.get("group", check_fam)),
        "family": check_fam,
        "pct": pct,
        "reset_time": worst.get("reset_time", ""),
        "target_slot": best_target,
        "target_email": best_target_email,
        "target_pct": best_target_pct,
    }


def choose_baton_target(reg: dict, current_slot: str, model_name: str = "") -> str:
    """Pick the best authenticated account slot different from current_slot for Baton Pass."""
    fam = model_to_quota_family(model_name) if model_name else "gemini"
    best_slot = ""
    best_score = -2.0
    for sname, sdata in reg.get("accounts", {}).items():
        if sname == current_slot:
            continue
        if not sdata.get("authenticated", False):
            continue
        q = sdata.get("quota", {})
        if q.get("ok"):
            score = float(q.get("families", {}).get(fam, q.get("min_pct") or 0.0))
        else:
            score = -0.5
        if score > best_score:
            best_score = score
            best_slot = sname
    return best_slot


def exec_agy_optimized(
    extra_args: list[str],
    cwd: str | None = None,
    preview_conv: str = "",
    preview_title: str = "",
    account_slot: str | None = None,
    model: str = "",
) -> int:
    enable_ksm_memory_merge()
    env = build_agy_env(account_slot)
    slot_used = env.get("AGY_ACCOUNT_SLOT", "primary")
    target_cwd = cwd if (cwd and Path(cwd).is_dir()) else str(Path.cwd())
    os.chdir(target_cwd)

    final_args = list(extra_args)
    model_disp, model_slug = resolve_model_arg(model)
    if model and model_slug and "--model" not in final_args:
        final_args.extend(["--model", model_slug])

    if preview_conv:
        print_instant_zig_preview(preview_conv, preview_title or preview_conv[:8], target_cwd)
        print(f"  \033[1;35m💳 Cuenta:\033[0m [{slot_used}]  \033[1;36m🧠 Modelo:\033[0m {model_disp or model_slug or 'default settings.json'}")

    if ensure_zig_core():
        cmd = [str(ZIG_CORE_BIN), "ksm-exec", AGY_BIN] + final_args
        os.execvpe(str(ZIG_CORE_BIN), cmd, env)
    cmd = [AGY_BIN] + final_args
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
set -g allow-rename off
setw -g automatic-rename off

# Top dashboard bar powered by Zig `agy-fast-core statusbar` (<7ms)
set -g status-position top
set -g status-interval 1
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


def _find_dash_window_idx() -> str:
    cp = tmux_cmd("list-windows", "-t", TMUX_SESSION, "-F", "#{window_index}|#{window_name}", capture=True)
    if cp.returncode == 0 and cp.stdout:
        for line in cp.stdout.splitlines():
            parts = line.split("|", 1)
            if len(parts) == 2 and "DASH" in parts[1]:
                return parts[0].strip()
    return "0"


def _select_dash_window() -> None:
    tmux_cmd("select-window", "-t", f"{TMUX_SESSION}:{_find_dash_window_idx()}")


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
    account_slot: str | None = None,
    model: str = "",
) -> str:
    self_path = Path(__file__).resolve()
    existing_idx = _find_tmux_window_by_conv(session.conversation_id)
    cwd = session.workspace if Path(session.workspace).is_dir() else str(Path.cwd())
    reg = load_accounts_registry()
    chosen_slot = account_slot or reg.get("conv_accounts", {}).get(session.conversation_id) or reg.get("active_slot", "primary")
    if account_slot:
        reg.setdefault("conv_accounts", {})[session.conversation_id] = chosen_slot
        save_accounts_registry(reg)

    # If external session is busy (RUNNING / BG-TASK) and user didn't force interactive duplicate,
    # open a <3MB Real-Time Zig Live Mirror instead of spawning a stale duplicate `agy`!
    use_mirror = force_live_mirror or (
        (not as_stub) and (not force_interactive) and session.external_busy
    )

    migrated_ext = False
    if not as_stub and not use_mirror and session.proc is not None and (not session.proc.in_dash or account_slot):
        if not session.proc.active_children and session.db_status != "CASCADE_RUN_STATUS_RUNNING" and not is_ancestor_pid(session.proc.pid):
            if not session.proc.in_dash:
                try:
                    os.kill(session.proc.pid, signal.SIGTERM)
                    migrated_ext = True
                except OSError:
                    pass

    model_disp, model_slug = resolve_model_arg(model)
    slot_flag = f" --account {sh_quote(chosen_slot)}" if chosen_slot else ""
    model_flag = f" --model {sh_quote(model_slug)}" if (model and model_slug) else ""

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
        tab_prefix = f"●{chosen_slot[:3]}·" if chosen_slot != "primary" else "●"
        tab_name = _short_tab_title(session.title, prefix=tab_prefix)
        cmd = (
            f"exec python3 {sh_quote(str(self_path))} run "
            f"--conversation {sh_quote(session.conversation_id)} --cwd {sh_quote(cwd)} "
            f"--title {sh_quote(session.title)}{slot_flag}{model_flag}"
        )

    if existing_idx is not None:
        if force_interactive or force_live_mirror or account_slot or (
            not as_stub and (session.proc is None or not session.proc.in_dash or (use_mirror and "👁" not in session.dash_win_name))
        ):
            hib_reg = load_hibernated_registry()
            if session.conversation_id in hib_reg:
                del hib_reg[session.conversation_id]
                save_hibernated_registry(hib_reg)
            tmux_cmd("respawn-pane", "-k", "-t", f"{TMUX_SESSION}:{existing_idx}.0", cmd)
            tmux_cmd("rename-window", "-t", f"{TMUX_SESSION}:{existing_idx}", tab_name)
            tmux_cmd("set-window-option", "-t", f"{TMUX_SESSION}:{existing_idx}", "@agy_account", chosen_slot)
        if switch_to:
            tmux_cmd("select-window", "-t", f"{TMUX_SESSION}:{existing_idx}")
        if account_slot:
            return f"🔀 Baton Pass -> [{chosen_slot}] ({model_disp or 'modelo actual'}) en pestaña #{existing_idx}."
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
        tmux_cmd("set-window-option", "-t", f"{TMUX_SESSION}:{last_win}", "@agy_account", chosen_slot)
    if account_slot:
        return f"🔀 Baton Pass -> '{session.title}' continuada en [{chosen_slot}] ({model_disp or 'modelo actual'})."
    if use_mirror:
        return f"Espejo en vivo tiempo real ('{session.title}') — actualiza en <0.4s desde {session.proc.tty if session.proc else 'ext'}."
    if migrated_ext:
        return f"Migrada '{session.title}' desde consola externa al Dashboard."
    return f"Abierta '{session.title}' en pestaña del Dashboard."


def baton_pass_session(
    session: AgySession,
    *,
    target_slot: str = "",
    model: str = "",
    switch_to: bool = True,
) -> tuple[bool, str]:
    """Hand off an IDLE/disk conversation to another account slot while keeping conversation + model."""
    if session.external_busy:
        return False, "⚠️ Turno o tarea activa en ejecución; espera a IDLE antes de pasar la estafeta."
    if session.proc and is_ancestor_pid(session.proc.pid):
        return False, "⚠️ No se puede reemplazar el proceso AGY ancestro directo del dashboard."

    reg = load_accounts_registry()
    cur_slot = reg.get("conv_accounts", {}).get(session.conversation_id) or reg.get("active_slot", "primary")
    model_disp, model_slug = resolve_model_arg(model)

    chosen_slot = target_slot
    if not chosen_slot:
        chosen_slot = choose_baton_target(reg, cur_slot, model_disp or model_slug)
    if not chosen_slot or chosen_slot not in reg.get("accounts", {}):
        return False, "⚠️ No hay otra cuenta Pro autenticada. Pulsa [u] Cuentas -> [+] para agregar tu 2ª cuenta."
    if not reg["accounts"][chosen_slot].get("authenticated", False):
        return False, f"⚠️ La cuenta [{chosen_slot}] no ha completado login OAuth. Pulsa [u] para autenticarla."

    if session.proc and not session.proc.in_dash:
        try:
            os.kill(session.proc.pid, signal.SIGTERM)
        except OSError:
            pass

    hib_reg = load_hibernated_registry()
    if session.conversation_id in hib_reg:
        del hib_reg[session.conversation_id]
        save_hibernated_registry(hib_reg)

    msg = open_session_in_dashboard_tab(
        session,
        switch_to=switch_to,
        force_interactive=True,
        account_slot=chosen_slot,
        model=model_slug or model_disp,
    )
    return True, msg


def launch_account_login_tab(slot: str) -> str:
    """Open an isolated tmux tab in agy-dash to sign into an additional Google account without logging out primary."""
    safe_slot = validate_slot_name(slot)
    if safe_slot == "primary":
        return "La cuenta `primary` usa el keyring del sistema ya autenticado."
    ensure_isolated_home(safe_slot)
    reg = load_accounts_registry()
    reg["accounts"].setdefault(
        safe_slot,
        {
            "slot": safe_slot,
            "mode": "isolated",
            "label": f"Pro Aislada ({safe_slot})",
            "email": "",
            "authenticated": False,
            "quota": {},
            "updated_at": "",
        },
    )
    save_accounts_registry(reg)
    self_path = Path(__file__).resolve()
    cmd = f"exec python3 {sh_quote(str(self_path))} account-login {sh_quote(safe_slot)}"
    tmux_cmd("new-window", "-t", TMUX_SESSION, "-n", f"🔑{safe_slot[:12]}", cmd)
    return f"Abierta pestaña 🔑 Login aislado para [{safe_slot}]. Completa Google OAuth en el navegador y sal (/exit)."


# ============================================================================
# Live LED Tab Marquee & Braille Spinner
# ============================================================================

_SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
_TOOL_LABELS = {
    "edit_file": "Editing files...",
    "write_file": "Writing file...",
    "replace_file_content": "Editing files...",
    "read_file": "Reading files...",
    "run_command": "Running cmd...",
    "manage_task": "Updating task...",
    "ziggrep": "Searching code...",
    "grep_search": "Searching code...",
    "find_by_name": "Finding files...",
    "search_web": "Searching web...",
    "read_url": "Reading URL...",
    "invoke_subagent": "Subagent busy...",
    "call_mcp_tool": "MCP tool...",
}


def session_live_activity(
    session: AgySession,
    activity_cache: dict[str, tuple[tuple[int, int], str]],
) -> tuple[bool, str]:
    """Return (is_busy, short_activity_label) from session state and latest transcript turn."""
    if session.hibernated:
        return (False, "0MB HIB")
    is_busy = session.state_label in ("RUNNING", "BG-TASK")
    if not is_busy:
        return (False, "IDLE")

    cid = session.conversation_id
    sig = transcript_sig(cid)
    cached = activity_cache.get(cid)
    label = ""
    if cached and cached[0] == sig:
        label = cached[1]
    else:
        turns = zig_tail_transcript(cid, max_turns=2, include_tools=True)
        for t in reversed(turns):
            tools = t.get("tools") or []
            if tools:
                raw_tool = str(tools[-1])
                label = _TOOL_LABELS.get(raw_tool, f"{raw_tool[:14]}...")
                break
            if t.get("role") == "TOOL" and t.get("content"):
                label = str(t["content"]).replace("🔧", "").strip()[:18]
                break
        activity_cache[cid] = (sig, label)

    if not label and session.proc and session.proc.active_children:
        label = f"Running {session.proc.active_children[0][:10]}..."
    return (True, label or "Working...")


def format_led_tab_title(
    session: AgySession,
    tick_idx: int,
    activity_text: str,
    busy: bool,
    width: int = 16,
) -> str:
    """Build a compact LED-marquee tab label with left-side Braille spinner dots when busy."""
    clean_title = re.sub(r"[^A-Za-z0-9áéíóúÁÉÍÓÚñÑ_-]+", "-", session.title.strip()).strip("-") or "session"
    if session.hibernated:
        return f"💤{clean_title[:width - 2]}"
    is_mirror = session.dash_win_name.startswith("👁")
    if not busy:
        prefix = "👁" if is_mirror else "●"
        return f"{prefix}{clean_title[:width - 1]}"

    spin = _SPINNER_FRAMES[tick_idx % len(_SPINNER_FRAMES)]
    prefix = f"{spin}{'👁' if is_mirror else ''}"
    slot_w = max(8, width - len(prefix))
    tape = f"{clean_title[:22]} · {activity_text} · "
    if len(tape) <= slot_w:
        body = tape
    else:
        offset = tick_idx % len(tape)
        doubled = tape + tape
        body = doubled[offset : offset + slot_w]
    return f"{prefix}{body}"


def sync_led_tmux_tabs(
    sessions: list[AgySession],
    tick_idx: int,
    last_tab_names: dict[str, str],
    activity_cache: dict[str, tuple[tuple[int, int], str]],
) -> None:
    """Update open tmux tab names with animated spinner + LED action marquee."""
    cmds: list[str] = []
    for s in sessions:
        if s.dash_win_idx is None:
            continue
        # Do not clobber interactive login windows
        if s.dash_win_name.startswith("🔑"):
            continue
        busy, act_txt = session_live_activity(s, activity_cache)
        new_name = format_led_tab_title(s, tick_idx, act_txt, busy)
        prev = last_tab_names.get(s.dash_win_idx)
        if prev != new_name:
            last_tab_names[s.dash_win_idx] = new_name
            cmds.extend(["rename-window", "-t", f"{TMUX_SESSION}:{s.dash_win_idx}", new_name, ";"])
    if cmds:
        tmux_cmd(*cmds[:-1])


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
        bar = " [r] 🔄 Actualizar  [b] 🔀 Baton Pass  [m] 🚀 Migrar  [o] AGY  [d/q] ⬅ ⚡DASH "
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
                if mx < 19:
                    ch = ord("r")
                elif mx < 37:
                    ch = ord("b")
                elif mx < 51:
                    ch = ord("m")
                elif mx < 60:
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
            _select_dash_window()
        elif ch in (ord("b"), ord("B")):
            if sess and not sess.external_busy:
                return "baton"
            status_msg = "⚠️ Espera a que termine el turno actual (IDLE) antes de hacer Baton Pass."
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
    if action == "baton":
        _, _, sessions = load_sessions(limit=40)
        sess = next((s for s in sessions if s.conversation_id == cid), None)
        if sess:
            ok, msg = baton_pass_session(sess)
            if not ok:
                print(msg, file=sys.stderr)
                time.sleep(2.0)
                return cmd_live_mirror(args)
        return 0
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
        _select_dash_window()
        return cmd_wake_stub(args)

    reg = load_hibernated_registry()
    if cid in reg:
        del reg[cid]
        save_hibernated_registry(reg)
    return exec_agy_optimized(["--conversation", cid], cwd=cwd, preview_conv=cid, preview_title=title)


def cmd_open_new_tab(args: argparse.Namespace) -> int:
    self_path = Path(__file__).resolve()
    cwd = getattr(args, "cwd", None) or str(Path.cwd())
    reg = load_accounts_registry()
    slot = getattr(args, "account", "") or reg.get("active_slot", "primary")
    tab_prefix = f"●{slot[:3]}·" if slot != "primary" else "●"
    cmd = f"exec python3 {sh_quote(str(self_path))} run --cwd {sh_quote(cwd)} --account {sh_quote(slot)}"
    tmux_cmd("new-window", "-t", TMUX_SESSION, "-n", f"{tab_prefix}nueva", "-c", cwd, cmd)
    return 0


def _print_accounts_table(reg: dict) -> None:
    active_slot = reg.get("active_slot", "primary")
    model_disp, _ = resolve_model_arg()
    fam_focus = model_to_quota_family(model_disp)
    print(f"  Cuenta activa por defecto: [{active_slot}] | Modelo actual: {model_disp or 'default'} (grupo: {fam_focus})")
    print(f"  {'SLOT':<12} {'ACT':<4} {'MODO':<9} {'EMAIL':<28} {'ARGON':>7} {'GEMINI':>7} {'CLAUDE/GPT':>10}")
    print("  " + "─" * 86)
    for slot, acct in reg.get("accounts", {}).items():
        mark = "★" if slot == active_slot else " "
        q = acct.get("quota", {})
        fams = q.get("families", {}) if q.get("ok") else {}
        def _fmt(k: str) -> str:
            return f"{fams[k]:.0f}%" if k in fams else "-"
        email = acct.get("email", "") or ("(sin login)" if not acct.get("authenticated") else "(pendiente)")
        print(
            f"  {slot:<12} {mark:<4} {acct.get('mode', '?'):<9} {email[:28]:<28} "
            f"{_fmt('argon'):>7} {_fmt('gemini'):>7} {_fmt('claude_gpt'):>10}"
        )
    alert = compute_quota_alert(reg, model_name=model_disp)
    if alert:
        target_msg = (
            f" -> usa `agy-dash baton <conv> --to {alert['target_slot']}` ({alert['target_pct']:.0f}%)"
            if alert["target_slot"]
            else " -> agrega 2ª cuenta con `agy-dash account add pro2`"
        )
        print(f"\n  🚨 ALERTA [{alert['level']}]: {alert['active_slot']} {alert['bucket_name']}={alert['pct']:.0f}%{target_msg}")


def cmd_accounts(args: argparse.Namespace) -> int:
    action = getattr(args, "account_cmd", None) or "list"
    slot_arg = getattr(args, "slot", "")
    reg = load_accounts_registry()

    if action == "list":
        _print_accounts_table(reg)
        return 0
    if action == "use":
        if not slot_arg:
            print("Uso: agy-dash account use <slot>", file=sys.stderr)
            return 1
        safe = validate_slot_name(slot_arg)
        if safe not in reg.get("accounts", {}):
            print(f"Cuenta '{safe}' no registrada.", file=sys.stderr)
            return 1
        reg["active_slot"] = safe
        save_accounts_registry(reg)
        print(f"Cuenta por defecto cambiada a [{safe}].")
        return 0
    if action == "add":
        if not slot_arg:
            idx = 2
            while f"pro{idx}" in reg.get("accounts", {}):
                idx += 1
            slot_arg = f"pro{idx}"
        safe = validate_slot_name(slot_arg)
        if getattr(args, "tab", False) and shutil.which("tmux"):
            has = tmux_cmd("has-session", "-t", TMUX_SESSION, capture=True)
            if has.returncode == 0:
                print(launch_account_login_tab(safe))
                return 0
        return cmd_account_login(argparse.Namespace(slot=safe))
    if action == "remove":
        if not slot_arg:
            print("Uso: agy-dash account remove <slot>", file=sys.stderr)
            return 1
        safe = validate_slot_name(slot_arg)
        if safe == "primary":
            print("No se elimina `primary` (usa el keyring del sistema).", file=sys.stderr)
            return 1
        reg.get("accounts", {}).pop(safe, None)
        if reg.get("active_slot") == safe:
            reg["active_slot"] = "primary"
        save_accounts_registry(reg)
        print(f"Slot [{safe}] quitado del registro (directorios conservados en {ACCOUNTS_DIR / safe}).")
        return 0
    print(f"Subcomando desconocido: {action}", file=sys.stderr)
    return 1


def cmd_account_login(args: argparse.Namespace) -> int:
    safe_slot = validate_slot_name(args.slot)
    if safe_slot == "primary":
        print("`primary` usa el keyring del sistema; ejecutando check de cuota...")
        _, msg = refresh_account_quota("primary")
        print(msg)
        return 0

    ensure_isolated_home(safe_slot)
    reg = load_accounts_registry()
    reg["accounts"].setdefault(
        safe_slot,
        {
            "slot": safe_slot,
            "mode": "isolated",
            "label": f"Pro Aislada ({safe_slot})",
            "email": "",
            "authenticated": False,
            "quota": {},
            "updated_at": "",
        },
    )
    save_accounts_registry(reg)
    env = build_agy_env(safe_slot)

    print("╔════════════════════════════════════════════════════════════════════════════╗")
    print(f"║  🔑 LOGIN AISLADO PARA CUENTA [{safe_slot:<12}] (SIN CERRAR TU PRINCIPAL)       ║")
    print("╚════════════════════════════════════════════════════════════════════════════╝")
    print("  1. Completa el inicio de sesión de Google en el navegador que se abrirá.")
    print("  2. Cuando veas el prompt de AGY listo, escribe `/exit` (o Ctrl+D) para guardar y volver.\n")
    subprocess.run([AGY_BIN], env=env)
    ok, msg = refresh_account_quota(safe_slot)
    print(f"\n  Resultado: {msg}")
    if sys.stdin.isatty():
        _select_dash_window()
    return 0 if ok else 1


def cmd_quota(args: argparse.Namespace) -> int:
    reg = load_accounts_registry()
    if getattr(args, "refresh", False):
        for slot in list(reg.get("accounts", {}).keys()):
            ok, msg = refresh_account_quota(slot)
            print(msg)
        reg = load_accounts_registry()
    _print_accounts_table(reg)
    return 0


def cmd_baton(args: argparse.Namespace) -> int:
    target = args.target
    _, _, sessions = load_sessions(limit=50)
    matches = [
        s
        for s in sessions
        if s.conversation_id == target
        or s.conversation_id.startswith(target)
        or target.lower() in s.title.lower()
    ]
    if not matches:
        print(f"No se encontró sesión coincidente con '{target}'.", file=sys.stderr)
        return 1
    sel = matches[0]
    ok, msg = baton_pass_session(
        sel,
        target_slot=getattr(args, "to", "") or "",
        model=getattr(args, "model", "") or "",
        switch_to=not getattr(args, "no_switch", False),
    )
    print(msg)
    return 0 if ok else 1


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
        CommandButton("baton", "B", "[B] 🔀 Baton Pass", 4, 2, half),
        CommandButton("accounts", "u", "[u] 💳 Cuentas", 4, 2 + half + 1, half),
        CommandButton("memory", "m", "[m] 🧠 Memoria Shared", 5, 2, half * 2 + 1),
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


def _prompt_curses_text(stdscr, prompt: str, default_val: str = "") -> str:
    """Prompt cleanly for short single-line input in the bottom banner."""
    max_y, max_x = stdscr.getmaxyx()
    stdscr.timeout(-1)
    curses.echo()
    try:
        curses.curs_set(1)
    except curses.error:
        pass
    msg = f" {prompt} [{default_val}]: " if default_val else f" {prompt}: "
    stdscr.move(max_y - 2, 0)
    stdscr.clrtoeol()
    stdscr.addstr(max_y - 2, 0, msg[: max_x - 2], curses.color_pair(4) | curses.A_BOLD)
    stdscr.refresh()
    try:
        raw = stdscr.getstr(max_y - 2, min(len(msg), max_x - 2), 24)
        val = raw.decode("utf-8", errors="ignore").strip()
    except Exception:
        val = ""
    finally:
        curses.noecho()
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        stdscr.timeout(500)
    return val or default_val


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
    saved_session_cursor = 0
    mode = "sessions"
    status_banner = "Listo — [r] Actualizar | [Enter] Abrir/Espejo | [B] Baton Pass | [u] Cuentas | [h/H] Hibernar | [m] Memoria"
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

    led_tick = 0
    last_led_ts = 0.0
    last_tab_names: dict[str, str] = {}
    activity_cache: dict[str, tuple[tuple[int, int], str]] = {}
    led_lock = threading.Lock()
    led_worker_busy = {"running": False}

    def trigger_led_sync(cur_sessions: list[AgySession], tick_n: int) -> None:
        if not any(s.dash_win_idx is not None for s in cur_sessions):
            return
        with led_lock:
            if led_worker_busy["running"]:
                return
            led_worker_busy["running"] = True
        snap = list(cur_sessions)

        def _led_work() -> None:
            try:
                sync_led_tmux_tabs(snap, tick_n, last_tab_names, activity_cache)
            finally:
                with led_lock:
                    led_worker_busy["running"] = False

        threading.Thread(target=_led_work, daemon=True).start()

    quota_lock = threading.Lock()
    quota_worker_state = {"running": False, "last_poll": 0.0, "msg": ""}

    def trigger_quota_poll(force: bool = False) -> None:
        with quota_lock:
            if quota_worker_state["running"]:
                return
            if not force and (time.time() - quota_worker_state["last_poll"]) < QUOTA_POLL_INTERVAL_SEC:
                return
            quota_worker_state["running"] = True

        def _worker() -> None:
            try:
                reg = load_accounts_registry()
                msgs = []
                for sname in list(reg.get("accounts", {}).keys()):
                    _, m = refresh_account_quota(sname, timeout_sec=14.0)
                    msgs.append(m)
                with quota_lock:
                    quota_worker_state["last_poll"] = time.time()
                    quota_worker_state["msg"] = " · ".join(msgs)
            finally:
                with quota_lock:
                    quota_worker_state["running"] = False

        threading.Thread(target=_worker, daemon=True).start()

    def db_wal_sig() -> tuple[int, int]:
        try:
            s1 = SUMMARIES_DB.stat().st_mtime_ns if SUMMARIES_DB.is_file() else 0
            wal = Path(str(SUMMARIES_DB) + "-wal")
            s2 = wal.stat().st_mtime_ns if wal.is_file() else 0
            return (s1, s2)
        except OSError:
            return (0, 0)

    def execute_action(action: str) -> str:
        nonlocal mode, cursor, saved_session_cursor, force_scan, cached_preview_sig
        if action == "refresh":
            force_scan = True
            cached_preview_sig = (-1, -1)
            tmux_cmd("refresh-client", "-S")
            return f"Actualizado en vivo a las {time.strftime('%H:%M:%S')} (Zig scan + DB + Vista)."
        if action == "refresh_quota":
            trigger_quota_poll(force=True)
            return "Consultando cuotas (/usage) de todas las cuentas en segundo plano..."
        if action == "memory":
            if mode == "sessions":
                saved_session_cursor = cursor
                mode = "memory"
                cursor = 0
                return "Vista: Memoria Compartida Multi-Harness (isymem)"
            mode = "sessions"
            cursor = saved_session_cursor
            return "Vista: Command Center AGY"
        if action == "accounts":
            if mode == "sessions":
                saved_session_cursor = cursor
                mode = "accounts"
                cursor = 0
                trigger_quota_poll(force=False)
                return "Vista: Cuentas Google Pro, Cuotas (/usage) y Baton Pass"
            mode = "sessions"
            cursor = saved_session_cursor
            return "Vista: Command Center AGY"

        sel = sessions[saved_session_cursor if mode != "sessions" else cursor] if sessions else None
        if action == "baton" and sel:
            ok, msg = baton_pass_session(sel)
            if not ok and "No hay otra cuenta" in msg:
                saved_session_cursor = cursor
                mode = "accounts"
                cursor = 0
            force_scan = True
            return msg

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

    # Initial non-blocking quota check when cached quota is absent/stale
    reg_init = load_accounts_registry()
    if any(not a.get("quota", {}).get("ok") for a in reg_init.get("accounts", {}).values()):
        trigger_quota_poll(force=True)

    while True:
        now = time.time()
        trigger_quota_poll(force=False)
        cur_db_sig = db_wal_sig()
        if force_scan or cur_db_sig != last_db_sig or (now - last_scan_ts) >= 1.2:
            last_db_sig = cur_db_sig
            last_scan_ts = now
            force_scan = False
            scan_us, ksm, sessions = load_sessions(limit=35)
            last_refresh_clock = time.strftime("%H:%M:%S")
        if (now - last_led_ts) >= 0.45:
            last_led_ts = now
            led_tick += 1
            trigger_led_sync(sessions, led_tick)

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

        reg = load_accounts_registry()
        active_slot = reg.get("active_slot", "primary")
        conv_accounts = reg.get("conv_accounts", {})
        model_disp, _ = resolve_model_arg()
        sel_for_alert = sessions[cursor] if (mode == "sessions" and sessions and 0 <= cursor < len(sessions)) else (
            sessions[saved_session_cursor] if (sessions and 0 <= saved_session_cursor < len(sessions)) else None
        )
        sel_slot = conv_accounts.get(sel_for_alert.conversation_id, active_slot) if sel_for_alert else active_slot
        quota_alert = compute_quota_alert(reg, active_slot=sel_slot, model_name=model_disp)

        live = [s for s in sessions if s.proc is not None]
        hib = [s for s in sessions if s.hibernated]
        all_procs = [p for s in live for p in ([s.proc] if s.proc else []) + s.extra_procs]
        total_pss_mb = sum(p.pss_kb for p in all_procs) // 1024
        total_shr_mb = max((p.shared_clean_kb for p in all_procs), default=0) // 1024
        total_ksm_mb = sum(p.ksm_kb for p in all_procs) // 1024
        hib_saved_mb = sum(s.hibernated_saved_kb for s in hib) // 1024

        hdr = (
            f" ⚡ AGY COMMAND CENTER (Zig {scan_us / 1000.0:.1f}ms + Python · Sync {last_refresh_clock}) — "
            f"{len(live)} activas · {len(hib)} hibernadas (0MB) · Cuenta [{active_slot}] "
        )
        stdscr.addstr(0, 0, hdr[: max_x - 1].ljust(max_x - 1), curses.color_pair(1) | curses.A_BOLD)

        ram_line = (
            f" RAM: {total_pss_mb} MB PSS | Shared: {total_shr_mb} MB + KSM: {total_ksm_mb} MB "
            f"({ksm.get('pages_sharing', 0)} págs) | Ahorrado (💤): {hib_saved_mb} MB | Modelo: {model_disp or 'default'}"
        )
        stdscr.addstr(1, 0, ram_line[: max_x - 1], curses.color_pair(2) | curses.A_BOLD)

        with quota_lock:
            polling_now = quota_worker_state["running"]

        if quota_alert:
            if quota_alert["target_slot"]:
                tpct = f" ({quota_alert['target_pct']:.0f}%)" if quota_alert["target_pct"] >= 0 else ""
                act_hint = f"Pulsa [B] Baton Pass -> [{quota_alert['target_slot']}]{tpct} (mismo chat + modelo)"
            else:
                act_hint = "Pulsa [u] Cuentas -> [+] para añadir 2ª Cuenta Pro"
            alert_txt = (
                f" 🚨 CUOTA BAJA [{quota_alert['active_slot']} {quota_alert['active_email']}]: "
                f"{quota_alert['bucket_name']} al {quota_alert['pct']:.0f}% — {act_hint} "
            )
            alert_attr = (curses.color_pair(1) | curses.A_BOLD) if quota_alert["level"] == "CRITICAL" else (curses.color_pair(4) | curses.A_REVERSE | curses.A_BOLD)
        else:
            acct_obj = reg.get("accounts", {}).get(sel_slot, {})
            fams = acct_obj.get("quota", {}).get("families", {})
            q_parts = [f"{k}:{v:.0f}%" for k, v in fams.items()]
            q_summary = " · ".join(q_parts) if q_parts else ("consultando /usage..." if polling_now else "pulsa [u] para cuotas")
            mail = acct_obj.get("email", "")
            mail_s = f" ({mail})" if mail else ""
            alert_txt = f" 💳 Cuenta [{sel_slot}]{mail_s}: {q_summary}  |  [B] Baton Pass  [u] Cuentas Pro ({len(reg.get('accounts', {}))}) "
            alert_attr = curses.color_pair(3) | curses.A_BOLD
        stdscr.addstr(2, 0, alert_txt[: max_x - 1].ljust(max_x - 1), alert_attr)
        stdscr.addstr(3, 0, "─" * (max_x - 1))

        # Layout split: Left Session List + Right Command Center Panel (if width >= 116)
        show_cmd_panel = (mode == "sessions" and max_x >= 116)
        left_w = min(92, max_x - 46) if show_cmd_panel else (max_x - 1)
        panel_x = left_w + 1
        panel_w = max_x - panel_x - 1
        panel_btn_y = 6
        cmd_buttons = build_command_buttons(panel_w) if show_cmd_panel else []
        list_start_y = 6
        list_avail = 0

        if mode == "sessions":
            if sessions:
                cursor = max(0, min(cursor, len(sessions) - 1))

            preview_height = max(7, min(12, max_y // 3)) if max_y >= 22 else 0
            list_avail = max(3, max_y - 9 - preview_height)

            col_hdr = f" {'ESTADO':<10} {'UBICACIÓN':<10} {'PSS':>6} {'SHR+KSM':>7} {'PASOS':>5}  {'TÍTULO DE SESIÓN':<28} {'WS'}"
            stdscr.addstr(4, 0, col_hdr[:left_w], curses.A_BOLD)
            stdscr.addstr(5, 0, "─" * left_w)

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
                for ry in range(4, 6 + list_avail):
                    if ry < max_y - 3:
                        stdscr.addstr(ry, left_w, "│")
                stdscr.addstr(4, panel_x, " 🎛️  CENTRO DE COMANDO (Clic o Tecla)"[:panel_w], curses.color_pair(2) | curses.A_BOLD)
                stdscr.addstr(5, panel_x, "─" * panel_w)
                for b in cmd_buttons:
                    by = panel_btn_y + b.row_offset
                    bx = panel_x + b.col_offset
                    if by < 6 + list_avail and bx + b.width < max_x:
                        btn_txt = f" {b.label} ".ljust(b.width)[: b.width]
                        btn_attr = curses.color_pair(1 if (b.action_id == "baton" and quota_alert) else 7) | curses.A_BOLD
                        stdscr.addstr(by, bx, btn_txt, btn_attr)

                insp_y = panel_btn_y + 7
                if insp_y < 6 + list_avail and sessions and 0 <= cursor < len(sessions):
                    sel = sessions[cursor]
                    stdscr.addstr(insp_y - 1, panel_x, "─" * panel_w)
                    t_sig = transcript_sig(sel.conversation_id)
                    age_s = int(max(0, now - (t_sig[0] / 1e9))) if t_sig[0] else -1
                    age_txt = f"hace {age_s}s" if age_s >= 0 else "sin log"
                    kids_txt = ", ".join(sel.proc.active_children) if (sel.proc and sel.proc.active_children) else "ninguno"
                    pid_txt = str(sel.proc.pid) if sel.proc else "-"
                    extra_txt = f" (+{len(sel.extra_procs)} dup)" if sel.extra_procs else ""
                    acct_tag = conv_accounts.get(sel.conversation_id, active_slot)
                    meta_lines = [
                        (f" 📌 {sel.title[:panel_w - 4]}", curses.A_BOLD),
                        (f" ID: {sel.conversation_id[:14]} PID:{pid_txt}{extra_txt} Cta:[{acct_tag}]", curses.A_NORMAL),
                        (f" Log: {t_sig[1] // 1024} KB ({age_txt}) | Hijos: {kids_txt[:panel_w - 28]}", curses.color_pair(4) if kids_txt != "ninguno" else curses.A_NORMAL),
                        (f" RAM: {_ram_bar(min(1.0, total_pss_mb / 2048.0), 18)} {total_pss_mb}M (KSM {total_ksm_mb}M)", curses.color_pair(3)),
                    ]
                    for mi, (ml, ma) in enumerate(meta_lines):
                        if insp_y + mi < 6 + list_avail:
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
        elif mode == "accounts":
            acct_list = list(reg.get("accounts", {}).items())
            sel_s_title = sel_for_alert.title[:28] if sel_for_alert else "-"
            ahdr = (
                f" 💳 CUENTAS GOOGLE PRO & BATON PASS (Sesión objetivo: {sel_s_title} · Modelo: {model_disp or 'default'})"
            )
            stdscr.addstr(4, 0, ahdr[: max_x - 1], curses.color_pair(2) | curses.A_BOLD)
            stdscr.addstr(5, 0, f" {'SLOT':<12} {'ACT':<4} {'MODO':<9} {'EMAIL':<28} {'ARGON':>7} {'GEMINI':>7} {'CLAUDE/GPT':>10} {'ESTADO'}", curses.A_BOLD)
            cursor = max(0, min(cursor, max(0, len(acct_list) - 1)))
            for idx, (sname, adata) in enumerate(acct_list):
                ry = 6 + idx
                if ry >= max_y - 5:
                    break
                mark = "★" if sname == active_slot else " "
                q = adata.get("quota", {})
                fams = q.get("families", {}) if q.get("ok") else {}
                def _fp(k: str) -> str:
                    return f"{fams[k]:.0f}%" if k in fams else "-"
                mail = adata.get("email", "") or ("(sin login OAuth)" if not adata.get("authenticated") else "(pendiente)")
                st_txt = "LISTA" if adata.get("authenticated") else "FALTA LOGIN [+]"
                row_txt = (
                    f" {sname:<12} {mark:<4} {adata.get('mode', '?'):<9} {mail[:28]:<28} "
                    f"{_fp('argon'):>7} {_fp('gemini'):>7} {_fp('claude_gpt'):>10}  {st_txt}"
                )
                attr = (curses.color_pair(6) | curses.A_BOLD) if idx == cursor else (
                    curses.color_pair(3) if adata.get("authenticated") else curses.color_pair(4)
                )
                stdscr.addstr(ry, 0, row_txt[: max_x - 1].ljust(max_x - 1), attr)

            if acct_list and 0 <= cursor < len(acct_list) and max_y >= 15:
                sname, adata = acct_list[cursor]
                buckets = adata.get("quota", {}).get("buckets", [])
                det_y = min(max_y - 6, 7 + len(acct_list))
                if det_y < max_y - 4:
                    stdscr.addstr(det_y, 0, f" 📊 Detalle [{sname}] (actualizado: {adata.get('updated_at') or 'nunca'}):"[: max_x - 1], curses.A_BOLD)
                    for bi, b in enumerate(buckets[:4]):
                        by = det_y + 1 + bi
                        if by >= max_y - 3:
                            break
                        b_line = f"    • {b.get('group', '')} — {b.get('name', '')}: {b.get('pct', 0):.0f}% libre  (reset {b.get('reset_time', '')})"
                        b_attr = curses.color_pair(1) if b.get("pct", 100) <= QUOTA_CRIT_PCT else (
                            curses.color_pair(4) if b.get("pct", 100) <= QUOTA_WARN_PCT else curses.color_pair(3)
                        )
                        stdscr.addstr(by, 0, b_line[: max_x - 1], b_attr)
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
                4,
                0,
                f" 🧠 MEMORIA COMPARTIDA MULTI-HARNESS ({len(mem_lines)} entradas en MEMORY.md — m:Volver)"[
                    : max_x - 1
                ],
                curses.color_pair(3) | curses.A_BOLD,
            )
            stdscr.addstr(5, 0, "─" * (max_x - 1))
            cursor = max(0, min(cursor, max(0, len(mem_lines) - 1)))
            avail_rows = max(1, max_y - 9)
            start_idx = max(0, cursor - avail_rows + 1) if cursor >= avail_rows else 0
            for idx, mline in enumerate(mem_lines[start_idx : start_idx + avail_rows], start=start_idx):
                row_y = 6 + (idx - start_idx)
                if row_y >= max_y - 3:
                    break
                attr = (curses.color_pair(6) | curses.A_BOLD) if idx == cursor else curses.A_NORMAL
                stdscr.addstr(row_y, 0, f" {mline}"[: max_x - 1].ljust(max_x - 1), attr)

        if max_y >= 3:
            stdscr.addstr(max_y - 3, 0, "─" * (max_x - 1))
            stdscr.addstr(max_y - 2, 0, f" {status_banner}"[: max_x - 1], curses.color_pair(4))
            if mode == "accounts":
                keys_help = (
                    " [+] Agregar 2ª Cuenta Pro   [B] Baton Pass a esta cuenta   [Enter/s] Activar default   "
                    "[R/r] Refrescar Cuotas   [u/Esc] Volver "
                )
            else:
                keys_help = (
                    " [r] Actualizar  [Enter] Abrir  [B] Baton Pass  [u] Cuentas  "
                    "[v] Espejo  [n] Nueva  [h/H] Hibernar  [m] Memoria  [Tab] Pestaña "
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
            elif mode == "accounts":
                acct_list = list(reg.get("accounts", {}).items())
                if 6 <= my < 6 + len(acct_list):
                    cursor = my - 6
            continue

        if mode == "accounts":
            acct_list = list(reg.get("accounts", {}).items())
            if ch in (ord("u"), ord("m"), 27):
                mode = "sessions"
                cursor = saved_session_cursor
                status_banner = "Vista: Command Center AGY"
            elif ch in (curses.KEY_UP, ord("k")):
                cursor = max(0, cursor - 1)
            elif ch in (curses.KEY_DOWN, ord("j")):
                cursor += 1
            elif ch in (ord("r"), ord("R")):
                status_banner = execute_action("refresh_quota")
            elif ch == ord("+"):
                idx = 2
                existing_accts = reg.get("accounts", {})
                while f"pro{idx}" in existing_accts:
                    idx += 1
                default_name = f"pro{idx}"
                chosen = _prompt_curses_text(stdscr, "Nombre de nueva cuenta Pro aislada", default_name)
                try:
                    status_banner = launch_account_login_tab(chosen)
                except ValueError as ve:
                    status_banner = f"Nombre inválido: {ve}"
            elif ch in (10, 13, curses.KEY_ENTER, ord("s")) and acct_list and 0 <= cursor < len(acct_list):
                chosen_slot = acct_list[cursor][0]
                reg["active_slot"] = chosen_slot
                save_accounts_registry(reg)
                status_banner = f"Cuenta por defecto cambiada a [{chosen_slot}]."
            elif ch in (ord("b"), ord("B")) and acct_list and 0 <= cursor < len(acct_list):
                chosen_slot = acct_list[cursor][0]
                target_sess = sessions[saved_session_cursor] if (sessions and 0 <= saved_session_cursor < len(sessions)) else None
                if not target_sess:
                    status_banner = "Selecciona una sesión primero antes de hacer Baton Pass."
                else:
                    ok, msg = baton_pass_session(target_sess, target_slot=chosen_slot)
                    status_banner = msg
                    if ok:
                        mode = "sessions"
                        cursor = saved_session_cursor
            elif ch in (ord("q"), 9):
                tmux_cmd("next-window")
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
        elif ch == ord("u"):
            status_banner = execute_action("accounts")
        elif ch == ord("U"):
            status_banner = execute_action("refresh_quota")
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
        elif mode == "sessions" and ch in (ord("b"), ord("B")):
            status_banner = execute_action("baton")


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
    p_new.add_argument("--account", default="")
    p_new.set_defaults(fn=cmd_open_new_tab)

    for cname in ("accounts", "account"):
        p_ac = sub.add_parser(cname, help="Manage multi-account Google Pro slots (list|add|use|remove)")
        p_ac.add_argument("account_cmd", nargs="?", default="list", choices=["list", "add", "use", "remove"])
        p_ac.add_argument("slot", nargs="?", default="")
        p_ac.add_argument("--tab", action="store_true", help="Open interactive login in a new dashboard tmux tab")
        p_ac.set_defaults(fn=cmd_accounts)

    p_al = sub.add_parser("account-login", help="Interactive isolated OAuth login for an extra account slot")
    p_al.add_argument("slot")
    p_al.set_defaults(fn=cmd_account_login)

    p_qt = sub.add_parser("quota", help="Show / refresh remaining quota across all configured accounts")
    p_qt.add_argument("--refresh", action="store_true", help="Poll live `/usage` for all accounts")
    p_qt.set_defaults(fn=cmd_quota)

    p_bt = sub.add_parser("baton", help="Pass the baton of a conversation to another account keeping same model")
    p_bt.add_argument("target", help="Conversation ID prefix or title substring")
    p_bt.add_argument("--to", default="", help="Target account slot (defaults to highest-quota alternate)")
    p_bt.add_argument("--model", default="", help="Explicit model display name or slug")
    p_bt.add_argument("--no-switch", action="store_true", help="Do not switch tmux focus to the baton tab")
    p_bt.set_defaults(fn=cmd_baton)

    return ap


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] == "run":
        conv = ""
        cwd = ""
        title = ""
        account = ""
        model = ""
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
            elif a == "--account" and i + 1 < len(argv):
                account = argv[i + 1]
                i += 2
            elif a == "--model" and i + 1 < len(argv):
                model = argv[i + 1]
                i += 2
            else:
                rest.extend(argv[i:])
                break
        extra = (["--conversation", conv] if conv else []) + rest
        return exec_agy_optimized(
            extra,
            cwd=cwd or None,
            preview_conv=conv,
            preview_title=title,
            account_slot=account or None,
            model=model,
        )

    ap = build_parser()
    args = ap.parse_args()
    if not getattr(args, "cmd", None):
        return cmd_dashboard(args)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
