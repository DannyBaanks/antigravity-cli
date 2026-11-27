"""Compactación automática previa a la invocación del modelo de AGY.

Verificado con agy 1.5.11 (2026-11-18, workspace desechable con .agents/hooks.json):
``invocationNum`` es 0 en la primera llamada al modelo de un turno y 1, 2… tras cada
herramienta; el payload trae ``lastUserInput`` (texto del prompt) aunque la doc embebida
no lo liste; ``terminationBehavior: "terminate"`` + ``errorMessage`` corta el turno sin
llamar al modelo (``agy -p`` sale con código 3). Solo se actúa en ``invocationNum == 0``
para no cortar nunca un turno a medias.

2026-11-27 (paridad con claude-dash): activo por defecto (``AGY_DASH_MACHINE_COMPACT=0`` lo
apaga); mientras una compactación de la conversación sigue viva, cada prompt se corta antes del
modelo y se guarda en cola (``compacted/.queued_<id>.jsonl``) para entregarlo al continuar, en vez
de mandarle al modelo el contexto gigante.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

from . import compact as _compact
from . import paths as _paths


_TRANSCRIPT_TAIL_BYTES = 256 * 1024
_DEBOUNCE_SECONDS = 600
_COMPACT_COMMAND = "/compactnvidia"
_USER_REQUEST_RE = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.DOTALL)


def last_user_input(transcript_path: str | Path) -> str:
    """Devuelve el último USER_INPUT válido leyendo solo la cola de 256 KiB."""
    try:
        path = Path(transcript_path)
        size = path.stat().st_size
        with path.open("rb") as transcript:
            transcript.seek(max(0, size - _TRANSCRIPT_TAIL_BYTES), os.SEEK_SET)
            tail = transcript.read().decode("utf-8", errors="replace")
    except (OSError, TypeError, ValueError):
        return ""

    last_content = ""
    for raw_line in tail.splitlines():
        try:
            event = json.loads(raw_line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(event, dict) or event.get("type") != "USER_INPUT":
            continue
        content = event.get("content")
        if not isinstance(content, str):
            continue
        match = _USER_REQUEST_RE.search(content)
        last_content = (match.group(1) if match else content).strip()
    return last_content


def _spawn_detached(argv: list[str]) -> subprocess.Popen:
    """Lanza un worker separado con salida en `.last_self_compact.log` (respaldo DEVNULL)."""
    log_fh = None
    try:
        _paths.COMPACTED_DIR.mkdir(parents=True, exist_ok=True)
        log_fh = (_paths.COMPACTED_DIR / ".last_self_compact.log").open("ab")
    except OSError:
        log_fh = None
    try:
        return subprocess.Popen(
            argv,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=log_fh if log_fh is not None else subprocess.DEVNULL,
            stderr=log_fh if log_fh is not None else subprocess.DEVNULL,
        )
    finally:
        if log_fh is not None:
            log_fh.close()


def running_file(conv_id: str) -> Path:
    return _paths.COMPACTED_DIR / f".running_{conv_id}.json"


def queue_file(conv_id: str) -> Path:
    return _paths.COMPACTED_DIR / f".queued_{conv_id}.jsonl"


def _pid_runs_compaction(pid: int, conv_id: str, proc_root: Path = Path("/proc")) -> bool:
    """The worker started for ``conv_id`` is still alive (its argv names the conversation)."""
    try:
        argv = (proc_root / str(pid) / "cmdline").read_bytes().split(b"\0")
    except OSError:
        return False
    joined = [a.decode("utf-8", "replace") for a in argv]
    return "self-compact" in joined and conv_id in joined


def compaction_running(conv_id: str, proc_root: Path = Path("/proc")) -> bool:
    try:
        pid = int(json.loads(running_file(conv_id).read_text(encoding="utf-8")).get("pid") or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        return False
    return pid > 0 and _pid_runs_compaction(pid, conv_id, proc_root)


def enqueue(conv_id: str, text: str, timestamp: float) -> None:
    f = queue_file(conv_id)
    f.parent.mkdir(parents=True, exist_ok=True)
    with f.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"at": timestamp, "text": text}, ensure_ascii=False) + "\n")


def drain_queue(conv_id: str) -> list[str]:
    """Prompts that arrived while compacting, oldest first; the file is consumed."""
    f = queue_file(conv_id)
    try:
        lines = f.read_text(encoding="utf-8").splitlines()
        f.unlink()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            text = str(json.loads(line).get("text") or "").strip()
        except (json.JSONDecodeError, AttributeError):
            continue
        if text:
            out.append(text)
    return out


def _recently_compacted(marker: Path, timestamp: float) -> bool:
    try:
        previous = float(marker.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    age = timestamp - previous
    return 0 <= age < _DEBOUNCE_SECONDS


def decide(
    payload: dict,
    *,
    env: dict | None = None,
    measure: Callable | None = None,
    spawn: Callable | None = None,
    now: float | Callable[[], float] | None = None,
    running: Callable[[str], bool] | None = None,
) -> dict:
    """Decide si el hook debe terminar el turno y comenzar una compactación.

    Solo dispara en la primera invocación del turno (``invocationNum`` 0 o ausente).
    El texto del usuario sale de ``lastUserInput``; el transcript es respaldo.
    Las excepciones se convierten en ``{}`` para que un fallo del hook no
    interrumpa AGY.
    """
    try:
        active_env = os.environ if env is None else env
        if active_env.get("AGY_DASH_MACHINE_COMPACT", "1") == "0":
            return {}

        invocation_num = payload.get("invocationNum", 0)
        if int(invocation_num) != 0:
            return {}

        conv_id = str(payload.get("conversationId") or "").strip()
        if not conv_id:
            return {}

        transcript_value = payload.get("transcriptPath")
        transcript_path = Path(transcript_value) if transcript_value else None
        user_input = str(payload.get("lastUserInput") or "")
        if not user_input and transcript_path:
            user_input = last_user_input(transcript_path)
        command_input = user_input.lstrip()
        forced = command_input.startswith(_COMPACT_COMMAND)

        clock_value = now() if callable(now) else now
        timestamp = time.time() if clock_value is None else float(clock_value)

        # A compaction of this conversation is still running: the model must not read the huge
        # context now. Cut the turn and keep the prompt for the continuation (claude-dash parity).
        if (running or compaction_running)(conv_id):
            text = "" if forced else user_input.strip()
            if text:
                enqueue(conv_id, text, timestamp)
            note = ("tu mensaje queda en cola y se entrega al continuar" if text
                    else "ya está en curso; no hace falta pedirla otra vez")
            return {
                "injectSteps": [{"errorMessage": {"userMessage":
                                 f"🗜 compactando con NVIDIA: {note}. Esta pestaña se relanza sola."}}],
                "terminationBehavior": "terminate",
            }

        marker = _paths.HEARTBEATS_DIR / f"{conv_id}.autocompact"
        if _recently_compacted(marker, timestamp):
            return {}

        threshold = int(active_env.get("AGY_DASH_AUTO_COMPACT_TOKENS", "250000"))
        if threshold <= 0 and not forced:
            return {}
        model_name = str(payload.get("modelName") or "")
        measure_fn = measure or _compact.measure_session_context
        metrics = measure_fn(
            conv_id,
            transcript_path=transcript_path,
            model_name=model_name,
        )
        used_tokens = int(metrics.get("used_tokens") or 0)
        if not forced and used_tokens < threshold:
            return {}

        if forced:
            resume_prompt = command_input[len(_COMPACT_COMMAND) :].strip() or "continúa"
        else:
            resume_prompt = user_input.strip() or "continúa"
        cwd = str(payload.get("cwd") or "").strip()
        if not cwd:
            ws_list = payload.get("workspacePaths")
            if isinstance(ws_list, list) and ws_list:
                cwd = str(ws_list[0] or "").strip()
        argv = [
            sys.executable,
            str(_paths.SELF_PATH),
            "self-compact",
            conv_id,
            "--resume-prompt",
            resume_prompt,
        ]
        if cwd:
            argv.extend(["--cwd", cwd])

        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(timestamp), encoding="utf-8")
        try:
            proc = (spawn or _spawn_detached)(argv)
            pid = getattr(proc, "pid", 0)
            if isinstance(pid, int) and pid > 0:
                rf = running_file(conv_id)
                rf.parent.mkdir(parents=True, exist_ok=True)
                rf.write_text(json.dumps({"pid": pid, "at": timestamp}), encoding="utf-8")
        except Exception:
            try:
                marker.unlink()
            except OSError:
                pass
            raise

        reason = (
            f"/compactnvidia (contexto {used_tokens} tokens)"
            if forced and used_tokens < threshold
            else f"contexto {used_tokens} tokens ≥ {threshold}"
        )
        message = (
            f"{reason}: compactando con NVIDIA sin pasar por el modelo; "
            "esta pestaña se relanza sola ya compactada y tu mensaje va como siguiente paso."
        )
        return {
            "injectSteps": [{"errorMessage": {"userMessage": message}}],
            "terminationBehavior": "terminate",
        }
    except Exception:
        return {}


def hook_main(stdin_text: str, env: dict | None = None) -> str:
    """Parsea la entrada del hook y serializa la decisión, sin propagar fallos."""
    try:
        payload = json.loads(stdin_text)
        if not isinstance(payload, dict):
            return "{}"
        return json.dumps(decide(payload, env=env), ensure_ascii=False)
    except Exception:
        return "{}"
