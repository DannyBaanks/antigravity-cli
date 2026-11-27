"""agy_dash.sessions — split verbatim from agy_dashboard.py @ f255869 (Phase 1, no behaviour change)."""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
import urllib.error
import urllib.parse
from collections.abc import Callable
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from . import paths as _paths
from . import procs as _procs
from . import tmuxctl as _tmuxctl


# `status` column of conversation_summaries.db holds CASCADE_RUN_STATUS_{RUNNING,IDLE,...}. A live process
# whose conversation has no row there gets DB_STATUS_UNKNOWN: never treated as idle.
DB_STATUS_UNKNOWN = "UNKNOWN"


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
    proc: _procs.ProcMemInfo | None = None
    extra_procs: list[_procs.ProcMemInfo] = field(default_factory=list)
    hibernated: bool = False
    hibernated_saved_kb: int = 0
    dash_win_idx: str | None = None
    dash_win_name: str = ""
    bg_win_idx: str | None = None
    transcript_busy: bool = False
    last_modified_ts: float = 0.0

    @property
    def state_label(self) -> str:
        if self.proc is not None:
            if self.db_status == "CASCADE_RUN_STATUS_RUNNING" or self.transcript_busy:
                return "RUNNING"
            if self.db_status == DB_STATUS_UNKNOWN:
                return "UNKNOWN"
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
        if _procs.is_ancestor_pid(self.proc.pid):
            return False
        if self.db_status == "CASCADE_RUN_STATUS_RUNNING" or self.transcript_busy:
            return False
        if self.db_status == DB_STATUS_UNKNOWN:
            return False  # no row in conversation_summaries.db: idleness cannot be proven (Regla de oro 1)
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


def refresh_session(conv_id: str) -> "AgySession | None":
    """Fresh state of one conversation right before a destructive action (snapshots go stale in seconds)."""
    _, _, sessions = load_sessions(limit=200)
    return next((s for s in sessions if s.conversation_id == conv_id), None)


def transcript_path_for(conv_id: str) -> Path:
    primary_p = _paths.BRAIN_DIR / conv_id / ".system_generated" / "logs" / "transcript.jsonl"
    if primary_p.is_file():
        return primary_p
    if _paths.ACCOUNTS_DIR.is_dir():
        for alt in _paths.ACCOUNTS_DIR.glob(f"*/home/.gemini/antigravity-cli/brain/{conv_id}/.system_generated/logs/transcript.jsonl"):
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
            if stype == "ERROR_MESSAGE":
                # Hook termination (e.g. autocompact) or SDK error ended the turn without a planner reply
                busy = False
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


_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def strip_controls(text: str) -> str:
    """Transcript text is untrusted (tool output, web pages): drop C0/C1 controls (ESC, OSC 52, DCS...)
    before it reaches a terminal."""
    return _CONTROL_RE.sub("", text)


def zig_tail_transcript(conv_id: str, max_turns: int = 12, include_tools: bool = True) -> list[dict]:
    """Use `agy-fast-core tail-lines` (Zig) to read the tail of transcript.jsonl in <1 ms and parse in Python."""
    tpath = transcript_path_for(conv_id)
    if not tpath.is_file():
        return []
    raw_lines: list[str] = []
    if _procs.ensure_zig_core():
        try:
            cp = subprocess.run(
                [str(_paths.ZIG_CORE_BIN), "tail-lines", str(tpath), str(max_turns * 4)],
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
                "content": strip_controls(re.sub(r"\s+", " ", content))[:260],
                "tools": [strip_controls(t) for t in tools[:4]],
                "ts": _format_local_hhmmss(obj.get("created_at", "")),
                "ts_local": _format_local_hhmm(obj.get("created_at", "")),
            }
        )
    return parsed[-max_turns:]


