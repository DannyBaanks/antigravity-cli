"""agy_dash.sessions — split verbatim from agy_dashboard.py @ f255869 (Phase 1, no behaviour change)."""
from __future__ import annotations

import datetime as dt
import json
import os
import re
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
                "ts": str(obj.get("created_at", ""))[11:19],
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
                    # Same conversation in several DBs (isolated accounts): title/steps from the richer row,
                    # status/not_fully_idle from the most recently modified one (it is the live truth).
                    prev_steps = int(prev_row[3] or 0)
                    new_steps = int(row[3] or 0)
                    newer = row if str(row[4] or "") >= str(prev_row[4] or "") else prev_row
                    richer = row if (new_steps > prev_steps or (not prev_row[1] and row[1])) else prev_row
                    merged_db_rows[cid] = (
                        cid,
                        richer[1] or prev_row[1] or row[1],
                        richer[2] or prev_row[2] or row[2],
                        max(prev_steps, new_steps),
                        max(str(prev_row[4] or ""), str(row[4] or "")),
                        richer[5] or prev_row[5] or row[5],
                        newer[6],
                        newer[7],
                    )
            conn.close()
        except Exception:
            pass

    missing_live = [cid for cid in live_by_conv if cid not in merged_db_rows]
    if missing_live:
        for db_path in _all_summaries_dbs():
            try:
                conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
                marks = ",".join("?" * len(missing_live))
                for row in conn.execute(
                    f"""SELECT conversation_id, title, preview, step_count, last_modified_time, workspace_uris,
                               status, not_fully_idle FROM conversation_summaries
                        WHERE conversation_id IN ({marks})""",
                    missing_live,
                ):
                    prev_row = merged_db_rows.get(row[0])
                    if prev_row is None or str(row[4] or "") >= str(prev_row[4] or ""):
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
                    _tmuxctl.tmux_cmd("set-window-option", "-t", f"{_paths.TMUX_SESSION}:{win_idx}", "@agy_conv_id", cid)
            bg_idx, _ = bg_map.get(cid, (None, ""))
            fallback_title = custom_titles.get(cid) or f"Sesión activa {cid[:8]}"
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
            )

    def sort_key(s: AgySession) -> tuple[int, str]:
        if s.proc is not None:
            rank = 0 if s.state_label == "RUNNING" else (1 if s.state_label in ("BG-TASK", "UNKNOWN") else 2)
            return (rank, s.last_modified)
        if s.hibernated:
            return (3, s.last_modified)
        return (4, "")

    live_and_hib = [s for s in rows_by_id.values() if s.proc is not None or s.hibernated]
    saved = [s for s in rows_by_id.values() if s.proc is None and not s.hibernated]
    live_and_hib.sort(key=sort_key)
    return scan_us, ksm_stats, (live_and_hib + saved)[:limit]


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
