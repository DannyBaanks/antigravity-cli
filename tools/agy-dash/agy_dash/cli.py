"""agy_dash.cli — split verbatim from agy_dashboard.py @ f255869 (Phase 1, no behaviour change)."""
from __future__ import annotations

import argparse
import curses
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import accounts as _accounts
from . import autocompact as _autocompact
from . import compact as _compact
from . import mirror as _mirror
from . import paths as _paths
from . import procs as _procs
from . import sessions as _sessions
from . import tmuxctl as _tmuxctl
from . import tui as _tui


KEYS_SHEET = [
    ("AGY DASH (cualquier pestaña)", [
        ("Alt+D  /  Alt+0", "ir al Dashboard (clic en ⚡: ir y volver)"),
        ("Alt+1 … Alt+9", "ir a la pestaña N"),
        ("Alt+←  /  Alt+→", "pestaña anterior / siguiente"),
        ("Alt+N", "nueva sesión AGY"),
        ("Alt+H", "hibernar todas las sesiones idle (0 MB)"),
        ("Alt+I", "pestaña ISyMCP (admin ISyMCP)"),
        ("Alt+C", "modo copia (q/Esc sale · Enter/y copia)"),
        ("clic en ×", "cerrar pestaña (si trabaja, va a agy-bg)"),
        ("clic medio en pestaña", "cerrar esa pestaña"),
        ("clic derecho", "menú de tmux (mover, renombrar…)"),
        ("arrastrar/doble/triple", "copiar selección / palabra / línea"),
        ("rueda arriba", "scroll del historial"),
    ]),
    ("COMMAND CENTER (pestaña ⚡ DASH)", [
        ("↑↓  /  j k", "mover cursor"),
        ("Enter", "abrir la sesión"),
        ("n", "nueva sesión"),
        ("t", "renombrar"),
        ("c  /  C", "compactar NVIDIA e ir / al Baúl"),
        ("V", "Baúl de compactadas"),
        ("v", "espejo en vivo de la sesión"),
        ("h  /  H", "hibernar la seleccionada / todas las idle"),
        ("B", "Baton pass a otra cuenta"),
        ("u  /  U", "cuentas / refrescar cuota"),
        ("K", "key de NVIDIA"),
        ("m", "memoria"),
        ("M", "ranking de modelos (ping/bench, swarm-ext board)"),
        ("r  /  R", "refrescar / recargar"),
        ("x", "limpiar duplicados"),
        ("q / Esc / Tab", "volver a la pestaña siguiente"),
        ("D", "desconectar (las sesiones siguen vivas)"),
    ]),
    ("AGY (dentro de una sesión)", [
        ("Alt+S  /  Alt+X", "board del swarm / ocultar aviso del swarm"),
        ("Ctrl+O", "expandir / colapsar herramientas"),
        ("Shift+Tab", "cambiar modo (plan, accept-edits, auto…)"),
        ("Shift+Enter  /  Ctrl+J", "nueva línea en el prompt"),
        ("Ctrl+G", "editar el prompt en $EDITOR"),
        ("Ctrl+R", "revisar artifacts pendientes"),
        ("Ctrl+B", "shell a segundo plano"),
        ("Ctrl+L", "limpiar pantalla"),
        ("Ctrl+Z", "deshacer en el prompt"),
        ("Alt+F / Alt+B · Ctrl+W", "palabra adelante / atrás · borrar palabra"),
        ("Ctrl+C", "interrumpir (2 veces: salir)"),
        ("?", "atajos propios de AGY"),
    ]),
]


def _keys_block(title: str, rows: list, kw: int) -> list[tuple[str, int]]:
    """Lines as (ansi_text, visible_len)."""
    bold, yel, rst = "\033[1m", "\033[33m", "\033[0m"
    out = [(f"{bold}{title}{rst}", len(title))]
    for k, desc in rows:
        out.append((f"  {yel}{k:<{kw}}{rst} {desc}", 3 + max(kw, len(k)) + len(desc)))
    out.append(("", 0))
    return out


