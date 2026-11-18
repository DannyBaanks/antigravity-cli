# Plan: ola de agentes isycode (Nemotron, modo flow) para partir agy-dash en módulos

Fecha: 2026-11-18 · Autor: claude_code_eb8d9505fcae41a2 · Estado: **APROBADO por Danny 2026-11-18 ("aprobado :p")**
Base: fork `DannyBaanks/antigravity-cli`, rama `feat/agy-dash-vault-heartbeat` @ `6309a77`
(auditoría de seguridad ya integrada; copia del monorepo `tools/agy_limits` @ `ee8fc0fa`).

## Objetivo

`tools/agy-dash/agy_dashboard.py` = 5.2k líneas en un archivo; `run_curses_tui` ~1000 líneas en una
función. Partirlo en un paquete legible **sin cambiar comportamiento**, como claude-dash
(`paths/procs/sessions/tmuxctl/compact/tui/cli`). En paralelo: averiguar si AGY puede compactarse
**del lado de la máquina** (hook que lanza Nemotron y bloquea el prompt), como ya hace claude-dash.

## Por qué NO se reparte el archivo grande entre varios LLM a la vez

Varios agentes editando el mismo archivo de 5k líneas = conflictos y código mutilado. Por eso:

1. **Fase 1 (mecánica, un solo paso, determinista):** un script mueve rangos de código tal cual a
   módulos y deja `agy_dashboard.py` como shim que re-exporta todo. Sin LLM, verificable por diff.
2. **Fase 2 (la ola):** cada agente es dueño de **un módulo distinto** (archivos disjuntos): limpia
   imports, separa funciones gigantes, añade tests de su módulo. Cero conflictos de merge.

## Fase 0 — humo (antes de lanzar nada, ~5 min)

- `isycode -p --mode flow --model nvidia/nemotron-3-ultra-550b-a55b "responde OK"` y lo mismo con
  `nvidia/nemotron-3.5-super-vl-120b-a12b` (no aparece en `isycode models`: puede necesitar
  `--provider nvidia`). Si un modelo falla por auth/cuota, no entra a la ola.
- Ver dónde escribe flow (que respete `-C`/cwd del worktree) y que no haga `git push`.
- Medir cuánto tarda en leer un módulo de ~800 líneas (los modelos lentos tardan 10+ min).

## Fase 1 — esqueleto (yo, determinista)

Paquete `tools/agy-dash/agy_dash/`:

| Módulo | Contenido (por nombre, no por línea) |
|---|---|
| `paths.py` | `_resolve_real_home`, HOME y rutas, constantes/umbrales de entorno |
| `procs.py` | `ProcMemInfo`, `zig_scan`, `ensure_zig_core`, KSM, `is_ancestor_pid`, `proc_start_ticks`, `terminate_verified` |
| `sessions.py` | `AgySession`, `load_sessions`, `refresh_session`, transcript (`transcript_path_for`, `is_transcript_turn_active`, `zig_tail_transcript`), registro hibernado, títulos |
| `tmuxctl.py` | `tmux_cmd`, `write_tmux_config`, mapas de ventanas, `open_session_in_dashboard_tab`, wake stub, `close_tab_safely`, LED (`sync_led_tmux_tabs`) |
| `accounts.py` | home aislado, registro, cuota, `baton_*`, `remove_account_slot`, `rescue_diverged_shared` |
| `compact.py` | NVAPI, `compact_conversation_with_nvapi`, Baúl, `measure_session_context`, heartbeat, `respawn_compacted_session_in_place`, `cmd_self_compact` core |
| `mirror.py` | `run_live_mirror_curses`, `latest_active_task_log` |
| `tui.py` | `CommandButton`, botones, `_prompt_curses_text`, `run_curses_tui` |
| `cli.py` | `cmd_*`, `build_parser`, `main` |

- `agy_dashboard.py` queda como shim: `from agy_dash.cli import main` + re-export (`from agy_dash.X import *`)
  para que **el hook, `~/.local/bin/agy-dash`, `agy-limited` y la línea `exec python3 agy_dashboard.py ...`
  de las pestañas sigan funcionando** sin tocarlos.
- **Trampa clave:** los tests hacen `ad.BRAIN_DIR = ...` / `mock.patch.object(ad, "refresh_session")`.
  Tras partir, ese parche no llega al módulo dueño. Fase 1 actualiza los tests para parchear el
  módulo real (`agy_dash.sessions.BRAIN_DIR`), y las rutas se leen vía `paths.X` (no copiadas con
  `from paths import X`) para que un solo parche alcance a todos.
- Puerta de Fase 1: 23/23 tests, `ruff --select F` limpio, `agy_dashboard.py --help` y la ayuda de
  cada subcomando **idénticas** byte a byte a `6309a77`, `status` en vivo (solo lectura) con las mismas
  sesiones/ubicaciones. Commit en rama `refactor/agy-dash-modules`.

## Fase 2 — la ola (isycode `--mode flow`, un worktree + rama por agente)

| Agente | Modelo | Módulo(s) | Tarea |
|---|---|---|---|
| A | Nemotron 550B | `tui.py` | Convertir `run_curses_tui` en clase `Dashboard` con `render_list`, `render_inspector`, `render_preview`, `handle_key`, `execute_action`; ningún método >150 líneas |
| B | MiniMax 3.1 (relanzado: 550B dio HTTP 429 con 3 en paralelo; MiniMax pasó con ISYCODE_STREAM_TIMEOUT_S=400) | `accounts.py` | Separar cuota / baton / perfiles; tests de `baton_preflight` y purga |
| C | Step 5 (`stepfun-ai/step-5-preview`) | `sessions.py` + `procs.py` | Tests de `load_sessions` con DB sqlite falsa; docstrings; sin cambios de lógica |
| D | Nemotron 550B (MiniMax 3.1 dio timeout 2 veces; Danny: "SI NO, USA NEMOTRON 550B") | `compact.py` + `mirror.py` | Separar NVAPI / Baúl / heartbeat; tests del heartbeat y del Baúl |
| E | Mistral 4 (`mistralai/mistral-large-4-1t-a52b`) | `tmuxctl.py` + `cli.py` | Tests de construcción de comandos tmux (sin servidor real) |