def print_instant_zig_preview(conv_id: str, title: str, workspace: str) -> None:
    """Print <4ms conversation preview before `agy --conversation` finishes loading its 14MB DB."""
    turns = zig_tail_transcript(conv_id, max_turns=8)
    print("\033[2J\033[H", end="")
    print("\033[1;36m╔══════════════════════════════════════════════════════════════════════════════════════════╗\033[0m")
    title = strip_controls(title)
    backend = "ZIG" if _procs.ensure_zig_core() else "Python"
    print(f"\033[1;36m║\033[0m ⚡ \033[1;32mVISTA INSTANTÁNEA {backend:<6}\033[0m — \033[1;37m{title[:54]:<54}\033[0m \033[1;36m║\033[0m")
    print(f"\033[1;36m║\033[0m 📂 Workspace: {strip_controls(workspace)[:74]:<74} \033[1;36m║\033[0m")
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
    gomem = os.environ.get("GOMEMLIMIT", _paths.DEFAULT_GOMEMLIMIT)
    print(f"\033[1;35m  ⏳ Sincronizando motor TUI interactivo de AGY (KSM + GOMEMLIMIT={gomem})...\033[0m", flush=True)


def load_hibernated_registry() -> dict[str, dict]:
    if not _paths.HIBERNATED_FILE.is_file():
        return {}
    try:
        return json.loads(_paths.HIBERNATED_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_hibernated_registry(reg: dict[str, dict]) -> None:
    _paths.atomic_write_text(_paths.HIBERNATED_FILE, json.dumps(reg, ensure_ascii=False, indent=2))


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
    p = titles_path or _paths.CUSTOM_TITLES_FILE
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in data.items() if str(v).strip()} if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_custom_title(conv_id: str, new_title: str, *, titles_path: Path | None = None) -> str:
    p = titles_path or _paths.CUSTOM_TITLES_FILE
    clean = re.sub(r"\s+", " ", new_title.strip())[:60]
    if not clean:
        return ""
    with _paths.file_lock(p):
        titles = load_custom_titles(titles_path=p)
        titles[conv_id] = clean
        _paths.atomic_write_text(p, json.dumps(titles, ensure_ascii=False, indent=2))

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


_ISO_FRAC_RE = re.compile(r"(\.\d{6})\d+")


def _parse_iso_ts(raw: str | None) -> float:
    """Parse an ISO-8601 timestamp (including Go nanosecond SQLite strings) into UTC epoch seconds."""
    s = str(raw or "").strip()
    if not s or s.startswith("0001-"):
        return 0.0
    s = _ISO_FRAC_RE.sub(r"\1", s.replace(" ", "T", 1))
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(s)
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    ts = parsed.timestamp()
    return ts if ts > 0.0 else 0.0


