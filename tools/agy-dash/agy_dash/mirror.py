"""agy_dash.mirror — split verbatim from agy_dashboard.py @ f255869 (Phase 1, no behaviour change)."""
from __future__ import annotations

import curses
import os
import textwrap
import time
from pathlib import Path

from . import paths as _paths
from . import sessions as _sessions
from . import tmuxctl as _tmuxctl


# ============================================================================
# Real-Time Live Mirror View (`agy-dash mirror <conv_id>`)
# ============================================================================

def latest_active_task_log(conv_id: str, max_lines: int = 4) -> tuple[str, tuple[int, int], list[str]]:
    """Return (filename, (mtime_ns, size), tail_lines) for the newest task-*.log of conv_id."""
    # Same roots as transcript_path_for (isolated account homes included): <brain>/<conv>/.system_generated/
    tasks_dir = _sessions.transcript_path_for(conv_id).parent.parent / "tasks"
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
    sess: _sessions.AgySession | None = None
    last_update_str = _paths.format_display_clock(with_tz=True)
    status_msg = "En vivo (auto-refresco 0.4s). [r] Actualizar  [↑/↓] Scroll  [m] Migrar  [o] Abrir AGY  [d] ⚡DASH"
    force_refresh = True
    scroll_offset = 0

    while True:
        now = time.time()
        sig = _sessions.transcript_sig(conv_id)
        if force_refresh or sig != last_sig:
            last_sig = sig
            turns = _sessions.zig_tail_transcript(conv_id, max_turns=40, include_tools=True)
            last_update_str = _paths.format_display_clock(with_tz=True)
        tname, tsig, tlines = latest_active_task_log(conv_id, max_lines=4)
        if force_refresh or tsig != last_task_sig:
            last_task_sig = tsig
            task_name = tname
            task_lines = tlines
            last_update_str = _paths.format_display_clock(with_tz=True)
        if force_refresh or (now - last_scan) >= 1.5:
            last_scan = now
            _, _, sessions = _sessions.load_sessions(limit=30)
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
            _tmuxctl.tmux_cmd("refresh-client", "-S")
            status_msg = f"✅ Actualizado a las {_paths.format_display_clock(with_tz=True)} (transcript + {task_name or 'estado'})."
        elif ch in (curses.KEY_UP, ord("k")):
            scroll_offset += 2
        elif ch in (curses.KEY_DOWN, ord("j")):
            scroll_offset = max(0, scroll_offset - 2)
        elif ch == curses.KEY_PPAGE:
            scroll_offset += max(4, body_rows // 2)
        elif ch == curses.KEY_NPAGE:
            scroll_offset = max(0, scroll_offset - max(4, body_rows // 2))
        elif ch in (ord("d"), ord("q"), 27):
            _tmuxctl._select_dash_window()
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
