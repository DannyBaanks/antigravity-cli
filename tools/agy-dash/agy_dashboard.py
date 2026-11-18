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
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

def _resolve_real_home() -> Path:
    raw = os.environ.get("AGY_REAL_HOME") or str(Path.home())
    marker = "/.gemini/antigravity-cli/accounts/"
    if marker in raw:
        raw = raw.split(marker, 1)[0]
    return Path(raw)


HOME = _resolve_real_home()
SCRIPT_DIR = Path(__file__).resolve().parent
ZIG_CORE_DIR = SCRIPT_DIR / "zig_core"
ZIG_CORE_BIN = SCRIPT_DIR / "agy-fast-core"

AGY_DATA_DIR = HOME / ".gemini" / "antigravity-cli"
SUMMARIES_DB = AGY_DATA_DIR / "conversation_summaries.db"
BRAIN_DIR = AGY_DATA_DIR / "brain"
PRESENCE_DIR = AGY_DATA_DIR / "presence"
HIBERNATED_FILE = AGY_DATA_DIR / "dashboard_hibernated.json"
CUSTOM_TITLES_FILE = AGY_DATA_DIR / "custom_titles.json"
NVAPI_CONFIG_FILE = AGY_DATA_DIR / "nvapi_config.json"
COMPACTED_DIR = AGY_DATA_DIR / "compacted"
HEARTBEATS_DIR = AGY_DATA_DIR / "heartbeats"
ACCOUNTS_DIR = AGY_DATA_DIR / "accounts"
ACCOUNTS_REGISTRY = ACCOUNTS_DIR / "registry.json"
TMUX_CONF = AGY_DATA_DIR / "agy-dash.tmux.conf"
TMUX_SOCKET = "agy-dash"
TMUX_SESSION = "agy"
TMUX_BG_SESSION = "agy-bg"

AGY_BIN = os.environ.get("AGY_BIN", str(HOME / ".local" / "bin" / "agy"))
DEFAULT_GOGC = os.environ.get("AGY_GOGC", "35")
DEFAULT_GOMEMLIMIT = os.environ.get("AGY_GOMEMLIMIT", "180MiB")
DEFAULT_IDLE_HIBERNATE_SEC = int(os.environ.get("AGY_IDLE_HIBERNATE_SEC", "0"))
QUOTA_WARN_PCT = float(os.environ.get("AGY_QUOTA_WARN_PCT", "15"))
QUOTA_CRIT_PCT = float(os.environ.get("AGY_QUOTA_CRIT_PCT", "5"))
QUOTA_POLL_INTERVAL_SEC = float(os.environ.get("AGY_QUOTA_POLL_SEC", "120"))
DEFAULT_NVAPI_BASE_URL = os.environ.get("AGY_NVAPI_BASE_URL", "https://integrate.api.nvidia.com/v1")
DEFAULT_NVAPI_MODEL = os.environ.get("AGY_NVAPI_MODEL", "nvidia/nemotron-3-ultra-550b-a55b")
HEARTBEAT_INTERVAL_SEC = float(os.environ.get("AGY_HB_INTERVAL_SEC", "300"))
HEARTBEAT_WARN_TOKENS = int(os.environ.get("AGY_HB_WARN_TOKENS", "120000"))
HEARTBEAT_RECOMMEND_TOKENS = int(os.environ.get("AGY_HB_RECOMMEND_TOKENS", "220000"))
HEARTBEAT_URGENT_TOKENS = int(os.environ.get("AGY_HB_URGENT_TOKENS", "350000"))
COMPACT_MIN_STEPS = int(os.environ.get("AGY_COMPACT_MIN_STEPS", "40"))
COMPACT_MIN_BYTES = int(os.environ.get("AGY_COMPACT_MIN_BYTES", "49152"))

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
    bg_win_idx: str | None = None
    transcript_busy: bool = False

    @property
    def state_label(self) -> str:
        if self.proc is not None:
            if self.db_status == "CASCADE_RUN_STATUS_RUNNING" or self.transcript_busy:
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
        if self.bg_win_idx is not None and self.proc is not None:
            return f"bg:{self.bg_win_idx}"
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
        if self.db_status == "CASCADE_RUN_STATUS_RUNNING" or self.transcript_busy:
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
            or self.transcript_busy
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
    primary_p = BRAIN_DIR / conv_id / ".system_generated" / "logs" / "transcript.jsonl"
    if primary_p.is_file():
        return primary_p
    if ACCOUNTS_DIR.is_dir():
        for alt in ACCOUNTS_DIR.glob(f"*/home/.gemini/antigravity-cli/brain/{conv_id}/.system_generated/logs/transcript.jsonl"):
            if alt.is_file():
                return alt
    return primary_p


_TRANSCRIPT_BUSY_CACHE: dict[str, tuple[tuple[int, int], float, bool]] = {}


def is_transcript_turn_active(conv_id: str, max_recent_sec: float = 45.0, *, now: float | None = None) -> bool:
    """Return True if transcript.jsonl was written recently or its last step is an unfinished turn."""
    tpath = transcript_path_for(conv_id)
    try:
        st = tpath.stat()
    except OSError:
        return False
    cur_now = now if now is not None else time.time()
    age_s = max(0.0, cur_now - (st.st_mtime_ns / 1e9))
    if age_s <= max_recent_sec:
        return True

    sig = (st.st_mtime_ns, st.st_size)
    cached = _TRANSCRIPT_BUSY_CACHE.get(conv_id)
    if cached and cached[0] == sig and (cur_now - cached[1]) < 15.0:
        return cached[2]

    busy = False
    try:
        with tpath.open("rb") as f:
            f.seek(max(0, st.st_size - 32768), os.SEEK_SET)
            lines = [l for l in f.read().decode("utf-8", errors="replace").splitlines() if l.strip().startswith("{")]
        for raw in reversed(lines[-6:]):
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            stype = obj.get("type", "")
            status = obj.get("status", "DONE")
            if status != "DONE":
                busy = True
                break
            if stype in ("USER_INPUT", "GENERIC"):
                # Only treat recent (<600s) unfinished tool/user turns as active
                busy = age_s <= 600.0
                break
            if stype == "PLANNER_RESPONSE":
                tcalls = obj.get("tool_calls") or []
                busy = bool(tcalls) and age_s <= 600.0
                break
    except OSError:
        busy = False

    _TRANSCRIPT_BUSY_CACHE[conv_id] = (sig, cur_now, busy)
    return busy


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


def _get_window_map_for_session(tmux_session: str) -> dict[str, tuple[str, str]]:
    """Return mapping conv_id -> (window_index, window_name) inside a tmux session."""
    out: dict[str, tuple[str, str]] = {}
    cp = tmux_cmd(
        "list-windows",
        "-t",
        tmux_session,
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


def _get_dash_tty_map() -> dict[str, tuple[str, str]]:
    """Return mapping tty ('pts/10') -> (window_index, window_name) for untagged tabs."""
    out: dict[str, tuple[str, str]] = {}
    cp = tmux_cmd(
        "list-windows",
        "-t",
        TMUX_SESSION,
        "-F",
        "#{window_index}|#{window_name}|#{pane_tty}",
        capture=True,
    )
    if cp.returncode == 0 and cp.stdout:
        for line in cp.stdout.splitlines():
            parts = line.split("|", 2)
            if len(parts) == 3 and parts[2].strip():
                tty_short = parts[2].strip().removeprefix("/dev/")
                out[tty_short] = (parts[0].strip(), parts[1].strip())
    return out


def _get_dash_window_map() -> dict[str, tuple[str, str]]:
    """Return mapping conv_id -> (window_index, window_name) inside tmux -L agy-dash."""
    return _get_window_map_for_session(TMUX_SESSION)


def _get_bg_window_map() -> dict[str, tuple[str, str]]:
    """Return mapping conv_id -> (window_index, window_name) for parked background tabs."""
    return _get_window_map_for_session(TMUX_BG_SESSION)


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


def load_custom_titles(titles_path: Path | None = None) -> dict[str, str]:
    p = titles_path or CUSTOM_TITLES_FILE
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in data.items() if str(v).strip()} if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_custom_title(conv_id: str, new_title: str, *, titles_path: Path | None = None) -> str:
    p = titles_path or CUSTOM_TITLES_FILE
    clean = re.sub(r"\s+", " ", new_title.strip())[:60]
    if not clean:
        return ""
    titles = load_custom_titles(titles_path=p)
    titles[conv_id] = clean
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(titles, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)

    if titles_path is None:
        for db_p in _all_summaries_dbs():
            try:
                conn = sqlite3.connect(str(db_p), timeout=1.5)
                conn.execute("UPDATE conversation_summaries SET title = ? WHERE conversation_id = ?", (clean, conv_id))
                conn.commit()
                conn.close()
            except Exception:
                pass
    return clean


def _all_summaries_dbs() -> list[Path]:
    dbs: list[Path] = []
    if SUMMARIES_DB.is_file():
        dbs.append(SUMMARIES_DB)
    if ACCOUNTS_DIR.is_dir():
        for p in ACCOUNTS_DIR.glob("*/home/.gemini/antigravity-cli/conversation_summaries.db"):
            if p.is_file() and not p.is_symlink():
                dbs.append(p)
    return dbs