def _format_utc_ts(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _format_local_hhmmss(raw: str | None) -> str:
    """Convert an ISO UTC timestamp to display-timezone (CDMX) HH:MM:SS."""
    ts = _parse_iso_ts(raw)
    if ts > 0.0:
        return _paths.format_display_clock(ts, fmt="%H:%M:%S")
    s = str(raw or "")
    return s[11:19] if len(s) >= 19 else ""


def _format_local_hhmm(raw: str | None) -> str:
    """Convert an ISO UTC timestamp to display-timezone (CDMX) HH:MM for human-friendly chat previews."""
    ts = _parse_iso_ts(raw)
    if ts > 0.0:
        return _paths.format_display_clock(ts, fmt="%H:%M")
    s = str(raw or "")
    return s[11:16] if len(s) >= 16 else ""


def _all_conversations_dirs() -> list[Path]:
    dirs: list[Path] = []
    if _paths.CONVERSATIONS_DIR.is_dir():
        dirs.append(_paths.CONVERSATIONS_DIR)
    if _paths.ACCOUNTS_DIR.is_dir():
        for p in _paths.ACCOUNTS_DIR.glob("*/home/.gemini/antigravity-cli/conversations"):
            if p.is_dir() and not p.is_symlink():
                dirs.append(p)
    return dirs


def _scan_conversation_file_mtimes() -> dict[str, float]:
    """Return {conversation_id: max mtime} across conversations/*.db|.db-wal|.pb in one scandir."""
    mtimes: dict[str, float] = {}
    for cdir in _all_conversations_dirs():
        try:
            with os.scandir(cdir) as it:
                for entry in it:
                    name = entry.name
                    if not name.endswith((".db", ".db-wal", ".pb")):
                        continue
                    cid = name.split(".", 1)[0]
                    if not cid:
                        continue
                    try:
                        st = entry.stat()
                    except OSError:
                        continue
                    # SQLite in WAL mode touches a 0-byte .db-wal even on read-only opens (e.g. summary
                    # reconciliation on old conversations): only non-empty WAL files carry real writes.
                    if name.endswith(".db-wal") and st.st_size == 0:
                        continue
                    m = st.st_mtime
                    if m > mtimes.get(cid, 0.0):
                        mtimes[cid] = m
        except OSError:
            continue
    return mtimes


def _transcript_mtime(conv_id: str) -> float:
    try:
        return transcript_path_for(conv_id).stat().st_mtime
    except OSError:
        return 0.0


def _all_summaries_dbs() -> list[Path]:
    dbs: list[Path] = []
    if _paths.SUMMARIES_DB.is_file():
        dbs.append(_paths.SUMMARIES_DB)
    if _paths.ACCOUNTS_DIR.is_dir():
        for p in _paths.ACCOUNTS_DIR.glob("*/home/.gemini/antigravity-cli/conversation_summaries.db"):
            if p.is_file() and not p.is_symlink():
                dbs.append(p)
    return dbs


def load_sessions(limit: int = 40) -> tuple[int, dict[str, int], list[AgySession]]:
    scan_us, ksm_stats, live_by_conv = _procs.zig_scan()
    tmux_timeouts = _tmuxctl.TMUX_STATE["timeouts"]
    win_map = _tmuxctl._get_dash_window_map()
    tty_map = _tmuxctl._get_dash_tty_map()
    bg_map = _tmuxctl._get_bg_window_map()
    if _tmuxctl.TMUX_STATE["timeouts"] != tmux_timeouts:
        # Without the tab maps a session in a tab looks "not open": as unreliable as a failed /proc scan.
        _procs._mark_scan(True, f"tmux no respondió en {_tmuxctl.TMUX_TIMEOUT_SEC:g} s")
    hib_reg = load_hibernated_registry()
    custom_titles = load_custom_titles()
    conv_mtimes = _scan_conversation_file_mtimes()

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
    dbs = _all_summaries_dbs()
    sql_limit = max(limit, 80)
    for db_path in dbs:
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
                (sql_limit,),
            )
            for row in cur.fetchall():
                cid = row[0]
                prev_row = merged_db_rows.get(cid)
                if prev_row is None:
                    merged_db_rows[cid] = row
                else:
                    # Same conversation in several DBs (isolated accounts): title/steps from the richer row,
                    # status/not_fully_idle from the most recently modified one (it is the live truth).
                    prev_steps = int(prev_row[3] or 0)
                    new_steps = int(row[3] or 0)
                    newer = row if _parse_iso_ts(row[4]) >= _parse_iso_ts(prev_row[4]) else prev_row
                    richer = row if (new_steps > prev_steps or (not prev_row[1] and row[1])) else prev_row
                    merged_db_rows[cid] = (
                        cid,
                        richer[1] or prev_row[1] or row[1],
                        richer[2] or prev_row[2] or row[2],
                        max(prev_steps, new_steps),
                        newer[4],
                        richer[5] or prev_row[5] or row[5],
                        newer[6],
                        newer[7],
                    )
            conn.close()
        except Exception:
            pass

    missing_live = [cid for cid in live_by_conv if cid not in merged_db_rows]
    top_disk_cids = sorted(conv_mtimes, key=conv_mtimes.__getitem__, reverse=True)[:sql_limit]
    missing_recent = list(
        dict.fromkeys(
            cid
            for cid in (*top_disk_cids, *hib_reg.keys())
            if cid not in merged_db_rows and cid not in live_by_conv
        )
    )
    if missing_live or missing_recent:
        for db_path in dbs:
            try:
                conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
                if missing_live:
                    marks = ",".join("?" * len(missing_live))
                    for row in conn.execute(
                        f"""SELECT conversation_id, title, preview, step_count, last_modified_time, workspace_uris,
                                   status, not_fully_idle FROM conversation_summaries
                            WHERE conversation_id IN ({marks})""",
                        missing_live,
                    ):
                        prev_row = merged_db_rows.get(row[0])
                        if prev_row is None or _parse_iso_ts(row[4]) >= _parse_iso_ts(prev_row[4]):
                            merged_db_rows[row[0]] = row
                if missing_recent:
                    marks = ",".join("?" * len(missing_recent))
                    for row in conn.execute(
                        f"""SELECT conversation_id, title, preview, step_count, last_modified_time, workspace_uris,
                                   status, not_fully_idle FROM conversation_summaries
                            WHERE nesting_depth = 0 AND killed = 0 AND conversation_id IN ({marks})""",
                        missing_recent,
                    ):
                        prev_row = merged_db_rows.get(row[0])
                        if prev_row is None or _parse_iso_ts(row[4]) >= _parse_iso_ts(prev_row[4]):
                            merged_db_rows[row[0]] = row
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
                _tmuxctl.tmux_cmd("set-window-option", "-t", f"{_paths.TMUX_SESSION}:{win_idx}", "@agy_conv_id", cid)
        bg_idx, _ = bg_map.get(cid, (None, ""))
        t_busy = is_transcript_turn_active(cid) if primary is not None else False
        clean_title = custom_titles.get(cid) or (title or "").strip() or f"Sesión {cid[:8]}"
        clean_prev = re.sub(r"\s+", " ", (preview or "").strip())[:100]
        is_hib = hib_info is not None and primary is None
        # Effective recency: AGY's conversation_summaries.db can lag behind live turns or be
        # overwritten backwards by background summary reconciliation; transcript.jsonl and
        # conversations/<cid>.db mtimes on disk preserve the true latest activity time.
        db_ts = _parse_iso_ts(lastmod)
        disk_ts = max(conv_mtimes.get(cid, 0.0), _transcript_mtime(cid))
        hib_ts = _parse_iso_ts((hib_info or {}).get("hibernated_at")) if is_hib else 0.0
        eff_ts = max(db_ts, disk_ts) or hib_ts
        eff_lastmod = _format_utc_ts(eff_ts) if eff_ts > db_ts else str(lastmod or "")[:19]
        rows_by_id[cid] = AgySession(
            conversation_id=cid,
            title=clean_title,
            preview=clean_prev,
            step_count=int(steps or 0),
            last_modified=eff_lastmod,
            workspace=parse_workspace_uri(ws_uris),
            db_status=str(status or ""),
            not_fully_idle=bool(not_idle),
            proc=primary,
            extra_procs=extras,
            hibernated=is_hib,
            hibernated_saved_kb=int((hib_info or {}).get("saved_pss_kb", 250_000)),
            dash_win_idx=win_idx,
            dash_win_name=win_name,
            bg_win_idx=bg_idx,
            transcript_busy=t_busy,
            last_modified_ts=eff_ts,
        )

    now_ts = time.time()
    for cid, procs in live_by_conv.items():
        if cid not in rows_by_id and procs:
            win_idx, win_name = win_map.get(cid, (None, ""))
            if win_idx is None and procs[0].tty in tty_map:
                win_idx, win_name = tty_map[procs[0].tty]
                if "DASH" not in win_name:
                    _tmuxctl.tmux_cmd("set-window-option", "-t", f"{_paths.TMUX_SESSION}:{win_idx}", "@agy_conv_id", cid)
            bg_idx, _ = bg_map.get(cid, (None, ""))
            fallback_title = custom_titles.get(cid) or f"Sesión activa {cid[:8]}"
            eff_ts = max(conv_mtimes.get(cid, 0.0), _transcript_mtime(cid), now_ts)
            rows_by_id[cid] = AgySession(
                conversation_id=cid,
                title=fallback_title,
                preview="",
                step_count=0,
                last_modified="ahora",
                workspace=str(Path.cwd()),
                db_status=DB_STATUS_UNKNOWN,
                not_fully_idle=True,
                proc=procs[0],
                extra_procs=procs[1:],
                dash_win_idx=win_idx,
                dash_win_name=win_name,
                bg_win_idx=bg_idx,
                transcript_busy=is_transcript_turn_active(cid),
                last_modified_ts=eff_ts,
            )

    def sort_key(s: AgySession) -> tuple[int, float, str, str]:
        if s.proc is not None:
            rank = 0 if s.state_label == "RUNNING" else (1 if s.state_label in ("BG-TASK", "UNKNOWN") else 2)
        elif s.hibernated:
            rank = 3
        else:
            rank = 4
        # Newest first inside every tier (RUNNING, BG-TASK/UNKNOWN, IDLE, HIBERNATED, SAVED).
        return (rank, -s.last_modified_ts, "" if s.last_modified == "ahora" else s.last_modified, s.conversation_id)

    ordered = sorted(rows_by_id.values(), key=sort_key)
    return scan_us, ksm_stats, ordered[:limit]


