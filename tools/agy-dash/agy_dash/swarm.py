"""Model selector & /swarm picker for AGY (ported from claude-dash/swarm.py).

Interactive curses popup with type-to-filter, ★ ranking, live `p` ping in a
background thread, `M` to open `swarm-ext board`, and `Enter` to select (with
TUI selector for `nvapi:*` models). Used both by `agy-dash swarm pick` and by
the dashboard's `[M] 🏆 Modelos` button to pick the active AGY model.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import accounts as _accounts
from . import paths as _paths

EXIT_NO_POPUP = 3
EXIT_CANCELLED = 2

EXT_PROVIDERS = "agy,nvapi,codex,claude,opencode,freellm,qwen"
# TUIs that run nvapi:<model> (ISyCo tools/swarm_models, 2026-11-18 medians); first = default.
VIA_TUIS = [
    ("isycode", "1.2 s"),
    ("opencode", "1.2 s"),
    ("cn", "2.6 s"),
    ("forge", "3.1 s"),
    ("openisy", "3.3 s"),
    ("crush", "3.6 s"),
]


def _swarm_ext(swarm_ext: str | None = None) -> str | None:
    return swarm_ext if swarm_ext is not None else shutil.which("swarm-ext")


def _rank_key(r: dict) -> tuple:
    """recommended first, then ranked (1, 2, ...), then the rest in catalog order."""
    rk = r.get("rank")
    return (not r.get("recommended"), not isinstance(rk, (int, float)), rk if isinstance(rk, (int, float)) else 0)


def _builtin_agy_rows() -> list[dict]:
    """Fallback list of native AGY models when swarm-ext is unavailable."""
    return [
        {"provider": "agy", "spec": f"agy:{slug}", "label": f"[AGY] {disp}", "free": False}
        for disp, slug in _paths.MODEL_DISPLAY_TO_SLUG.items()
    ]


def catalog(
    xkiro: bool = False,
    show_all: bool = False,
    swarm_ext: str | None = None,
    rank: str | None = None,
    providers: str | None = None,
) -> tuple[list[dict], str]:
    """(rows, note). Queries `swarm-ext catalog --json` and falls back to native AGY models."""
    exe = _swarm_ext(swarm_ext)
    if not exe:
        return _builtin_agy_rows(), (
            "swarm-ext no está en el PATH: solo modelos AGY locales "
            "(Codex/Claude/NVAPI/OpenCode necesitan ISyCo tools/swarm_models)"
        )
    prov = providers or (EXT_PROVIDERS + (",xkiro" if xkiro else ""))
    cmd = [exe, "catalog", "--json", "--providers", prov]
    if show_all:
        cmd.append("--all")
    if rank:
        cmd += ["--rank", rank]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
        ext = json.loads(p.stdout or "[]")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as e:
        return _builtin_agy_rows(), f"swarm-ext catalog falló ({e}); solo modelos AGY locales"
    rows = [r for r in ext if isinstance(r, dict) and r.get("spec") and r.get("label")]
    if not rows:
        return _builtin_agy_rows(), "swarm-ext devolvió catálogo vacío; usando modelos AGY locales"
    if rank:
        rows.sort(key=_rank_key)
    return rows, ""


def badge(r: dict) -> str:
    """Status column: ★ recommended · ping ✓/✗/…/· · latency · score."""
    st = r.get("stats") or {}
    star = "★" if r.get("recommended") else " "
    ok = st.get("last_ping_ok")
    ping = "…" if r.get("_pinging") else "✓" if ok is True else "✗" if ok is False else "·"
    lat = st.get("latency_s")
    lat_s = f"{lat:5.1f}s" if isinstance(lat, (int, float)) else "    -"
    sc = r.get("score")
    sc_s = f"{sc:.2f}" if isinstance(sc, (int, float)) else "  - "
    return f"{star} {ping} {lat_s} {sc_s}"


def ping_row(r: dict, swarm_ext: str | None = None, timeout: int = 90) -> dict:
    """Ping one model through swarm-ext (it records the result in its corpus)."""
    exe = _swarm_ext(swarm_ext)
    if not exe:
        return {"ok": False, "err": "sin swarm-ext"}
    try:
        p = subprocess.run(
            [exe, "ping", "--json", "--timeout", str(timeout), r["spec"]],
            capture_output=True,
            text=True,
            timeout=timeout + 30,
            stdin=subprocess.DEVNULL,
        )
        res = json.loads(p.stdout or "[]")
        return res[0] if res else {"ok": False, "err": (p.stderr or "sin salida").strip()[:120]}
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as e:
        return {"ok": False, "err": str(e)[:120]}


def apply_ping(r: dict, res: dict) -> None:
    st = r.setdefault("stats", {})
    if res.get("ok") is None:
        return
    st["pinged"] = True
    st["last_ping_ok"] = bool(res.get("ok"))
    st["last_err"] = res.get("err", "")
    if isinstance(res.get("latency_s"), (int, float)):
        st["latency_s"] = res["latency_s"]


def parse_workers(items: list[str]) -> list[dict]:
    out = []
    for it in items:
        name, _, desc = it.partition("=")
        name = name.strip()
        if not name:
            raise ValueError(f"worker sin nombre: {it!r}")
        out.append({"name": name, "desc": desc.strip()})
    if len({w["name"] for w in out}) != len(out):
        raise ValueError("nombres de worker repetidos")
    return out


def numbered(rows: list[dict]) -> str:
    out = []
    for i, r in enumerate(rows, 1):
        b = badge(r)
        out.append(f"{i}\t{r['label']}" + (f"  [{b.strip()}]" if b.strip() else "") + f"\t{r['spec']}")
    return "\n".join(out)


def set_active_model(spec_or_label: str, agy_data_dir: Path | None = None) -> tuple[bool, str]:
    """Persist the chosen AGY model display name into `settings.json`."""
    raw = (spec_or_label or "").strip()
    if raw.startswith("agy:"):
        raw = raw[4:].strip()
    elif raw.startswith("[AGY] "):
        raw = raw[6:].strip()
    disp, _slug = _accounts.resolve_model_arg(raw, agy_data_dir=agy_data_dir)
    if not disp:
        return False, "Modelo inválido"
    data_dir = agy_data_dir or _paths.AGY_DATA_DIR
    data_dir.mkdir(parents=True, exist_ok=True)
    settings_path = data_dir / "settings.json"
    cfg: dict = {}
    if settings_path.is_file():
        try:
            loaded = json.loads(settings_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                cfg = loaded
        except Exception:
            cfg = {}
    cfg["model"] = disp
    tmp = settings_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(settings_path)
    return True, disp


# ------------------------------------------------------------------ popup UI


def pick_ui(req_path: Path, out_path: Path) -> int:
    """curses picker, one screen per worker (its own ranked catalog). Keys: p pings the
    highlighted model in a thread, M opens `swarm-ext board`, Enter picks (nvapi asks
    which TUI runs it unless skip_via is set). Writes {"picks": [...]} or {"cancelled": true}."""
    import curses
    import threading

    from .termtext import first_fit, put

    req = json.loads(req_path.read_text(encoding="utf-8"))
    workers = req["workers"]
    exe = req.get("swarm_ext") or _swarm_ext(None)
    skip_via = bool(req.get("skip_via"))
    current_model = str(req.get("current_model") or "").strip()
    picks: list[dict] = []
    msg = [""]

    def colors() -> None:
        try:
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_CYAN)
            curses.init_pair(2, curses.COLOR_YELLOW, -1)
            curses.init_pair(3, curses.COLOR_GREEN, -1)
        except curses.error:
            pass

    def ping_bg(r: dict) -> None:
        r["_pinging"] = True
        msg[0] = f"ping {r['spec']} …"
        res = ping_row(r, exe)
        apply_ping(r, res)
        r.pop("_pinging", None)
        msg[0] = (
            f"ping {r['spec']}: "
            + ("PONG " if res.get("ok") else "sin respuesta ")
            + (f"{res.get('latency_s', '')}s" if res.get("ok") else str(res.get("err", ""))[:60])
        )

    def board(stdscr, w: dict) -> None:
        if not exe:
            msg[0] = "swarm-ext no está instalado: sin board"
            return
        curses.def_prog_mode()
        curses.endwin()
        try:
            cmd = [exe, "board", "--providers", EXT_PROVIDERS]
            if w.get("desc") and not skip_via:
                cmd += ["--task", w.get("desc") or w["name"]]
            subprocess.run(cmd)
        except OSError as e:
            msg[0] = f"board falló: {e}"
        curses.reset_prog_mode()
        stdscr.clear()

    def via_screen(stdscr, r: dict) -> str | None:
        cur = 0
        while True:
            stdscr.erase()
            H, W = stdscr.getmaxyx()
            put(stdscr, 0, 0, f"¿Qué TUI ejecuta {r['spec']}? (isycode por defecto · bench 2026-11-18)", W - 1, curses.A_BOLD)
            for i, (name, lat) in enumerate(VIA_TUIS):
                attr = curses.color_pair(1) | curses.A_BOLD if i == cur else curses.A_NORMAL
                put(stdscr, 2 + i, 0, f"  {name:<10} mediana {lat}" + ("  (por defecto)" if i == 0 else ""), W - 1, attr)
            put(stdscr, H - 1, 0, "↑↓ mover · Enter elegir · Esc volver", W - 1, curses.A_DIM)
            stdscr.refresh()
            try:
                k = stdscr.get_wch()
            except curses.error:
                continue
            if k in ("\n", "\r", curses.KEY_ENTER):
                return VIA_TUIS[cur][0]
            if k == "\x1b":
                return None
            if k == curses.KEY_UP:
                cur = max(0, cur - 1)
            elif k == curses.KEY_DOWN:
                cur = min(len(VIA_TUIS) - 1, cur + 1)

    def screen(stdscr, w: dict, idx: int, rows: list[dict]) -> tuple[int, str | None] | None:
        curses.curs_set(0)
        colors()
        stdscr.timeout(400)
        flt, cur, top = "", 0, 0
        if current_model:
            for ci, r in enumerate(rows):
                lbl = r.get("label", "")
                if lbl == f"[AGY] {current_model}" or lbl == current_model or r.get("spec") == f"agy:{current_model}":
                    cur = ci
                    break
        while True:
            vis = [i for i, r in enumerate(rows) if flt.lower() in (r["label"] + r["spec"]).lower()]
            cur = max(0, min(cur, len(vis) - 1))
            H, W = stdscr.getmaxyx()
            body = max(1, H - 6)
            if cur < top:
                top = cur
            elif cur >= top + body:
                top = cur - body + 1
            stdscr.erase()
            hdr = w.get("title") or f"¿Qué modelo para `{w['name']}`? ({idx + 1}/{len(workers)})"
            put(stdscr, 0, 0, hdr, W - 1, curses.A_BOLD)
            if w.get("desc"):
                put(stdscr, 1, 0, w["desc"], W - 1, curses.color_pair(2))
            put(stdscr, 2, 0, "        ★ ping   lat  score  modelo", W - 1, curses.A_DIM)
            for line, vi in enumerate(vis[top : top + body]):
                r = rows[vi]
                sel = top + line == cur
                is_cur = bool(current_model and r.get("label") == f"[AGY] {current_model}")
                mark = "● " if is_cur else "  "
                attr = (
                    curses.color_pair(1) | curses.A_BOLD
                    if sel
                    else (curses.color_pair(3) | curses.A_BOLD if (r.get("recommended") or is_cur) else curses.A_NORMAL)
                )
                put(stdscr, 3 + line, 0, f" {vi + 1:>4}  {badge(r):<18} {mark}{r['label']}", W - 1, attr)
            if msg[0]:
                put(stdscr, H - 2, 0, msg[0], W - 1, curses.color_pair(2))
            pre = f"filtro: {flt}_ · " if flt else ""
            foot = first_fit(
                [
                    pre + "↑↓ PgUp/PgDn · escribe para filtrar · p/Ctrl+P ping · M board · Enter elegir · Esc cancelar",
                    pre + "↑↓ · teclea=filtro · p ping · M board · Enter elegir · Esc salir",
                    pre + "↑↓ Enter Esc · p ping · M board",
                ],
                W - 1,
            )
            put(stdscr, H - 1, 0, foot, W - 1, curses.A_DIM)
            stdscr.refresh()
            try:
                k = stdscr.get_wch()
            except curses.error:
                continue
            if k in ("\n", "\r", curses.KEY_ENTER):
                if not vis:
                    continue
                r = rows[vis[cur]]
                if r.get("provider") != "nvapi" or skip_via:
                    return vis[cur], None
                stdscr.timeout(-1)
                via = via_screen(stdscr, r)
                stdscr.timeout(400)
                if via:
                    return vis[cur], via
            elif k == "\x1b":
                if flt:
                    flt, cur = "", 0
                else:
                    return None
            elif k == curses.KEY_UP:
                cur -= 1
            elif k == curses.KEY_DOWN:
                cur += 1
            elif k == curses.KEY_PPAGE:
                cur -= body
            elif k == curses.KEY_NPAGE:
                cur += body
            elif k == curses.KEY_HOME:
                cur = 0
            elif k == curses.KEY_END:
                cur = len(vis) - 1
            elif k in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                flt, cur = flt[:-1], 0
            elif (k == "p" and not flt) or k == "\x10":
                if vis and not rows[vis[cur]].get("_pinging"):
                    threading.Thread(target=ping_bg, args=(rows[vis[cur]],), daemon=True).start()
            elif k == "M" and not flt:
                board(stdscr, w)
            elif isinstance(k, str) and k.isprintable():
                flt, cur = flt + k, 0

    def run(stdscr) -> bool:
        for i, w in enumerate(workers):
            rows = w.get("catalog") or req.get("catalog") or []
            got = screen(stdscr, w, i, rows)
            if got is None:
                return False
            sel, via = got
            r = rows[sel]
            pk = {"worker": w["name"], "provider": r["provider"], "spec": r["spec"], "label": r["label"]}
            if via:
                pk["via"] = via
            picks.append(pk)
        return True

    os.environ.setdefault("ESCDELAY", "25")
    try:
        ok = curses.wrapper(run)
    except KeyboardInterrupt:
        ok = False
    out_path.write_text(json.dumps({"picks": picks} if ok else {"cancelled": True}), encoding="utf-8")
    return 0


# ------------------------------------------------------------------ pick command


def pick(
    workers: list[dict],
    xkiro: bool = False,
    show_all: bool = False,
    popup: bool = True,
    wait: float = 900.0,
    swarm_ext: str | None = None,
    state_dir: Path | None = None,
) -> tuple[int, str]:
    notes: list[str] = []
    for w in workers:
        w["catalog"], note = catalog(xkiro, show_all, swarm_ext, rank=w.get("desc") or w["name"])
        if note and note not in notes:
            notes.append(note)
    note = "\n".join(notes)
    work = (state_dir or _paths.AGY_DATA_DIR) / "swarm"
    work.mkdir(parents=True, exist_ok=True)
    stamp = f"{os.getpid()}-{int(time.time())}"
    req, out = work / f"req-{stamp}.json", work / f"pick-{stamp}.json"
    req.write_text(json.dumps({"workers": workers, "swarm_ext": _swarm_ext(swarm_ext)}), encoding="utf-8")
    lists = "\n".join(
        f"--- {w['name']} ({w.get('desc') or 'sin descripción'}) · ★ = recomendado por ranking\n"
        + numbered(w["catalog"])
        for w in workers
    )
    fallback = (
        (f"{note}\n" if note else "")
        + "SIN POPUP: muestra al usuario estas listas COMPLETAS (una por worker, ya ordenadas por ranking) y pídele "
        + f"`worker=N` para cada worker ({', '.join(w['name'] for w in workers)}); para un nvapi:… pregúntale además "
        + f"--via ({'|'.join(v for v, _ in VIA_TUIS)}, por defecto {VIA_TUIS[0][0]}).\n"
        + lists
    )

    if not (popup and os.environ.get("TMUX") and shutil.which("tmux")):
        return EXIT_NO_POPUP, fallback
    ui = " ".join(
        [
            shutil.which("python3") or sys.executable,
            str(_paths.SELF_PATH),
            "swarm",
            "pick-ui",
            str(req),
            str(out),
        ]
    )
    target = ["-t", os.environ["TMUX_PANE"]] if os.environ.get("TMUX_PANE") else []
    try:
        p = subprocess.run(
            ["tmux", "display-popup", *target, "-E", "-w", "90%", "-h", "85%", "-T", " /swarm · modelo por worker ", ui],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=wait,
        )
    except subprocess.TimeoutExpired:
        return EXIT_NO_POPUP, "el popup no respondió a tiempo\n" + fallback
    deadline = time.time() + (wait if p.returncode == 0 else 0)
    while not out.exists() and time.time() < deadline:
        time.sleep(0.3)
    if not out.exists():
        err = (p.stderr or "").strip() or f"rc={p.returncode}"
        return EXIT_NO_POPUP, f"no se pudo abrir el popup ({err})\n" + fallback
    res = json.loads(out.read_text(encoding="utf-8"))
    req.unlink(missing_ok=True)
    out.unlink(missing_ok=True)
    if res.get("cancelled"):
        return EXIT_CANCELLED, "cancelado por el usuario: no lances el swarm."
    body = json.dumps(res["picks"], ensure_ascii=False, indent=1)
    return 0, (f"{note}\n" if note else "") + body
