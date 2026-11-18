"""agy_dash.tui — split verbatim from agy_dashboard.py @ f255869 (Phase 1, no behaviour change)."""
from __future__ import annotations

import argparse
import curses
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import accounts as _accounts
from . import cli as _cli
from . import compact as _compact
from . import paths as _paths
from . import sessions as _sessions
from . import tmuxctl as _tmuxctl


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



class Dashboard:
    """Estado y ciclo interactivo de la interfaz curses del dashboard."""

    def __init__(self, stdscr) -> None:
        self.stdscr = stdscr
        self.cursor = 0
        self.saved_session_cursor = 0
        self.mode = "sessions"
        self.status_banner = "Listo — [Enter] Abrir (o Compactar NV) | [c] Compactar NV | [t] Renombrar | [B] Baton Pass | [u] Cuentas"
        self.last_auto_check = time.time()
        self.idle_seen_since: dict[str, float] = {}
        self.cached_preview_cid = ""
        self.cached_preview_sig = (-1, -1)
        self.cached_turns: list[dict] = []
        self.last_refresh_clock = time.strftime("%H:%M:%S")
        self.scan_us = 0
        self.ksm: dict[str, int] = {"run": 1, "pages_shared": 0, "pages_sharing": 0, "pages_scanned": 0}
        self.sessions: list[_sessions.AgySession] = []
        self.force_scan = True
        self.ctx_cache: dict[str, tuple[tuple[int, int], dict]] = {}
        self.led_tick = 0
        self.last_led_ts = 0.0
        self.last_tab_names: dict[str, str] = {}
        self.activity_cache: dict[str, tuple[tuple[int, int], str]] = {}
        self.led_lock = threading.Lock()
        self.led_worker_busy = {"running": False}
        self.quota_lock = threading.Lock()
        self.quota_worker_state = {"running": False, "last_poll": 0.0, "msg": ""}
        self.pending_delete_info: dict | None = None
        self.pending_compact_sess: _sessions.AgySession | None = None
        self.compact_lock = threading.Lock()
        self.compact_jobs: dict[str, dict] = {}
        self.compact_results: list[tuple[_sessions.AgySession, bool, str, Path | None, bool]] = []
        self.scan_lock = threading.Lock()
        self.scan_wake = threading.Event()
        self.scan_box: dict = {}
        self.seen_scan_ver = 0
        self.running_compact: list[dict] = []
        self.reg: dict = {}
        self.active_slot = "primary"
        self.conv_accounts: dict = {}
        self.model_disp = ""
        self.sel_for_alert: _sessions.AgySession | None = None
        self.sel_slot = "primary"
        self.quota_alert: dict | None = None
        self.live: list[_sessions.AgySession] = []
        self.hib: list[_sessions.AgySession] = []
        self.all_procs: list = []
        self.total_pss_mb = 0
        self.total_shr_mb = 0
        self.total_ksm_mb = 0
        self.hib_saved_mb = 0
        self.polling_now = False
        self.max_y = 0
        self.max_x = 0
        self.show_cmd_panel = False
        self.left_w = 0
        self.panel_x = 0
        self.panel_w = 0
        self.panel_btn_y = 6
        self.cmd_buttons: list[CommandButton] = []
        self.list_start_y = 6
        self.list_avail = 0
        self.preview_height = 0
        self.acct_list: list = []
        self.vault_entries: list[dict] = []
        self.mem_lines: list[str] = []
        self.frame_now = 0.0

    def _configure_curses(self) -> None:
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
        self.stdscr.timeout(250)
        self.last_auto_check = time.time()
        self.last_refresh_clock = time.strftime("%H:%M:%S")

    def cached_context(self, s: _sessions.AgySession) -> dict:
        """measure_session_context re-parses 64 KB of JSON: only redo it when the transcript changed."""
        sig = _sessions.transcript_sig(s.conversation_id)
        hit = self.ctx_cache.get(s.conversation_id)
        if hit is None or hit[0] != sig:
            hit = (sig, _compact.measure_session_context(s.conversation_id, step_count=s.step_count))
            self.ctx_cache[s.conversation_id] = hit
        return hit[1]

    def trigger_led_sync(self, cur_sessions: list[_sessions.AgySession], tick_n: int) -> None:
        if not any(s.dash_win_idx is not None for s in cur_sessions):
            return
        with self.led_lock:
            if self.led_worker_busy["running"]:
                return
            self.led_worker_busy["running"] = True
        snap = list(cur_sessions)
        threading.Thread(target=self._led_work, args=(snap, tick_n), daemon=True).start()

    def _led_work(self, snap: list[_sessions.AgySession], tick_n: int) -> None:
        try:
            _tmuxctl.sync_led_tmux_tabs(snap, tick_n, self.last_tab_names, self.activity_cache)
        finally:
            with self.led_lock:
                self.led_worker_busy["running"] = False

    def start_async_compaction(
        self,
        target_sess: _sessions.AgySession,
        *,
        open_when_done: bool = False,
        prompt_if_missing_key: bool = True,
    ) -> str:
        cid = target_sess.conversation_id
        with self.compact_lock:
            existing = self.compact_jobs.get(cid)
            if existing and existing.get("status") == "running":
                if open_when_done:
                    existing["open_when_done"] = True
                return f"⏳ '{target_sess.title[:28]}' ya se está compactando en 2º plano..."
        cfg = _compact.load_nvapi_config()
        if not str(cfg.get("api_key", "")).strip():
            if not prompt_if_missing_key:
                return "⚠️ Falta NVIDIA API Key (`nvapi-...`). Pulsa [K] para configurarla."
            entered = _prompt_curses_text(self.stdscr, "Ingresa tu NVIDIA API Key (nvapi-...) para compactar", "", max_len=110)
            if not entered:
                return "Compactación cancelada: falta NVIDIA API Key."
            _compact.save_nvapi_config(api_key=entered)
        with self.compact_lock:
            self.compact_jobs[cid] = {
                "title": target_sess.title,
                "started": time.time(),
                "status": "running",
                "open_when_done": open_when_done,
            }
        threading.Thread(
            target=self._compact_worker,
            args=(target_sess, cid, open_when_done),
            daemon=True,
        ).start()
        dest = "y abrirá pestaña al terminar" if open_when_done else "al Baúl NV (sin abrir pestaña)"
        return f"⏳ Compactando '{target_sess.title[:26]}' en 2º plano ({dest}) — el dashboard sigue libre."

    def _compact_worker(
        self,
        target_sess: _sessions.AgySession,
        cid: str,
        open_when_done: bool,
    ) -> None:
        ok = False
        msg = ""
        hfile: Path | None = None
        try:
            ok, msg, hfile = _compact.compact_conversation_with_nvapi(target_sess)
        except Exception as exc:
            ok = False
            msg = f"❌ Error inesperado en compactación: {exc}"
            hfile = None
        with self.compact_lock:
            job = self.compact_jobs.get(cid) or {}
            should_open = bool(job.get("open_when_done", open_when_done))
            self.compact_jobs.pop(cid, None)
            self.compact_results.append((target_sess, ok, msg, hfile, should_open))

    def trigger_quota_poll(self, force: bool = False) -> None:
        with self.quota_lock:
            if self.quota_worker_state["running"]:
                return
            if not force and (time.time() - self.quota_worker_state["last_poll"]) < _paths.QUOTA_POLL_INTERVAL_SEC:
                return
            self.quota_worker_state["running"] = True
        threading.Thread(target=self._quota_worker, daemon=True).start()

    def _quota_worker(self) -> None:
        try:
            reg = _accounts.load_accounts_registry()
            msgs = []
            for sname in list(reg.get("accounts", {}).keys()):
                _, m = _accounts.refresh_account_quota(sname, timeout_sec=14.0)
                msgs.append(m)
            with self.quota_lock:
                self.quota_worker_state["last_poll"] = time.time()
                self.quota_worker_state["msg"] = " · ".join(msgs)
        finally:
            with self.quota_lock:
                self.quota_worker_state["running"] = False

    def db_wal_sig(self) -> tuple[int, int, int]:
        try:
            s1 = _paths.SUMMARIES_DB.stat().st_mtime_ns if _paths.SUMMARIES_DB.is_file() else 0
            wal = Path(str(_paths.SUMMARIES_DB) + "-wal")
            s2 = wal.stat().st_mtime_ns if wal.is_file() else 0
            s3 = _paths.PRESENCE_DIR.stat().st_mtime_ns if _paths.PRESENCE_DIR.is_dir() else 0
            return (s1, s2, s3)
        except OSError:
            return (0, 0, 0)

    def execute_action(self, action: str) -> str:
        if action in ("refresh", "refresh_quota", "memory", "accounts", "vault", "nvapi_key"):
            return self._execute_global_action(action)
        sel = self.sessions[self.saved_session_cursor if self.mode != "sessions" else self.cursor] if self.sessions else None
        if action == "baton" and sel:
            ok, msg = _accounts.baton_pass_session(sel)
            if not ok and "No hay otra cuenta" in msg:
                self.saved_session_cursor = self.cursor
                self.mode = "accounts"
                self.cursor = 0
            self.force_scan = True
            return msg
        if self.mode != "sessions":
            return ""
        sel = self.sessions[self.cursor] if (self.sessions and 0 <= self.cursor < len(self.sessions)) else None
        return self._execute_session_action(action, sel)

    def _execute_global_action(self, action: str) -> str:
        if action == "refresh":
            self.force_scan = True
            self.cached_preview_sig = (-1, -1)
            _tmuxctl.tmux_cmd("refresh-client", "-S")
            return f"Actualizado en vivo a las {time.strftime('%H:%M:%S')} (Zig scan + DB + Vista)."
        if action == "refresh_quota":
            self.trigger_quota_poll(force=True)
            return "Consultando cuotas (/usage) de todas las cuentas en segundo plano..."
        if action == "memory":
            if self.mode == "sessions":
                self.saved_session_cursor = self.cursor
                self.mode = "memory"
                self.cursor = 0
                return "Vista: Memoria Compartida Multi-Harness (isymem)"
            self.mode = "sessions"
            self.cursor = self.saved_session_cursor
            return "Vista: Command Center AGY"
        if action == "accounts":
            if self.mode == "sessions":
                self.saved_session_cursor = self.cursor
                self.mode = "accounts"
                self.cursor = 0
                self.trigger_quota_poll(force=False)
                return "Vista: Cuentas Google Pro, Cuotas (/usage) y Baton Pass"
            self.mode = "sessions"
            self.cursor = self.saved_session_cursor
            return "Vista: Command Center AGY"
        if action == "vault":
            if self.mode != "vault":
                if self.mode == "sessions":
                    self.saved_session_cursor = self.cursor
                self.mode = "vault"
                self.cursor = 0
                return "Vista: Baúl de Handoffs Compactados (NVIDIA NIM)"
            self.mode = "sessions"
            self.cursor = self.saved_session_cursor
            return "Vista: Command Center AGY"
        if action == "nvapi_key":
            cur_cfg = _compact.load_nvapi_config()
            masked = _compact.mask_api_key(str(cur_cfg.get("api_key", "")))
            entered = _prompt_curses_text(
                self.stdscr,
                f"NVIDIA API Key (nvapi-... · actual {masked})",
                "",
                max_len=110,
            )
            if not entered:
                return f"Cancelado — NVIDIA API Key sin cambios ({masked})."
            if not entered.startswith("nvapi-") and len(entered) < 20:
                return f"⚠️ Formato inválido (debe empezar por `nvapi-`). Key anterior conservada ({masked})."
            _compact.save_nvapi_config(api_key=entered)
            return f"✅ NVIDIA API Key guardada ({_compact.mask_api_key(entered)}) — lista para compactar con Nemotron."
        return ""

    def _execute_session_action(self, action: str, sel: _sessions.AgySession | None) -> str:
        if action == "open" and sel:
            offer, est_tok, _ = _compact.should_offer_compaction(sel)
            if offer:
                self.pending_compact_sess = sel
                return (
                    f"⚡ '{sel.title}' consume ~{est_tok:,} tokens de entrada: "
                    f"[c] Compactar con NVIDIA Nemotron | [Enter/o] Abrir completa | [k] Key NV | [Esc] Cancelar"
                )
            return _tmuxctl.open_session_in_dashboard_tab(sel, switch_to=True)
        if action == "open_full" and sel:
            self.pending_compact_sess = None
            return _tmuxctl.open_session_in_dashboard_tab(sel, switch_to=True)
        if action == "compact" and sel:
            self.pending_compact_sess = None
            return self.start_async_compaction(sel, open_when_done=True, prompt_if_missing_key=True)
        if action == "vault_compact" and sel:
            self.pending_compact_sess = None
            return self.start_async_compaction(sel, open_when_done=False, prompt_if_missing_key=True)
        if action == "open_vault" and sel:
            self.pending_compact_sess = None
            vh = _compact.get_vault_handoff(sel.conversation_id)
            if vh is None:
                return f"No hay handoff previo en el Baúl para '{sel.title[:26]}'. Pulsa [c] para compactar."
            return _compact.open_compacted_session_in_tab(sel, vh, switch_to=True)
        if action == "rename" and sel:
            new_name = _prompt_curses_text(self.stdscr, f"Renombrar '{sel.title[:24]}'", sel.title, max_len=64)
            if not new_name or new_name == sel.title:
                return "Renombrado sin cambios."
            clean = _sessions.save_custom_title(sel.conversation_id, new_name)
            sel.title = clean
            self.last_tab_names.pop(sel.conversation_id, None)
            self.trigger_led_sync(self.sessions, self.led_tick + 1)
            self.force_scan = True
            return f"✏️ Sesión renombrada permanentemente a '{clean}'."
        if action == "mirror" and sel:
            return _tmuxctl.open_session_in_dashboard_tab(sel, switch_to=True, force_live_mirror=True)
        if action == "reload" and sel:
            return _compact.reload_session_tab(sel)
        if action == "new":
            _cli.cmd_open_new_tab(argparse.Namespace(cwd=str(Path.cwd())))
            return "Creada nueva pestaña AGY optimizada (KSM + GOMEMLIMIT=180MiB)."
        if action == "hibernate" and sel:
            ok, msg = _sessions.hibernate_session(sel)
            if ok:
                _tmuxctl._replace_tmux_pane_with_wake_stub(sel)
            self.force_scan = True
            return msg
        if action == "hib_all":
            freed = 0
            count = 0
            for s in self.sessions:
                freed += _sessions.deduplicate_session_procs(s)
                freed += _sessions.close_stale_dash_duplicates(s)
                if s.can_hibernate:
                    saved = (s.proc.pss_kb + s.proc.swap_kb) if s.proc else 0
                    ok, _ = _sessions.hibernate_session(s)
                    if ok:
                        freed += saved
                        count += 1
                        _tmuxctl.open_session_in_dashboard_tab(s, switch_to=False, as_stub=True)
            self.force_scan = True
            return (
                f"Adoptadas/hibernadas {count} sesiones y deduplicadas copias -> liberados {freed // 1024} MB."
                if (count or freed)
                else "No hay sesiones IDLE o copias duplicadas que limpiar."
            )
        if action == "dedup":
            freed = 0
            for s in self.sessions:
                freed += _sessions.deduplicate_session_procs(s)
                freed += _sessions.close_stale_dash_duplicates(s)
            self.force_scan = True
            return (
                f"Deduplicadas copias redundantes -> liberados {freed // 1024} MB RAM."
                if freed
                else "No hay copias duplicadas inactivas para cerrar."
            )
        return ""

    def _scanner(self) -> None:
        last_sig = self.db_wal_sig()
        last_t = time.time()
        while True:
            forced = self.scan_wake.wait(0.1)
            self.scan_wake.clear()
            sig = self.db_wal_sig()
            if not forced and sig == last_sig and (time.time() - last_t) < 0.6:
                continue
            last_sig, last_t = sig, time.time()
            try:
                res = _sessions.load_sessions(limit=35)
            except Exception:
                continue
            with self.scan_lock:
                self.scan_box["data"] = res
                self.scan_box["ver"] += 1

    def _start_scanner(self) -> None:
        self.scan_box = {"data": _sessions.load_sessions(limit=35), "ver": 1}
        self.seen_scan_ver = 0
        threading.Thread(target=self._scanner, daemon=True).start()

    def _process_compact_results(self) -> None:
        done_compact_batch: list[tuple[_sessions.AgySession, bool, str, Path | None, bool]] = []
        with self.compact_lock:
            if self.compact_results:
                done_compact_batch = list(self.compact_results)
                self.compact_results.clear()
            self.running_compact = list(self.compact_jobs.values())
        for c_sess, c_ok, c_msg, c_hfile, c_open in done_compact_batch:
            if c_ok and c_hfile is not None:
                if c_open:
                    open_msg = _compact.open_compacted_session_in_tab(c_sess, c_hfile, switch_to=True)
                    self.status_banner = f"{c_msg} · {open_msg}"
                else:
                    self.status_banner = f"✅ Guardado en Baúl NV: '{c_sess.title[:26]}' — {c_msg} (pulsa [V] para abrir)"
                self.force_scan = True
            else:
                self.status_banner = f"❌ {c_msg}"

    def _adopt_scan_snapshot(self) -> None:
        if self.force_scan:
            self.force_scan = False
            self.scan_wake.set()
        with self.scan_lock:
            snap_ver, snap = self.scan_box["ver"], self.scan_box["data"]
        if snap_ver != self.seen_scan_ver:
            self.seen_scan_ver = snap_ver
            self.scan_us, self.ksm, self.sessions = snap
            self.last_refresh_clock = time.strftime("%H:%M:%S")

    def _periodic_idle_check(self, now: float) -> None:
        if now - self.last_auto_check < 10.0:
            return
        self.last_auto_check = now
        active_idle_ids: set[str] = set()
        for s in self.sessions:
            if s.extra_procs and not self.status_banner:
                # Never close a copy automatically: it may be the one Danny is typing in elsewhere.
                self.status_banner = (f"'{s.title[:30]}' está abierta {1 + len(s.extra_procs)} veces "
                                      f"(+{sum(p.pss_kb for p in s.extra_procs) // 1024} MB): pulsa [x] si sobra.")
            if s.can_hibernate and s.proc is not None and s.dash_win_idx is not None:
                active_idle_ids.add(s.conversation_id)
                first_t = self.idle_seen_since.setdefault(s.conversation_id, now)
                if _paths.DEFAULT_IDLE_HIBERNATE_SEC > 0 and (now - first_t) >= _paths.DEFAULT_IDLE_HIBERNATE_SEC:
                    ok, msg = _sessions.hibernate_session(s)
                    if ok:
                        _tmuxctl._replace_tmux_pane_with_wake_stub(s)
                        self.status_banner = f"Auto-Hibernate: {msg}"
                        self.idle_seen_since.pop(s.conversation_id, None)
        for cid in list(self.idle_seen_since.keys()):
            if cid not in active_idle_ids:
                del self.idle_seen_since[cid]

    def _advance_background(self, now: float) -> None:
        self.trigger_quota_poll(force=False)
        self._process_compact_results()
        self._adopt_scan_snapshot()
        if (now - self.last_led_ts) >= _paths.LED_TICK_SEC:
            self.last_led_ts = now
            self.led_tick += 1
            self.trigger_led_sync(self.sessions, self.led_tick)
        self._periodic_idle_check(now)

    def run(self) -> None:
        """Configura curses e inicia el ciclo del dashboard."""
        self._configure_curses()
        self._start_scanner()
        reg_init = _accounts.load_accounts_registry()
        if any(not a.get("quota", {}).get("ok") for a in reg_init.get("accounts", {}).values()):
            self.trigger_quota_poll(force=True)
        while True:
            now = time.time()
            self.frame_now = now
            self._advance_background(now)
            self.draw(now)
            ch = self.stdscr.getch()
            if ch == -1:
                continue
            if ch == curses.KEY_MOUSE:
                ch = self.handle_mouse()
                if ch is None:
                    continue
            self.handle_key(ch)

    def _prepare_frame(self, now: float) -> None:
        self.reg = _accounts.load_accounts_registry()
        self.active_slot = self.reg.get("active_slot", "primary")
        self.conv_accounts = self.reg.get("conv_accounts", {})
        self.model_disp, _ = _accounts.resolve_model_arg()
        self.sel_for_alert = self.sessions[self.cursor] if (self.mode == "sessions" and self.sessions and 0 <= self.cursor < len(self.sessions)) else (
            self.sessions[self.saved_session_cursor] if (self.sessions and 0 <= self.saved_session_cursor < len(self.sessions)) else None
        )
        self.sel_slot = self.conv_accounts.get(self.sel_for_alert.conversation_id, self.active_slot) if self.sel_for_alert else self.active_slot
        self.quota_alert = _accounts.compute_quota_alert(self.reg, active_slot=self.sel_slot, model_name=self.model_disp)
        self.live = [s for s in self.sessions if s.proc is not None]
        self.hib = [s for s in self.sessions if s.hibernated]
        self.all_procs = [p for s in self.live for p in ([s.proc] if s.proc else []) + s.extra_procs]
        self.total_pss_mb = sum(p.pss_kb for p in self.all_procs) // 1024
        self.total_shr_mb = max((p.shared_clean_kb for p in self.all_procs), default=0) // 1024
        self.total_ksm_mb = sum(p.ksm_kb for p in self.all_procs) // 1024
        self.hib_saved_mb = sum(s.hibernated_saved_kb for s in self.hib) // 1024

    def _draw_header(self) -> None:
        hdr = (
            f" ⚡ AGY COMMAND CENTER (Zig {self.scan_us / 1000.0:.1f}ms + Python · Sync {self.last_refresh_clock}) — "
            f"{len(self.live)} activas · {len(self.hib)} hibernadas (0MB) · Cuenta [{self.active_slot}] "
        )
        self.stdscr.addstr(0, 0, hdr[: self.max_x - 1].ljust(self.max_x - 1), curses.color_pair(1) | curses.A_BOLD)
        ram_line = (
            f" RAM: {self.total_pss_mb} MB PSS | Shared: {self.total_shr_mb} MB + KSM: {self.total_ksm_mb} MB "
            f"({self.ksm.get('pages_sharing', 0)} págs) | Ahorrado (💤): {self.hib_saved_mb} MB | Modelo: {self.model_disp or 'default'}"
        )
        self.stdscr.addstr(1, 0, ram_line[: self.max_x - 1], curses.color_pair(2) | curses.A_BOLD)

    def _draw_quota_alert(self) -> None:
        with self.quota_lock:
            polling_now = self.quota_worker_state["running"]
        if self.quota_alert:
            pf_badge = ""
            if self.sel_for_alert:
                pf = _accounts.baton_preflight(self.sel_for_alert, target_slot=self.quota_alert.get("target_slot", ""), model=self.model_disp)
                pf_badge = " [Preflight OK · 0 dup]" if pf["ok"] else " [Esperando IDLE]"
            if self.quota_alert["target_slot"]:
                tpct = f" ({self.quota_alert['target_pct']:.0f}%)" if self.quota_alert["target_pct"] >= 0 else ""
                act_hint = f"Pulsa [B] Baton Pass -> [{self.quota_alert['target_slot']}]{tpct}{pf_badge}"
            else:
                act_hint = "Pulsa [u] Cuentas -> [+] para añadir 2ª Cuenta Pro"
            alert_txt = (
                f" 🚨 CUOTA BAJA [{self.quota_alert['active_slot']} {self.quota_alert['active_email']}]: "
                f"{self.quota_alert['bucket_name']} al {self.quota_alert['pct']:.0f}% — {act_hint} "
            )
            alert_attr = (curses.color_pair(1) | curses.A_BOLD) if self.quota_alert["level"] == "CRITICAL" else (curses.color_pair(4) | curses.A_REVERSE | curses.A_BOLD)
        else:
            acct_obj = self.reg.get("accounts", {}).get(self.sel_slot, {})
            fams = acct_obj.get("quota", {}).get("families", {})
            q_parts = [f"{k}:{v:.0f}%" for k, v in fams.items()]
            q_summary = " · ".join(q_parts) if q_parts else ("consultando /usage..." if polling_now else "pulsa [u] para cuotas")
            mail = acct_obj.get("email", "")
            mail_s = f" ({mail})" if mail else ""
            alert_txt = f" 💳 Cuenta [{self.sel_slot}]{mail_s}: {q_summary}  |  [B] Baton Pass  [u] Cuentas Pro ({len(self.reg.get('accounts', {}))}) "
            alert_attr = curses.color_pair(3) | curses.A_BOLD
        self.stdscr.addstr(2, 0, alert_txt[: self.max_x - 1].ljust(self.max_x - 1), alert_attr)

    def _compute_layout(self) -> None:
        # Layout split: Left Session List + Right Command Center Panel (if width >= 116)
        self.show_cmd_panel = self.mode == "sessions" and self.max_x >= 116
        self.left_w = min(92, self.max_x - 46) if self.show_cmd_panel else (self.max_x - 1)
        self.panel_x = self.left_w + 1
        self.panel_w = self.max_x - self.panel_x - 1
        self.panel_btn_y = 6
        self.cmd_buttons = build_command_buttons(self.panel_w) if self.show_cmd_panel else []
        self.list_start_y = 6
        self.list_avail = 0

    def _draw_session_list(self) -> None:
        if self.sessions:
            self.cursor = max(0, min(self.cursor, len(self.sessions) - 1))
        self.preview_height = max(7, min(12, self.max_y // 3)) if self.max_y >= 22 else 0
        self.list_avail = max(3, self.max_y - 9 - self.preview_height)
        col_hdr = f" {'ESTADO':<10} {'UBICACIÓN':<10} {'PSS':>6} {'SHR+KSM':>7} {'PASOS':>5}  {'TÍTULO DE SESIÓN':<28} {'WS'}"
        self.stdscr.addstr(4, 0, col_hdr[: self.left_w], curses.A_BOLD)
        self.stdscr.addstr(5, 0, "─" * self.left_w)
        start_idx = max(0, self.cursor - self.list_avail + 1) if self.cursor >= self.list_avail else 0
        for idx, s in enumerate(self.sessions[start_idx : start_idx + self.list_avail], start=start_idx):
            row_y = self.list_start_y + (idx - start_idx)
            if row_y >= self.list_start_y + self.list_avail:
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
            if idx == self.cursor:
                attr = curses.color_pair(6) | curses.A_BOLD
            elif st == "RUNNING":
                attr = curses.color_pair(3) | curses.A_BOLD
            elif st == "BG-TASK":
                attr = curses.color_pair(4)
            elif st == "HIBERNATED":
                attr = curses.color_pair(5)
            self.stdscr.addstr(row_y, 0, line[: self.left_w].ljust(self.left_w), attr)

    def _draw_buttons(self) -> None:
        for ry in range(4, 6 + self.list_avail):
            if ry < self.max_y - 3:
                self.stdscr.addstr(ry, self.left_w, "│")
        self.stdscr.addstr(4, self.panel_x, " 🎛️  CENTRO DE COMANDO (Clic o Tecla)"[: self.panel_w], curses.color_pair(2) | curses.A_BOLD)
        self.stdscr.addstr(5, self.panel_x, "─" * self.panel_w)
        for b in self.cmd_buttons:
            by = self.panel_btn_y + b.row_offset
            bx = self.panel_x + b.col_offset
            if by < 6 + self.list_avail and bx + b.width < self.max_x:
                btn_txt = f" {b.label} ".ljust(b.width)[: b.width]
                btn_attr = curses.color_pair(1 if (b.action_id == "baton" and self.quota_alert) else 7) | curses.A_BOLD
                self.stdscr.addstr(by, bx, btn_txt, btn_attr)

    def _draw_inspector(self, now: float) -> None:
        insp_y = self.panel_btn_y + 9
        if insp_y >= 6 + self.list_avail or not self.sessions or not (0 <= self.cursor < len(self.sessions)):
            return
        sel = self.sessions[self.cursor]
        self.stdscr.addstr(insp_y - 1, self.panel_x, "─" * self.panel_w)
        t_sig = _sessions.transcript_sig(sel.conversation_id)
        age_s = int(max(0, now - (t_sig[0] / 1e9))) if t_sig[0] else -1
        age_txt = f"hace {age_s}s" if age_s >= 0 else "sin log"
        kids_txt = ", ".join(sel.proc.active_children) if (sel.proc and sel.proc.active_children) else "ninguno"
        pid_txt = str(sel.proc.pid) if sel.proc else "-"
        extra_txt = f" (+{len(sel.extra_procs)} dup)" if sel.extra_procs else ""
        acct_tag = self.conv_accounts.get(sel.conversation_id, self.active_slot)
        nv_cfg = _compact.load_nvapi_config()
        nv_lbl = _compact.mask_api_key(str(nv_cfg.get("api_key", "")))
        ctx_m = self.cached_context(sel)
        ctx_tok = ctx_m["used_tokens"]
        ctx_pct = ctx_m["pct"]
        ctx_lvl = ctx_m["level"]
        meta_lines = [
            (f" 📌 {sel.title[:self.panel_w - 4]}", curses.A_BOLD),
            (f" ID: {sel.conversation_id[:14]} PID:{pid_txt}{extra_txt} Cta:[{acct_tag}]", curses.A_NORMAL),
            (f" Ctx: ~{ctx_tok // 1000}k ({ctx_pct:.0f}% · {ctx_lvl}) · {age_txt} | NV:{nv_lbl[:11]}", curses.color_pair(1 if "URGENT" in ctx_lvl else (4 if ("RECOMMEND" in ctx_lvl or kids_txt != "ninguno") else 3))),
            (f" RAM: {_ram_bar(min(1.0, self.total_pss_mb / 2048.0), 18)} {self.total_pss_mb}M (KSM {self.total_ksm_mb}M)", curses.color_pair(3)),
        ]
        for mi, (ml, ma) in enumerate(meta_lines):
            if insp_y + mi < 6 + self.list_avail:
                self.stdscr.addstr(insp_y + mi, self.panel_x, ml[:self.panel_w], ma)

    def _draw_preview(self, now: float) -> None:
        if self.preview_height <= 0 or not self.sessions or not (0 <= self.cursor < len(self.sessions)):
            return
        sel = self.sessions[self.cursor]
        py = self.max_y - 3 - self.preview_height
        self.stdscr.addstr(py, 0, "─" * (self.max_x - 1))
        if self.pending_compact_sess is not None:
            self._draw_compaction_modal(self.pending_compact_sess, py, now)
        else:
            self._draw_live_preview(sel, py, now)

    def _draw_compaction_modal(self, pcs: _sessions.AgySession, py: int, now: float) -> None:
        _, est_tok, log_kb = _compact.should_offer_compaction(pcs)
        vault_file = _compact.get_vault_handoff(pcs.conversation_id)
        nv_cfg = _compact.load_nvapi_config()
        has_nv = bool(str(nv_cfg.get("api_key", "")).strip())
        nv_status = (
            f"LISTA ({_compact.mask_api_key(str(nv_cfg.get('api_key', '')))} · {nv_cfg.get('model')})"
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
            if ry < self.max_y - 3:
                self.stdscr.addstr(ry, 0, ml[: self.max_x - 1].ljust(self.max_x - 1), ma)

    def _draw_live_preview(self, sel: _sessions.AgySession, py: int, now: float) -> None:
        cur_sig = _sessions.transcript_sig(sel.conversation_id)
        if sel.conversation_id != self.cached_preview_cid or cur_sig != self.cached_preview_sig:
            self.cached_preview_cid = sel.conversation_id
            self.cached_preview_sig = cur_sig
            self.cached_turns = _sessions.zig_tail_transcript(sel.conversation_id, max_turns=self.preview_height - 2)
        age_s = int(max(0, now - (cur_sig[0] / 1e9))) if cur_sig[0] else -1
        age_lbl = f"hace {age_s}s" if age_s >= 0 else "sin log"
        phdr = (
            f" ⚡ VISTA EN VIVO ZIG (<4ms · {age_lbl}): {sel.title[:38]} ({sel.conversation_id[:8]}) "
            f"[{sel.state_label} · {sel.location_label}]"
        )
        self.stdscr.addstr(py + 1, 0, phdr[: self.max_x - 1], curses.color_pair(2) | curses.A_BOLD)
        if not self.cached_turns:
            self.stdscr.addstr(py + 2, 0, "   (Sin transcript.jsonl local o conversación recién creada)"[: self.max_x - 1])
        else:
            for tidx, t in enumerate(self.cached_turns[-(self.preview_height - 2) :]):
                ry = py + 2 + tidx
                if ry >= self.max_y - 3:
                    break
                if t["role"] == "USER":
                    txt = f"  👤 [{t['ts']} #{t['idx']}] TÚ: {t['content']}"
                    self.stdscr.addstr(ry, 0, txt[: self.max_x - 1], curses.color_pair(4))
                elif t["role"] == "TOOL":
                    txt = f"  ⏳ [{t['ts']} #{t['idx']}] {t['content']}"
                    self.stdscr.addstr(ry, 0, txt[: self.max_x - 1], curses.color_pair(5))
                else:
                    tools_s = f" [🔧 {', '.join(t['tools'])}]" if t["tools"] else ""
                    body_s = t["content"] or "(ejecutando herramientas...)"
                    txt = f"  🤖 [{t['ts']} #{t['idx']}] AGY: {body_s}{tools_s}"
                    self.stdscr.addstr(ry, 0, txt[: self.max_x - 1], curses.color_pair(3))

    def _draw_accounts(self) -> None:
        self.acct_list = list(self.reg.get("accounts", {}).items())
        email_counts: dict[str, int] = {}
        for _, ad_row in self.acct_list:
            em = str(ad_row.get("email", "")).strip().lower()
            if em:
                email_counts[em] = email_counts.get(em, 0) + 1
        sel_s_title = self.sel_for_alert.title[:28] if self.sel_for_alert else "-"
        ahdr = (
            f" 💳 CUENTAS GOOGLE PRO & BATON PASS (Sesión objetivo: {sel_s_title} · Modelo: {self.model_disp or 'default'})"
        )
        self.stdscr.addstr(4, 0, ahdr[: self.max_x - 1], curses.color_pair(2) | curses.A_BOLD)
        self.stdscr.addstr(5, 0, f" {'SLOT':<12} {'ACT':<4} {'MODO':<9} {'EMAIL':<28} {'ARGON':>7} {'GEMINI':>7} {'CLAUDE/GPT':>10} {'ESTADO'}", curses.A_BOLD)
        self.cursor = max(0, min(self.cursor, max(0, len(self.acct_list) - 1)))
        for idx, (sname, adata) in enumerate(self.acct_list):
            ry = 6 + idx
            if ry >= self.max_y - 5:
                break
            mark = "★" if sname == self.active_slot else " "
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
            attr = (curses.color_pair(6) | curses.A_BOLD) if idx == self.cursor else (
                curses.color_pair(4) if (not adata.get("authenticated") or is_dup) else curses.color_pair(3)
            )
            self.stdscr.addstr(ry, 0, row_txt[: self.max_x - 1].ljust(self.max_x - 1), attr)
        if self.pending_delete_info is not None:
            self._draw_account_removal()
        elif self.acct_list and 0 <= self.cursor < len(self.acct_list) and self.max_y >= 15:
            self._draw_account_details()

    def _draw_account_removal(self) -> None:
        det_y = min(self.max_y - 8, 7 + len(self.acct_list))
        pdi = self.pending_delete_info
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
            if cy < self.max_y - 3:
                self.stdscr.addstr(cy, 0, cl[: self.max_x - 1].ljust(self.max_x - 1), ca)

    def _draw_account_details(self) -> None:
        sname, adata = self.acct_list[self.cursor]
        buckets = adata.get("quota", {}).get("buckets", [])
        det_y = min(self.max_y - 6, 7 + len(self.acct_list))
        if det_y >= self.max_y - 4:
            return
        self.stdscr.addstr(det_y, 0, f" 📊 Detalle [{sname}] (actualizado: {adata.get('updated_at') or 'nunca'}):"[: self.max_x - 1], curses.A_BOLD)
        for bi, b in enumerate(buckets[:4]):
            by = det_y + 1 + bi
            if by >= self.max_y - 3:
                break
            b_line = f"    • {b.get('group', '')} — {b.get('name', '')}: {b.get('pct', 0):.0f}% libre  (reset {b.get('reset_time', '')})"
            b_attr = curses.color_pair(1) if b.get("pct", 100) <= _paths.QUOTA_CRIT_PCT else (
                curses.color_pair(4) if b.get("pct", 100) <= _paths.QUOTA_WARN_PCT else curses.color_pair(3)
            )
            self.stdscr.addstr(by, 0, b_line[: self.max_x - 1], b_attr)

    def _draw_vault(self, now: float) -> None:
        self.vault_entries = _compact.list_compacted_vault()
        self.stdscr.addstr(
            4,
            0,
            f" 🗄️ BAÚL DE HANDOFFS COMPACTADOS ({len(self.vault_entries)} guardados en compacted/ — [Enter] Abrir nueva sesión · [A] Compactar pesadas al Baúl · [V/Esc] Volver)"[
                : self.max_x - 1
            ],
            curses.color_pair(2) | curses.A_BOLD,
        )
        self.stdscr.addstr(
            5,
            0,
            f" {'ID':<10} {'TÍTULO':<32} {'REDUCCIÓN / AHORRO':<36} {'HACE':<8} {'WORKSPACE'}",
            curses.A_BOLD,
        )
        self.cursor = max(0, min(self.cursor, max(0, len(self.vault_entries) - 1)))
        avail_rows = max(1, self.max_y - 9)
        start_idx = max(0, self.cursor - avail_rows + 1) if self.cursor >= avail_rows else 0
        if not self.vault_entries:
            self.stdscr.addstr(6, 0, "   (Baúl vacío — pulsa [C] sobre una sesión o [A] aquí para compactar pesadas en 2º plano sin abrirlas)"[: self.max_x - 1])
        else:
            for idx, ve in enumerate(self.vault_entries[start_idx : start_idx + avail_rows], start=start_idx):
                row_y = 6 + (idx - start_idx)
                if row_y >= self.max_y - 3:
                    break
                age_m = int(max(0, (now - ve["mtime"]) // 60)) if ve["mtime"] else 0
                age_s = f"{age_m}m" if age_m < 60 else f"{age_m // 60}h"
                ws_s = os.path.basename(str(ve["workspace"]).rstrip("/")) or str(ve["workspace"])
                red_s = (ve["reduction"] or f"{ve['size_bytes']} B")[:36]
                row_txt = f" {ve['short_id']:<10} {ve['title'][:32]:<32} {red_s:<36} {age_s:<8} {ws_s}"
                attr = (curses.color_pair(6) | curses.A_BOLD) if idx == self.cursor else curses.color_pair(3)
                self.stdscr.addstr(row_y, 0, row_txt[: self.max_x - 1].ljust(self.max_x - 1), attr)

    def _draw_memory(self) -> None:
        mem_candidates = [
            Path(os.environ["ISYMEM_DIR"]) / "MEMORY.md" if os.environ.get("ISYMEM_DIR") else None,
            Path.cwd() / "docs" / "agent" / "memory" / "MEMORY.md",
            _paths.HOME / ".config" / "isyco-memory" / "memory" / "MEMORY.md",
        ]
        mem_idx = next((p for p in mem_candidates if p is not None and p.is_file()), Path.cwd() / "docs" / "agent" / "memory" / "MEMORY.md")
        self.mem_lines = (
            [line for line in mem_idx.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]
            if mem_idx.is_file()
            else ["(MEMORY.md not found — run `isymem setup` in your workspace)"]
        )
        self.stdscr.addstr(
            4,
            0,
            f" 🧠 MEMORIA COMPARTIDA MULTI-HARNESS ({len(self.mem_lines)} entradas en MEMORY.md — m:Volver)"[
                : self.max_x - 1
            ],
            curses.color_pair(3) | curses.A_BOLD,
        )
        self.stdscr.addstr(5, 0, "─" * (self.max_x - 1))
        self.cursor = max(0, min(self.cursor, max(0, len(self.mem_lines) - 1)))
        avail_rows = max(1, self.max_y - 9)
        start_idx = max(0, self.cursor - avail_rows + 1) if self.cursor >= avail_rows else 0
        for idx, mline in enumerate(self.mem_lines[start_idx : start_idx + avail_rows], start=start_idx):
            row_y = 6 + (idx - start_idx)
            if row_y >= self.max_y - 3:
                break
            attr = (curses.color_pair(6) | curses.A_BOLD) if idx == self.cursor else curses.A_NORMAL
            self.stdscr.addstr(row_y, 0, f" {mline}"[: self.max_x - 1].ljust(self.max_x - 1), attr)

    def _draw_footer(self, now: float) -> None:
        if self.max_y < 3:
            return
        self.stdscr.addstr(self.max_y - 3, 0, "─" * (self.max_x - 1))
        if self.running_compact:
            j0 = self.running_compact[0]
            el_s = int(max(0, now - j0["started"]))
            spin = _tmuxctl._SPINNER_FRAMES[self.led_tick % len(_tmuxctl._SPINNER_FRAMES)]
            banner_line = (
                f"{spin} Compactando 2º plano ({len(self.running_compact)}): '{j0['title'][:26]}' ({el_s}s) | {self.status_banner}"
            )
        else:
            banner_line = self.status_banner
        self.stdscr.addstr(self.max_y - 2, 0, f" {banner_line}"[: self.max_x - 1], curses.color_pair(4))
        if self.mode == "accounts":
            keys_help = (
                " [+] Agregar   [x/Del] Eliminar   [B] Baton Pass   [Enter/s] Activar   "
                "[R] Cuotas   [u/Esc] Volver "
            )
        elif self.mode == "vault":
            keys_help = (
                " [Enter] Abrir del Baúl   [r] Re-compactar al Baúl   [A] Compactar pesadas al Baúl (2ºp)   "
                "[x/Del] Borrar   [V/Esc] Volver "
            )
        else:
            keys_help = (
                " [Enter] Abrir  [c] Compactar+Ir  [C] Guardar Baúl  [V] Baúl  [t] Renombrar  "
                "[K] Key NV  [B] Baton  [u] Cuentas  [h/H] Hibernar  [m] Memoria "
            )
        self.stdscr.addstr(self.max_y - 1, 0, keys_help[: self.max_x - 1], curses.A_REVERSE)

    def draw(self, now: float | None = None) -> None:
        """Dibuja una iteración completa respetando el orden de las vistas."""
        if now is None:
            now = time.time()
        self.frame_now = now
        self.stdscr.erase()
        self.max_y, self.max_x = self.stdscr.getmaxyx()
        self._prepare_frame(now)
        self._draw_header()
        self._draw_quota_alert()
        self.stdscr.addstr(3, 0, "─" * (self.max_x - 1))
        self._compute_layout()
        if self.mode == "sessions":
            self._draw_session_list()
            if self.show_cmd_panel:
                self._draw_buttons()
                self._draw_inspector(now)
            self._draw_preview(now)
        elif self.mode == "accounts":
            self._draw_accounts()
        elif self.mode == "vault":
            self._draw_vault(now)
        else:
            self._draw_memory()
        self._draw_footer(now)
        self.stdscr.refresh()

    def handle_mouse(self) -> int | None:
        """Procesa un evento de ratón y devuelve una tecla traducida si aplica."""
        try:
            _, mx, my, _, bstate = curses.getmouse()
        except curses.error:
            return None
        btn4 = getattr(curses, "BUTTON4_PRESSED", 0)
        btn5 = getattr(curses, "BUTTON5_PRESSED", 0)
        if btn4 and (bstate & btn4):
            self.cursor = max(0, self.cursor - 1)
            return None
        if btn5 and (bstate & btn5):
            self.cursor += 1
            return None
        click_mask = curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED | curses.BUTTON1_DOUBLE_CLICKED
        if not (bstate & click_mask):
            return None
        is_dbl_click = bool(bstate & curses.BUTTON1_DOUBLE_CLICKED)
        if self.show_cmd_panel and mx >= self.panel_x:
            act = hit_test_button(self.cmd_buttons, self.panel_x, self.panel_btn_y, mx, my)
            if act:
                res = self.execute_action(act)
                if res:
                    self.status_banner = res
                if act in ("compact", "vault_compact", "open", "open_full", "new", "baton", "mirror", "reload"):
                    curses.flushinp()
        elif self.mode == "sessions" and self.list_start_y <= my < self.list_start_y + self.list_avail and mx < self.left_w:
            clicked_idx = (max(0, self.cursor - self.list_avail + 1) if self.cursor >= self.list_avail else 0) + (my - self.list_start_y)
            if 0 <= clicked_idx < len(self.sessions):
                self.pending_compact_sess = None
                self.cursor = clicked_idx
                if is_dbl_click:
                    self.status_banner = self.execute_action("open")
                    curses.flushinp()
        elif self.mode == "vault":
            vault_entries = _compact.list_compacted_vault()
            if 6 <= my < 6 + len(vault_entries):
                self.cursor = my - 6
                if is_dbl_click:
                    self.status_banner = _compact.open_vault_entry_in_tab(vault_entries[self.cursor], switch_to=True)
                    curses.flushinp()
        elif self.mode == "accounts":
            acct_list = list(self.reg.get("accounts", {}).items())
            if 6 <= my < 6 + len(acct_list):
                self.cursor = my - 6
            elif my == self.max_y - 1:
                if mx < 14:
                    return ord("+")
                if mx < 32:
                    return ord("x")
                if mx < 49:
                    return ord("B")
                if mx < 69:
                    return ord("s")
                if mx < 82:
                    return ord("R")
                return ord("u")
        return None

    def handle_key(self, ch: int | str) -> None:
        """Despacha una tecla según la vista activa."""
        if isinstance(ch, str) and len(ch) == 1:
            ch = ord(ch)
        if self.mode == "accounts":
            self._handle_account_key(ch)
            return
        if self.mode == "vault":
            self._handle_vault_key(ch)
            return
        if self.mode == "sessions" and self.pending_compact_sess is not None:
            self._handle_pending_compaction_key(ch)
            return
        self._handle_session_key(ch)

    def _handle_account_key(self, ch: int) -> None:
        self.acct_list = list(self.reg.get("accounts", {}).items())
        if self.pending_delete_info is not None:
            if ch in (ord("y"), ord("Y"), ord("s"), ord("S")) and not self.pending_delete_info.get("blocked_reason"):
                ok, self.status_banner = _accounts.remove_account_slot(
                    self.pending_delete_info["slot"],
                    confirmed=True,
                    sessions=self.sessions,
                )
                self.pending_delete_info = None
                if ok and self.cursor > 0:
                    self.cursor -= 1
            elif ch in (ord("n"), ord("N"), 27, ord("u"), ord("q")):
                self.pending_delete_info = None
                self.status_banner = "Eliminación de perfil cancelada."
            return
        if ch in (ord("u"), ord("m"), 27):
            self.mode = "sessions"
            self.cursor = self.saved_session_cursor
            self.status_banner = "Vista: Command Center AGY"
        elif ch in (curses.KEY_UP, ord("k")):
            self.cursor = max(0, self.cursor - 1)
        elif ch in (curses.KEY_DOWN, ord("j")):
            self.cursor += 1
        elif ch in (ord("r"), ord("R")):
            self.status_banner = self.execute_action("refresh_quota")
        elif ch == ord("+"):
            idx = 2
            existing_accts = self.reg.get("accounts", {})
            while f"pro{idx}" in existing_accts:
                idx += 1
            default_name = f"pro{idx}"
            chosen = _prompt_curses_text(self.stdscr, "Nombre de nueva cuenta Pro aislada", default_name)
            try:
                self.status_banner = _tmuxctl.launch_account_login_tab(chosen)
            except ValueError as ve:
                self.status_banner = f"Nombre inválido: {ve}"
        elif ch in (ord("x"), curses.KEY_DC, 127) and self.acct_list and 0 <= self.cursor < len(self.acct_list):
            del_slot = self.acct_list[self.cursor][0]
            info = _accounts.inspect_account_removal(del_slot, sessions=self.sessions)
            if info["blocked_reason"]:
                self.status_banner = f"⚠️ {info['blocked_reason']}"
            else:
                self.pending_delete_info = info
                self.status_banner = f"Revisa identidad/alcance de [{del_slot}] arriba: [S/Y] Confirmar | [N/Esc] Cancelar"
        elif ch in (10, 13, curses.KEY_ENTER, ord("s")) and self.acct_list and 0 <= self.cursor < len(self.acct_list):
            chosen_slot = self.acct_list[self.cursor][0]
            self.reg["active_slot"] = chosen_slot
            _accounts.save_accounts_registry(self.reg)
            self.status_banner = f"Cuenta por defecto cambiada a [{chosen_slot}]."
        elif ch in (ord("b"), ord("B")) and self.acct_list and 0 <= self.cursor < len(self.acct_list):
            chosen_slot = self.acct_list[self.cursor][0]
            target_sess = self.sessions[self.saved_session_cursor] if (self.sessions and 0 <= self.saved_session_cursor < len(self.sessions)) else None
            if not target_sess:
                self.status_banner = "Selecciona una sesión primero antes de hacer Baton Pass."
            else:
                ok, msg = _accounts.baton_pass_session(target_sess, target_slot=chosen_slot)
                self.status_banner = msg
                if ok:
                    self.mode = "sessions"
                    self.cursor = self.saved_session_cursor
        elif ch in (ord("q"), 9):
            _tmuxctl.tmux_cmd("next-window")

    def _handle_vault_key(self, ch: int) -> None:
        vault_entries = _compact.list_compacted_vault()
        if ch in (ord("v"), ord("V"), 27):
            self.mode = "sessions"
            self.cursor = self.saved_session_cursor
            self.status_banner = "Vista: Command Center AGY"
        elif ch in (curses.KEY_UP, ord("k")):
            self.cursor = max(0, self.cursor - 1)
        elif ch in (curses.KEY_DOWN, ord("j")):
            self.cursor += 1
        elif ch in (10, 13, curses.KEY_ENTER) and vault_entries and 0 <= self.cursor < len(vault_entries):
            self.status_banner = _compact.open_vault_entry_in_tab(vault_entries[self.cursor], switch_to=True)
            curses.flushinp()
        elif ch == ord("r") and vault_entries and 0 <= self.cursor < len(vault_entries):
            ve = vault_entries[self.cursor]
            match_s = next((s for s in self.sessions if s.conversation_id.startswith(ve["short_id"])), None)
            if match_s is None:
                match_s = _sessions.AgySession(
                    conversation_id=str(ve["conv_id"]),
                    title=str(ve["title"]),
                    preview="",
                    step_count=0,
                    last_modified="",
                    workspace=str(ve["workspace"]),
                    db_status="CASCADE_RUN_STATUS_IDLE",
                    not_fully_idle=False,
                )
            self.status_banner = self.start_async_compaction(match_s, open_when_done=False, prompt_if_missing_key=True)
        elif ch in (ord("A"), ord("a")):
            heavy = [s for s in self.sessions if _compact.should_offer_compaction(s)[0] or s.step_count >= _paths.COMPACT_MIN_STEPS]
            if not heavy:
                self.status_banner = "No hay sesiones pesadas para compactar al Baúl."
            else:
                started = 0
                for hs in heavy[:12]:
                    msg_s = self.start_async_compaction(hs, open_when_done=False, prompt_if_missing_key=(started == 0))
                    if msg_s.startswith("⏳ Compactando"):
                        started += 1
                    elif "falta" in msg_s.lower():
                        self.status_banner = msg_s
                        break
                if started:
                    self.status_banner = f"📦 Compactando {started} sesiones pesadas en 2º plano al Baúl NV (sin abrir pestañas)."
        elif ch in (ord("x"), curses.KEY_DC, 127) and vault_entries and 0 <= self.cursor < len(vault_entries):
            ve = vault_entries[self.cursor]
            if _compact.delete_vault_handoff(Path(ve["path"])):
                self.status_banner = f"🗑️ Eliminado '{ve['title'][:28]}' del Baúl."
                if self.cursor > 0:
                    self.cursor -= 1
        elif ch in (ord("q"), 9):
            _tmuxctl.tmux_cmd("next-window")

    def _handle_pending_compaction_key(self, ch: int) -> None:
        if ch == ord("c"):
            self.status_banner = self.execute_action("compact")
        elif ch == ord("C"):
            self.status_banner = self.execute_action("vault_compact")
        elif ch in (ord("b"), ord("B")):
            self.status_banner = self.execute_action("open_vault")
        elif ch in (10, 13, curses.KEY_ENTER, ord("o"), ord("O")):
            self.status_banner = self.execute_action("open_full")
        elif ch in (ord("k"), ord("K")):
            self.status_banner = self.execute_action("nvapi_key")
        elif ch in (27, ord("n"), ord("N"), ord("q")):
            self.pending_compact_sess = None
            self.status_banner = "Reactivación cancelada."

    def _handle_session_key(self, ch: int) -> None:
        if ch in (ord("q"), 27, 9):
            _tmuxctl.tmux_cmd("next-window")
        elif ch == ord("D"):
            _tmuxctl.tmux_cmd("detach-client")
        elif ch in (curses.KEY_UP, ord("k")):
            self.cursor = max(0, self.cursor - 1)
        elif ch in (curses.KEY_DOWN, ord("j")):
            self.cursor += 1
        elif ch == ord("r"):
            self.status_banner = self.execute_action("refresh")
        elif ch == ord("m"):
            self.status_banner = self.execute_action("memory")
        elif ch == ord("u"):
            self.status_banner = self.execute_action("accounts")
        elif ch == ord("U"):
            self.status_banner = self.execute_action("refresh_quota")
        elif ch in (ord("V"),):
            self.status_banner = self.execute_action("vault")
        elif self.mode == "sessions" and ch in (10, 13, curses.KEY_ENTER):
            self.status_banner = self.execute_action("open")
        elif self.mode == "sessions" and ch == ord("c"):
            self.status_banner = self.execute_action("compact")
        elif self.mode == "sessions" and ch == ord("C"):
            self.status_banner = self.execute_action("vault_compact")
        elif self.mode == "sessions" and ch == ord("t"):
            self.status_banner = self.execute_action("rename")
        elif self.mode == "sessions" and ch == ord("K"):
            self.status_banner = self.execute_action("nvapi_key")
        elif self.mode == "sessions" and ch == ord("v"):
            self.status_banner = self.execute_action("mirror")
        elif self.mode == "sessions" and ch == ord("R"):
            self.status_banner = self.execute_action("reload")
        elif self.mode == "sessions" and ch == ord("n"):
            self.status_banner = self.execute_action("new")
        elif self.mode == "sessions" and ch == ord("h"):
            self.status_banner = self.execute_action("hibernate")
        elif self.mode == "sessions" and ch in (ord("H"), ord("a")):
            self.status_banner = self.execute_action("hib_all")
        elif self.mode == "sessions" and ch == ord("x"):
            self.status_banner = self.execute_action("dedup")
        elif self.mode == "sessions" and ch in (ord("b"), ord("B")):
            self.status_banner = self.execute_action("baton")


def run_curses_tui(stdscr) -> None:
    """Mantiene la interfaz pública y delega la ejecución en Dashboard."""
    Dashboard(stdscr).run()
