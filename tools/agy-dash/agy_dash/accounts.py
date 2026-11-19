"""agy_dash.accounts — split verbatim from agy_dashboard.py @ f255869 (Phase 1, no behaviour change)."""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from . import paths as _paths
from . import procs as _procs
from . import sessions as _sessions
from . import tmuxctl as _tmuxctl


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
    real_home = base_home or _paths.HOME
    real_agy_data = agy_data_dir or _paths.AGY_DATA_DIR
    acct_root = accounts_dir or _paths.ACCOUNTS_DIR

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
    reg_file = registry_path or _paths.ACCOUNTS_REGISTRY
    real_home = base_home or _paths.HOME
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
    reg_file = registry_path or _paths.ACCOUNTS_REGISTRY
    reg_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(reg_file.parent, 0o700)
    except OSError:
        pass
    _paths.atomic_write_text(reg_file, json.dumps(data, indent=2, ensure_ascii=False), mode=0o600)


def update_accounts_registry(mutate, registry_path: Path | None = None) -> dict:
    """Reload + mutate + save under a lock: a slow caller never writes back a registry it read long ago."""
    reg_file = registry_path or _paths.ACCOUNTS_REGISTRY
    with _paths.file_lock(reg_file):
        reg = load_accounts_registry(registry_path=registry_path)
        mutate(reg)
        save_accounts_registry(reg, registry_path=registry_path)
    return reg