def hibernate_session(session: AgySession, *, fresh: bool = True) -> tuple[bool, str]:
    if _procs.scan_failed():
        return False, f"No se hiberna: el escaneo de procesos falló ({_procs.SCAN_STATE.get('reason')}); estados no fiables."
    if fresh and session.proc is not None:
        # The TUI snapshot can be seconds old: a turn may have started since. Re-read before killing.
        cur = refresh_session(session.conversation_id)
        if cur is not None:
            session = cur
    if session.proc is None:
        return False, f"La sesión {session.conversation_id[:8]} no está en RAM."
    if not session.can_hibernate:
        reason = (
            "es la sesión actual"
            if _procs.is_ancestor_pid(session.proc.pid)
            else f"estado={session.state_label} (tiene turno o tarea activa)"
        )
        return False, f"No se hiberna {session.conversation_id[:8]} ({session.title}): {reason}."

    all_procs = [session.proc] + list(session.extra_procs)
    saved_pss = sum(p.pss_kb + p.swap_kb for p in all_procs)
    failures = []
    for p in all_procs:
        ok, why = _procs.terminate_verified(p)
        if not ok:
            failures.append(why)
    if failures:
        closed = len(all_procs) - len(failures)
        partial = f" ({closed} de {len(all_procs)} copias sí se cerraron)" if closed else ""
        return False, f"No se hibernó '{session.title}' ({session.conversation_id[:8]}){partial}: {'; '.join(failures)}"

    hib_reg = load_hibernated_registry()
    hib_reg[session.conversation_id] = {
        "conversation_id": session.conversation_id,
        "title": session.title,
        "workspace": session.workspace,
        "step_count": session.step_count,
        "saved_pss_kb": saved_pss,
        "hibernated_pid": session.proc.pid,
        "hibernated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    save_hibernated_registry(hib_reg)
    return True, (
        f"Hibernada '{session.title}' ({session.conversation_id[:8]}) -> "
        f"liberados {saved_pss // 1024} MB RAM (ahora 0 MB)"
    )


def hibernate_batch(
    candidates: list[AgySession],
    on_ok: Callable[[AgySession], object] | None = None,
) -> tuple[int, int, list[tuple[bool, str]]]:
    """Hibernate several sessions with ONE fresh scan for the whole batch (not one full scan per
    session). Each candidate is re-checked against that scan; the freed KB come from the fresh
    objects, not from the caller's snapshot. Returns (count, freed_kb, [(ok, msg), ...]).
    `on_ok(fresh_session)` runs after each success (the caller swaps the tab for a wake stub)."""
    if not candidates:
        return 0, 0, []
    _, _, sessions = load_sessions(limit=200)
    fresh_by_cid = {s.conversation_id: s for s in sessions}
    count = freed_kb = 0
    results: list[tuple[bool, str]] = []
    for cand in candidates:
        cur = fresh_by_cid.get(cand.conversation_id)
        if cur is None or cur.proc is None:
            results.append((False, f"La sesión {cand.conversation_id[:8]} ya no está en RAM."))
            continue
        saved = sum(p.pss_kb + p.swap_kb for p in [cur.proc, *cur.extra_procs])
        ok, msg = hibernate_session(cur, fresh=False)
        results.append((ok, msg))
        if ok:
            count += 1
            freed_kb += saved
            if on_ok is not None:
                on_ok(cur)
    return count, freed_kb, results


def hibernate_all_idle(
    sessions: list[AgySession],
    on_ok: Callable[[AgySession], object] | None = None,
) -> tuple[int, int, list[tuple[bool, str]]]:
    """Hibernate every session the snapshot shows as hibernatable (TUI [H] and `hibernate all-idle`)."""
    return hibernate_batch([s for s in sessions if s.can_hibernate], on_ok)


def _dup_kill_ok(session: AgySession) -> bool:
    """Duplicates are only closed when the whole conversation is quiet (no turn, no background task)."""
    return not (
        session.db_status == "CASCADE_RUN_STATUS_RUNNING"
        or session.transcript_busy
        or session.not_fully_idle
    )


def deduplicate_session_procs(session: AgySession) -> int:
    """Close idle duplicate copies when a session already has a primary process (manual action only)."""
    if not session.extra_procs or _procs.scan_failed():
        return 0
    session = refresh_session(session.conversation_id) or session
    if not _dup_kill_ok(session):
        return 0
    freed_kb = 0
    for ep in list(session.extra_procs):
        if not ep.in_dash and not ep.active_children:
            ok, _ = _procs.terminate_verified(ep)
            if ok:
                freed_kb += ep.pss_kb + ep.swap_kb
    return freed_kb


def close_stale_dash_duplicates(session: AgySession) -> int:
    """Close a stale in-dash duplicate `agy` when external `ext:pts/*` holds active work (manual only)."""
    all_procs = ([session.proc] if session.proc else []) + list(session.extra_procs)
    if _procs.scan_failed() or not any((not p.in_dash) and p.active_children for p in all_procs):
        return 0
    session = refresh_session(session.conversation_id) or session
    all_procs = ([session.proc] if session.proc else []) + list(session.extra_procs)
    if not any((not p.in_dash) and p.active_children for p in all_procs):
        return 0
    # The external copy holds a background task (so not_fully_idle is expected), but a running turn could be
    # the in-dash copy's own: never close anything mid-turn.
    if session.db_status == "CASCADE_RUN_STATUS_RUNNING" or session.transcript_busy:
        return 0
    freed_kb = 0
    for p in all_procs:
        if p.in_dash and not p.active_children:
            ok, _ = _procs.terminate_verified(p)
            if ok:
                freed_kb += p.pss_kb + p.swap_kb
    return freed_kb


_SAFE_CONV_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,127}$")