def cmd_keys(args) -> int:
    """Print the cheat sheet (two columns if it fits); in a popup wait for one key."""
    import shutil as _sh
    cols = _sh.get_terminal_size((100, 40)).columns
    kw = 22
    blocks = [_keys_block(t, r, kw) for t, r in KEYS_SHEET]
    left_w = max(n for b in (blocks[0], blocks[2]) for _, n in b)
    right_w = max(n for _, n in blocks[1])
    if cols >= left_w + 3 + right_w:
        left, right = blocks[0] + blocks[2], blocks[1]
        lines = []
        for k in range(max(len(left), len(right))):
            lt, ln = left[k] if k < len(left) else ("", 0)
            rt, _ = right[k] if k < len(right) else ("", 0)
            lines.append(lt + " " * (left_w - ln + 3) + rt if rt else lt)
    else:
        lines = [t for b in blocks for t, _ in b]
    while lines and not lines[-1]:
        lines.pop()
    lines.append("\033[2mEl Alt+D del prompt de AGY (borrar palabra) lo usa el dash.   Cualquier tecla cierra.\033[0m")
    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()
    if sys.stdin.isatty():
        import termios, tty
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            os.read(fd, 16)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
    return 0


def cmd_live_mirror(args: argparse.Namespace) -> int:
    cid = args.conversation_id
    cwd = args.cwd or str(Path.cwd())
    title = args.title or f"Sesión {cid[:8]}"
    action = curses.wrapper(lambda stdscr: _mirror.run_live_mirror_curses(stdscr, cid, cwd, title))
    if action == "baton":
        _, _, sessions = _sessions.load_sessions(limit=40)
        sess = next((s for s in sessions if s.conversation_id == cid), None)
        if sess:
            ok, msg = _accounts.baton_pass_session(sess)
            if not ok:
                print(msg, file=sys.stderr)
                time.sleep(2.0)
                return cmd_live_mirror(args)
        return 0
    if action in ("migrate", "interactive"):
        _, _, sessions = _sessions.load_sessions(limit=40)
        sess = next((s for s in sessions if s.conversation_id == cid), None)
        if action == "migrate" and sess and sess.proc and not sess.proc.in_dash:
            if sess.external_busy:
                print("La copia externa tiene un turno o tarea activa; sigo en el espejo.", file=sys.stderr)
                time.sleep(2.0)
                return cmd_live_mirror(args)
            ok, why = _procs.terminate_verified(sess.proc)
            if not ok:
                print(f"No se migró: {why}", file=sys.stderr)
                time.sleep(2.0)
                return cmd_live_mirror(args)
        return _accounts.exec_agy_optimized(["--conversation", cid], cwd=cwd, preview_conv=cid, preview_title=title)
    return 0


# ============================================================================
# CLI Subcommands: status, statusbar, hibernate, wake-stub, open-new-tab
# ============================================================================

def cmd_status(_args: argparse.Namespace) -> int:
    scan_us, ksm_sys, sessions = _sessions.load_sessions(limit=25)
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
        mark = " *" if (s.proc and _procs.is_ancestor_pid(s.proc.pid)) else ""
        print(
            f"  {s.state_label:<11} {pid_s:<8} {s.location_label:<11} {pss_s:>6} {shr_s:>8} {swp_s:>6}  "
            f"{s.conversation_id[:8]:<9} {s.step_count:>5}  {s.title[:34]} ({ws_short}){mark}"
        )
    print()
    print("  Comandos: `agy-dash` (Dashboard 1 ventana) | `agy-dash hibernate all-idle` | `isymem list`")
    return 0


def cmd_statusbar(_args: argparse.Namespace) -> int:
    if _procs.ensure_zig_core():
        os.execv(str(_paths.ZIG_CORE_BIN), [str(_paths.ZIG_CORE_BIN), "statusbar"])
    _, _, sessions = _sessions.load_sessions(limit=20)
    live = [s for s in sessions if s.proc is not None]
    hib = [s for s in sessions if s.hibernated]
    pss_mb = sum(s.proc.pss_kb for s in live if s.proc) // 1024
    print(f"RAM:{pss_mb}M PSS · {len(live)}🔥 {len(hib)}💤")
    return 0