def _mark_unauthenticated(slot: str) -> None:
    def _m(reg: dict) -> None:
        if slot in reg.get("accounts", {}):  # a slot removed meanwhile is not resurrected
            reg["accounts"][slot]["authenticated"] = False
    update_accounts_registry(_m)


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
        settings_path = (agy_data_dir or _paths.AGY_DATA_DIR) / "settings.json"
        if settings_path.is_file():
            try:
                cfg = json.loads(settings_path.read_text(encoding="utf-8"))
                chosen = str(cfg.get("model", "")).strip()
            except Exception:
                chosen = ""
    if not chosen:
        return ("", "")
    if chosen in _paths.MODEL_DISPLAY_TO_SLUG:
        return (chosen, _paths.MODEL_DISPLAY_TO_SLUG[chosen])
    slug_to_disp = {v: k for k, v in _paths.MODEL_DISPLAY_TO_SLUG.items()}
    if chosen in slug_to_disp:
        return (slug_to_disp[chosen], chosen)
    for disp, slug in _paths.MODEL_DISPLAY_TO_SLUG.items():
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
                raw_frac = b.get("remaining_fraction")
                if raw_frac is None:
                    continue  # unknown quota is not exhausted quota: no false CRITICAL / Baton Pass advice
                try:
                    frac = float(raw_frac)
                except (TypeError, ValueError):
                    continue
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
    env.setdefault("GOGC", _paths.DEFAULT_GOGC)
    env.setdefault("GOMEMLIMIT", _paths.DEFAULT_GOMEMLIMIT)

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
        env["AGY_REAL_HOME"] = str(_paths.HOME)
        if "AGY_REAL_XDG_CONFIG" in os.environ:
            env["AGY_REAL_XDG_CONFIG"] = os.environ["AGY_REAL_XDG_CONFIG"]
        elif "XDG_CONFIG_HOME" in os.environ and "/accounts/" not in os.environ["XDG_CONFIG_HOME"]:
            env["AGY_REAL_XDG_CONFIG"] = os.environ["XDG_CONFIG_HOME"]
        else:
            env["AGY_REAL_XDG_CONFIG"] = str(_paths.HOME / ".config")
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
    is_isolated = acct_info.get("mode", "system" if slot == "primary" else "isolated") == "isolated"
    if is_isolated and not acct_info.get("authenticated", False) and not allow_unauthenticated:
        return False, f"[{slot}] Pendiente de login OAuth (sin abrir navegador)."

    env = build_agy_env(slot)
    # Never trigger interactive browser tabs during background /usage checks
    env["AGY_NO_BROWSER"] = "1"
    env["BROWSER"] = "/bin/true"
    no_browser_bin = ensure_isolated_home(slot).parent / "bin" if is_isolated else (_paths.ACCOUNTS_DIR / "_nobrowser_bin")
    if not is_isolated:
        no_browser_bin.mkdir(parents=True, exist_ok=True)
        xo = no_browser_bin / "xdg-open"
        if not xo.exists():
            xo.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            os.chmod(xo, 0o755)
        env["PATH"] = f"{no_browser_bin}:{env.get('PATH', '')}"
    home_for_log = Path(env.get("HOME", str(_paths.HOME)))
    log_path = home_for_log / ".gemini" / "antigravity-cli" / "cli.log"
    try:
        log_start = log_path.stat().st_size  # only lines written after the probe started count
    except OSError:
        log_start = 0
    try:
        proc = subprocess.Popen(
            [_paths.AGY_BIN, "-p", "/usage", "--output-format", "json"],
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
                    _mark_unauthenticated(slot)
                    return False, f"[{slot}] Sin sesión iniciada (requiere login OAuth)."
                return False, f"[{slot}] Timeout consultando /usage."
            try:
                stdout_text, stderr_text = proc.communicate(timeout=min(0.6, rem))
                break
            except subprocess.TimeoutExpired:
                # Isolated slots only: primary shares cli.log with every live agy, so its tail proves nothing.
                if not is_isolated:
                    continue
                try:
                    st = log_path.stat()
                    if st.st_size > log_start and (time.time() - st.st_mtime) < 15:
                        with log_path.open("rb") as lf:
                            lf.seek(max(log_start, st.st_size - 8192), os.SEEK_SET)
                            tail = lf.read().decode("utf-8", errors="replace")
                        if "starting OAuth flow" in tail and proc.poll() is None:
                            proc.kill()
                            stdout_text, stderr_text = proc.communicate()
                            _mark_unauthenticated(slot)
                            return False, f"[{slot}] Sin sesión iniciada (requiere login OAuth)."
                except OSError:
                    pass
                continue
    except OSError as e:
        return False, f"[{slot}] Error ejecutando agy: {e}"

    if "Authentication required" in stderr_text:
        _mark_unauthenticated(slot)
        return False, f"[{slot}] Sin sesión iniciada (requiere login OAuth)."

    parsed = parse_usage_output(stdout_text)
    if not parsed["ok"]:
        return False, f"[{slot}] /usage falló: {parsed.get('error', stderr_text[:100])}"

    email = extract_email_from_cli_log(home_for_log)

    def _store(reg: dict) -> None:
        acct = reg["accounts"].get(slot)
        if acct is None:
            return  # removed while /usage ran: do not resurrect it
        if email:
            acct["email"] = email
        acct["authenticated"] = True
        acct["quota"] = parsed
        acct["updated_at"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    update_accounts_registry(_store)
    return True, f"[{slot}] Cuota actualizada (mín {parsed['min_pct']}%)."


def compute_quota_alert(
    reg: dict,
    *,
    active_slot: str | None = None,
    model_name: str = "",
    warn_pct: float = _paths.QUOTA_WARN_PCT,
    crit_pct: float = _paths.QUOTA_CRIT_PCT,
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
        fallback = q.get("min_pct")
        other_pct = float(q.get("families", {}).get(check_fam, 0.0 if fallback is None else fallback))
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
            fallback = q.get("min_pct")
            score = float(q.get("families", {}).get(fam, 0.0 if fallback is None else fallback))
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
        return (base_home or _paths.HOME) / ".config" / "antigravity"
    return (accounts_dir or _paths.ACCOUNTS_DIR) / slot / "home" / ".config" / "antigravity"


def baton_preflight(
    session: _sessions.AgySession,
    *,
    target_slot: str = "",
    model: str = "",
    min_target_pct: float = _paths.QUOTA_WARN_PCT,
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
    is_anc = bool(session.proc and _procs.is_ancestor_pid(session.proc.pid))
    no_dup_ok = (not is_busy) and (not is_anc)
    if is_busy:
        blockers.append(f"Sesión en estado {session.state_label} (turno/subtarea activa): espera a IDLE.")
    elif is_anc:
        blockers.append("Proceso actual es ancestro directo del dashboard.")

    # Verify preserved conversation state on disk and symlinked into target isolated home
    data_dir = agy_data_dir or _paths.AGY_DATA_DIR
    has_conv_pb = (data_dir / "conversations" / f"{session.conversation_id}.pb").is_file()
    has_brain = (data_dir / "brain" / session.conversation_id).is_dir()
    state_ok = has_conv_pb or has_brain or session.step_count > 0
    if chosen_slot and accounts.get(chosen_slot, {}).get("mode", "system") == "isolated":
        t_agy = (accounts_dir or _paths.ACCOUNTS_DIR) / chosen_slot / "home" / ".gemini" / "antigravity-cli"
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
            "target_has_quota": target_pct is None or target_pct > min_target_pct,  # unknown quota != exhausted
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
    _procs.enable_ksm_memory_merge()
    env = build_agy_env(account_slot)
    slot_used = env.get("AGY_ACCOUNT_SLOT", "primary")
    target_cwd = cwd if (cwd and Path(cwd).is_dir()) else str(Path.cwd())
    os.chdir(target_cwd)

    final_args = list(extra_args)
    model_disp, model_slug = resolve_model_arg(model)
    if model and model_slug and "--model" not in final_args:
        final_args.extend(["--model", model_slug])

    if preview_conv:
        _sessions.print_instant_zig_preview(preview_conv, preview_title or preview_conv[:8], target_cwd)
        print(f"  \033[1;35m💳 Cuenta:\033[0m [{slot_used}]  \033[1;36m🧠 Modelo:\033[0m {model_disp or model_slug or 'default settings.json'}")

    if _procs.ensure_zig_core():
        cmd = [str(_paths.ZIG_CORE_BIN), "ksm-exec", _paths.AGY_BIN] + final_args
        try:
            os.execvpe(str(_paths.ZIG_CORE_BIN), cmd, env)
        except OSError as exc:
            print(f"  agy-fast-core ksm-exec falló ({exc}); lanzando agy directo")
    cmd = [_paths.AGY_BIN] + final_args
    os.execvpe(_paths.AGY_BIN, cmd, env)
    return 0


def baton_pass_session(
    session: _sessions.AgySession,
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

    # The caller's snapshot can be seconds old: re-read, and never cut a turn in progress.
    session = _sessions.refresh_session(session.conversation_id) or session
    if session.db_status == "CASCADE_RUN_STATUS_RUNNING" or session.transcript_busy:
        return False, "⚠️ Baton Pass cancelado: la sesión tiene un turno en curso; espera a que termine."
    # Ensure zero duplicate writers: stop live external copies (verified) BEFORE the new account opens it.
    for p in ([session.proc] if session.proc else []) + list(session.extra_procs):
        if p and not p.in_dash:
            ok, why = _procs.terminate_verified(p)
            if not ok:
                return False, f"⚠️ Baton Pass cancelado: {why}"

    hib_reg = _sessions.load_hibernated_registry()
    if session.conversation_id in hib_reg:
        del hib_reg[session.conversation_id]
        _sessions.save_hibernated_registry(hib_reg)

    msg = _tmuxctl.open_session_in_dashboard_tab(
        session,
        switch_to=switch_to,
        force_interactive=True,
        account_slot=chosen_slot,
        model=model_slug or model_disp,
    )
    return True, msg


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
    sessions: list[_sessions.AgySession] | None = None,
    registry_path: Path | None = None,
    accounts_dir: Path | None = None,
    base_home: Path | None = None,
) -> dict:
    """Inspect identity, duplicate siblings, associated sessions, live PIDs, and local-only files before deletion."""
    safe = validate_slot_name(slot)
    reg = load_accounts_registry(registry_path=registry_path, base_home=base_home)
    accounts = reg.get("accounts", {})
    acct = accounts.get(safe)
    acct_root = (accounts_dir or _paths.ACCOUNTS_DIR).resolve()
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
        "diverged_shared": [str(x) for x in diverged_shared_items(safe, accounts_dir=accounts_dir)]
        if safe != "primary" else [],
        "blocked_reason": blocked,
    }


def diverged_shared_items(slot: str, *, accounts_dir: Path | None = None) -> list[Path]:
    """Items that should be symlinks to the shared data but AGY replaced with real files (e.g. it rewrote
    conversation_summaries.db in place). They hold real data, so a purge must rescue them first."""
    safe = validate_slot_name(slot)
    slot_home = (accounts_dir or _paths.ACCOUNTS_DIR) / safe / "home"
    found = []
    # _SHARED_HOME_ITEMS too (.cache, .local, .config/gh...): a real dir there is user data (AGENTS §0c).
    for base, names in ((slot_home / ".gemini" / "antigravity-cli", _SHARED_AGY_DATA_ITEMS),
                        (slot_home / ".gemini", _SHARED_GEMINI_ITEMS),
                        (slot_home, _SHARED_HOME_ITEMS),
                        (slot_home / ".config", ("gh",))):
        for name in names:
            fp = base / name
            if fp.exists() and not fp.is_symlink():
                found.append(fp)
    return found


def rescue_diverged_shared(slot: str, *, accounts_dir: Path | None = None) -> Path | None:
    """Move diverged shared items to accounts/.rescued/<slot>-<ts>/ (never deleted). Returns that dir."""
    items = diverged_shared_items(slot, accounts_dir=accounts_dir)
    if not items:
        return None
    acct_root = accounts_dir or _paths.ACCOUNTS_DIR
    slot_home = acct_root / validate_slot_name(slot) / "home"
    dest = acct_root / ".rescued" / f"{validate_slot_name(slot)}-{time.strftime('%Y%m%d-%H%M%S')}"
    for fp in items:
        target = dest / fp.relative_to(slot_home)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(fp), str(target))
    return dest


def purge_isolated_slot_dir(slot: str, *, accounts_dir: Path | None = None) -> tuple[int, int]:
    """Delete ONLY local regular files inside `accounts/<slot>/`; unlink symlinks without ever descending into targets."""
    safe = validate_slot_name(slot)
    if safe == "primary":
        raise ValueError("Refusing to purge primary account.")
    acct_root = (accounts_dir or _paths.ACCOUNTS_DIR).resolve()
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
    sessions: list[_sessions.AgySession] | None = None,
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
    rescued: Path | None = None
    if purge_local and info["mode"] == "isolated":
        rescued = rescue_diverged_shared(safe, accounts_dir=accounts_dir)
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
        f"({files_del} locales borrados, {links_del} symlinks desligados, compartido intacto)"
        + (f"; datos propios del perfil rescatados en {rescued}." if rescued else "."),
    )