def _all_data_roots(data_dir: Path | None = None) -> list[Path]:
    """Primary AGY data dir plus any isolated account data dir that is not a symlink to it."""
    base = data_dir or _paths.AGY_DATA_DIR
    roots: list[Path] = []
    seen: set[Path] = set()
    if base.is_dir():
        res = base.resolve(strict=False)
        roots.append(base)
        seen.add(res)
    if data_dir is None and _paths.ACCOUNTS_DIR.is_dir():
        for p in _paths.ACCOUNTS_DIR.glob("*/home/.gemini/antigravity-cli"):
            if p.is_dir() and not p.is_symlink():
                res = p.resolve(strict=False)
                if res not in seen:
                    roots.append(p)
                    seen.add(res)
    return roots


def _safe_child(parent: Path, name: str) -> Path | None:
    """Return `parent / name` only when it resolves strictly inside `parent`."""
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        return None
    p = parent / name
    try:
        parent_res = parent.resolve(strict=False)
        child_res = p.resolve(strict=False)
        if not p.is_symlink() and not child_res.is_relative_to(parent_res):
            return None
        if p.parent.resolve(strict=False) != parent_res:
            return None
    except OSError:
        return None
    return p


def _collect_conversation_artifacts(conv_id: str, data_dir: Path | None = None) -> tuple[list[Path], list[Path]]:
    """Return (files, dirs) belonging strictly to `conv_id` across AGY data roots (excluding Vault)."""
    files: list[Path] = []
    dirs: list[Path] = []
    seen_files: set[Path] = set()
    seen_dirs: set[Path] = set()

    def _add_file(parent: Path, name: str) -> None:
        if not parent.is_dir() or parent.is_symlink():
            return
        fp = _safe_child(parent, name)
        if fp is None or not (fp.is_file() or fp.is_symlink()):
            return
        key = fp.resolve(strict=False) if not fp.is_symlink() else fp
        if key not in seen_files:
            seen_files.add(key)
            files.append(fp)

    def _add_dir(parent: Path, name: str) -> None:
        if not parent.is_dir() or parent.is_symlink():
            return
        dp = _safe_child(parent, name)
        if dp is None or not dp.is_dir() or dp.is_symlink():
            return
        key = dp.resolve(strict=False)
        if key not in seen_dirs:
            seen_dirs.add(key)
            dirs.append(dp)

    for root in _all_data_roots(data_dir):
        cdir = root / "conversations"
        for ext in (".db", ".db-wal", ".db-shm", ".pb"):
            _add_file(cdir, f"{conv_id}{ext}")
        _add_dir(root / "brain", conv_id)
        _add_file(root / "annotations", f"{conv_id}.pbtxt")
        _add_file(root / "implicit", f"{conv_id}.pb")
        _add_file(root / "presence", f"{conv_id}.lock")
        hb_dir = root / "heartbeats"
        _add_file(hb_dir, f"{conv_id[:16]}.json")
        _add_file(hb_dir, f"{conv_id}.autocompact")

    return files, dirs


