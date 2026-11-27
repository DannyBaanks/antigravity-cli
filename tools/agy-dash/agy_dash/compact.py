"""agy_dash.compact — split verbatim from agy_dashboard.py @ f255869 (Phase 1, no behaviour change)."""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import accounts as _accounts
from . import paths as _paths
from . import procs as _procs
from . import sessions as _sessions
from . import tmuxctl as _tmuxctl


# ============================================================================
# External Context Compactor via NVIDIA NIM API (`nvapi` / Nemotron Ultra)
# ============================================================================

# Checked against NVIDIA's /v1/models on 2026-11-27: the old fallbacks (nemotron-3-ultra-550b,
# llama-3_1-nemotron-ultra-253b-v1, llama-3.3-nemotron-super-49b-v1, llama-3.1-nemotron-70b-instruct)
# are gone (HTTP 404/410 "end of life"), so a slow 550B left the compaction with no fallback at all.
_NEMOTRON_CANDIDATE_MODELS = (
    "nvidia/nemotron-3-ultra-550b-a55b",
    "nvidia/nemotron-3.5-super-vl-120b-a12b",
    "nvidia/nemotron-3.5-lightning-30b-a3b",
)
# Nemotron 3.5 writes its reasoning into `content` unless thinking is off (verified 2026-11-27).
_NO_THINKING_MODELS = frozenset({
    "nvidia/nemotron-3.5-super-vl-120b-a12b",
    "nvidia/nemotron-3.5-lightning-30b-a3b",
})


def mask_api_key(key: str) -> str:
    k = key.strip()
    if not k:
        return "(no configurada)"
    if len(k) <= 10:
        return "****"
    return f"{k[:6]}...{k[-4:]}"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib keeps the Authorization header across redirects: never follow one with the bearer key."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirect)


def _nvapi_url_ok(base_url: str) -> bool:
    parsed = urllib.parse.urlparse(base_url)
    return parsed.scheme == "https" and bool(parsed.hostname)


def load_nvapi_config(
    config_path: Path | None = None,
    *,
    cfg_path: Path | None = None,
) -> dict:
    p = cfg_path or config_path or _paths.NVAPI_CONFIG_FILE
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
    base_url = str(data.get("base_url") or _paths.DEFAULT_NVAPI_BASE_URL).strip().rstrip("/")
    raw_model = str(data.get("model") or "").strip()
    if raw_model == "nvidia/llama-3_1-nemotron-ultra-253b-v1":
        raw_model = _paths.DEFAULT_NVAPI_MODEL
    model = raw_model or _paths.DEFAULT_NVAPI_MODEL
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
    p = cfg_path or config_path or _paths.NVAPI_CONFIG_FILE
    # Start from the FILE, not load_nvapi_config(): that merges NVIDIA_API_KEY from the environment, and
    # saving only --model must not copy an env key to disk.
    try:
        raw = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
    except Exception:
        raw = {}
    cur = {k: raw[k] for k in ("api_key", "base_url", "model") if isinstance(raw, dict) and raw.get(k)}
    if api_key.strip():
        cur["api_key"] = api_key.strip()
    if model.strip():
        cur["model"] = model.strip()
    if base_url.strip():
        cur["base_url"] = base_url.strip().rstrip("/")
    _paths.atomic_write_text(p, json.dumps(cur, indent=2, ensure_ascii=False), mode=0o600)
    return load_nvapi_config(config_path=p)


