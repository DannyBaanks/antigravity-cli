"""Compactación automática previa a la invocación del modelo de AGY.

Verificado con agy 1.5.11 (2026-11-18, workspace desechable con .agents/hooks.json):
``invocationNum`` es 0 en la primera llamada al modelo de un turno y 1, 2… tras cada
herramienta; el payload trae ``lastUserInput`` (texto del prompt) aunque la doc embebida
no lo liste; ``terminationBehavior: "terminate"`` + ``errorMessage`` corta el turno sin
llamar al modelo (``agy -p`` sale con código 3). Solo se actúa en ``invocationNum == 0``
para no cortar nunca un turno a medias.
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


def _spawn_detached(argv: list[str]) -> None:
    """Lanza un worker separado y sin descriptores estándar compartidos."""
    subprocess.Popen(
        argv,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


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
) -> dict:
    """Decide si el hook debe terminar el turno y comenzar una compactación.

    Solo dispara en la primera invocación del turno (``invocationNum`` 0 o ausente).
    El texto del usuario sale de ``lastUserInput``; el transcript es respaldo.
    Las excepciones se convierten en ``{}`` para que un fallo del hook no
    interrumpa AGY.
    """
    try:
        active_env = os.environ if env is None else env
        if active_env.get("AGY_DASH_MACHINE_COMPACT") != "1":
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
        marker = _paths.HEARTBEATS_DIR / f"{conv_id}.autocompact"
        if _recently_compacted(marker, timestamp):
            return {}

        threshold = int(active_env.get("AGY_DASH_AUTO_COMPACT_TOKENS", "250000"))
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
        argv = [
            sys.executable,
            str(_paths.SELF_PATH),
            "self-compact",
            conv_id,
            "--resume-prompt",
            resume_prompt,
        ]

        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(timestamp), encoding="utf-8")
        try:
            (spawn or _spawn_detached)(argv)
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