def _path_size_bytes(p: Path) -> int:
    try:
        if p.is_symlink() or p.is_file():
            return p.lstat().st_size
        if p.is_dir():
            total = 0
            for root_str, _, filenames in os.walk(p, followlinks=False):
                rp = Path(root_str)
                for fn in filenames:
                    try:
                        total += (rp / fn).lstat().st_size
                    except OSError:
                        pass
            return total
    except OSError:
        return 0
    return 0


def _delete_block_reason(session: AgySession) -> str:
    if _procs.scan_failed():
        return f"Escaneo de procesos falló ({_procs.SCAN_STATE.get('reason')}); estados no fiables."
    cid = session.conversation_id
    if not _SAFE_CONV_ID_RE.match(cid) or ".." in cid:
        return f"ID de conversación inválido ({cid!r})."
    all_procs = ([session.proc] if session.proc else []) + list(session.extra_procs)
    for p in all_procs:
        if _procs.is_ancestor_pid(p.pid):
            return "No puedes eliminar la conversación actual mientras está en uso."
    if session.proc is not None:
        if session.db_status == "CASCADE_RUN_STATUS_RUNNING" or session.transcript_busy:
            return "La conversación tiene un turno activo; espera a que termine antes de eliminarla."
        if session.db_status == DB_STATUS_UNKNOWN:
            return "Estado de conversación desconocido (sin fila en DB); no se elimina por seguridad."
        if session.not_fully_idle or session.proc.active_children:
            return "La conversación tiene tareas en segundo plano activas; espera a que terminen."
    return ""