def cmd_hibernate(args: argparse.Namespace) -> int:
    _, _, sessions = _sessions.load_sessions(limit=60)
    target = args.target.strip()
    if target in ("all-idle", "idle", "all"):
        dedup_kb = 0
        for s in sessions:
            dedup_kb += _sessions.deduplicate_session_procs(s)
            dedup_kb += _sessions.close_stale_dash_duplicates(s)
        candidates = [s for s in sessions if s.can_hibernate]
        if not candidates and dedup_kb == 0:
            print("No hay sesiones AGY ociosas (IDLE con not_fully_idle=0) fuera de la sesión actual.")
            return 0
        total_freed = dedup_kb
        for s in candidates:
            saved = (s.proc.pss_kb + s.proc.swap_kb) if s.proc else 0
            ok, msg = _sessions.hibernate_session(s)
            print(("  [OK] " if ok else "  [--] ") + msg)
            if ok:
                total_freed += saved
                _tmuxctl._replace_tmux_pane_with_wake_stub(s)
        print(f"Total liberado: {total_freed // 1024} MB RAM.")
        return 0

    for s in sessions:
        if (
            s.conversation_id.startswith(target)
            or (s.proc and str(s.proc.pid) == target)
            or target.lower() in s.title.lower()
        ):
            ok, msg = _sessions.hibernate_session(s)
            print(msg)
            if ok:
                _tmuxctl._replace_tmux_pane_with_wake_stub(s)
            return 0 if ok else 1

    print(f"No se encontró sesión coincidente con '{target}'.", file=sys.stderr)
    return 1