def load_sessions(limit: int = 40) -> tuple[int, dict[str, int], list[AgySession]]:
    scan_us, ksm_stats, live_by_conv = zig_scan()
    win_map = _get_dash_window_map()
    tty_map = _get_dash_tty_map()
    bg_map = _get_bg_window_map()
    hib_reg = load_hibernated_registry()
    custom_titles = load_custom_titles()

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

    merged_db_rows: dict[str, tuple] = {}
    for db_path in _all_summaries_dbs():
        try:
            uri = f"file:{db_path}?mode=ro"
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
            for row in cur.fetchall():
                cid = row[0]
                prev_row = merged_db_rows.get(cid)
                if prev_row is None:
                    merged_db_rows[cid] = row
                else:
                    # Prefer row with higher step_count or non-empty title
                    prev_steps = int(prev_row[3] or 0)
                    new_steps = int(row[3] or 0)
                    if new_steps > prev_steps or (not prev_row[1] and row[1]):
                        best_title = row[1] or prev_row[1]
                        merged_db_rows[cid] = (
                            cid,
                            best_title,
                            row[2] or prev_row[2],
                            max(prev_steps, new_steps),
                            max(str(prev_row[4] or ""), str(row[4] or "")),
                            row[5] or prev_row[5],
                            row[6] or prev_row[6],
                            row[7] or prev_row[7],
                        )
            conn.close()
        except Exception:
            pass

    rows_by_id: dict[str, AgySession] = {}
    for cid, title, preview, steps, lastmod, ws_uris, status, not_idle in merged_db_rows.values():
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
        if win_idx is None and primary and primary.tty in tty_map:
            win_idx, win_name = tty_map[primary.tty]
            if "DASH" not in win_name:
                tmux_cmd("set-window-option", "-t", f"{TMUX_SESSION}:{win_idx}", "@agy_conv_id", cid)
        bg_idx, _ = bg_map.get(cid, (None, ""))
        t_busy = is_transcript_turn_active(cid) if primary is not None else False
        clean_title = custom_titles.get(cid) or (title or "").strip() or f"Sesión {cid[:8]}"
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
            bg_win_idx=bg_idx,
            transcript_busy=t_busy,
        )

    for cid, procs in live_by_conv.items():
        if cid not in rows_by_id and procs:
            win_idx, win_name = win_map.get(cid, (None, ""))
            if win_idx is None and procs[0].tty in tty_map:
                win_idx, win_name = tty_map[procs[0].tty]
                if "DASH" not in win_name:
                    tmux_cmd("set-window-option", "-t", f"{TMUX_SESSION}:{win_idx}", "@agy_conv_id", cid)
            bg_idx, _ = bg_map.get(cid, (None, ""))
            fallback_title = custom_titles.get(cid) or f"Sesión activa {cid[:8]}"
            rows_by_id[cid] = AgySession(
                conversation_id=cid,
                title=fallback_title,
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
                bg_win_idx=bg_idx,
                transcript_busy=is_transcript_turn_active(cid),
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
    slot_bin = slot_dir / "bin"

    for d in (acct_root, slot_dir, slot_home, slot_cfg, slot_cfg_agy, slot_gemini, slot_agy_data, slot_bin):
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass

    xdg_shim = slot_bin / "xdg-open"
    shim_code = f"""#!/usr/bin/env bash
set -u
if [ "${{AGY_NO_BROWSER:-0}}" = "1" ]; then
  exit 0
fi
url="${{1:-}}"
if [ -n "$url" ]; then
  printf '%s' "$url" > "{slot_dir / 'last_oauth.url'}" 2>/dev/null || true
  chmod 600 "{slot_dir / 'last_oauth.url'}" 2>/dev/null || true
  if command -v wl-copy >/dev/null 2>&1; then
    printf '%s' "$url" | wl-copy 2>/dev/null || true
  elif command -v xclip >/dev/null 2>&1; then
    printf '%s' "$url" | xclip -in -selection clipboard 2>/dev/null || true
  elif command -v xsel >/dev/null 2>&1; then
    printf '%s' "$url" | xsel --clipboard --input 2>/dev/null || true
  fi
fi
if [ -n "${{AGY_REAL_DBUS:-}}" ]; then export DBUS_SESSION_BUS_ADDRESS="$AGY_REAL_DBUS"; else unset DBUS_SESSION_BUS_ADDRESS; fi
if [ -n "${{AGY_REAL_HOME:-}}" ]; then export HOME="$AGY_REAL_HOME"; fi
if [ -n "${{AGY_REAL_XDG_CONFIG:-}}" ]; then export XDG_CONFIG_HOME="$AGY_REAL_XDG_CONFIG"; else unset XDG_CONFIG_HOME; fi
PATH="$(printf '%s' "$PATH" | awk -v RS=: -v ORS=: -v rm="{slot_bin}" '$0 != rm' | sed 's/:$//')"
export PATH
exec xdg-open "$@"
"""
    xdg_shim.write_text(shim_code, encoding="utf-8")
    try:
        os.chmod(xdg_shim, 0o755)
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
        slot_bin = slot_home.parent / "bin"
        cur_dbus = os.environ.get("AGY_REAL_DBUS") or os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
        if not cur_dbus or "nonexistent" in cur_dbus:
            candidate_bus = Path(f"/run/user/{os.getuid()}/bus")
            cur_dbus = f"unix:path={candidate_bus}" if candidate_bus.exists() else ""
        env["AGY_REAL_DBUS"] = cur_dbus
        env["AGY_REAL_HOME"] = str(HOME)
        if "AGY_REAL_XDG_CONFIG" in os.environ:
            env["AGY_REAL_XDG_CONFIG"] = os.environ["AGY_REAL_XDG_CONFIG"]
        elif "XDG_CONFIG_HOME" in os.environ and "/accounts/" not in os.environ["XDG_CONFIG_HOME"]:
            env["AGY_REAL_XDG_CONFIG"] = os.environ["XDG_CONFIG_HOME"]
        else:
            env["AGY_REAL_XDG_CONFIG"] = str(HOME / ".config")
        env["PATH"] = f"{slot_bin}:{env.get('PATH', '')}"
        env["HOME"] = str(slot_home)
        env["XDG_CONFIG_HOME"] = str(slot_home / ".config")
        env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/nonexistent"
        env["AGY_ACCOUNT_SLOT"] = slot
    else:
        env["AGY_ACCOUNT_SLOT"] = slot
    return env


def refresh_account_quota(slot: str, timeout_sec: float = 15.0, *, allow_unauthenticated: bool = False) -> tuple[bool, str]:
    """Query `/usage --output-format json` for one account slot without ever opening a browser."""
    reg = load_accounts_registry()
    if slot not in reg["accounts"]:
        return False, f"Cuenta '{slot}' no existe en el registro."
    acct_info = reg["accounts"][slot]
    if acct_info.get("mode") == "isolated" and not acct_info.get("authenticated", False) and not allow_unauthenticated:
        return False, f"[{slot}] Pendiente de login OAuth (sin abrir navegador)."

    env = build_agy_env(slot)
    # Never trigger interactive browser tabs during background /usage checks
    env["AGY_NO_BROWSER"] = "1"
    env["BROWSER"] = "/bin/true"
    no_browser_bin = ensure_isolated_home(slot).parent / "bin" if acct_info.get("mode") == "isolated" else (ACCOUNTS_DIR / "_nobrowser_bin")
    if acct_info.get("mode") != "isolated":
        no_browser_bin.mkdir(parents=True, exist_ok=True)
        xo = no_browser_bin / "xdg-open"
        if not xo.exists():
            xo.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            os.chmod(xo, 0o755)
        env["PATH"] = f"{no_browser_bin}:{env.get('PATH', '')}"
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
    cur_email = str(cur.get("email", "")).strip().lower()
    best_target = ""
    best_target_email = ""
    best_target_pct = -1.0
    for sname, sdata in accounts.items():
        if sname == slot or not sdata.get("authenticated", False):
            continue
        other_email = str(sdata.get("email", "")).strip().lower()
        if cur_email and other_email and other_email == cur_email:
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
    """Pick the best authenticated account slot (preferring distinct Google identity) for Baton Pass."""
    fam = model_to_quota_family(model_name) if model_name else "gemini"
    accounts = reg.get("accounts", {})
    cur_email = str(accounts.get(current_slot, {}).get("email", "")).strip().lower()
    best_slot = ""
    best_score = -2.0
    for sname, sdata in accounts.items():
        if sname == current_slot:
            continue
        if not sdata.get("authenticated", False):
            continue
        other_email = str(sdata.get("email", "")).strip().lower()
        if cur_email and other_email and other_email == cur_email:
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


def _profile_cred_domain(slot: str, reg: dict, *, accounts_dir: Path | None = None, base_home: Path | None = None) -> Path:
    acct = reg.get("accounts", {}).get(slot, {})
    mode = acct.get("mode", "system" if slot == "primary" else "isolated")
    if mode != "isolated":
        return (base_home or HOME) / ".config" / "antigravity"
    return (accounts_dir or ACCOUNTS_DIR) / slot / "home" / ".config" / "antigravity"


def baton_preflight(
    session: AgySession,
    *,
    target_slot: str = "",
    model: str = "",
    min_target_pct: float = QUOTA_WARN_PCT,
    registry_path: Path | None = None,
    accounts_dir: Path | None = None,
    agy_data_dir: Path | None = None,
    base_home: Path | None = None,
) -> dict:
    """Verify whether Baton Pass can safely continue `session` on another account without duplicates or data loss."""
    reg = load_accounts_registry(registry_path=registry_path, base_home=base_home)
    accounts = reg.get("accounts", {})
    source_slot = reg.get("conv_accounts", {}).get(session.conversation_id) or reg.get("active_slot", "primary")
    source_acct = accounts.get(source_slot, {})
    model_disp, model_slug = resolve_model_arg(model, agy_data_dir=agy_data_dir)
    fam = model_to_quota_family(model_disp or model_slug or "gemini")

    chosen_slot = target_slot or choose_baton_target(reg, source_slot, model_disp or model_slug)
    target_acct = accounts.get(chosen_slot, {}) if chosen_slot else {}

    source_email = str(source_acct.get("email", "")).strip()
    target_email = str(target_acct.get("email", "")).strip()
    src_fams = source_acct.get("quota", {}).get("families", {})
    tgt_fams = target_acct.get("quota", {}).get("families", {})
    source_pct = float(src_fams[fam]) if fam in src_fams else None
    target_pct = float(tgt_fams[fam]) if fam in tgt_fams else None

    blockers: list[str] = []
    if not chosen_slot or chosen_slot not in accounts:
        blockers.append("No hay otra cuenta autorizada con identidad distinta.")
    else:
        if chosen_slot == source_slot:
            blockers.append(f"La cuenta destino [{chosen_slot}] es igual a la actual.")
        if not target_acct.get("authenticated", False):
            blockers.append(f"La cuenta [{chosen_slot}] no completó login OAuth.")
        if source_email and target_email and source_email.lower() == target_email.lower():
            blockers.append(f"[{chosen_slot}] usa el mismo correo ({target_email}) que [{source_slot}].")
        if target_pct is not None and target_pct <= min_target_pct:
            blockers.append(f"Cuota {fam} de [{chosen_slot}] demasiado baja ({target_pct:.0f}% <= {min_target_pct:.0f}%).")

    # Verify disjoint credential isolation (no shared .config/antigravity symlink)
    creds_isolated = False
    if chosen_slot and chosen_slot in accounts:
        src_dom = _profile_cred_domain(source_slot, reg, accounts_dir=accounts_dir, base_home=base_home)
        tgt_dom = _profile_cred_domain(chosen_slot, reg, accounts_dir=accounts_dir, base_home=base_home)
        creds_isolated = (
            src_dom.resolve(strict=False) != tgt_dom.resolve(strict=False)
            and not tgt_dom.is_symlink()
        )
        if not creds_isolated:
            blockers.append("Dominio de credenciales no está aislado entre perfiles.")

    # Verify single-execution / zero-duplicate safety
    is_busy = session.external_busy or session.state_label in ("RUNNING", "BG-TASK")
    is_anc = bool(session.proc and is_ancestor_pid(session.proc.pid))
    no_dup_ok = (not is_busy) and (not is_anc)
    if is_busy:
        blockers.append(f"Sesión en estado {session.state_label} (turno/subtarea activa): espera a IDLE.")
    elif is_anc:
        blockers.append("Proceso actual es ancestro directo del dashboard.")

    # Verify preserved conversation state on disk and symlinked into target isolated home
    data_dir = agy_data_dir or AGY_DATA_DIR
    has_conv_pb = (data_dir / "conversations" / f"{session.conversation_id}.pb").is_file()
    has_brain = (data_dir / "brain" / session.conversation_id).is_dir()
    state_ok = has_conv_pb or has_brain or session.step_count > 0
    if chosen_slot and accounts.get(chosen_slot, {}).get("mode", "system") == "isolated":
        t_agy = (accounts_dir or ACCOUNTS_DIR) / chosen_slot / "home" / ".gemini" / "antigravity-cli"
        if t_agy.exists():
            state_ok = state_ok and ((t_agy / "conversations").is_symlink() or (t_agy / "conversation_summaries.db").is_symlink())
    if not state_ok:
        blockers.append("No se verificó estado/historial de conversación compartido en disco.")

    return {
        "ok": len(blockers) == 0,
        "source_slot": source_slot,
        "source_email": source_email,
        "source_pct": source_pct,
        "target_slot": chosen_slot,
        "target_email": target_email,
        "target_pct": target_pct,
        "model_display": model_disp,
        "model_slug": model_slug,
        "quota_family": fam,
        "step_count": session.step_count,
        "checks": {
            "target_authorized": bool(chosen_slot and target_acct.get("authenticated", False)),
            "distinct_identity": not (source_email and target_email and source_email.lower() == target_email.lower()),
            "credentials_isolated": creds_isolated,
            "target_has_quota": target_pct is None or target_pct > min_target_pct,
            "no_duplicate_operation": no_dup_ok,
            "state_preserved": state_ok,
        },
        "blockers": blockers,
    }


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


def tmux_dquote(s: str) -> str:
    """Quote a command string cleanly inside tmux `{ ... }` / double quotes."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_tmux_config(self_path: Path) -> None:
    TMUX_CONF.parent.mkdir(parents=True, exist_ok=True)
    if ensure_zig_core():
        status_cmd = f"'{ZIG_CORE_BIN}' statusbar"
    else:
        status_cmd = f"python3 '{self_path}' statusbar"

    if shutil.which("wl-copy"):
        clip_cmd = "wl-copy"
    elif shutil.which("xclip"):
        clip_cmd = "xclip -in -selection clipboard >/dev/null 2>&1"
    elif shutil.which("xsel"):
        clip_cmd = "xsel --clipboard --input >/dev/null 2>&1"
    else:
        clip_cmd = ""

    copy_action = f"send-keys -X copy-pipe-no-clear {tmux_dquote(clip_cmd)}" if clip_cmd else "send-keys -X copy-selection-no-clear"
    copy_exit_action = f"send-keys -X copy-pipe-and-cancel {tmux_dquote(clip_cmd)}" if clip_cmd else "send-keys -X copy-selection-and-cancel"
    close_btn = " #[range=user|x_#{window_index} fg=#ff5555 bold]×#[range=window|#{window_index}]"
    py_self = f'python3 "{self_path}"'
    win_fmt = f"#{{?#{{m:*DASH*,#W}},,#[fg=#6272a4] #I:#W{close_btn} }}"
    win_cur_fmt = f"#{{?#{{m:*DASH*,#W}},,#[bg=#282a36,fg=#50fa7b,bold] ▸ #I:#W{close_btn} #[default]}}"
    is_dash_w = "#{m:*DASH*,#{window_name}}"
    dash_btn_fmt = (
        f"#[bg=#{{?{is_dash_w},#50fa7b,#e94560}},fg=#{{?{is_dash_w},#14171f,#ffffff}},bold]"
        f" ⚡ AGY DASH #{{?{is_dash_w},▾ ,}}"
        f"#[bg=#14171f,fg=#{{?{is_dash_w},#50fa7b,#e94560}}] "
    )

    conf = f"""\
# Auto-generated by agy-dash (Zig+Python Single-Window Command Center)
set -g default-terminal "tmux-256color"
set -ga terminal-overrides ",*256col*:Tc"
set -g mouse on
setw -g mode-keys vi
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
set -g window-status-separator ""

# Freeze-select & persistent clipboard copy even while AGY streams output!
bind -n MouseDrag1Pane if -F '#{{m:*DASH*,#{{window_name}}}}' 'send-keys -M' 'copy-mode -M'
bind -n WheelUpPane if -F '#{{||:#{{m:*DASH*,#{{window_name}}}},#{{m:*👁*,#{{window_name}}}}}}' 'send-keys -M' 'if -F "#{{pane_in_mode}}" "send-keys -M" "copy-mode -e; send-keys -M"'
bind -T copy-mode MouseDragEnd1Pane {copy_action}
bind -T copy-mode-vi MouseDragEnd1Pane {copy_action}
bind -T copy-mode DoubleClick1Pane select-pane \\; send-keys -X select-word \\; {copy_action}
bind -T copy-mode-vi DoubleClick1Pane select-pane \\; send-keys -X select-word \\; {copy_action}
bind -T copy-mode TripleClick1Pane select-pane \\; send-keys -X select-line \\; {copy_action}
bind -T copy-mode-vi TripleClick1Pane select-pane \\; send-keys -X select-line \\; {copy_action}
bind -T copy-mode Enter {copy_exit_action}
bind -T copy-mode-vi y {copy_exit_action}
bind -T copy-mode-vi Enter {copy_exit_action}
bind -n M-s copy-mode

# Red ⚡ AGY DASH button (top-left) toggles Command Center; × closes tabs keeping busy agents alive
bind -T root MouseDown1StatusLeft run-shell {tmux_dquote(py_self + " show-dash --toggle")}
bind -T root MouseDown1Status if -F '#{{m:x_*,#{{mouse_status_range}}}}' {{ run-shell {tmux_dquote(py_self + " close-tab #{s/x_//:#{mouse_status_range}}")} }} {{ select-window -t = }}
bind -T root MouseDown2Status run-shell {tmux_dquote(py_self + " close-tab #{window_index}")}

# Top dashboard bar powered by Zig `agy-fast-core statusbar` (<7ms)
set -g status-position top
set -g status-interval 1
set -g status-style "bg=#14171f,fg=#d8dee9"
set -g status-left-length 32
set -g status-right-length 78
set -g status-left "{dash_btn_fmt}"
set -g status-right "#[fg=#414650]│ #[fg=#8be9fd]#({status_cmd}) #[fg=#414650]│ #[fg=#f1fa8c]Alt+D:Dash Alt+1..9:Tab Alt+S:Copy Alt+N:New "

setw -g window-status-format "{win_fmt}"
setw -g window-status-current-format "{win_cur_fmt}"

bind -n M-0 run-shell {tmux_dquote(py_self + " show-dash")}
bind -n M-d run-shell {tmux_dquote(py_self + " show-dash")}
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
bind -n M-n run-shell {tmux_dquote(py_self + " open-new-tab")}
bind -n M-h run-shell {tmux_dquote(py_self + " hibernate all-idle")}
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
    cp_new = tmux_cmd(
        "new-window", "-d", "-P", "-F", "#{window_index}", "-t", TMUX_SESSION, "-n", "⚡DASH", tui_cmd, capture=True
    )
    if cp_new.returncode == 0 and cp_new.stdout.strip():
        new_idx = cp_new.stdout.strip().splitlines()[-1]
        if new_idx != "0":
            tmux_cmd("swap-window", "-d", "-s", f"{TMUX_SESSION}:{new_idx}", "-t", f"{TMUX_SESSION}:0")


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
    # If this session was parked in hidden background `agy-bg`, pop the live window right back into view!
    bg_info = _get_bg_window_map().get(session.conversation_id)
    if bg_info is not None:
        bg_idx = bg_info[0]
        tmux_cmd("move-window", "-d", "-s", f"{TMUX_BG_SESSION}:{bg_idx}", "-t", f"{TMUX_SESSION}:")
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
    """Hand off an IDLE/disk conversation to another authorized account keeping exact state & model."""
    pf = baton_preflight(session, target_slot=target_slot, model=model)
    if not pf["ok"]:
        return False, "⚠️ Preflight Baton Pass bloqueado: " + " | ".join(pf["blockers"])

    chosen_slot = pf["target_slot"]
    model_disp = pf["model_display"]
    model_slug = pf["model_slug"]

    # Ensure zero duplicate operations: terminate any live IDLE/duplicate procs and wait for clean exit
    pids_to_stop: list[int] = []
    for p in ([session.proc] if session.proc else []) + list(session.extra_procs):
        if p and not p.in_dash and not is_ancestor_pid(p.pid):
            pids_to_stop.append(p.pid)
            try:
                os.kill(p.pid, signal.SIGTERM)
            except OSError:
                pass
    if pids_to_stop:
        for _ in range(40):
            if all(not Path(f"/proc/{pid}").exists() for pid in pids_to_stop):
                break
            time.sleep(0.1)

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
                raw_tool = str(tools[-1]).split("(", 1)[0].strip()
                label = _TOOL_LABELS.get(raw_tool, f"{raw_tool[:14]}...")
                break
            if t.get("role") == "TOOL" and t.get("content"):
                content_s = str(t["content"])
                if "Created At:" in content_s or "Tool is running" in content_s or "background task" in content_s:
                    label = "Task running..."
                else:
                    clean_c = content_s.replace("🔧", "").strip(" \"'")[:16]
                    label = clean_c or "Task running..."
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
    is_mirror = "👁" in session.dash_win_name
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


def _is_led_generated_name(name: str) -> bool:
    if not name:
        return True
    return name.startswith(_SPINNER_FRAMES) or name.startswith(("●", "👁", "💤", "🔑", "⚡"))


def sync_led_tmux_tabs(
    sessions: list[AgySession],
    tick_idx: int,
    last_tab_names: dict[str, str],
    activity_cache: dict[str, tuple[tuple[int, int], str]],
) -> None:
    """Update open tmux tab names with animated spinner + LED action marquee, respecting user rename-window."""
    cmds: list[str] = []
    for s in sessions:
        if s.dash_win_idx is None:
            continue
        if s.dash_win_name.startswith("🔑"):
            continue
        prev = last_tab_names.get(s.dash_win_idx)
        # If the user manually renamed the window in tmux (`Ctrl+B ,`), adopt and persist their custom title!
        if (
            prev is not None
            and s.dash_win_name
            and s.dash_win_name != prev
            and not _is_led_generated_name(s.dash_win_name)
        ):
            save_custom_title(s.conversation_id, s.dash_win_name)
            s.title = s.dash_win_name.strip()

        busy, act_txt = session_live_activity(s, activity_cache)
        new_name = format_led_tab_title(s, tick_idx, act_txt, busy)
        if prev != new_name:
            last_tab_names[s.dash_win_idx] = new_name
            cmds.extend(["rename-window", "-t", f"{TMUX_SESSION}:{s.dash_win_idx}", new_name, ";"])
    if cmds:
        tmux_cmd(*cmds[:-1])


# ============================================================================
# External Context Compactor via NVIDIA NIM API (`nvapi` / Nemotron Ultra)
# ============================================================================

_NEMOTRON_CANDIDATE_MODELS = (
    "nvidia/nemotron-3-ultra-550b-a55b",
    "nvidia/nemotron-3-ultra-550b",
    "nvidia/llama-3_1-nemotron-ultra-253b-v1",
    "nvidia/llama-3.3-nemotron-super-49b-v1",
    "nvidia/llama-3.1-nemotron-70b-instruct",
)


def mask_api_key(key: str) -> str:
    k = key.strip()
    if not k:
        return "(no configurada)"
    if len(k) <= 10:
        return "****"
    return f"{k[:6]}...{k[-4:]}"


def load_nvapi_config(
    config_path: Path | None = None,
    *,
    cfg_path: Path | None = None,
) -> dict:
    p = cfg_path or config_path or NVAPI_CONFIG_FILE
    data: dict = {}
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    if not isinstance(data, dict):
        data = {}
    env_key = os.environ.get("NVIDIA_API_KEY") or os.environ.get("NVAPI_KEY") or ""
    api_key = str(data.get("api_key") or env_key).strip()
    base_url = str(data.get("base_url") or DEFAULT_NVAPI_BASE_URL).strip().rstrip("/")
    raw_model = str(data.get("model") or "").strip()
    if raw_model == "nvidia/llama-3_1-nemotron-ultra-253b-v1":
        raw_model = DEFAULT_NVAPI_MODEL
    model = raw_model or DEFAULT_NVAPI_MODEL
    return {
        "api_key": api_key,
        "base_url": base_url,
        "model": model,
    }


def save_nvapi_config(
    api_key: str = "",
    *,
    model: str = "",
    base_url: str = "",
    config_path: Path | None = None,
    cfg_path: Path | None = None,
) -> dict:
    p = cfg_path or config_path or NVAPI_CONFIG_FILE
    cur = load_nvapi_config(config_path=p)
    if api_key.strip():
        cur["api_key"] = api_key.strip()
    if model.strip():
        cur["model"] = model.strip()
    if base_url.strip():
        cur["base_url"] = base_url.strip().rstrip("/")
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(cur, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, p)
    return cur


def estimate_session_tokens(conv_or_session: str | AgySession, step_count: int = 0) -> tuple[int, int]:
    """Return (estimated_input_tokens, transcript_bytes) for a conversation."""
    if isinstance(conv_or_session, AgySession):
        conv_id = conv_or_session.conversation_id
        step_count = conv_or_session.step_count
    else:
        conv_id = str(conv_or_session)
    tpath = transcript_path_for(conv_id)
    try:
        size_b = tpath.stat().st_size if tpath.is_file() else 0
    except OSError:
        size_b = 0
    est_tokens = max(step_count * 140, size_b // 4)
    return (est_tokens, size_b)


def should_offer_compaction(session: AgySession) -> tuple[bool, int, int]:
    """Return (is_heavy, est_tokens, size_bytes) when reopening a SAVED/HIBERNATED conversation."""
    est_tok, size_b = estimate_session_tokens(session.conversation_id, session.step_count)
    if session.proc is not None:
        return (False, est_tok, size_b)
    is_heavy = session.step_count >= COMPACT_MIN_STEPS or size_b >= COMPACT_MIN_BYTES
    return (is_heavy, est_tokens := est_tok, size_b)


def build_compaction_digest(conv_id: str, title: str, workspace: str, max_chars: int = 44000) -> tuple[str, int]:
    """Extract high-signal user requests, tool actions, and recent turns from transcript.jsonl."""
    tpath = transcript_path_for(conv_id)
    if not tpath.is_file():
        return (f"Título: {title}\nWorkspace: {workspace}\n(Sin transcript.jsonl local)", 0)

    try:
        raw_bytes = tpath.read_bytes()
    except OSError:
        return (f"Título: {title}\nWorkspace: {workspace}", 0)

    lines = [l for l in raw_bytes.decode("utf-8", errors="replace").splitlines() if l.strip().startswith("{")]
    user_prompts: list[str] = []
    tool_events: list[str] = []
    recent_turns: list[str] = []

    for line in lines:
        try:
            obj = json.loads(line)
        except Exception:
            continue
        idx = obj.get("step_index", 0)
        stype = obj.get("type", "")
        content = (obj.get("content") or "").strip()
        if "<USER_REQUEST>" in content and "</USER_REQUEST>" in content:
            m = re.search(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", content, re.DOTALL)
            if m:
                content = m.group(1).strip()
        if stype == "USER_INPUT" and content:
            user_prompts.append(f"[Paso #{idx} USUARIO]: {content[:600]}")
            recent_turns.append(f"[Paso #{idx} USUARIO]: {content[:900]}")
        elif stype == "PLANNER_RESPONSE":
            tcs = []
            for tc in obj.get("tool_calls") or []:
                if isinstance(tc, dict):
                    tn = tc.get("name") or "tool"
                    args = tc.get("arguments") or {}
                    sm = args.get("toolSummary") or args.get("Path") or args.get("CommandLine") or ""
                    tcs.append(f"{tn}({str(sm)[:80]})" if sm else tn)
            if tcs:
                tool_events.append(f"[Paso #{idx} TOOLS]: {', '.join(tcs)}")
            if content or tcs:
                recent_turns.append(f"[Paso #{idx} AGENTE]: {content[:1100]} {' | Tools: ' + ', '.join(tcs) if tcs else ''}")

    head_users = "\n".join(user_prompts[:8])
    tail_users = "\n".join(user_prompts[-8:]) if len(user_prompts) > 8 else ""
    tools_sample = "\n".join(tool_events[-25:])
    tail_recent = "\n".join(recent_turns[-18:])

    digest = (
        f"# Conversación a Compactar: {title} ({conv_id})\n"
        f"- Workspace: {workspace}\n"
        f"- Tamaño original: {len(raw_bytes) // 1024} KB (~{len(raw_bytes) // 4} tokens)\n\n"
        f"## Peticiones iniciales del usuario\n{head_users}\n\n"
        f"## Peticiones recientes del usuario\n{tail_users}\n\n"
        f"## Herramientas y archivos tocados recientemente\n{tools_sample}\n\n"
        f"## Últimos turnos detallados\n{tail_recent}\n"
    )
    if len(digest) > max_chars:
        digest = digest[: max_chars // 3] + "\n\n...[TRUNCADO]...\n\n" + digest[-(max_chars * 2 // 3) :]
    return (digest, len(raw_bytes))


def _discover_nvapi_nemotron_model(base_url: str, api_key: str) -> str:
    req = urllib.request.Request(
        f"{base_url}/models",
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=8.0) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        models = [str(m.get("id", "")) for m in (data.get("data") or []) if isinstance(m, dict)]
        for pref in ("nemotron-3-ultra", "nemotron-ultra", "550b", "253b", "nemotron-super", "nemotron"):
            for mid in models:
                if pref in mid.lower():
                    return mid
    except Exception:
        pass
    return DEFAULT_NVAPI_MODEL


def compact_conversation_with_nvapi(
    session: AgySession,
    *,
    config_path: Path | None = None,
    cfg_path: Path | None = None,
    handoffs_dir: Path | None = None,
    opener=None,
) -> tuple[bool, str, Path | None]:
    """Send conversation digest to NVIDIA NIM (Nemotron) and write a compact continuation handoff."""
    cfg = load_nvapi_config(config_path=cfg_path or config_path)
    api_key = cfg["api_key"]
    if not api_key:
        return (
            False,
            "⚠️ Falta configurar tu NVIDIA API Key (`nvapi-...`). Pulsa [K] o corre `agy-dash nvapi set-key <key>`.",
            None,
        )

    digest, orig_bytes = build_compaction_digest(session.conversation_id, session.title, session.workspace)
    orig_tokens = max(session.step_count * 140, orig_bytes // 4)
    base_url = cfg["base_url"]
    chosen_model = cfg["model"]
    if chosen_model.lower() == "auto":
        chosen_model = _discover_nvapi_nemotron_model(base_url, api_key)

    sys_prompt = (
        "Eres un compactador experto de contexto para sesiones de ingeniería de software (Antigravity CLI). "
        "Resume la conversación en español en un documento Markdown ultra-denso (<1200 palabras) con estas secciones exactas:\n"
        "1. OBJETIVO Y PETICIONES DEL USUARIO\n"
        "2. TRABAJO COMPLETADO Y DECISIONES TÉCNICAS\n"
        "3. ARCHIVOS MODIFICADOS / RUTAS Y COMANDOS CLAVE\n"
        "4. ESTADO ACTUAL EXACTO\n"
        "5. SIGUIENTE PASO ACCIONABLE INMEDIATO\n"
        "Conserva rutas exactas, hashes de commits, nombres de funciones y errores pendientes sin inventar nada."
    )

    candidates = [chosen_model] + [m for m in _NEMOTRON_CANDIDATE_MODELS if m != chosen_model]
    url_open = opener or urllib.request.urlopen
    summary_text = ""
    used_model = chosen_model
    last_err = ""

    for cand in candidates[:4]:
        payload = json.dumps({
            "model": cand,
            "messages": [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": digest},
            ],
            "temperature": 0.2,
            "max_tokens": 2048,
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{base_url}/chat/completions",
            data=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with url_open(req, timeout=110.0) as resp:
                body = json.loads(resp.read().decode("utf-8", errors="replace"))
            choices = body.get("choices") or []
            if choices:
                msg_obj = choices[0].get("message") or {}
                summary_text = str(msg_obj.get("content") or msg_obj.get("reasoning_content") or "").strip()
                if summary_text:
                    used_model = cand
                    break
        except urllib.error.HTTPError as he:
            err_body = ""
            try:
                err_body = he.read().decode("utf-8", errors="replace")[:160]
            except Exception:
                pass
            last_err = f"HTTP {he.code}: {err_body or he.reason}"
            if he.code in (401, 403):
                return (False, f"❌ NVIDIA API Key rechazada ({last_err}). Revisa con [K].", None)
            continue
        except Exception as e:
            last_err = str(e)
            break

    if not summary_text:
        return (False, f"❌ Falló compactación NVAPI ({used_model}): {last_err or 'respuesta vacía'}", None)

    out_dir = handoffs_dir or COMPACTED_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    handoff_file = out_dir / f"{session.conversation_id[:8]}_handoff.md"
    compact_tokens = max(1, len(summary_text) // 4)
    saved_pct = round(max(0.0, (1.0 - (compact_tokens / max(1, orig_tokens))) * 100.0), 1)
    header = (
        f"# 🗜️ Handoff Compactado por NVIDIA NIM (`{used_model}`)\n"
        f"- **Conversación original:** `{session.conversation_id}` ({session.title})\n"
        f"- **Workspace:** `{session.workspace}`\n"
        f"- **Reducción de contexto:** `~{orig_tokens:,} tokens` -> `~{compact_tokens:,} tokens` (**{saved_pct}% ahorrado**)\n\n"
    )
    handoff_file.write_text(header + summary_text + "\n", encoding="utf-8")
    try:
        os.chmod(handoff_file, 0o600)
    except OSError:
        pass
    return (
        True,
        f"🗜️ Compactada con `{used_model}`: ~{orig_tokens:,} -> ~{compact_tokens:,} tokens ({saved_pct}% ahorro).",
        handoff_file,
    )


def open_compacted_session_in_tab(session: AgySession, handoff_file: Path, *, switch_to: bool = True) -> str:
    """Open a fresh lightweight AGY session tab pre-loaded with the NVAPI compacted handoff."""
    self_path = Path(__file__).resolve()
    cwd = session.workspace if Path(session.workspace).is_dir() else str(Path.cwd())
    reg = load_accounts_registry()
    slot = reg.get("conv_accounts", {}).get(session.conversation_id) or reg.get("active_slot", "primary")
    tab_name = _short_tab_title(session.title, prefix="🗜")
    boot_prompt = (
        f"Contexto compactado de la sesión '{session.title}' ({session.conversation_id[:8]}). "
        f"Ejecuta primero la tool de ISyMCP `isymcp(action='handoff.load', arguments={{'target': '{session.conversation_id[:8]}', 'workspace': '{cwd}'}})` "
        f"(o lee `{handoff_file}` si no está disponible) para obtener el handoff y contexto ziggrep en un solo paso y confirma que estás listo."
    )
    cmd = (
        f"exec python3 {sh_quote(str(self_path))} run "
        f"--cwd {sh_quote(cwd)} --account {sh_quote(slot)} --title {sh_quote(session.title)} "
        f"-- -i {sh_quote(boot_prompt)}"
    )
    args = ["new-window", "-t", TMUX_SESSION, "-n", tab_name, "-c", cwd]
    if not switch_to:
        args.insert(1, "-d")
    args.append(cmd)
    tmux_cmd(*args)
    return f"🚀 Abierta sesión compactada '{session.title}' en pestaña `{tab_name}` (leyendo {handoff_file.name})."


def parse_handoff_metadata(handoff_file: Path) -> dict:
    """Extract metadata from a compacted handoff markdown file in the vault."""
    conv_id = handoff_file.stem.replace("_handoff", "")
    title = conv_id
    workspace = str(Path.cwd())
    model = ""
    reduction = ""
    try:
        st = handoff_file.stat()
        mtime = st.st_mtime
        size_b = st.st_size
        head = handoff_file.read_text(encoding="utf-8", errors="replace")[:2048]
    except OSError:
        mtime = 0.0
        size_b = 0
        head = ""
    for line in head.splitlines():
        if line.startswith("# ") and "(`" in line:
            m = re.search(r"\(`([^`]+)`\)", line)
            if m:
                model = m.group(1)
        elif "**Conversación original:**" in line:
            m = re.search(r"`([^`]+)`\s*\(([^)]*)\)", line)
            if m:
                conv_id = m.group(1).strip() or conv_id
                title = m.group(2).strip() or title
        elif "**Workspace:**" in line:
            m = re.search(r"`([^`]+)`", line)
            if m:
                workspace = m.group(1).strip() or workspace
        elif "**Reducción de contexto:**" in line:
            reduction = line.split(":", 1)[1].strip().replace("`", "").replace("*", "")
    return {
        "conv_id": conv_id,
        "short_id": conv_id[:8],
        "title": title,
        "workspace": workspace,
        "model": model,
        "reduction": reduction,
        "path": handoff_file,
        "mtime": mtime,
        "size_bytes": size_b,
    }


def get_vault_handoff(conv_id: str, *, handoffs_dir: Path | None = None) -> Path | None:
    """Return existing compacted handoff file for conv_id in the vault, if any."""
    out_dir = handoffs_dir or COMPACTED_DIR
    p = out_dir / f"{conv_id[:8]}_handoff.md"
    return p if p.is_file() else None


def list_compacted_vault(*, handoffs_dir: Path | None = None) -> list[dict]:
    """List all compacted handoffs stored in the vault, newest first."""
    out_dir = handoffs_dir or COMPACTED_DIR
    if not out_dir.is_dir():
        return []
    entries = [parse_handoff_metadata(p) for p in out_dir.glob("*_handoff.md") if p.is_file()]
    entries.sort(key=lambda e: e["mtime"], reverse=True)
    return entries


def open_vault_entry_in_tab(entry: dict, *, switch_to: bool = True) -> str:
    """Launch a fresh lightweight AGY tab from a stored vault handoff without re-compacting."""
    dummy = AgySession(
        conversation_id=str(entry.get("conv_id") or entry.get("short_id") or ""),
        title=str(entry.get("title") or "Handoff"),
        preview="",
        step_count=0,
        last_modified="",
        workspace=str(entry.get("workspace") or Path.cwd()),
        db_status="CASCADE_RUN_STATUS_IDLE",
        not_fully_idle=False,
    )
    return open_compacted_session_in_tab(dummy, Path(entry["path"]), switch_to=switch_to)


def delete_vault_handoff(handoff_path: Path) -> bool:
    """Remove a compacted handoff markdown file from the vault."""
    try:
        if handoff_path.is_file():
            handoff_path.unlink()
            return True
    except OSError:
        pass
    return False


def _context_window_for_model(model_name: str = "") -> int:
    low = (model_name or "").lower()
    if any(k in low for k in ("claude", "gpt", "opus", "sonnet", "oss")):
        return 200_000
    return 1_000_000


def measure_session_context(
    conv_id: str,
    *,
    transcript_path: Path | None = None,
    step_count: int = 0,
    model_name: str = "",
) -> dict:
    """Measure real context token usage from transcript.jsonl PLANNER_RESPONSE records (or estimate)."""
    tpath = transcript_path if (transcript_path and transcript_path.is_file()) else transcript_path_for(conv_id)
    max_tokens = _context_window_for_model(model_name)
    used_tokens = 0
    input_tok = 0
    cache_tok = 0
    output_tok = 0
    source = "estimated"
    size_b = 0
    detected_model = model_name

    if tpath.is_file():
        try:
            st = tpath.stat()
            size_b = st.st_size
            with tpath.open("rb") as f:
                pos = max(0, size_b - 65536)
                f.seek(pos, os.SEEK_SET)
                tail = f.read().decode("utf-8", errors="replace")
            for raw_line in reversed(tail.splitlines()):
                line = raw_line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                if ev.get("type") == "PLANNER_RESPONSE" and (
                    "input_tokens" in ev or "cache_read_tokens" in ev
                ):
                    input_tok = int(ev.get("input_tokens") or 0)
                    cache_tok = int(ev.get("cache_read_tokens") or 0)
                    output_tok = int(ev.get("output_tokens") or 0)
                    used_tokens = input_tok + cache_tok + output_tok
                    if not detected_model and ev.get("model"):
                        detected_model = str(ev.get("model"))
                        max_tokens = _context_window_for_model(detected_model)
                    source = "transcript_usage"
                    break
        except OSError:
            pass

    if used_tokens <= 0:
        used_tokens = max(step_count * 140, size_b // 4)

    pct = round((used_tokens / max(1, max_tokens)) * 100.0, 1)
    # Classify both by absolute cost/latency tokens and window percentage
    if used_tokens >= HEARTBEAT_URGENT_TOKENS or pct >= 70.0:
        level = "COMPACT_URGENT"
    elif used_tokens >= HEARTBEAT_RECOMMEND_TOKENS or pct >= 45.0:
        level = "COMPACT_RECOMMENDED"
    elif used_tokens >= HEARTBEAT_WARN_TOKENS or pct >= 20.0:
        level = "WARN"
    else:
        level = "OK"

    return {
        "conv_id": conv_id,
        "used_tokens": used_tokens,
        "input_tokens": input_tok,
        "cache_read_tokens": cache_tok,
        "output_tokens": output_tok,
        "max_tokens": max_tokens,
        "pct": pct,
        "level": level,
        "model": detected_model,
        "source": source,
        "transcript_bytes": size_b,
    }


def evaluate_context_heartbeat(
    conv_id: str,
    *,
    transcript_path: Path | None = None,
    model_name: str = "",
    cwd: str = "",
    step_count: int = 0,
    interval_sec: float | None = None,
    now: float | None = None,
    force: bool = False,
    heartbeats_dir: Path | None = None,
) -> dict:
    """Check whether a 5-min context heartbeat notice should be delivered to the active agent."""
    cur_t = time.time() if now is None else float(now)
    iv = HEARTBEAT_INTERVAL_SEC if interval_sec is None else float(interval_sec)
    hb_dir = heartbeats_dir or HEARTBEATS_DIR
    hb_dir.mkdir(parents=True, exist_ok=True)
    hb_file = hb_dir / f"{conv_id[:16]}.json"

    prev: dict = {}
    if hb_file.is_file():
        try:
            prev = json.loads(hb_file.read_text(encoding="utf-8"))
        except Exception:
            prev = {}
    if not isinstance(prev, dict):
        prev = {}

    metrics = measure_session_context(
        conv_id,
        transcript_path=transcript_path,
        step_count=step_count,
        model_name=model_name,
    )
    last_ts = float(prev.get("last_notified_ts") or 0.0)
    last_level = str(prev.get("last_level") or "")
    escalated_urgent = metrics["level"] == "COMPACT_URGENT" and last_level != "COMPACT_URGENT"
    due = force or (last_ts <= 0.0) or ((cur_t - last_ts) >= iv) or escalated_urgent

    ws = cwd or str(Path.cwd())
    short_id = conv_id[:8]
    tok_s = f"{metrics['used_tokens']:,}"
    max_s = f"{metrics['max_tokens']:,}"
    pct_s = f"{metrics['pct']:.1f}%"
    lvl = metrics["level"]

    if lvl in ("COMPACT_RECOMMENDED", "COMPACT_URGENT"):
        urgency = "URGENTE" if lvl == "COMPACT_URGENT" else "RECOMENDADO"
        notice = (
            f"[AGY-DASH Heartbeat · Contexto {urgency}: {tok_s}/{max_s} tokens ({pct_s}) — rango {lvl}] "
            f"Si estás listo para aligerar tu ventana sin perder el hilo ni cambiar de pestaña, "
            f"ejecuta `isymcp(action='handoff.self_compact', arguments={{'conv_id': '{conv_id}', 'cwd': '{ws}', "
            f"'resume_prompt': '<breve instrucción exacta de cómo continuar>'}})` (o la tool `agy_self_compact`). "
            f"Compactará esta sesión con NVIDIA Nemotron 550B y continuará automáticamente en esta misma TUI."
        )
        user_md = f"💓 Heartbeat AGY: contexto `{tok_s}` tokens (`{pct_s}`, rango `{lvl}` — listo para auto-compactar con Nemotron 550B)."
    else:
        notice = (
            f"[AGY-DASH Heartbeat] Contexto actual: {tok_s}/{max_s} tokens ({pct_s} · rango {lvl}). "
            f"No hace falta compactar todavía."
        )
        user_md = f"💓 Heartbeat AGY: contexto `{tok_s}` tokens (`{pct_s}`, rango `{lvl}`)."

    state_out = {
        **metrics,
        "updated_at": cur_t,
        "last_notified_ts": cur_t if due else last_ts,
        "last_level": lvl if due else last_level,
    }
    try:
        hb_file.write_text(json.dumps(state_out, indent=2), encoding="utf-8")
    except OSError:
        pass

    return {
        **state_out,
        "should_notify": bool(due),
        "notice": notice,
        "user_markdown": user_md,
    }


def respawn_compacted_session_in_place(
    session: AgySession,
    handoff_file: Path,
    *,
    resume_prompt: str = "",
    win_idx: str | None = None,
) -> str:
    """Replace the active session's tmux pane in-place with a lightweight Nemotron-compacted continuation."""
    self_path = Path(__file__).resolve()
    cwd = session.workspace if Path(session.workspace).is_dir() else str(Path.cwd())
    reg = load_accounts_registry()
    slot = reg.get("conv_accounts", {}).get(session.conversation_id) or reg.get("active_slot", "primary")
    tab_name = _short_tab_title(session.title, prefix="🗜")

    cont_clause = (
        f" y continúa inmediatamente con: {resume_prompt.strip()}"
        if resume_prompt.strip()
        else " y confirma que estás listo para continuar."
    )
    boot_prompt = (
        f"Contexto compactado con NVIDIA Nemotron 550B de tu sesión '{session.title}' ({session.conversation_id[:8]}). "
        f"Ejecuta ÚNICAMENTE una llamada a `agy_handoff_context` (o `isymcp(action='handoff.load', arguments={{'target': '{session.conversation_id[:8]}', 'workspace': '{cwd}'}})` — si aún no está cargada en este hub, lee `{handoff_file}`){cont_clause}"
    )
    cmd = (
        f"exec python3 {sh_quote(str(self_path))} run "
        f"--cwd {sh_quote(cwd)} --account {sh_quote(slot)} --title {sh_quote(session.title)} "
        f"-- -i {sh_quote(boot_prompt)}"
    )

    target_win = win_idx if win_idx is not None else _find_tmux_window_by_conv(session.conversation_id)
    if target_win is not None:
        tmux_cmd("rename-window", "-t", f"{TMUX_SESSION}:{target_win}", tab_name)
        tmux_cmd("respawn-pane", "-k", "-t", f"{TMUX_SESSION}:{target_win}", cmd)
        return f"♻️ Sesión '{session.title}' compactada con Nemotron 550B y continuada in-place en pestaña `{tab_name}`."

    tmux_cmd("new-window", "-t", TMUX_SESSION, "-n", tab_name, "-c", cwd, cmd)
    return f"🚀 Sesión '{session.title}' compactada con Nemotron 550B en pestaña `{tab_name}`."


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
    title = load_custom_titles().get(cid) or args.title or f"Sesión {cid[:8]}"

    print_instant_zig_preview(cid, title, cwd)
    est_tok, size_b = estimate_session_tokens(cid)
    nv_cfg = load_nvapi_config()
    print(
        f"\n\033[1;33m  💤 SESIÓN EN MODO HIBERNADO (0 MB RAM — Contexto en disco: {size_b // 1024} KB / ~{est_tok:,} tokens est.)\033[0m"
    )
    print(
        f"  \033[1;35m[c]\033[0m 🗜️ Compactar con NVIDIA Nemotron ({mask_api_key(nv_cfg['api_key'])}) y abrir ligera (~95%+ ahorro tokens)"
    )
    print(
        "  \033[1;32m[ENTER]\033[0m Despertar conversación completa tal cual  |  \033[1;36m[k]\033[0m Configurar NVAPI Key  |  \033[1;36m[d]\033[0m ⚡DASH  |  \033[1;31m[q]\033[0m Cerrar"
    )
    try:
        ans = input("\n  Acción [c / ENTER / k / d / q]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return 0
    if ans == "q":
        return 0
    if ans == "d":
        _select_dash_window()
        return cmd_wake_stub(args)
    if ans == "k":
        try:
            k_in = input("  Introduce tu NVIDIA API Key (nvapi-...): ").strip()
            if k_in:
                save_nvapi_config(k_in)
                print("  ✅ NVIDIA API Key guardada en ~/.gemini/antigravity-cli/nvapi_config.json (0600).")
        except (EOFError, KeyboardInterrupt):
            pass
        return cmd_wake_stub(args)
    if ans == "c":
        print("  ⏳ Compactando conversación con NVIDIA NIM (Nemotron)...")
        dummy_sess = AgySession(
            conversation_id=cid,
            title=title,
            preview="",
            step_count=0,
            last_modified="",
            workspace=cwd,
            db_status="CASCADE_RUN_STATUS_IDLE",
            not_fully_idle=False,
        )
        ok, msg, hfile = compact_conversation_with_nvapi(dummy_sess)
        print(f"  {msg}")
        if ok and hfile is not None:
            reg = load_hibernated_registry()
            if cid in reg:
                del reg[cid]
                save_hibernated_registry(reg)
            boot_prompt = (
                f"Contexto compactado de la sesión '{title}' ({cid[:8]}). "
                f"Lee primero `{hfile}` para retomar el estado exacto sin gastar tokens de historial y confirma que estás listo."
            )
            return exec_agy_optimized(["-i", boot_prompt], cwd=cwd, preview_conv=cid, preview_title=title)
        time.sleep(2.0)
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


def _ensure_bg_session() -> None:
    has = tmux_cmd("has-session", "-t", TMUX_BG_SESSION, capture=True)
    if has.returncode != 0:
        tmux_cmd("new-session", "-d", "-s", TMUX_BG_SESSION, "-n", "_bg_anchor", "sleep infinity")


def close_tab_safely(win_idx: str) -> str:
    """Close a tab (`×`), parking RUNNING/BG-TASK sessions into hidden `agy-bg` instead of killing them."""
    cp = tmux_cmd(
        "display-message",
        "-p",
        "-t",
        f"{TMUX_SESSION}:{win_idx}",
        "#{window_name}|#{@agy_conv_id}|#{pane_current_command}|#{pane_pid}",
        capture=True,
    )
    if cp.returncode != 0 or not cp.stdout.strip():
        return f"Pestaña #{win_idx} ya no existe."
    parts = cp.stdout.strip().split("|", 3)
    win_name = parts[0] if len(parts) > 0 else ""
    conv_id = parts[1].strip() if len(parts) > 1 else ""
    pane_cmd = parts[2].strip() if len(parts) > 2 else ""
    pane_pid = int(parts[3].strip()) if len(parts) > 3 and parts[3].strip().isdigit() else 0

    if "DASH" in win_name:
        return "No se cierra la pestaña principal ⚡DASH."

    # Live mirrors, wake stubs, and OAuth login helpers can close UI tab directly without affecting background AGY
    if any(ch in win_name for ch in ("👁", "💤", "🔑")) or pane_cmd == "python3":
        tmux_cmd("kill-window", "-t", f"{TMUX_SESSION}:{win_idx}")
        return f"Pestaña '{win_name}' cerrada (solo vista)."

    _, _, sessions = load_sessions(limit=50)
    sess = next(
        (
            s
            for s in sessions
            if (conv_id and s.conversation_id == conv_id)
            or s.dash_win_idx == win_idx
            or (pane_pid and s.proc and (s.proc.pid == pane_pid or s.proc.ppid == pane_pid))
        ),
        None,
    )
    is_busy = bool(sess and sess.state_label in ("RUNNING", "BG-TASK"))
    if is_busy:
        if sess and not conv_id:
            tmux_cmd("set-window-option", "-t", f"{TMUX_SESSION}:{win_idx}", "@agy_conv_id", sess.conversation_id)
        _ensure_bg_session()
        tmux_cmd("move-window", "-d", "-s", f"{TMUX_SESSION}:{win_idx}", "-t", f"{TMUX_BG_SESSION}:")
        return f"'{sess.title if sess else win_name}' ocultada en segundo plano (sigue trabajando viva)."

    tmux_cmd("kill-window", "-t", f"{TMUX_SESSION}:{win_idx}")
    return f"Pestaña '{win_name}' cerrada en IDLE (reabrible desde 0:⚡DASH)."


def cmd_close_tab(args: argparse.Namespace) -> int:
    msg = close_tab_safely(str(args.window_index))
    tmux_cmd("display-message", "-d", "2500", msg)
    return 0


def cmd_show_dash(args: argparse.Namespace) -> int:
    """Ensure ⚡DASH exists at window 0 and focus (or toggle back if already there)."""
    ensure_dash_window_zero()
    if getattr(args, "toggle", False):
        cur = tmux_cmd("display-message", "-p", "-t", TMUX_SESSION, "#{window_name}", capture=True)
        if cur.returncode == 0 and "DASH" in (cur.stdout or ""):
            win_count = tmux_cmd("list-windows", "-t", TMUX_SESSION, "-F", "#{window_index}", capture=True)
            if win_count.returncode == 0 and len(win_count.stdout.strip().splitlines()) > 1:
                tmux_cmd("last-window", "-t", TMUX_SESSION)
                return 0
    _select_dash_window()
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


def _active_pids_for_slot(slot: str) -> list[int]:
    """Scan /proc/*/environ cautiously to detect live agy processes pinned to `slot`."""
    pids: list[int] = []
    needle = f"AGY_ACCOUNT_SLOT={slot}".encode()
    try:
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                comm = (entry / "comm").read_text(errors="ignore").strip()
                if comm != "agy":
                    continue
                env_bytes = (entry / "environ").read_bytes()
                if needle in env_bytes.split(b"\x00"):
                    pids.append(int(entry.name))
            except OSError:
                continue
    except OSError:
        pass
    return pids


def inspect_account_removal(
    slot: str,
    *,
    sessions: list[AgySession] | None = None,
    registry_path: Path | None = None,
    accounts_dir: Path | None = None,
    base_home: Path | None = None,
) -> dict:
    """Inspect identity, duplicate siblings, associated sessions, live PIDs, and local-only files before deletion."""
    safe = validate_slot_name(slot)
    reg = load_accounts_registry(registry_path=registry_path, base_home=base_home)
    accounts = reg.get("accounts", {})
    acct = accounts.get(safe)
    acct_root = (accounts_dir or ACCOUNTS_DIR).resolve()
    slot_dir = (acct_root / safe).resolve(strict=False)

    email = str(acct.get("email", "")).strip() if acct else ""
    duplicate_of = [
        sname
        for sname, sdata in accounts.items()
        if sname != safe and email and str(sdata.get("email", "")).strip().lower() == email.lower()
    ]
    sess_titles = {s.conversation_id: s.title for s in (sessions or [])}
    associated_convs = [
        {"conversation_id": cid, "title": sess_titles.get(cid, cid[:8])}
        for cid, cslot in reg.get("conv_accounts", {}).items()
        if cslot == safe
    ]
    active_pids = _active_pids_for_slot(safe) if safe != "primary" else []

    local_files = 0
    symlinks = 0
    if safe != "primary" and slot_dir.is_dir() and slot_dir.is_relative_to(acct_root):
        for root_str, dirnames, filenames in os.walk(slot_dir, topdown=True, followlinks=False):
            rp = Path(root_str)
            keep_dirs = []
            for dname in dirnames:
                dp = rp / dname
                if dp.is_symlink():
                    symlinks += 1
                else:
                    keep_dirs.append(dname)
            dirnames[:] = keep_dirs
            for fname in filenames:
                fp = rp / fname
                if fp.is_symlink():
                    symlinks += 1
                else:
                    local_files += 1

    blocked = ""
    if safe == "primary":
        blocked = "No se elimina `primary` (usa el keyring del sistema)."
    elif acct is None:
        blocked = f"La cuenta [{safe}] no está registrada."
    elif active_pids:
        blocked = f"Hay {len(active_pids)} proceso(s) AGY vivos usando [{safe}] (PIDs: {', '.join(map(str, active_pids))})."

    return {
        "slot": safe,
        "exists": acct is not None,
        "mode": acct.get("mode", "isolated") if acct else "",
        "email": email,
        "is_active_default": reg.get("active_slot") == safe,
        "duplicate_of": duplicate_of,
        "associated_sessions": associated_convs,
        "active_pids": active_pids,
        "slot_dir": str(slot_dir),
        "local_files": local_files,
        "shared_symlinks": symlinks,
        "blocked_reason": blocked,
    }


def purge_isolated_slot_dir(slot: str, *, accounts_dir: Path | None = None) -> tuple[int, int]:
    """Delete ONLY local regular files inside `accounts/<slot>/`; unlink symlinks without ever descending into targets."""
    safe = validate_slot_name(slot)
    if safe == "primary":
        raise ValueError("Refusing to purge primary account.")
    acct_root = (accounts_dir or ACCOUNTS_DIR).resolve()
    slot_dir = (acct_root / safe).resolve(strict=False)
    if not slot_dir.is_relative_to(acct_root) or slot_dir == acct_root:
        raise ValueError(f"Unsafe slot path outside vault: {slot_dir}")
    if not slot_dir.exists():
        return (0, 0)
    if slot_dir.is_symlink():
        slot_dir.unlink()
        return (0, 1)

    files_deleted = 0
    symlinks_unlinked = 0
    for root_str, dirnames, filenames in os.walk(slot_dir, topdown=False, followlinks=False):
        rp = Path(root_str).resolve(strict=False)
        if not rp.is_relative_to(slot_dir):
            continue
        for fname in filenames:
            fp = rp / fname
            if fp.is_symlink():
                fp.unlink()
                symlinks_unlinked += 1
            elif fp.parent.resolve(strict=False).is_relative_to(slot_dir):
                fp.unlink()
                files_deleted += 1
        for dname in dirnames:
            dp = rp / dname
            if dp.is_symlink():
                dp.unlink()
                symlinks_unlinked += 1
            elif dp.is_dir() and dp.resolve(strict=False).is_relative_to(slot_dir):
                try:
                    dp.rmdir()
                except OSError:
                    pass
    try:
        slot_dir.rmdir()
    except OSError:
        pass
    return (files_deleted, symlinks_unlinked)


def remove_account_slot(
    slot: str,
    *,
    confirmed: bool = False,
    purge_local: bool = True,
    sessions: list[AgySession] | None = None,
    registry_path: Path | None = None,
    accounts_dir: Path | None = None,
    base_home: Path | None = None,
) -> tuple[bool, str]:
    """Safely remove ONLY the selected isolated profile after confirming identity and associated sessions."""
    info = inspect_account_removal(
        slot,
        sessions=sessions,
        registry_path=registry_path,
        accounts_dir=accounts_dir,
        base_home=base_home,
    )
    if info["blocked_reason"]:
        return False, f"⚠️ {info['blocked_reason']}"
    if not confirmed:
        return (
            False,
            f"⚠️ Confirmación requerida para eliminar [{info['slot']}] ({info['email'] or 'sin email'}, "
            f"{len(info['associated_sessions'])} sesiones asociadas).",
        )

    safe = info["slot"]
    repoint_to = info["duplicate_of"][0] if info["duplicate_of"] else "primary"
    files_del = 0
    links_del = 0
    if purge_local and info["mode"] == "isolated":
        files_del, links_del = purge_isolated_slot_dir(safe, accounts_dir=accounts_dir)

    reg = load_accounts_registry(registry_path=registry_path, base_home=base_home)
    reg["accounts"].pop(safe, None)
    if reg.get("active_slot") == safe:
        reg["active_slot"] = repoint_to
    conv_map = reg.get("conv_accounts", {})
    for cid, cslot in list(conv_map.items()):
        if cslot == safe:
            conv_map[cid] = repoint_to
    save_accounts_registry(reg, registry_path=registry_path)
    return (
        True,
        f"🗑 Perfil local [{safe}] ({info['email'] or 'sin email'}) eliminado "
        f"({files_del} locales borrados, {links_del} symlinks desligados, compartido intacto).",
    )


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
            print("Uso: agy-dash account remove <slot> [--yes]", file=sys.stderr)
            return 1
        _, _, sessions = load_sessions(limit=50)
        info = inspect_account_removal(slot_arg, sessions=sessions)
        if info["blocked_reason"]:
            print(f"⚠️ {info['blocked_reason']}", file=sys.stderr)
            return 1
        confirmed = bool(getattr(args, "yes", False))
        if not confirmed:
            print(f"  Perfil      : [{info['slot']}] ({info['mode']})")
            print(f"  Identidad   : {info['email'] or '(sin email)'}")
            if info["duplicate_of"]:
                print(f"  Duplicada de: {', '.join(info['duplicate_of'])}")
            print(f"  Sesiones    : {len(info['associated_sessions'])} asociadas")
            print(f"  Alcance     : SOLO {info['slot_dir']} ({info['local_files']} archivos locales; compartido intacto)")
            if not sys.stdin.isatty():
                print("Usa `--yes` en modo no interactivo tras revisar el alcance.", file=sys.stderr)
                return 1
            ans = input(f"  ¿Confirmas eliminar el perfil local [{info['slot']}]? [y/N]: ").strip().lower()
            confirmed = ans in ("y", "yes", "s", "si", "sí")
        ok, msg = remove_account_slot(slot_arg, confirmed=confirmed, sessions=sessions)
        print(msg, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1
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
    print("  1. Se abrirá el navegador automáticamente y la URL completa se copiará al portapapeles.")
    print("  2. Cuando veas el prompt de AGY listo, escribe `/exit` (o Ctrl+D) para guardar y volver.\n")
    subprocess.run([AGY_BIN], env=env)
    ok, msg = refresh_account_quota(safe_slot, allow_unauthenticated=True)
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
    target_slot = getattr(args, "to", "") or ""
    model = getattr(args, "model", "") or ""

    if getattr(args, "check", False):
        pf = baton_preflight(sel, target_slot=target_slot, model=model)
        if getattr(args, "json", False):
            print(json.dumps(pf, indent=2, ensure_ascii=False))
        else:
            verdict = "LISTO (READY)" if pf["ok"] else "BLOQUEADO"
            print(f"  Baton Preflight : {verdict}")
            print(f"  Conversación    : {sel.title} ({sel.conversation_id[:8]}) · {pf['step_count']} pasos")
            print(f"  Modelo          : {pf['model_display'] or pf['model_slug'] or 'default'} (grupo: {pf['quota_family']})")
            src_pct_s = f"{pf['source_pct']:.0f}%" if pf["source_pct"] is not None else "?"
            tgt_pct_s = f"{pf['target_pct']:.0f}%" if pf["target_pct"] is not None else "?"
            print(f"  Origen          : [{pf['source_slot']}] {pf['source_email']} ({src_pct_s} libre)")
            print(f"  Destino         : [{pf['target_slot'] or '-'}] {pf['target_email']} ({tgt_pct_s} libre)")
            for k, v in pf["checks"].items():
                print(f"    [{'OK' if v else 'FAIL'}] {k}")
            if pf["blockers"]:
                print("  Bloqueos        : " + " | ".join(pf["blockers"]))
        return 0 if pf["ok"] else 1

    ok, msg = baton_pass_session(
        sel,
        target_slot=target_slot,
        model=model,
        switch_to=not getattr(args, "no_switch", False),
    )
    print(msg)
    return 0 if ok else 1


def cmd_nvapi(args: argparse.Namespace) -> int:
    sub_act = getattr(args, "nvapi_cmd", None) or "status"
    if sub_act == "status":
        cfg = load_nvapi_config()
        print(f"  NVIDIA API Key : {mask_api_key(cfg['api_key'])}")
        print(f"  Base URL       : {cfg['base_url']}")
        print(f"  Modelo Nemotron: {cfg['model']}")
        print(f"  Archivo config : {NVAPI_CONFIG_FILE} (0600)")
        return 0
    if sub_act == "set-key":
        key_val = getattr(args, "key", "") or ""
        if not key_val and sys.stdin.isatty():
            key_val = input("Introduce tu NVIDIA API Key (nvapi-...): ").strip()
        if not key_val:
            print("Uso: agy-dash nvapi set-key <nvapi-...> [--model ...] [--base-url ...]", file=sys.stderr)
            return 1
        cfg = save_nvapi_config(
            key_val,
            model=getattr(args, "model", "") or "",
            base_url=getattr(args, "base_url", "") or "",
        )
        print(f"✅ NVIDIA API Key guardada ({mask_api_key(cfg['api_key'])}, modelo={cfg['model']}).")
        return 0
    return 1


def detect_caller_conversation_id(sessions: list[AgySession]) -> str | None:
    """Resolve the active AGY conversation_id that invoked the current command."""
    env_cid = (os.environ.get("AGY_CONVERSATION_ID") or "").strip()
    if env_cid:
        return env_cid
    for s in sessions:
        if s.proc and is_ancestor_pid(s.proc.pid):
            return s.conversation_id
        for ep in s.extra_procs:
            if is_ancestor_pid(ep.pid):
                return s.conversation_id
    if os.environ.get("TMUX"):
        cp = subprocess.run(
            ["tmux", "display-message", "-p", "#{@agy_conv_id}"],
            capture_output=True,
            text=True,
            timeout=1.5,
        )
        t_cid = (cp.stdout or "").strip()
        if cp.returncode == 0 and t_cid:
            return t_cid
    cwd_s = str(Path.cwd())
    active_ws = [s for s in sessions if s.proc is not None and s.workspace == cwd_s]
    if active_ws:
        return active_ws[0].conversation_id
    return None


def cmd_compact(args: argparse.Namespace) -> int:
    target = (getattr(args, "target", "") or "self").strip()
    _, _, sessions = load_sessions(limit=60)
    if target.lower() in ("self", "current", "."):
        resolved_cid = detect_caller_conversation_id(sessions)
        if not resolved_cid:
            print("No se pudo detectar automáticamente la conversación AGY actual; pasa su ID.", file=sys.stderr)
            return 1
        target = resolved_cid

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

    if getattr(args, "bg", False) and not getattr(args, "bg_worker", False):
        self_path = Path(__file__).resolve()
        worker_cmd = [sys.executable, str(self_path), "compact", sel.conversation_id, "--_bg-worker"]
        if getattr(args, "open", False):
            worker_cmd.append("--open")
        if getattr(args, "model", ""):
            worker_cmd.extend(["--model", str(args.model)])
        subprocess.Popen(
            worker_cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        if _find_tmux_window_by_conv(sel.conversation_id) is not None or tmux_cmd("has-session", "-t", TMUX_SESSION, capture=True).returncode == 0:
            tmux_cmd("display-message", "-d", "4000", f"🗜️ Compactando '{sel.title[:28]}' con NVIDIA Nemotron en 2º plano...")
        print(f"🗜️ Compactando '{sel.title}' ({sel.conversation_id[:8]}) con NVIDIA Nemotron en 2º plano...")
        print(f"[███░░░░░░░] ⏳ Generando handoff -> {COMPACTED_DIR / (sel.conversation_id[:8] + '_handoff.md')}")
        if getattr(args, "open", False):
            print("🚀 Al terminar se abrirá automáticamente la nueva pestaña ligera con `agy_handoff_context`.")
        return 0

    if getattr(args, "model", ""):
        save_nvapi_config(model=str(args.model))

    ok, msg, hfile = compact_conversation_with_nvapi(sel)
    if getattr(args, "bg_worker", False):
        if ok and hfile is not None:
            if getattr(args, "open", False):
                open_compacted_session_in_tab(sel, hfile, switch_to=True)
            tmux_cmd("display-message", "-d", "4000", f"✅ {msg}")
        else:
            tmux_cmd("display-message", "-d", "5000", f"❌ {msg}")
        return 0 if ok else 1

    print(msg, file=sys.stdout if ok else sys.stderr)
    if not ok or hfile is None:
        return 1
    print(f"  Handoff guardado en: {hfile}")
    if getattr(args, "open", False):
        print(f"  {open_compacted_session_in_tab(sel, hfile)}")
    return 0


def cmd_rename(args: argparse.Namespace) -> int:
    target = args.target
    _, _, sessions = load_sessions(limit=60)
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
    new_t = getattr(args, "new_title", "") or getattr(args, "title", "")
    clean = save_custom_title(sel.conversation_id, new_t)
    print(f"✏️ Sesión {sel.conversation_id[:8]} renombrada permanentemente a '{clean}'.")
    return 0


def cmd_heartbeat_hook(args: argparse.Namespace) -> int:
    """Fast (<15ms) AGY PreInvocation hook that delivers a 5-min context heartbeat when due."""
    raw = ""
    try:
        if not sys.stdin.isatty():
            raw = sys.stdin.read()
    except Exception:
        raw = ""
    payload: dict = {}
    if raw.strip():
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {}

    conv_id = str(payload.get("conversationId") or getattr(args, "conv_id", "") or "").strip()
    if not conv_id:
        print("{}")
        return 0

    tpath_raw = str(payload.get("transcriptPath") or "").strip()
    tpath = Path(tpath_raw) if tpath_raw else None
    model_name = str(payload.get("modelName") or "").strip()
    cwd = str(payload.get("cwd") or "").strip()
    if not cwd:
        ws_list = payload.get("workspacePaths")
        if isinstance(ws_list, list) and ws_list:
            cwd = str(ws_list[0])

    hb = evaluate_context_heartbeat(
        conv_id,
        transcript_path=tpath,
        model_name=model_name,
        cwd=cwd,
        force=bool(getattr(args, "force", False)),
    )
    if getattr(args, "status", False):
        print(json.dumps(hb, indent=2, ensure_ascii=False))
        return 0
    if not hb.get("should_notify"):
        print("{}")
        return 0

    out = {
        "injectSteps": [
            {
                "systemMessage": {
                    "systemMessage": hb["notice"],
                    "metadata": {"user_markdown": hb["user_markdown"]},
                }
            }
        ]
    }
    print(json.dumps(out, ensure_ascii=False))
    return 0


def _write_compact_status(conv_id: str, status: str, msg: str = "", handoff_file: Path | None = None) -> None:
    try:
        COMPACTED_DIR.mkdir(parents=True, exist_ok=True)
        sf = COMPACTED_DIR / f".status_{conv_id[:8]}.json"
        sf.write_text(
            json.dumps(
                {
                    "conv_id": conv_id,
                    "status": status,
                    "message": msg,
                    "handoff_file": str(handoff_file) if handoff_file else "",
                    "updated_at": time.time(),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass


def cmd_self_compact(args: argparse.Namespace) -> int:
    """Compact an active agent session with NVIDIA Nemotron 550B and continue in-place in the same TUI tab."""
    target = (getattr(args, "target", "") or "self").strip()
    _, _, sessions = load_sessions(limit=60)
    if target.lower() in ("self", "current", "."):
        resolved_cid = detect_caller_conversation_id(sessions)
        if not resolved_cid:
            print("No se pudo detectar la conversación AGY actual; pasa `conv_id`.", file=sys.stderr)
            return 1
        target = resolved_cid

    matches = [
        s
        for s in sessions
        if s.conversation_id == target
        or s.conversation_id.startswith(target)
        or target.lower() in s.title.lower()
    ]
    if matches:
        sel = matches[0]
    else:
        custom_titles = load_custom_titles()
        sel = AgySession(
            conversation_id=target,
            title=custom_titles.get(target) or f"Sesión {target[:8]}",
            preview="",
            step_count=0,
            last_modified="",
            workspace=getattr(args, "cwd", "") or str(Path.cwd()),
            db_status="CASCADE_RUN_STATUS_IDLE",
            not_fully_idle=False,
        )
    if getattr(args, "cwd", ""):
        sel.workspace = str(args.cwd)

    win_idx = (getattr(args, "win", "") or "").strip() or _find_tmux_window_by_conv(sel.conversation_id) or ""
    resume_prompt = str(getattr(args, "resume_prompt", "") or "").strip()
    no_respawn = bool(getattr(args, "no_respawn", False))

    if getattr(args, "bg", False) and not getattr(args, "bg_worker", False):
        _write_compact_status(sel.conversation_id, "running", "Compactando con NVIDIA Nemotron 550B...")
        self_path = Path(__file__).resolve()
        worker_cmd = [
            sys.executable,
            str(self_path),
            "self-compact",
            sel.conversation_id,
            "--_bg-worker",
            "--cwd",
            sel.workspace,
        ]
        if win_idx:
            worker_cmd.extend(["--win", win_idx])
        if resume_prompt:
            worker_cmd.extend(["--resume-prompt", resume_prompt])
        if getattr(args, "model", ""):
            worker_cmd.extend(["--model", str(args.model)])
        if not getattr(args, "in_place", True):
            worker_cmd.append("--no-in-place")
        if no_respawn:
            worker_cmd.append("--no-respawn")
        COMPACTED_DIR.mkdir(parents=True, exist_ok=True)
        err_log = (COMPACTED_DIR / ".last_self_compact.log").open("ab")
        try:
            subprocess.Popen(
                worker_cmd,
                stdin=subprocess.DEVNULL,
                stdout=err_log,
                stderr=err_log,
                start_new_session=True,
            )
        finally:
            err_log.close()
        tmux_cmd(
            "display-message",
            "-d",
            "4000",
            f"🗜️ Auto-compactando '{sel.title[:26]}' con Nemotron 550B en 2º plano...",
        )
        print(
            json.dumps(
                {
                    "ok": True,
                    "conv_id": sel.conversation_id,
                    "title": sel.title,
                    "win_idx": win_idx or None,
                    "in_place": bool(getattr(args, "in_place", True)),
                    "handoff_target": str(COMPACTED_DIR / f"{sel.conversation_id[:8]}_handoff.md"),
                    "message": "Auto-compactación Nemotron 550B iniciada en 2º plano.",
                },
                ensure_ascii=False,
            )
        )
        return 0

    _write_compact_status(sel.conversation_id, "running", "Llamando a NVIDIA NIM (Nemotron 550B)...")
    if getattr(args, "model", ""):
        save_nvapi_config(model=str(args.model))

    if getattr(args, "respawn_only", False):
        hfile = get_vault_handoff(sel.conversation_id)
        if hfile is None:
            print(f"No existe handoff en Baúl para '{sel.conversation_id[:8]}'.", file=sys.stderr)
            return 1
        ok, msg = True, f"Reanudando desde Baúl ({hfile.name})"
    else:
        ok, msg, hfile = compact_conversation_with_nvapi(sel)
    if not ok or hfile is None:
        _write_compact_status(sel.conversation_id, "error", msg)
        tmux_cmd("display-message", "-d", "5000", f"❌ {msg}")
        print(msg, file=sys.stderr)
        return 1

    _write_compact_status(sel.conversation_id, "done", msg, hfile)
    if no_respawn:
        tmux_cmd("display-message", "-d", "4000", f"✅ {msg}")
        if not getattr(args, "bg_worker", False):
            print(f"{msg} · Handoff: {hfile}")
        return 0

    # Give the agent up to 3s to finish outputting its final tool reply before in-place respawn
    deadline = time.time() + 3.0
    while time.time() < deadline and is_transcript_turn_active(sel.conversation_id, max_recent_sec=2.0):
        time.sleep(0.4)

    if getattr(args, "in_place", True):
        res_msg = respawn_compacted_session_in_place(
            sel,
            hfile,
            resume_prompt=resume_prompt,
            win_idx=win_idx or None,
        )
    else:
        res_msg = open_compacted_session_in_tab(sel, hfile, switch_to=True)
    tmux_cmd("display-message", "-d", "4000", f"✅ {res_msg}")
    if not getattr(args, "bg_worker", False):
        print(f"{msg} · {res_msg}")
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
        CommandButton("baton", "B", "[B] 🔀 Baton Pass", 4, 2, half),
        CommandButton("accounts", "u", "[u] 💳 Cuentas", 4, 2 + half + 1, half),
        CommandButton("compact", "c", "[c] 🗜 Compactar+Ir", 5, 2, half),
        CommandButton("rename", "t", "[t] ✏️ Renombrar", 5, 2 + half + 1, half),
        CommandButton("vault_compact", "C", "[C] 📦 Guardar Baúl", 6, 2, half),
        CommandButton("vault", "V", "[V] 🗄️ Abrir Baúl", 6, 2 + half + 1, half),
        CommandButton("nvapi_key", "K", "[K] 🔑 Key NVIDIA", 7, 2, half),
        CommandButton("memory", "m", "[m] 🧠 Memoria", 7, 2 + half + 1, half),
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


def _prompt_curses_text(stdscr, prompt: str, default_val: str = "", max_len: int = 96) -> str:
    """Prompt cleanly for single-line input in the bottom banner; Esc / Ctrl+C cancels immediately."""
    max_y, max_x = stdscr.getmaxyx()
    stdscr.timeout(-1)
    curses.noecho()
    try:
        curses.curs_set(1)
    except curses.error:
        pass
    msg = f" {prompt} [{default_val}] (Esc=Cancelar): " if default_val else f" {prompt} (Esc=Cancelar): "
    buf: list[str] = []
    cancelled = False
    try:
        while True:
            stdscr.move(max_y - 2, 0)
            stdscr.clrtoeol()
            shown = (msg + "".join(buf))[-(max_x - 2) :]
            stdscr.addstr(max_y - 2, 0, shown, curses.color_pair(4) | curses.A_BOLD)
            stdscr.refresh()
            try:
                key = stdscr.get_wch()
            except curses.error:
                continue
            if key in ("\x1b", 27):
                # Distinguish standalone Esc from terminal escape sequences (e.g. arrows)
                stdscr.nodelay(True)
                try:
                    nxt = stdscr.getch()
                except Exception:
                    nxt = -1
                finally:
                    stdscr.nodelay(False)
                    stdscr.timeout(-1)
                if nxt == -1:
                    cancelled = True
                    break
                # Drain remaining sequence bytes and ignore non-text escape keys
                stdscr.nodelay(True)
                try:
                    while stdscr.getch() != -1:
                        pass
                finally:
                    stdscr.nodelay(False)
                    stdscr.timeout(-1)
                continue
            if key in ("\x03", "\x07", 3, 7):
                cancelled = True
                break
            if key in ("\n", "\r", 10, 13, curses.KEY_ENTER):
                break
            if key in ("\x7f", "\b", "\x08", 127, 8, curses.KEY_BACKSPACE):
                if buf:
                    buf.pop()
                continue
            if isinstance(key, str) and key.isprintable() and len(buf) < max_len:
                buf.append(key)
    except Exception:
        cancelled = True
    finally:
        curses.noecho()
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        stdscr.timeout(250)
    if cancelled:
        return ""
    val = "".join(buf).strip()
    return val or default_val


def run_curses_tui(stdscr) -> None:
    curses.curs_set(0)
    curses.use_default_colors()
    mouse_mask = (
        curses.BUTTON1_CLICKED
        | curses.BUTTON1_PRESSED
        | curses.BUTTON1_DOUBLE_CLICKED
        | getattr(curses, "BUTTON4_PRESSED", 0)
        | getattr(curses, "BUTTON5_PRESSED", 0)
    )
    curses.mousemask(mouse_mask)
    if curses.has_colors():
        curses.init_pair(1, curses.COLOR_WHITE, curses.COLOR_RED)      # Header badge
        curses.init_pair(2, curses.COLOR_CYAN, -1)                     # Accent cyan
        curses.init_pair(3, curses.COLOR_GREEN, -1)                    # RUNNING / AGY
        curses.init_pair(4, curses.COLOR_YELLOW, -1)                   # BG-TASK / USER
        curses.init_pair(5, curses.COLOR_MAGENTA, -1)                  # HIBERNATED / Tools
        curses.init_pair(6, curses.COLOR_BLACK, curses.COLOR_CYAN)     # Selected row
        curses.init_pair(7, curses.COLOR_WHITE, curses.COLOR_BLUE)     # Button surface

    stdscr.timeout(250)
    cursor = 0
    saved_session_cursor = 0
    mode = "sessions"
    status_banner = "Listo — [Enter] Abrir (o Compactar NV) | [c] Compactar NV | [t] Renombrar | [B] Baton Pass | [u] Cuentas"
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
    last_db_sig = (0, 0, 0)
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
    pending_delete_info: dict | None = None
    pending_compact_sess: AgySession | None = None
    compact_lock = threading.Lock()
    compact_jobs: dict[str, dict] = {}
    compact_results: list[tuple[AgySession, bool, str, Path | None, bool]] = []

    def start_async_compaction(
        target_sess: AgySession,
        *,
        open_when_done: bool = False,
        prompt_if_missing_key: bool = True,
    ) -> str:
        cid = target_sess.conversation_id
        with compact_lock:
            existing = compact_jobs.get(cid)
            if existing and existing.get("status") == "running":
                if open_when_done:
                    existing["open_when_done"] = True
                return f"⏳ '{target_sess.title[:28]}' ya se está compactando en 2º plano..."
        cfg = load_nvapi_config()
        if not str(cfg.get("api_key", "")).strip():
            if not prompt_if_missing_key:
                return "⚠️ Falta NVIDIA API Key (`nvapi-...`). Pulsa [K] para configurarla."
            entered = _prompt_curses_text(stdscr, "Ingresa tu NVIDIA API Key (nvapi-...) para compactar", "", max_len=110)
            if not entered:
                return "Compactación cancelada: falta NVIDIA API Key."
            save_nvapi_config(api_key=entered)
        with compact_lock:
            compact_jobs[cid] = {
                "title": target_sess.title,
                "started": time.time(),
                "status": "running",
                "open_when_done": open_when_done,
            }

        def _compact_worker() -> None:
            ok = False
            msg = ""
            hfile: Path | None = None
            try:
                ok, msg, hfile = compact_conversation_with_nvapi(target_sess)
            except Exception as exc:
                ok = False
                msg = f"❌ Error inesperado en compactación: {exc}"
                hfile = None
            with compact_lock:
                job = compact_jobs.get(cid) or {}
                should_open = bool(job.get("open_when_done", open_when_done))
                compact_jobs.pop(cid, None)
                compact_results.append((target_sess, ok, msg, hfile, should_open))

        threading.Thread(target=_compact_worker, daemon=True).start()
        dest = "y abrirá pestaña al terminar" if open_when_done else "al Baúl NV (sin abrir pestaña)"
        return f"⏳ Compactando '{target_sess.title[:26]}' en 2º plano ({dest}) — el dashboard sigue libre."

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

    def db_wal_sig() -> tuple[int, int, int]:
        try:
            s1 = SUMMARIES_DB.stat().st_mtime_ns if SUMMARIES_DB.is_file() else 0
            wal = Path(str(SUMMARIES_DB) + "-wal")
            s2 = wal.stat().st_mtime_ns if wal.is_file() else 0
            s3 = PRESENCE_DIR.stat().st_mtime_ns if PRESENCE_DIR.is_dir() else 0
            return (s1, s2, s3)
        except OSError:
            return (0, 0, 0)

    def execute_action(action: str) -> str:
        nonlocal mode, cursor, saved_session_cursor, force_scan, cached_preview_sig, pending_compact_sess
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
        if action == "vault":
            if mode != "vault":
                if mode == "sessions":
                    saved_session_cursor = cursor
                mode = "vault"
                cursor = 0
                return "Vista: Baúl de Handoffs Compactados (NVIDIA NIM)"
            mode = "sessions"
            cursor = saved_session_cursor
            return "Vista: Command Center AGY"
        if action == "nvapi_key":
            cur_cfg = load_nvapi_config()
            masked = mask_api_key(str(cur_cfg.get("api_key", "")))
            entered = _prompt_curses_text(
                stdscr,
                f"NVIDIA API Key (nvapi-... · actual {masked})",
                "",
                max_len=110,
            )
            if not entered:
                return f"Cancelado — NVIDIA API Key sin cambios ({masked})."
            if not entered.startswith("nvapi-") and len(entered) < 20:
                return f"⚠️ Formato inválido (debe empezar por `nvapi-`). Key anterior conservada ({masked})."
            save_nvapi_config(api_key=entered)
            return f"✅ NVIDIA API Key guardada ({mask_api_key(entered)}) — lista para compactar con Nemotron."

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
            offer, est_tok, _ = should_offer_compaction(sel)
            if offer:
                pending_compact_sess = sel
                return (
                    f"⚡ '{sel.title}' consume ~{est_tok:,} tokens de entrada: "
                    f"[c] Compactar con NVIDIA Nemotron | [Enter/o] Abrir completa | [k] Key NV | [Esc] Cancelar"
                )
            return open_session_in_dashboard_tab(sel, switch_to=True)
        if action == "open_full" and sel:
            pending_compact_sess = None
            return open_session_in_dashboard_tab(sel, switch_to=True)
        if action == "compact" and sel:
            pending_compact_sess = None
            return start_async_compaction(sel, open_when_done=True, prompt_if_missing_key=True)
        if action == "vault_compact" and sel:
            pending_compact_sess = None
            return start_async_compaction(sel, open_when_done=False, prompt_if_missing_key=True)
        if action == "open_vault" and sel:
            pending_compact_sess = None
            vh = get_vault_handoff(sel.conversation_id)
            if vh is None:
                return f"No hay handoff previo en el Baúl para '{sel.title[:26]}'. Pulsa [c] para compactar."
            return open_compacted_session_in_tab(sel, vh, switch_to=True)
        if action == "rename" and sel:
            new_name = _prompt_curses_text(stdscr, f"Renombrar '{sel.title[:24]}'", sel.title, max_len=64)
            if not new_name or new_name == sel.title:
                return "Renombrado sin cambios."
            clean = save_custom_title(sel.conversation_id, new_name)
            sel.title = clean
            last_tab_names.pop(sel.conversation_id, None)
            trigger_led_sync(sessions, led_tick + 1)
            force_scan = True
            return f"✏️ Sesión renombrada permanentemente a '{clean}'."
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
        done_compact_batch: list[tuple[AgySession, bool, str, Path | None, bool]] = []
        with compact_lock:
            if compact_results:
                done_compact_batch = list(compact_results)
                compact_results.clear()
            running_compact = list(compact_jobs.values())
        for c_sess, c_ok, c_msg, c_hfile, c_open in done_compact_batch:
            if c_ok and c_hfile is not None:
                if c_open:
                    open_msg = open_compacted_session_in_tab(c_sess, c_hfile, switch_to=True)
                    status_banner = f"{c_msg} · {open_msg}"
                else:
                    status_banner = f"✅ Guardado en Baúl NV: '{c_sess.title[:26]}' — {c_msg} (pulsa [V] para abrir)"
                force_scan = True
            else:
                status_banner = f"❌ {c_msg}"
        cur_db_sig = db_wal_sig()
        if force_scan or cur_db_sig != last_db_sig or (now - last_scan_ts) >= 0.6:
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
            pf_badge = ""
            if sel_for_alert:
                pf = baton_preflight(sel_for_alert, target_slot=quota_alert.get("target_slot", ""), model=model_disp)
                pf_badge = " [Preflight OK · 0 dup]" if pf["ok"] else " [Esperando IDLE]"
            if quota_alert["target_slot"]:
                tpct = f" ({quota_alert['target_pct']:.0f}%)" if quota_alert["target_pct"] >= 0 else ""
                act_hint = f"Pulsa [B] Baton Pass -> [{quota_alert['target_slot']}]{tpct}{pf_badge}"
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

                insp_y = panel_btn_y + 9
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
                    nv_cfg = load_nvapi_config()
                    nv_lbl = mask_api_key(str(nv_cfg.get("api_key", "")))
                    ctx_m = measure_session_context(sel.conversation_id, step_count=sel.step_count)
                    ctx_tok = ctx_m["used_tokens"]
                    ctx_pct = ctx_m["pct"]
                    ctx_lvl = ctx_m["level"]
                    meta_lines = [
                        (f" 📌 {sel.title[:panel_w - 4]}", curses.A_BOLD),
                        (f" ID: {sel.conversation_id[:14]} PID:{pid_txt}{extra_txt} Cta:[{acct_tag}]", curses.A_NORMAL),
                        (f" Ctx: ~{ctx_tok // 1000}k ({ctx_pct:.0f}% · {ctx_lvl}) · {age_txt} | NV:{nv_lbl[:11]}", curses.color_pair(1 if "URGENT" in ctx_lvl else (4 if ("RECOMMEND" in ctx_lvl or kids_txt != "ninguno") else 3))),
                        (f" RAM: {_ram_bar(min(1.0, total_pss_mb / 2048.0), 18)} {total_pss_mb}M (KSM {total_ksm_mb}M)", curses.color_pair(3)),
                    ]
                    for mi, (ml, ma) in enumerate(meta_lines):
                        if insp_y + mi < 6 + list_avail:
                            stdscr.addstr(insp_y + mi, panel_x, ml[:panel_w], ma)

            # Bottom Real-Time Zig Live Conversation Preview (or Pre-Open Compaction Modal)!
            if preview_height > 0 and sessions and 0 <= cursor < len(sessions):
                sel = sessions[cursor]
                py = max_y - 3 - preview_height
                stdscr.addstr(py, 0, "─" * (max_x - 1))
                if pending_compact_sess is not None:
                    pcs = pending_compact_sess
                    _, est_tok, log_kb = should_offer_compaction(pcs)
                    vault_file = get_vault_handoff(pcs.conversation_id)
                    nv_cfg = load_nvapi_config()
                    has_nv = bool(str(nv_cfg.get("api_key", "")).strip())
                    nv_status = (
                        f"LISTA ({mask_api_key(str(nv_cfg.get('api_key', '')))} · {nv_cfg.get('model')})"
                        if has_nv
                        else "FALTA KEY — pulsa [k] para guardarla"
                    )
                    vault_opt = ""
                    if vault_file is not None:
                        v_age = int(max(0, (now - vault_file.stat().st_mtime) // 60))
                        vault_opt = f"[b] 📦 Abrir del Baúl ({v_age}m)  |  "
                    modal_lines = [
                        (
                            f" 🗜  REACTIVACIÓN DE SESIÓN PESADA: '{pcs.title[:38]}' ({pcs.step_count} pasos · {log_kb} KB · ~{est_tok:,} tokens de entrada)",
                            curses.color_pair(1) | curses.A_BOLD,
                        ),
                        (
                            f"    Reabrir esta sesión completa cargará ~{est_tok:,} tokens de entrada en cada mensaje.",
                            curses.color_pair(4) | curses.A_BOLD,
                        ),
                        (
                            f"    🔑 NVIDIA NIM ({nv_status}): puede compactarla con Nemotron Ultra antes de abrirla.",
                            curses.color_pair(3 if has_nv else 4),
                        ),
                        (
                            f"    👉 {vault_opt}[c] ⚡ Compactar y abrir   |   [C] 📦 Compactar solo al Baúl (2º plano)",
                            curses.color_pair(3) | curses.A_BOLD,
                        ),
                        (
                            "    👉 [Enter / o] 🚀 Abrir conversación original completa   |   [k] Key NVIDIA   |   [Esc] Cancelar",
                            curses.color_pair(2) | curses.A_BOLD,
                        ),
                    ]
                    for mi, (ml, ma) in enumerate(modal_lines):
                        ry = py + 1 + mi
                        if ry < max_y - 3:
                            stdscr.addstr(ry, 0, ml[: max_x - 1].ljust(max_x - 1), ma)
                else:
                    cur_sig = transcript_sig(sel.conversation_id)
                    if sel.conversation_id != cached_preview_cid or cur_sig != cached_preview_sig:
                        cached_preview_cid = sel.conversation_id
                        cached_preview_sig = cur_sig
                        cached_turns = zig_tail_transcript(sel.conversation_id, max_turns=preview_height - 2)

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
            email_counts: dict[str, int] = {}
            for _, ad_row in acct_list:
                em = str(ad_row.get("email", "")).strip().lower()
                if em:
                    email_counts[em] = email_counts.get(em, 0) + 1
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
                raw_mail = str(adata.get("email", "")).strip()
                mail = raw_mail or ("(sin login OAuth)" if not adata.get("authenticated") else "(pendiente)")
                is_dup = bool(raw_mail and email_counts.get(raw_mail.lower(), 0) > 1 and sname != "primary")
                if not adata.get("authenticated"):
                    st_txt = "FALTA LOGIN [+]"
                elif is_dup:
                    st_txt = "LISTA (DUPLICADA -> [x])"
                else:
                    st_txt = "LISTA"
                row_txt = (
                    f" {sname:<12} {mark:<4} {adata.get('mode', '?'):<9} {mail[:28]:<28} "
                    f"{_fp('argon'):>7} {_fp('gemini'):>7} {_fp('claude_gpt'):>10}  {st_txt}"
                )
                attr = (curses.color_pair(6) | curses.A_BOLD) if idx == cursor else (
                    curses.color_pair(4) if (not adata.get("authenticated") or is_dup) else curses.color_pair(3)
                )
                stdscr.addstr(ry, 0, row_txt[: max_x - 1].ljust(max_x - 1), attr)

            if pending_delete_info is not None:
                det_y = min(max_y - 8, 7 + len(acct_list))
                pdi = pending_delete_info
                dup_s = f" ⚠️ Duplicada de [{', '.join(pdi['duplicate_of'])}]" if pdi["duplicate_of"] else ""
                conf_lines = [
                    (f" 🗑 CONFIRMAR ELIMINACIÓN DE PERFIL LOCAL [{pdi['slot']}] — NADA DE OTRAS CUENTAS", curses.color_pair(1) | curses.A_BOLD),
                    (f"   Identidad : {pdi['email'] or '(sin login OAuth)'}{dup_s}", curses.color_pair(4) | curses.A_BOLD),
                    (f"   Sesiones  : {len(pdi['associated_sessions'])} asociadas  |  PIDs vivos en [{pdi['slot']}]: {len(pdi['active_pids'])}", curses.A_NORMAL),
                    (f"   Alcance   : SOLO {pdi['slot_dir']} ({pdi['local_files']} archivos locales; {pdi['shared_symlinks']} symlinks compartidos intactos)", curses.A_NORMAL),
                    (
                        f"   ❌ BLOQUEADO: {pdi['blocked_reason']}  (Esc para volver)"
                        if pdi["blocked_reason"]
                        else f"   👉 Pulsa [S] o [Y] para confirmar borrar SOLO [{pdi['slot']}]  |  [N / Esc] Cancelar",
                        curses.color_pair(1 if pdi["blocked_reason"] else 4) | curses.A_BOLD,
                    ),
                ]
                for ci, (cl, ca) in enumerate(conf_lines):
                    cy = det_y + ci
                    if cy < max_y - 3:
                        stdscr.addstr(cy, 0, cl[: max_x - 1].ljust(max_x - 1), ca)
            elif acct_list and 0 <= cursor < len(acct_list) and max_y >= 15:
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
        elif mode == "vault":
            vault_entries = list_compacted_vault()
            stdscr.addstr(
                4,
                0,
                f" 🗄️ BAÚL DE HANDOFFS COMPACTADOS ({len(vault_entries)} guardados en compacted/ — [Enter] Abrir nueva sesión · [A] Compactar pesadas al Baúl · [V/Esc] Volver)"[
                    : max_x - 1
                ],
                curses.color_pair(2) | curses.A_BOLD,
            )
            stdscr.addstr(
                5,
                0,
                f" {'ID':<10} {'TÍTULO':<32} {'REDUCCIÓN / AHORRO':<36} {'HACE':<8} {'WORKSPACE'}",
                curses.A_BOLD,
            )
            cursor = max(0, min(cursor, max(0, len(vault_entries) - 1)))
            avail_rows = max(1, max_y - 9)
            start_idx = max(0, cursor - avail_rows + 1) if cursor >= avail_rows else 0
            if not vault_entries:
                stdscr.addstr(6, 0, "   (Baúl vacío — pulsa [C] sobre una sesión o [A] aquí para compactar pesadas en 2º plano sin abrirlas)"[: max_x - 1])
            else:
                for idx, ve in enumerate(vault_entries[start_idx : start_idx + avail_rows], start=start_idx):
                    row_y = 6 + (idx - start_idx)
                    if row_y >= max_y - 3:
                        break
                    age_m = int(max(0, (now - ve["mtime"]) // 60)) if ve["mtime"] else 0
                    age_s = f"{age_m}m" if age_m < 60 else f"{age_m // 60}h"
                    ws_s = os.path.basename(str(ve["workspace"]).rstrip("/")) or str(ve["workspace"])
                    red_s = (ve["reduction"] or f"{ve['size_bytes']} B")[:36]
                    row_txt = f" {ve['short_id']:<10} {ve['title'][:32]:<32} {red_s:<36} {age_s:<8} {ws_s}"
                    attr = (curses.color_pair(6) | curses.A_BOLD) if idx == cursor else curses.color_pair(3)
                    stdscr.addstr(row_y, 0, row_txt[: max_x - 1].ljust(max_x - 1), attr)
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
            if running_compact:
                j0 = running_compact[0]
                el_s = int(max(0, now - j0["started"]))
                spin = _LED_SPINNER_FRAMES[led_tick % len(_LED_SPINNER_FRAMES)]
                banner_line = (
                    f"{spin} Compactando 2º plano ({len(running_compact)}): '{j0['title'][:26]}' ({el_s}s) | {status_banner}"
                )
            else:
                banner_line = status_banner
            stdscr.addstr(max_y - 2, 0, f" {banner_line}"[: max_x - 1], curses.color_pair(4))
            if mode == "accounts":
                keys_help = (
                    " [+] Agregar   [x/Del] Eliminar   [B] Baton Pass   [Enter/s] Activar   "
                    "[R] Cuotas   [u/Esc] Volver "
                )
            elif mode == "vault":
                keys_help = (
                    " [Enter] Abrir del Baúl   [r] Re-compactar al Baúl   [A] Compactar pesadas al Baúl (2ºp)   "
                    "[x/Del] Borrar   [V/Esc] Volver "
                )
            else:
                keys_help = (
                    " [Enter] Abrir  [c] Compactar+Ir  [C] Guardar Baúl  [V] Baúl  [t] Renombrar  "
                    "[K] Key NV  [B] Baton  [u] Cuentas  [h/H] Hibernar  [m] Memoria "
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
            btn4 = getattr(curses, "BUTTON4_PRESSED", 0)
            btn5 = getattr(curses, "BUTTON5_PRESSED", 0)
            if btn4 and (bstate & btn4):
                cursor = max(0, cursor - 1)
                continue
            if btn5 and (bstate & btn5):
                cursor += 1
                continue
            click_mask = curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED | curses.BUTTON1_DOUBLE_CLICKED
            if not (bstate & click_mask):
                continue
            is_dbl_click = bool(bstate & curses.BUTTON1_DOUBLE_CLICKED)
            if show_cmd_panel and mx >= panel_x:
                act = hit_test_button(cmd_buttons, panel_x, panel_btn_y, mx, my)
                if act:
                    res = execute_action(act)
                    if res:
                        status_banner = res
                    if act in ("compact", "vault_compact", "open", "open_full", "new", "baton", "mirror", "reload"):
                        curses.flushinp()
            elif mode == "sessions" and list_start_y <= my < list_start_y + list_avail and mx < left_w:
                clicked_idx = (max(0, cursor - list_avail + 1) if cursor >= list_avail else 0) + (my - list_start_y)
                if 0 <= clicked_idx < len(sessions):
                    pending_compact_sess = None
                    cursor = clicked_idx
                    if is_dbl_click:
                        status_banner = execute_action("open")
                        curses.flushinp()
            elif mode == "vault":
                vault_entries = list_compacted_vault()
                if 6 <= my < 6 + len(vault_entries):
                    cursor = my - 6
                    if is_dbl_click:
                        status_banner = open_vault_entry_in_tab(vault_entries[cursor], switch_to=True)
                        curses.flushinp()
            elif mode == "accounts":
                acct_list = list(reg.get("accounts", {}).items())
                if 6 <= my < 6 + len(acct_list):
                    cursor = my - 6
                elif my == max_y - 1:
                    if mx < 14:
                        ch = ord("+")
                    elif mx < 32:
                        ch = ord("x")
                    elif mx < 49:
                        ch = ord("B")
                    elif mx < 69:
                        ch = ord("s")
                    elif mx < 82:
                        ch = ord("R")
                    else:
                        ch = ord("u")
            if ch == curses.KEY_MOUSE:
                continue

        if mode == "accounts":
            acct_list = list(reg.get("accounts", {}).items())
            if pending_delete_info is not None:
                if ch in (ord("y"), ord("Y"), ord("s"), ord("S")) and not pending_delete_info.get("blocked_reason"):
                    ok, status_banner = remove_account_slot(
                        pending_delete_info["slot"],
                        confirmed=True,
                        sessions=sessions,
                    )
                    pending_delete_info = None
                    if ok and cursor > 0:
                        cursor -= 1
                elif ch in (ord("n"), ord("N"), 27, ord("u"), ord("q")):
                    pending_delete_info = None
                    status_banner = "Eliminación de perfil cancelada."
                continue
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
            elif ch in (ord("x"), curses.KEY_DC, 127) and acct_list and 0 <= cursor < len(acct_list):
                del_slot = acct_list[cursor][0]
                info = inspect_account_removal(del_slot, sessions=sessions)
                if info["blocked_reason"]:
                    status_banner = f"⚠️ {info['blocked_reason']}"
                else:
                    pending_delete_info = info
                    status_banner = f"Revisa identidad/alcance de [{del_slot}] arriba: [S/Y] Confirmar | [N/Esc] Cancelar"
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

        if mode == "vault":
            vault_entries = list_compacted_vault()
            if ch in (ord("v"), ord("V"), 27):
                mode = "sessions"
                cursor = saved_session_cursor
                status_banner = "Vista: Command Center AGY"
            elif ch in (curses.KEY_UP, ord("k")):
                cursor = max(0, cursor - 1)
            elif ch in (curses.KEY_DOWN, ord("j")):
                cursor += 1
            elif ch in (10, 13, curses.KEY_ENTER) and vault_entries and 0 <= cursor < len(vault_entries):
                status_banner = open_vault_entry_in_tab(vault_entries[cursor], switch_to=True)
                curses.flushinp()
            elif ch == ord("r") and vault_entries and 0 <= cursor < len(vault_entries):
                ve = vault_entries[cursor]
                match_s = next((s for s in sessions if s.conversation_id.startswith(ve["short_id"])), None)
                if match_s is None:
                    match_s = AgySession(
                        conversation_id=str(ve["conv_id"]),
                        title=str(ve["title"]),
                        preview="",
                        step_count=0,
                        last_modified="",
                        workspace=str(ve["workspace"]),
                        db_status="CASCADE_RUN_STATUS_IDLE",
                        not_fully_idle=False,
                    )
                status_banner = start_async_compaction(match_s, open_when_done=False, prompt_if_missing_key=True)
            elif ch in (ord("A"), ord("a")):
                heavy = [s for s in sessions if should_offer_compaction(s)[0] or s.step_count >= COMPACT_MIN_STEPS]
                if not heavy:
                    status_banner = "No hay sesiones pesadas para compactar al Baúl."
                else:
                    started = 0
                    for hs in heavy[:12]:
                        msg_s = start_async_compaction(hs, open_when_done=False, prompt_if_missing_key=(started == 0))
                        if msg_s.startswith("⏳ Compactando"):
                            started += 1
                        elif "falta" in msg_s.lower():
                            status_banner = msg_s
                            break
                    if started:
                        status_banner = f"📦 Compactando {started} sesiones pesadas en 2º plano al Baúl NV (sin abrir pestañas)."
            elif ch in (ord("x"), curses.KEY_DC, 127) and vault_entries and 0 <= cursor < len(vault_entries):
                ve = vault_entries[cursor]
                if delete_vault_handoff(Path(ve["path"])):
                    status_banner = f"🗑️ Eliminado '{ve['title'][:28]}' del Baúl."
                    if cursor > 0:
                        cursor -= 1
            elif ch in (ord("q"), 9):
                tmux_cmd("next-window")
            continue

        if mode == "sessions" and pending_compact_sess is not None:
            if ch == ord("c"):
                status_banner = execute_action("compact")
            elif ch == ord("C"):
                status_banner = execute_action("vault_compact")
            elif ch in (ord("b"), ord("B")):
                status_banner = execute_action("open_vault")
            elif ch in (10, 13, curses.KEY_ENTER, ord("o"), ord("O")):
                status_banner = execute_action("open_full")
            elif ch in (ord("k"), ord("K")):
                status_banner = execute_action("nvapi_key")
            elif ch in (27, ord("n"), ord("N"), ord("q")):
                pending_compact_sess = None
                status_banner = "Reactivación cancelada."
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
        elif ch in (ord("V"),):
            status_banner = execute_action("vault")
        elif mode == "sessions" and ch in (10, 13, curses.KEY_ENTER):
            status_banner = execute_action("open")
        elif mode == "sessions" and ch == ord("c"):
            status_banner = execute_action("compact")
        elif mode == "sessions" and ch == ord("C"):
            status_banner = execute_action("vault_compact")
        elif mode == "sessions" and ch == ord("t"):
            status_banner = execute_action("rename")
        elif mode == "sessions" and ch == ord("K"):
            status_banner = execute_action("nvapi_key")
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
    detach_only = getattr(args, "detach", False) or not sys.stdin.isatty()

    if TMUX_SOCKET in os.environ.get("TMUX", ""):
        ensure_dash_window_zero()
        tmux_cmd("source-file", str(TMUX_CONF))
        if not detach_only:
            _select_dash_window()
        return 0

    has = tmux_cmd("has-session", "-t", TMUX_SESSION, capture=True)
    if has.returncode != 0:
        tui_cmd = f"exec python3 {sh_quote(str(self_path))} tui"
        tmux_cmd("new-session", "-d", "-s", TMUX_SESSION, "-n", "⚡DASH", tui_cmd)
        for s in load_sessions(limit=20)[2]:
            if s.hibernated:
                open_session_in_dashboard_tab(s, switch_to=False, as_stub=True)
        _select_dash_window()
    else:
        ensure_dash_window_zero()
        tmux_cmd("source-file", str(TMUX_CONF))

    if detach_only:
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

    p_ct = sub.add_parser("close-tab", help="Safely close tab `×` (parks active RUNNING/BG-TASK tabs in agy-bg)")
    p_ct.add_argument("window_index")
    p_ct.set_defaults(fn=cmd_close_tab)

    p_sd = sub.add_parser("show-dash", help="Ensure ⚡DASH exists and switch focus to it (Alt+D / ⚡AGY DASH button)")
    p_sd.add_argument("--toggle", action="store_true", help="Return to previous tab when already viewing DASH")
    p_sd.set_defaults(fn=cmd_show_dash)

    for cname in ("accounts", "account"):
        p_ac = sub.add_parser(cname, help="Manage multi-account Google Pro slots (list|add|use|remove)")
        p_ac.add_argument("account_cmd", nargs="?", default="list", choices=["list", "add", "use", "remove"])
        p_ac.add_argument("slot", nargs="?", default="")
        p_ac.add_argument("--tab", action="store_true", help="Open interactive login in a new dashboard tmux tab")
        p_ac.add_argument("--yes", action="store_true", help="Confirm local profile removal non-interactively")
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
    p_bt.add_argument("--check", action="store_true", help="Run Baton Pass preflight checks only (no changes)")
    p_bt.add_argument("--json", action="store_true", help="Emit JSON output for `--check`")
    p_bt.set_defaults(fn=cmd_baton)

    p_nv = sub.add_parser("nvapi", help="Configure NVIDIA NIM API key & model for external context compaction")
    p_nv.add_argument("nvapi_cmd", nargs="?", default="status", choices=["status", "set-key"])
    p_nv.add_argument("value", nargs="?", default="", help="NVIDIA API Key (`nvapi-...`) for `set-key`")
    p_nv.add_argument("--model", default="", help="NVIDIA NIM model (default: nvidia/llama-3.1-nemotron-ultra-253b-v1)")
    p_nv.add_argument("--base-url", default="", help="Custom OpenAI-compatible NIM base URL")
    p_nv.set_defaults(fn=cmd_nvapi)

    p_cp = sub.add_parser("compact", help="Compact a heavy conversation via NVIDIA NIM (Nemotron) before reopening")
    p_cp.add_argument("target", nargs="?", default="self", help="Conversation ID prefix, title substring, or 'self'")
    p_cp.add_argument("--model", default="", help="Override NVIDIA NIM model for this compaction")
    p_cp.add_argument("--open", action="store_true", help="Open a new lightweight tab pre-loaded with the compacted handoff")
    p_cp.add_argument("--bg", action="store_true", help="Run compaction detached in background with tmux progress notice")
    p_cp.add_argument("--_bg-worker", dest="bg_worker", action="store_true", help=argparse.SUPPRESS)
    p_cp.set_defaults(fn=cmd_compact)

    p_rn = sub.add_parser("rename", help="Permanently rename a conversation across dashboard and LED tabs")
    p_rn.add_argument("target", help="Conversation ID prefix or title substring")
    p_rn.add_argument("new_title", help="New custom title for the conversation")
    p_rn.set_defaults(fn=cmd_rename)

    p_hb = sub.add_parser("heartbeat-hook", help="AGY PreInvocation hook: emit 5-min context heartbeat notice")
    p_hb.add_argument("--conv-id", default="", help="Optional conversation_id override when testing outside AGY")
    p_hb.add_argument("--force", action="store_true", help="Force heartbeat emission ignoring 5-min cooldown")
    p_hb.add_argument("--status", action="store_true", help="Print full heartbeat JSON instead of hook output")
    p_hb.set_defaults(fn=cmd_heartbeat_hook)

    p_sc = sub.add_parser("self-compact", help="Compact current session with Nemotron 550B and respawn in-place")
    p_sc.add_argument("target", nargs="?", default="self", help="Conversation ID prefix or 'self'")
    p_sc.add_argument("--cwd", default="", help="Workspace directory")
    p_sc.add_argument("--resume-prompt", default="", help="Continuation instruction for after compaction")
    p_sc.add_argument("--model", default="", help="Override NVIDIA NIM model")
    p_sc.add_argument("--win", default="", help="Explicit tmux window index to respawn in-place")
    p_sc.add_argument("--no-in-place", dest="in_place", action="store_false", help="Open a new tab instead of replacing current tab")
    p_sc.add_argument("--no-respawn", action="store_true", help="Compact and write handoff/status without respawning tmux tab")
    p_sc.add_argument("--respawn-only", action="store_true", help="Skip NVAPI call and respawn tmux tab from existing vault handoff")
    p_sc.add_argument("--bg", action="store_true", help="Run detached in background")
    p_sc.add_argument("--_bg-worker", dest="bg_worker", action="store_true", help=argparse.SUPPRESS)
    p_sc.set_defaults(fn=cmd_self_compact, in_place=True)

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