def inspect_session_deletion(
    session: AgySession,
    *,
    fresh: bool = False,
    data_dir: Path | None = None,
) -> dict:
    """Inspect safety guards, live processes/tabs, and disk footprint before deleting a conversation."""
    if fresh and data_dir is None:
        cur = refresh_session(session.conversation_id)
        if cur is not None:
            session = cur
    blocked = _delete_block_reason(session)
    files, dirs = _collect_conversation_artifacts(session.conversation_id, data_dir=data_dir)
    size_bytes = sum(_path_size_bytes(p) for p in files) + sum(_path_size_bytes(d) for d in dirs)
    return {
        "conversation_id": session.conversation_id,
        "title": session.title,
        "state_label": session.state_label,
        "workspace": session.workspace,
        "is_live": session.proc is not None,
        "is_hibernated": session.hibernated,
        "has_tab": session.dash_win_idx is not None or session.bg_win_idx is not None,
        "files_count": len(files),
        "has_brain": bool(dirs),
        "size_bytes": size_bytes,
        "blocked_reason": blocked,
    }


def _delete_from_summaries_dbs(conv_id: str, data_dir: Path | None = None) -> int:
    dbs = [data_dir / "conversation_summaries.db"] if data_dir is not None else _all_summaries_dbs()
    removed_rows = 0
    for db_p in dbs:
        if not db_p.is_file():
            continue
        conn = None
        try:
            conn = sqlite3.connect(str(db_p), timeout=2.0)
            cur = conn.execute("DELETE FROM conversation_summaries WHERE conversation_id = ?", (conv_id,))
            removed_rows += max(0, cur.rowcount)
            for sql, params in (
                ("DELETE FROM conversations_fts WHERE conversation_id = ?", (conv_id,)),
                ("DELETE FROM conversation_parents WHERE conversation_id = ? OR parent_id = ?", (conv_id, conv_id)),
            ):
                try:
                    conn.execute(sql, params)
                except sqlite3.OperationalError:
                    pass
            conn.commit()
        except Exception:
            pass
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
    return removed_rows