def cmd_wake_stub(args: argparse.Namespace) -> int:
    cid = args.conversation_id
    cwd = args.cwd or str(Path.cwd())
    title = _sessions.load_custom_titles().get(cid) or args.title or f"Sesión {cid[:8]}"

    _sessions.print_instant_zig_preview(cid, title, cwd)
    est_tok, size_b = _compact.estimate_session_tokens(cid)
    nv_cfg = _compact.load_nvapi_config()
    print(
        f"\n\033[1;33m  💤 SESIÓN EN MODO HIBERNADO (0 MB RAM — Contexto en disco: {size_b // 1024} KB / ~{est_tok:,} tokens est.)\033[0m"
    )
    print(
        f"  \033[1;35m[c]\033[0m 🗜️ Compactar con NVIDIA Nemotron ({_compact.mask_api_key(nv_cfg['api_key'])}) y abrir ligera (~95%+ ahorro tokens)"
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
        _tmuxctl._select_dash_window()
        return cmd_wake_stub(args)
    if ans == "k":
        try:
            k_in = input("  Introduce tu NVIDIA API Key (nvapi-...): ").strip()
            if k_in:
                _compact.save_nvapi_config(k_in)
                print("  ✅ NVIDIA API Key guardada en ~/.gemini/antigravity-cli/nvapi_config.json (0600).")
        except (EOFError, KeyboardInterrupt):
            pass
        return cmd_wake_stub(args)
    if ans == "c":
        print("  ⏳ Compactando conversación con NVIDIA NIM (Nemotron)...")
        dummy_sess = _sessions.AgySession(
            conversation_id=cid,
            title=title,
            preview="",
            step_count=0,
            last_modified="",
            workspace=cwd,
            db_status="CASCADE_RUN_STATUS_IDLE",
            not_fully_idle=False,
        )
        ok, msg, hfile = _compact.compact_conversation_with_nvapi(dummy_sess)
        print(f"  {msg}")
        if ok and hfile is not None:
            reg = _sessions.load_hibernated_registry()
            if cid in reg:
                del reg[cid]
                _sessions.save_hibernated_registry(reg)
            boot_prompt = (
                f"Contexto compactado de la sesión '{title}' ({cid[:8]}). "
                f"Lee primero `{hfile}` para retomar el estado exacto sin gastar tokens de historial y confirma que estás listo."
            )
            return _accounts.exec_agy_optimized(["-i", boot_prompt], cwd=cwd, preview_conv=cid, preview_title=title)
        time.sleep(2.0)
        return cmd_wake_stub(args)

    reg = _sessions.load_hibernated_registry()
    if cid in reg:
        del reg[cid]
        _sessions.save_hibernated_registry(reg)
    return _accounts.exec_agy_optimized(["--conversation", cid], cwd=cwd, preview_conv=cid, preview_title=title)


def cmd_open_new_tab(args: argparse.Namespace) -> int:
    self_path = _paths.SELF_PATH
    cwd = getattr(args, "cwd", None) or str(Path.cwd())
    reg = _accounts.load_accounts_registry()
    slot = getattr(args, "account", "") or reg.get("active_slot", "primary")
    tab_prefix = f"●{slot[:3]}·" if slot != "primary" else "●"
    cmd = f"exec python3 {_tmuxctl.sh_quote(str(self_path))} run --cwd {_tmuxctl.sh_quote(cwd)} --account {_tmuxctl.sh_quote(slot)}"
    _tmuxctl.tmux_cmd("new-window", "-t", _paths.TMUX_SESSION, "-n", f"{tab_prefix}nueva", "-c", cwd, cmd)
    return 0


def cmd_close_tab(args: argparse.Namespace) -> int:
    msg = _tmuxctl.close_tab_safely(str(args.window_index))
    _tmuxctl.tmux_cmd("display-message", "-d", "2500", msg)
    return 0


def cmd_show_dash(args: argparse.Namespace) -> int:
    """Ensure ⚡DASH exists at window 0 and focus (or toggle back if already there)."""
    _tmuxctl.ensure_dash_window_zero()
    if getattr(args, "toggle", False):
        cur = _tmuxctl.tmux_cmd("display-message", "-p", "-t", _paths.TMUX_SESSION, "#{window_name}", capture=True)
        if cur.returncode == 0 and "DASH" in (cur.stdout or ""):
            win_count = _tmuxctl.tmux_cmd("list-windows", "-t", _paths.TMUX_SESSION, "-F", "#{window_index}", capture=True)
            if win_count.returncode == 0 and len(win_count.stdout.strip().splitlines()) > 1:
                _tmuxctl.tmux_cmd("last-window", "-t", _paths.TMUX_SESSION)
                return 0
    _tmuxctl._select_dash_window()
    return 0


def cmd_isymcp_tab(args: argparse.Namespace) -> int:
    """Create or select the ISyMCP admin tab (🔌ISyMCP) running `isymcp dash`."""
    cp = _tmuxctl.tmux_cmd("list-windows", "-t", _paths.TMUX_SESSION, "-F", "#{window_index}|#{window_name}", capture=True)
    if cp.returncode == 0 and cp.stdout:
        for line in cp.stdout.splitlines():
            parts = line.split("|", 1)
            if len(parts) == 2 and "🔌ISyMCP" in parts[1]:
                _tmuxctl.tmux_cmd("select-window", "-t", f"{_paths.TMUX_SESSION}:{parts[0].strip()}")
                return 0
    # bash -c (not sh: dash has no read -n/-s/-p): fallback message if `isymcp dash` is missing
    cmd = "isymcp dash || { echo; echo \"isymcp dash no está disponible (actualiza ISyMCP).\"; read -n1 -s -r -p \"Pulsa una tecla para cerrar\"; }"
    cp_new = _tmuxctl.tmux_cmd(
        "new-window", "-d", "-P", "-F", "#{window_index}", "-t", _paths.TMUX_SESSION, "-n", "🔌ISyMCP", "bash", "-c", cmd, capture=True
    )
    if cp_new.returncode == 0 and cp_new.stdout.strip():
        new_idx = cp_new.stdout.strip().splitlines()[-1]
        _tmuxctl.tmux_cmd("select-window", "-t", f"{_paths.TMUX_SESSION}:{new_idx}")
    return 0


def cmd_accounts(args: argparse.Namespace) -> int:
    action = getattr(args, "account_cmd", None) or "list"
    slot_arg = getattr(args, "slot", "")
    reg = _accounts.load_accounts_registry()

    if action == "list":
        _accounts._print_accounts_table(reg)
        return 0
    if action == "use":
        if not slot_arg:
            print("Uso: agy-dash account use <slot>", file=sys.stderr)
            return 1
        safe = _accounts.validate_slot_name(slot_arg)
        if safe not in reg.get("accounts", {}):
            print(f"Cuenta '{safe}' no registrada.", file=sys.stderr)
            return 1
        reg["active_slot"] = safe
        _accounts.save_accounts_registry(reg)
        print(f"Cuenta por defecto cambiada a [{safe}].")
        return 0
    if action == "add":
        if not slot_arg:
            idx = 2
            while f"pro{idx}" in reg.get("accounts", {}):
                idx += 1
            slot_arg = f"pro{idx}"
        safe = _accounts.validate_slot_name(slot_arg)
        if getattr(args, "tab", False) and shutil.which("tmux"):
            has = _tmuxctl.tmux_cmd("has-session", "-t", _paths.TMUX_SESSION, capture=True)
            if has.returncode == 0:
                print(_tmuxctl.launch_account_login_tab(safe))
                return 0
        return cmd_account_login(argparse.Namespace(slot=safe))
    if action == "remove":
        if not slot_arg:
            print("Uso: agy-dash account remove <slot> [--yes]", file=sys.stderr)
            return 1
        _, _, sessions = _sessions.load_sessions(limit=50)
        info = _accounts.inspect_account_removal(slot_arg, sessions=sessions)
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
        ok, msg = _accounts.remove_account_slot(slot_arg, confirmed=confirmed, sessions=sessions)
        print(msg, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1
    print(f"Subcomando desconocido: {action}", file=sys.stderr)
    return 1


def cmd_account_login(args: argparse.Namespace) -> int:
    safe_slot = _accounts.validate_slot_name(args.slot)
    if safe_slot == "primary":
        print("`primary` usa el keyring del sistema; ejecutando check de cuota...")
        _, msg = _accounts.refresh_account_quota("primary")
        print(msg)
        return 0

    _accounts.ensure_isolated_home(safe_slot)
    reg = _accounts.load_accounts_registry()
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
    _accounts.save_accounts_registry(reg)
    env = _accounts.build_agy_env(safe_slot)

    print("╔════════════════════════════════════════════════════════════════════════════╗")
    print(f"║  🔑 LOGIN AISLADO PARA CUENTA [{safe_slot:<12}] (SIN CERRAR TU PRINCIPAL)       ║")
    print("╚════════════════════════════════════════════════════════════════════════════╝")
    print("  1. Se abrirá el navegador automáticamente y la URL completa se copiará al portapapeles.")
    print("  2. Cuando veas el prompt de AGY listo, escribe `/exit` (o Ctrl+D) para guardar y volver.\n")
    subprocess.run([_paths.AGY_BIN], env=env)
    ok, msg = _accounts.refresh_account_quota(safe_slot, allow_unauthenticated=True)
    print(f"\n  Resultado: {msg}")
    if sys.stdin.isatty():
        _tmuxctl._select_dash_window()
    return 0 if ok else 1


def cmd_quota(args: argparse.Namespace) -> int:
    reg = _accounts.load_accounts_registry()
    if getattr(args, "refresh", False):
        for slot in list(reg.get("accounts", {}).keys()):
            ok, msg = _accounts.refresh_account_quota(slot)
            print(msg)
        reg = _accounts.load_accounts_registry()
    _accounts._print_accounts_table(reg)
    return 0


def cmd_baton(args: argparse.Namespace) -> int:
    target = args.target
    _, _, sessions = _sessions.load_sessions(limit=50)
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
        pf = _accounts.baton_preflight(sel, target_slot=target_slot, model=model)
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

    ok, msg = _accounts.baton_pass_session(
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
        cfg = _compact.load_nvapi_config()
        print(f"  NVIDIA API Key : {_compact.mask_api_key(cfg['api_key'])}")
        print(f"  Base URL       : {cfg['base_url']}")
        print(f"  Modelo Nemotron: {cfg['model']}")
        print(f"  Archivo config : {_paths.NVAPI_CONFIG_FILE} (0600)")
        return 0
    if sub_act == "set-key":
        key_val = getattr(args, "key", "") or ""
        if not key_val and sys.stdin.isatty():
            key_val = input("Introduce tu NVIDIA API Key (nvapi-...): ").strip()
        if not key_val:
            print("Uso: agy-dash nvapi set-key <nvapi-...> [--model ...] [--base-url ...]", file=sys.stderr)
            return 1
        cfg = _compact.save_nvapi_config(
            key_val,
            model=getattr(args, "model", "") or "",
            base_url=getattr(args, "base_url", "") or "",
        )
        print(f"✅ NVIDIA API Key guardada ({_compact.mask_api_key(cfg['api_key'])}, modelo={cfg['model']}).")
        return 0
    return 1


def cmd_compact(args: argparse.Namespace) -> int:
    target = (getattr(args, "target", "") or "self").strip()
    _, _, sessions = _sessions.load_sessions(limit=60)
    if target.lower() in ("self", "current", "."):
        resolved_cid = _compact.detect_caller_conversation_id(sessions)
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
        self_path = _paths.SELF_PATH
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
        if _tmuxctl._find_tmux_window_by_conv(sel.conversation_id) is not None or _tmuxctl.tmux_cmd("has-session", "-t", _paths.TMUX_SESSION, capture=True).returncode == 0:
            _tmuxctl.tmux_cmd("display-message", "-d", "4000", f"🗜️ Compactando '{sel.title[:28]}' con NVIDIA Nemotron en 2º plano...")
        print(f"🗜️ Compactando '{sel.title}' ({sel.conversation_id[:8]}) con NVIDIA Nemotron en 2º plano...")
        print(f"[███░░░░░░░] ⏳ Generando handoff -> {_paths.COMPACTED_DIR / (sel.conversation_id[:8] + '_handoff.md')}")
        if getattr(args, "open", False):
            print("🚀 Al terminar se abrirá automáticamente la nueva pestaña ligera con `agy_handoff_context`.")
        return 0

    if getattr(args, "model", ""):
        _compact.save_nvapi_config(model=str(args.model))

    ok, msg, hfile = _compact.compact_conversation_with_nvapi(sel)
    if getattr(args, "bg_worker", False):
        if ok and hfile is not None:
            if getattr(args, "open", False):
                _compact.open_compacted_session_in_tab(sel, hfile, switch_to=True)
            _tmuxctl.tmux_cmd("display-message", "-d", "4000", f"✅ {msg}")
        else:
            _tmuxctl.tmux_cmd("display-message", "-d", "5000", f"❌ {msg}")
        return 0 if ok else 1

    print(msg, file=sys.stdout if ok else sys.stderr)
    if not ok or hfile is None:
        return 1
    print(f"  Handoff guardado en: {hfile}")
    if getattr(args, "open", False):
        print(f"  {_compact.open_compacted_session_in_tab(sel, hfile)}")
    return 0


def cmd_rename(args: argparse.Namespace) -> int:
    target = args.target
    _, _, sessions = _sessions.load_sessions(limit=60)
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
    clean = _sessions.save_custom_title(sel.conversation_id, new_t)
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

    # Machine-side compaction (off unless AGY_DASH_MACHINE_COMPACT=1): stops the turn before the
    # model reads the context and compacts with NVIDIA in the background. Fail-closed to {}.
    if payload and not getattr(args, "status", False):
        auto = _autocompact.decide(payload)
        if auto:
            print(json.dumps(auto, ensure_ascii=False))
            return 0

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

    hb = _compact.evaluate_context_heartbeat(
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


def cmd_self_compact(args: argparse.Namespace) -> int:
    """Compact an active agent session with NVIDIA Nemotron 550B and continue in-place in the same TUI tab."""
    target = (getattr(args, "target", "") or "self").strip()
    _, _, sessions = _sessions.load_sessions(limit=60)
    if target.lower() in ("self", "current", "."):
        resolved_cid = _compact.detect_caller_conversation_id(sessions)
        if not resolved_cid:
            print("No se pudo detectar la conversación AGY actual; pasa `conv_id`.", file=sys.stderr)
            return 1
        target = resolved_cid

    matches = [s for s in sessions if s.conversation_id == target]
    if not matches and len(target) >= 4:
        matches = [s for s in sessions if s.conversation_id.startswith(target)]
    if not matches:
        matches = [s for s in sessions if target.lower() in s.title.lower()]
    if len(matches) > 1:
        print(f"'{target}' coincide con {len(matches)} sesiones; pasa el conv_id completo.", file=sys.stderr)
        return 1
    if matches:
        sel = matches[0]
    else:
        custom_titles = _sessions.load_custom_titles()
        sel = _sessions.AgySession(
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

    win_idx = (getattr(args, "win", "") or "").strip() or _tmuxctl._find_tmux_window_by_conv(sel.conversation_id) or ""
    resume_prompt = str(getattr(args, "resume_prompt", "") or "").strip()
    no_respawn = bool(getattr(args, "no_respawn", False))

    if getattr(args, "bg", False) and not getattr(args, "bg_worker", False):
        _compact._write_compact_status(sel.conversation_id, "running", "Compactando con NVIDIA Nemotron 550B...")
        self_path = _paths.SELF_PATH
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
        worker_cmd.extend(["--wait", str(float(getattr(args, "wait", 900.0) or 900.0))])
        if resume_prompt:
            worker_cmd.extend(["--resume-prompt", resume_prompt])
        if getattr(args, "model", ""):
            worker_cmd.extend(["--model", str(args.model)])
        if not getattr(args, "in_place", True):
            worker_cmd.append("--no-in-place")
        if no_respawn:
            worker_cmd.append("--no-respawn")
        _paths.COMPACTED_DIR.mkdir(parents=True, exist_ok=True)
        err_log = (_paths.COMPACTED_DIR / ".last_self_compact.log").open("ab")
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
        _tmuxctl.tmux_cmd(
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
                    "handoff_target": str(_paths.COMPACTED_DIR / f"{sel.conversation_id[:8]}_handoff.md"),
                    "message": "Auto-compactación Nemotron 550B iniciada en 2º plano.",
                },
                ensure_ascii=False,
            )
        )
        return 0

    _compact._write_compact_status(sel.conversation_id, "running", "Llamando a NVIDIA NIM (Nemotron 550B)...")
    if getattr(args, "model", ""):
        _compact.save_nvapi_config(model=str(args.model))

    if getattr(args, "respawn_only", False):
        hfile = _compact.get_vault_handoff(sel.conversation_id)
        if hfile is None:
            print(f"No existe handoff en Baúl para '{sel.conversation_id[:8]}'.", file=sys.stderr)
            return 1
        ok, msg = True, f"Reanudando desde Baúl ({hfile.name})"
    else:
        ok, msg, hfile = _compact.compact_conversation_with_nvapi(sel)
    if not ok or hfile is None:
        _compact._write_compact_status(sel.conversation_id, "error", msg)
        _tmuxctl.tmux_cmd("display-message", "-d", "5000", f"❌ {msg}")
        print(msg, file=sys.stderr)
        return 1

    _compact._write_compact_status(sel.conversation_id, "done", msg, hfile)
    if no_respawn:
        _tmuxctl.tmux_cmd("display-message", "-d", "4000", f"✅ {msg}")
        if not getattr(args, "bg_worker", False):
            print(f"{msg} · Handoff: {hfile}")
        return 0

    # Respawning kills the pane: wait until the turn is really over (never cut a reply mid-way).
    wait_s = float(getattr(args, "wait", 900.0) or 900.0)
    deadline = time.time() + wait_s
    quiet_since = 0.0
    while time.time() < deadline:
        cur = _sessions.refresh_session(sel.conversation_id)
        busy = cur is not None and cur.proc is not None and (
            cur.db_status == "CASCADE_RUN_STATUS_RUNNING"
            or _sessions.is_transcript_turn_active(sel.conversation_id, max_recent_sec=3.0)
            or bool(cur.proc.active_children)
        )
        if not busy:
            quiet_since = quiet_since or time.time()
            if time.time() - quiet_since >= 3.0:
                break
        else:
            quiet_since = 0.0
        time.sleep(1.0)
    else:
        msg_busy = f"{msg} · la sesión siguió ocupada {wait_s:.0f}s; handoff en el Baúl, no se cortó el turno"
        _compact._write_compact_status(sel.conversation_id, "done", msg_busy, hfile)
        _tmuxctl.tmux_cmd("display-message", "-d", "5000", f"⚠ {msg_busy}")
        if not getattr(args, "bg_worker", False):
            print(msg_busy)
        return 2

    if getattr(args, "in_place", True):
        res_msg = _compact.respawn_compacted_session_in_place(
            sel,
            hfile,
            resume_prompt=resume_prompt,
            win_idx=win_idx or None,
        )
    else:
        res_msg = _compact.open_compacted_session_in_tab(sel, hfile, switch_to=True)
    _tmuxctl.tmux_cmd("display-message", "-d", "4000", f"✅ {res_msg}")
    if not getattr(args, "bg_worker", False):
        print(f"{msg} · {res_msg}")
    return 0


def cmd_tui(_args: argparse.Namespace) -> int:
    curses.wrapper(_tui.run_curses_tui)
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    if not shutil.which("tmux"):
        return cmd_tui(args)

    self_path = _paths.SELF_PATH
    _tmuxctl.write_tmux_config(self_path)
    detach_only = getattr(args, "detach", False) or not sys.stdin.isatty()

    if _paths.TMUX_SOCKET in os.environ.get("TMUX", ""):
        _tmuxctl.ensure_dash_window_zero()
        _tmuxctl.tmux_cmd("source-file", str(_paths.TMUX_CONF))
        if not detach_only:
            _tmuxctl._select_dash_window()
        return 0

    has = _tmuxctl.tmux_cmd("has-session", "-t", _paths.TMUX_SESSION, capture=True)
    if has.returncode != 0:
        tui_cmd = f"exec python3 {_tmuxctl.sh_quote(str(self_path))} tui"
        _tmuxctl.tmux_cmd("new-session", "-d", "-s", _paths.TMUX_SESSION, "-n", "⚡DASH", tui_cmd)
        for s in _sessions.load_sessions(limit=20)[2]:
            if s.hibernated:
                _tmuxctl.open_session_in_dashboard_tab(s, switch_to=False, as_stub=True)
        _tmuxctl._select_dash_window()
    else:
        _tmuxctl.ensure_dash_window_zero()
        _tmuxctl.tmux_cmd("source-file", str(_paths.TMUX_CONF))

    if detach_only:
        print(f"Dashboard listo en servidor tmux `{_paths.TMUX_SOCKET}` (sesión `{_paths.TMUX_SESSION}`).")
        return 0

    os.execvp("tmux", ["tmux", "-L", _paths.TMUX_SOCKET, "-f", str(_paths.TMUX_CONF), "attach-session", "-t", _paths.TMUX_SESSION])
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

    p_keys = sub.add_parser("keys", help="Cheat sheet of every shortcut (the ⌨ Atajos button opens it in a popup)")
    p_keys.set_defaults(fn=cmd_keys)

    p_sd = sub.add_parser("show-dash", help="Ensure ⚡DASH exists and switch focus to it (Alt+D / ⚡AGY DASH button)")
    p_sd.add_argument("--toggle", action="store_true", help="Return to previous tab when already viewing DASH")
    p_sd.set_defaults(fn=cmd_show_dash)

    p_ism = sub.add_parser("isymcp-tab", help="Create or select the ISyMCP admin tab (🔌ISyMCP) running `isymcp dash`")
    p_ism.set_defaults(fn=cmd_isymcp_tab)

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
    p_sc.add_argument("--wait", type=float, default=900.0, help="Max seconds waiting for the turn to end before respawn")
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
        return _accounts.exec_agy_optimized(
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