def estimate_session_tokens(conv_or_session: str | _sessions.AgySession, step_count: int = 0) -> tuple[int, int]:
    """Return (estimated_input_tokens, transcript_bytes) for a conversation."""
    if isinstance(conv_or_session, _sessions.AgySession):
        conv_id = conv_or_session.conversation_id
        step_count = conv_or_session.step_count
    else:
        conv_id = str(conv_or_session)
    tpath = _sessions.transcript_path_for(conv_id)
    try:
        size_b = tpath.stat().st_size if tpath.is_file() else 0
    except OSError:
        size_b = 0
    est_tokens = max(step_count * 140, size_b // 4)
    return (est_tokens, size_b)


def should_offer_compaction(session: _sessions.AgySession) -> tuple[bool, int, int]:
    """Return (is_heavy, est_tokens, size_bytes) when reopening a SAVED/HIBERNATED conversation."""
    est_tok, size_b = estimate_session_tokens(session.conversation_id, session.step_count)
    if session.proc is not None:
        return (False, est_tok, size_b)
    is_heavy = session.step_count >= _paths.COMPACT_MIN_STEPS or size_b >= _paths.COMPACT_MIN_BYTES
    return (is_heavy, est_tok, size_b)


def _shells_now(conv_id: str, session: _sessions.AgySession | None = None) -> str:
    """Sandbox jobs of `conv_id` + non-sandbox child shells running right now (those die on respawn)."""
    try:
        from . import sandbox as _sandbox
        cur = session or _sessions.refresh_session(conv_id)
        doomed: list[str] = []
        if cur is not None and cur.proc is not None:
            doomed = [
                x.command
                for x in _procs.shells_of(cur.proc.pid, cur.proc.active_children)
                if _sandbox.blocks_close(x.command)
            ]
        return _sandbox.describe_for_digest(conv_id, doomed).replace(
            "MURIÓ al compactar", "MORIRÁ al compactar"
        )
    except Exception:
        return ""


def build_compaction_digest(
    conv_id: str,
    title: str,
    workspace: str,
    max_chars: int = 44000,
    *,
    shells: str = "",
) -> tuple[str, int]:
    """Extract high-signal user requests, tool actions, and recent turns from transcript.jsonl."""
    tpath = _sessions.transcript_path_for(conv_id)
    shells_section = f"## SHELLS EN 2º PLANO\n{shells}\n\n" if shells.strip() else ""
    if not tpath.is_file():
        return (f"Título: {title}\nWorkspace: {workspace}\n{shells_section}(Sin transcript.jsonl local)", 0)

    try:
        raw_bytes = tpath.read_bytes()
    except OSError:
        return (f"Título: {title}\nWorkspace: {workspace}\n{shells_section}", 0)

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
        f"{shells_section}"
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
    return _paths.DEFAULT_NVAPI_MODEL


SYSTEM_PROMPT = (
    "Eres un compactador experto de contexto para sesiones de ingeniería de software (Antigravity CLI). "
    "Resume la conversación en español en un documento Markdown ultra-denso (<1200 palabras) con estas secciones exactas:\n"
    "1. OBJETIVO Y PETICIONES DEL USUARIO\n"
    "2. TRABAJO COMPLETADO Y DECISIONES TÉCNICAS\n"
    "3. ARCHIVOS MODIFICADOS / RUTAS Y COMANDOS CLAVE\n"
    "4. ESTADO ACTUAL EXACTO\n"
    "5. SIGUIENTE PASO ACCIONABLE INMEDIATO\n"
    "Conserva rutas exactas, hashes de commits, nombres de funciones y errores pendientes sin inventar nada. "
    "Si hay sección SHELLS EN 2º PLANO, en 5. SIGUIENTE PASO ordena reenganchar cada job que SIGUE CORRIENDO con "
    "`agy-dash sbx wait <id>` en 2º plano antes de seguir; los que murieron, relanzarlos solo si hacen falta."
)


def compact_conversation_with_nvapi(
    session: _sessions.AgySession,
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

    shells_block = _shells_now(session.conversation_id, session)
    digest, orig_bytes = build_compaction_digest(
        session.conversation_id, session.title, session.workspace, shells=shells_block
    )
    orig_tokens = max(session.step_count * 140, orig_bytes // 4)
    base_url = cfg["base_url"]
    if not _nvapi_url_ok(base_url):
        return (
            False,
            f"❌ base_url NVAPI rechazada ({base_url}): debe ser https:// (la key y la conversación viajan ahí). "
            "Para otro host https ponlo explícito con `agy-dash nvapi --base-url`.",
            None,
        )
    chosen_model = cfg["model"]
    if chosen_model.lower() == "auto":
        chosen_model = _discover_nvapi_nemotron_model(base_url, api_key)

    sys_prompt = SYSTEM_PROMPT

    candidates = [chosen_model] + [m for m in _NEMOTRON_CANDIDATE_MODELS if m != chosen_model]
    url_open = opener or _NO_REDIRECT_OPENER.open
    summary_text = ""
    used_model = chosen_model
    last_err = ""
    cand_prev = chosen_model
    errors: list[str] = []  # one per failed candidate: the last error alone hid the real cause

    for cand in candidates:
        if last_err:
            errors.append(f"{cand_prev}: {last_err}")
            last_err = ""
        cand_prev = cand
        req_body = {
            "model": cand,
            "messages": [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": digest},
            ],
            "temperature": 0.2,
            "max_tokens": 2048,
        }
        if cand in _NO_THINKING_MODELS:
            req_body["chat_template_kwargs"] = {"enable_thinking": False}
        payload = json.dumps(req_body).encode("utf-8")
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
        except urllib.error.URLError as ue:
            last_err = str(ue.reason)
            if isinstance(ue.reason, (TimeoutError, OSError)) and "timed out" in str(ue.reason):
                continue  # a slow model: the next candidate may answer
            break  # host unreachable / DNS: every candidate shares the host
        except (TimeoutError, json.JSONDecodeError) as e:
            last_err = str(e)
            continue
        except Exception as e:
            last_err = str(e)
            break

    if not summary_text:
        if last_err:
            errors.append(f"{cand_prev}: {last_err}")
        detail = " · ".join(errors) or "respuesta vacía"
        return (False, f"❌ Falló compactación NVAPI ({chosen_model} y respaldos): {detail}", None)

    out_dir = handoffs_dir or _paths.COMPACTED_DIR
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
    shells_footer = (
        f"\n\n## SHELLS EN 2º PLANO al compactar\n{shells_block}\n"
        if shells_block.strip()
        else ""
    )
    handoff_file.write_text(header + summary_text + shells_footer + "\n", encoding="utf-8")
    try:
        os.chmod(handoff_file, 0o600)
    except OSError:
        pass
    return (
        True,
        f"🗜️ Compactada con `{used_model}`: ~{orig_tokens:,} -> ~{compact_tokens:,} tokens ({saved_pct}% ahorro).",
        handoff_file,
    )


def _sandbox_clause(conv_id: str) -> str:
    try:
        from . import sandbox as _sandbox
        block = _sandbox.describe_for_digest(conv_id)
    except Exception:
        block = ""
    if not block.strip():
        return ""
    return (
        " Antes de seguir, reengancha en 2º plano (`run_command` con `WaitMsBeforeAsync=500`) "
        f"los shells del sandbox que siguen vivos:\n{block}"
    )


def continue_prompt(info: dict, resume_prompt: str = "") -> str:
    nxt = resume_prompt.strip() or "continúa exactamente donde quedaste."
    shells = ""
    if info.get("shells_block"):
        shells = (
            " Antes de seguir, reengancha en 2º plano (`run_command` con `WaitMsBeforeAsync=500`) "
            f"los shells que siguen vivos:\n{info['shells_block']}\n"
        )
    queued = ""
    if info.get("queued"):
        queued = (
            " Mientras se compactaba llegaron estos mensajes (no los viste; atiéndelos):\n"
            + "\n---\n".join(info["queued"])
            + "\n"
        )
    return (
        f"[agy-dash] {info.get('message', 'Contexto compactado con NVIDIA')}."
        f"{shells}{queued} Sigue trabajando: {nxt}"
    )


def open_compacted_session_in_tab(session: _sessions.AgySession, handoff_file: Path, *, switch_to: bool = True) -> str:
    """Open a fresh lightweight AGY session tab pre-loaded with the NVAPI compacted handoff."""
    self_path = _paths.SELF_PATH
    cwd = session.workspace if Path(session.workspace).is_dir() else str(Path.cwd())
    reg = _accounts.load_accounts_registry()
    slot = reg.get("conv_accounts", {}).get(session.conversation_id) or reg.get("active_slot", "primary")
    tab_name = _tmuxctl._short_tab_title(session.title, prefix="🗜")
    sbx_clause = _sandbox_clause(session.conversation_id)
    boot_prompt = (
        f"Contexto compactado de la sesión '{session.title}' ({session.conversation_id[:8]}). "
        f"Ejecuta primero la tool de ISyMCP `isymcp(action='handoff.load', arguments={{'target': '{session.conversation_id[:8]}', 'workspace': '{cwd}'}})` "
        f"(o lee `{handoff_file}` si no está disponible) para obtener el handoff y contexto ziggrep en un solo paso y confirma que estás listo.{sbx_clause}"
    )
    cmd = (
        f"exec python3 {_tmuxctl.sh_quote(str(self_path))} run "
        f"--cwd {_tmuxctl.sh_quote(cwd)} --account {_tmuxctl.sh_quote(slot)} --title {_tmuxctl.sh_quote(session.title)} "
        f"-- -i {_tmuxctl.sh_quote(boot_prompt)}"
    )
    args = ["new-window", "-t", _paths.TMUX_SESSION, "-n", tab_name, "-c", cwd]
    if not switch_to:
        args.insert(1, "-d")
    args.append(cmd)
    _tmuxctl.tmux_cmd(*args)
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
    out_dir = handoffs_dir or _paths.COMPACTED_DIR
    p = out_dir / f"{conv_id[:8]}_handoff.md"
    return p if p.is_file() else None


def list_compacted_vault(*, handoffs_dir: Path | None = None) -> list[dict]:
    """List all compacted handoffs stored in the vault, newest first."""
    out_dir = handoffs_dir or _paths.COMPACTED_DIR
    if not out_dir.is_dir():
        return []
    entries = [parse_handoff_metadata(p) for p in out_dir.glob("*_handoff.md") if p.is_file()]
    entries.sort(key=lambda e: e["mtime"], reverse=True)
    return entries


def open_vault_entry_in_tab(entry: dict, *, switch_to: bool = True) -> str:
    """Launch a fresh lightweight AGY tab from a stored vault handoff without re-compacting."""
    dummy = _sessions.AgySession(
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
    tpath = transcript_path if (transcript_path and transcript_path.is_file()) else _sessions.transcript_path_for(conv_id)
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
    if used_tokens >= _paths.HEARTBEAT_URGENT_TOKENS or pct >= 70.0:
        level = "COMPACT_URGENT"
    elif used_tokens >= _paths.HEARTBEAT_RECOMMEND_TOKENS or pct >= 45.0:
        level = "COMPACT_RECOMMENDED"
    elif used_tokens >= _paths.HEARTBEAT_WARN_TOKENS or pct >= 20.0:
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
    iv = _paths.HEARTBEAT_INTERVAL_SEC if interval_sec is None else float(interval_sec)
    hb_dir = heartbeats_dir or _paths.HEARTBEATS_DIR
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


RELAUNCH_WAIT_S = 8.0  # the respawned pane must still be alive this long after the relaunch


def await_pane_alive(target: str, timeout_s: float = RELAUNCH_WAIT_S, poll_s: float = 0.5,
                     tmux=None, clock=time.time, sleep=time.sleep) -> tuple[bool, str]:
    """True once the respawned pane has stayed alive ``timeout_s`` (agy crashing at start, a bad
    account slot or a missing binary leave it dead or close the window). claude-dash parity: never
    say "continuada" without checking anything ran."""
    tmux = tmux or _tmuxctl.tmux_cmd
    deadline = clock() + timeout_s
    while True:
        cp = tmux("display-message", "-p", "-t", target, "#{pane_dead} #{pane_dead_status}", capture=True)
        out = (getattr(cp, "stdout", "") or "").split()
        if getattr(cp, "returncode", 1) != 0 or not out:
            return False, "la pestaña se cerró al relanzar"
        if out[0] == "1":
            return False, f"agy salió al arrancar (código {out[1] if len(out) > 1 else '?'})"
        if clock() >= deadline:
            return True, ""
        sleep(poll_s)


def _queued_clause(conv_id: str) -> str:
    from . import autocompact as _autocompact
    queued = _autocompact.drain_queue(conv_id)
    if not queued:
        return ""
    items = " | ".join(q.replace("\n", " ")[:600] for q in queued[-10:])
    return f" Mensajes de Danny que llegaron mientras se compactaba (atiéndelos en orden): {items}"


def respawn_compacted_session_in_place(
    session: _sessions.AgySession,
    handoff_file: Path,
    *,
    resume_prompt: str = "",
    win_idx: str | None = None,
    verify=None,
) -> str:
    """Replace the active session's tmux pane in-place with a lightweight Nemotron-compacted continuation."""
    target_win = win_idx if win_idx is not None else _tmuxctl._find_tmux_window_by_conv(session.conversation_id)
    if target_win is None:
        # Outside the dash: opening a new tab while the original agy is still open elsewhere would
        # leave two sessions writing on the same work. Hand the user one line instead.
        cur = _sessions.refresh_session(session.conversation_id)
        if cur is not None and cur.proc is not None:
            return (f"🗜 handoff listo en el Baúl; agy sigue abierto fuera del dash. Ciérralo y corre: "
                    f"agy-dash self-compact {session.conversation_id[:8]} --respawn-only")
    self_path = _paths.SELF_PATH
    cwd = session.workspace if Path(session.workspace).is_dir() else str(Path.cwd())
    reg = _accounts.load_accounts_registry()
    slot = reg.get("conv_accounts", {}).get(session.conversation_id) or reg.get("active_slot", "primary")
    tab_name = _tmuxctl._short_tab_title(session.title, prefix="🗜")

    cont_clause = (
        f" y continúa inmediatamente con: {resume_prompt.strip()}"
        if resume_prompt.strip()
        else " y confirma que estás listo para continuar."
    ) + _queued_clause(session.conversation_id) + _sandbox_clause(session.conversation_id)
    boot_prompt = (
        f"Contexto compactado con NVIDIA Nemotron 550B de tu sesión '{session.title}' ({session.conversation_id[:8]}). "
        f"Ejecuta ÚNICAMENTE una llamada a `agy_handoff_context` (o `isymcp(action='handoff.load', arguments={{'target': '{session.conversation_id[:8]}', 'workspace': '{cwd}'}})` — si aún no está cargada en este hub, lee `{handoff_file}`){cont_clause}"
    )
    cmd = (
        f"exec python3 {_tmuxctl.sh_quote(str(self_path))} run "
        f"--cwd {_tmuxctl.sh_quote(cwd)} --account {_tmuxctl.sh_quote(slot)} --title {_tmuxctl.sh_quote(session.title)} "
        f"-- -i {_tmuxctl.sh_quote(boot_prompt)}"
    )

    if target_win is not None:
        target = f"{_paths.TMUX_SESSION}:{target_win}"
        _tmuxctl.tmux_cmd("rename-window", "-t", target, tab_name)
        rp = _tmuxctl.tmux_cmd("respawn-pane", "-k", "-t", target, cmd)
        if getattr(rp, "returncode", 0) != 0:
            return (f"⚠ handoff listo en el Baúl, pero tmux no pudo relanzar la pestaña {target_win}; "
                    f"corre: agy-dash self-compact {session.conversation_id[:8]} --respawn-only")
        ok, why = (verify or await_pane_alive)(target)
        if not ok:
            return (f"⚠ handoff listo en el Baúl, pero {why}; "
                    f"corre: agy-dash self-compact {session.conversation_id[:8]} --respawn-only")
        return f"♻️ Sesión '{session.title}' compactada con Nemotron 550B y continuada in-place en pestaña `{tab_name}`."

    _tmuxctl.tmux_cmd("new-window", "-t", _paths.TMUX_SESSION, "-n", tab_name, "-c", cwd, cmd)
    return f"🚀 Sesión '{session.title}' compactada con Nemotron 550B en pestaña `{tab_name}`."


def reload_session_tab(session: _sessions.AgySession) -> str:
    """Respawn an open dashboard tab cleanly to sync latest disk state."""
    existing_idx = _tmuxctl._find_tmux_window_by_conv(session.conversation_id)
    if existing_idx is None:
        return _tmuxctl.open_session_in_dashboard_tab(session, switch_to=True)
    if session.external_busy:
        return _tmuxctl.open_session_in_dashboard_tab(session, switch_to=True, force_live_mirror=True)
    return _tmuxctl.open_session_in_dashboard_tab(session, switch_to=True, force_interactive=True)


def detect_caller_conversation_id(sessions: list[_sessions.AgySession]) -> str | None:
    """Resolve the active AGY conversation_id that invoked the current command."""
    env_cid = (os.environ.get("AGY_CONVERSATION_ID") or "").strip()
    if env_cid:
        return env_cid
    for s in sessions:
        if s.proc and _procs.is_ancestor_pid(s.proc.pid):
            return s.conversation_id
        for ep in s.extra_procs:
            if _procs.is_ancestor_pid(ep.pid):
                return s.conversation_id
    pane = os.environ.get("TMUX_PANE", "")
    if os.environ.get("TMUX") and pane:
        # -t $TMUX_PANE: the caller's own window, not whichever tab Danny is looking at right now.
        cp = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane, "#{@agy_conv_id}"],
            capture_output=True,
            text=True,
            timeout=1.5,
        )
        t_cid = (cp.stdout or "").strip()
        if cp.returncode == 0 and t_cid:
            return t_cid
    cwd_s = str(Path.cwd())
    active_ws = [s for s in sessions if s.proc is not None and s.workspace == cwd_s]
    if len(active_ws) == 1:  # ambiguous when several sessions share the workspace: refuse to guess
        return active_ws[0].conversation_id
    return None


def _write_compact_status(conv_id: str, status: str, msg: str = "", handoff_file: Path | None = None) -> None:
    try:
        _paths.COMPACTED_DIR.mkdir(parents=True, exist_ok=True)
        sf = _paths.COMPACTED_DIR / f".status_{conv_id[:8]}.json"
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