def _cleanup_dash_metadata(conv_id: str, data_dir: Path | None = None) -> None:
    hib_file = (data_dir / "dashboard_hibernated.json") if data_dir is not None else _paths.HIBERNATED_FILE
    if hib_file.is_file():
        with _paths.file_lock(hib_file):
            try:
                reg = json.loads(hib_file.read_text(encoding="utf-8"))
                if isinstance(reg, dict) and conv_id in reg:
                    del reg[conv_id]
                    _paths.atomic_write_text(hib_file, json.dumps(reg, ensure_ascii=False, indent=2))
            except Exception:
                pass

    titles_file = (data_dir / "custom_titles.json") if data_dir is not None else _paths.CUSTOM_TITLES_FILE
    if titles_file.is_file():
        with _paths.file_lock(titles_file):
            titles = load_custom_titles(titles_path=titles_file)
            if conv_id in titles:
                del titles[conv_id]
                _paths.atomic_write_text(titles_file, json.dumps(titles, ensure_ascii=False, indent=2))

    reg_file = (data_dir / "accounts" / "registry.json") if data_dir is not None else _paths.ACCOUNTS_REGISTRY
    if reg_file.is_file():
        with _paths.file_lock(reg_file):
            try:
                reg_data = json.loads(reg_file.read_text(encoding="utf-8"))
                conv_map = reg_data.get("conv_accounts") if isinstance(reg_data, dict) else None
                if isinstance(conv_map, dict) and conv_id in conv_map:
                    del conv_map[conv_id]
                    _paths.atomic_write_text(reg_file, json.dumps(reg_data, ensure_ascii=False, indent=2))
            except Exception:
                pass

    _TRANSCRIPT_BUSY_CACHE.pop(conv_id, None)


def delete_session(
    session: AgySession,
    *,
    confirmed: bool = False,
    fresh: bool = True,
    on_close_tabs: Callable[[AgySession], object] | None = None,
    data_dir: Path | None = None,
) -> tuple[bool, str]:
    """Permanently delete a single AGY conversation (DB row, conversations/<id>.*, brain/<id>/, metadata).

    Fail-closed: requires `confirmed=True`, refuses active/running/unknown/ancestor conversations,
    terminates idle processes verified BEFORE touching disk, and never deletes Vault handoffs.
    """
    if not confirmed:
        return False, f"⚠️ Confirmación requerida para eliminar '{session.title}' ({session.conversation_id[:8]})."
    if fresh and data_dir is None:
        cur = refresh_session(session.conversation_id)
        if cur is not None:
            session = cur
    blocked = _delete_block_reason(session)
    if blocked:
        return False, f"⚠️ No se eliminó '{session.title}' ({session.conversation_id[:8]}): {blocked}"

    cid = session.conversation_id
    all_procs = ([session.proc] if session.proc else []) + list(session.extra_procs)
    for p in all_procs:
        ok, why = _procs.terminate_verified(p)
        if not ok:
            return False, f"⚠️ No se eliminó '{session.title}' ({cid[:8]}): no se pudo cerrar su proceso ({why})."

    if on_close_tabs is not None:
        try:
            on_close_tabs(session)
        except Exception:
            pass

    files, dirs = _collect_conversation_artifacts(cid, data_dir=data_dir)
    freed_bytes = sum(_path_size_bytes(p) for p in files) + sum(_path_size_bytes(d) for d in dirs)
    errors: list[str] = []

    for fp in files:
        try:
            fp.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            errors.append(f"{fp.name}: {exc}")

    for dp in dirs:
        try:
            if dp.is_symlink():
                dp.unlink()
            else:
                shutil.rmtree(dp)
        except FileNotFoundError:
            pass
        except OSError as exc:
            errors.append(f"{dp.name}: {exc}")

    _delete_from_summaries_dbs(cid, data_dir=data_dir)
    _cleanup_dash_metadata(cid, data_dir=data_dir)

    if errors:
        return False, f"⚠️ Eliminación parcial de '{session.title}' ({cid[:8]}): {'; '.join(errors[:2])}"

    if freed_bytes >= 1024 * 1024:
        size_s = f" · liberados {freed_bytes / (1024 * 1024):.1f} MB"
    elif freed_bytes >= 1024:
        size_s = f" · liberados {freed_bytes // 1024} KB"
    else:
        size_s = ""
    return True, f"🗑️ Eliminada '{session.title[:36]}' ({cid[:8]}){size_s}."