Reglas que va en el prompt de cada agente (literal):
- Solo editas tus archivos; **no** tocas otros módulos ni el shim; no `git push`, no borrar archivos,
  no `git commit` (yo reviso y hago commit), no ejecutar `agy`, `tmux kill-*`, `hibernate`, `kill`.
- **Cero cambios de comportamiento.** Si ves un bug, lo anotas en `REPORT_<agente>.md`, no lo arreglas.
- Al terminar: `python3 -m pytest -q` + `ruff check --select F` en tu worktree y pega la salida real en el reporte.
- Lanzamiento: `isycode -p --mode flow --model <id> --json "<prompt>"` con cwd = worktree, en segundo
  plano, log por agente. Alternativa si isycode se atora: `isymcp agentspipeline` (olas en worktrees con
  baseline de fallos y gate por cherry-pick).
- Monitor que vigila los `REPORT_*.md` / commits y **se termina solo** al acabar o abortar.

## Fase 3 — mi auditoría final (serial, por agente)

1. Diff completo de cada rama contra el esqueleto: ¿solo tocó sus archivos? ¿cambió lógica?
2. Tests + ruff + ayuda CLI idéntica + `status` en vivo de solo lectura.
3. Revisión a mano de toda ruta que mata procesos o borra archivos (no debe haber cambios ahí).
4. Integración una por una (cherry-pick), corriendo la suite entera tras cada una; la que rompa algo
   se rechaza con su motivo.
5. Sincronizar la copia del monorepo con lease del bridge; reiniciar **solo** la ventana 0 del panel.

## En paralelo (yo, mientras la ola trabaja) — compactación automática de AGY

Preguntas a responder con evidencia, sin tocar sesiones vivas:
1. ¿Qué eventos de hook tiene AGY (`~/.gemini/config/hooks.json`, `PreInvocation`) y su esquema de
   salida? ¿Puede un hook **bloquear** la invocación del modelo (como `decision: block` en Claude)?
   Método: `strings` del binario `agy` + hook de prueba que solo registra y bloquea, en una
   conversación nueva desechable.
2. ¿El payload trae el prompt del usuario y el `conversationId`?
3. ¿AGY puede continuar la **misma** conversación compactada (inyectando en `transcript.jsonl` /
   la DB) o solo una nueva con handoff (lo que hace hoy `respawn_compacted_session_in_place`)?
4. Si 1–2 son sí: prototipo "lado máquina" (umbral + `/compactnvidia`-equivalente que no pasa por el
   modelo), detrás de una variable de entorno, con tests. Si no: documentarlo como límite de AGY.

## Riesgos

- Modelos gratis/lentos: tiempo y deslices (palabras en otro idioma, imports rotos) → por eso la
  puerta de tests y la revisión.
- El panel vivo corre la copia del monorepo: nada se activa hasta la Fase 3.
- Cuota NVIDIA compartida con la compactación de AGY/claude-dash.

## Registro de ejecución

**2026-11-18 · Fase 0 (humo, `isycode -p --mode flow --provider nvidia --json`, tarea: crear test pytest + correrlo, sin commit):**
Danny cambió la ola: *"120b no, usa a minimax 3.1 step 5 y mistral 4"*.

| Modelo | Resultado | Tiempo |
|---|---|---|
| nemotron-3-ultra-550b-a55b | OK (eco) | 19 s |
| nemotron-3.5-super-vl-120b-a12b | OK, 3 tools, test pasa (descartado por Danny) | 7.5 s |
| stepfun-ai/step-5-preview | OK, 4 tools, test pasa | 84 s |
| mistralai/mistral-large-4-1t-a52b | OK, 4 tools, 3 tests pasan | 118 s |
| minimaxai/minimax-m3.1-flash | 1er intento: `StreamError: provider request timed out` (121 s); reintento: ver abajo | — |

**2026-11-18 · Fase 1 hecha (rama `refactor/agy-dash-modules`):** `scripts/split_agy.py` (determinista) +
`scripts/verify_split.py`. Puertas: AST de las 170 definiciones idéntico al original salvo prefijos
`_mod.` y `Path(__file__).resolve()` → `_paths.SELF_PATH`; 23/23 tests; `ruff --select F` limpio;
ayuda de la CLI y de los 19 subcomandos byte a byte igual; `status` en vivo misma forma (25 líneas).

**Desviación del plan (dato > plan):** en vez de reescribir los tests para parchear el módulo dueño,
`agy_dashboard.py` es un shim que **reenvía** lectura/escritura/borrado de atributos al módulo dueño
(`agy_dash.OWNER`), y los módulos se llaman entre sí con enlace tardío (`_sessions.load_sessions`).
Así `ad.BRAIN_DIR = x` y `mock.patch.object(ad, ...)` siguen llegando a todos los llamadores y los
tests no cambian (única excepción: un test que leía el *texto* del archivo ahora lee shim + paquete).
Los alias llevan `_` porque `sessions`/`accounts` chocaban con variables locales (ruff F823, 10 sitios).
**Trampa:** `~/.local/bin/agy-dash` → checkout principal del fork: no integrar ahí hasta la Fase 3.
