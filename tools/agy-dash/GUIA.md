# GUIA — agy-dash, agy-limited e isymem (Dashboard de 1 ventana, RAM compartida y Memoria Multi-Harness)

## El comando

```bash
agy-dash               # abre el Dashboard Multi-Sesión en 1 sola ventana (Motor Zig + Parser Python)
agy-fast-core scan     # escaneo nativo Zig (<8 ms) de /proc, smaps_rollup, KSM y sesiones AGY
agy-limited            # lanza agy con cgroup + KSM (PR_SET_MEMORY_MERGE) + GOMEMLIMIT=180MiB
isymem list            # muestra la Memoria Compartida Multi-Harness (Claude/AGY/Codex/Grok/OpenCode)
```

## Regla de oro

1. **Nunca hibernes ni mates a mano un PID con `BG-TASK` o `RUNNING`.** Usa siempre `agy-dash hibernate all-idle` (o la tecla `h`/`H` dentro de `agy-dash`): consulta `~/.gemini/antigravity-cli/conversation_summaries.db` (`not_fully_idle == 0` y `status == CASCADE_RUN_STATUS_IDLE`) más los hijos en `/proc/<pid>` para congelar a **0 MB de RAM** únicamente las sesiones 100% ociosas, dejando intactas las que tienen subagentes o tareas en segundo plano.
2. **Antes de un benchmark:** `agy-limited --status` y, si hay sesiones activas, `agy-dash hibernate all-idle` + `agy-limited --throttle 50%`.
3. **Nunca escribas secretos ni tokens (`IDENTITY_TOKEN`, `LEASE_TOKEN`) en `isymem save`.** La memoria compartida (`docs/agent/memory/`) es leída por todas las TUIs.

## Por qué existe

1. **RAM de múltiples pestañas de `agy`:** Cada instancia de `agy` (`1.5.10`, binario Go+C++ de 267 MB) reservaba ~230–290 MB de RAM privada (`Pss_Anon`) más 45–150 MB de Swap (~350 MB por pestaña; 4 pestañas = ~1.5 GB).
2. **`agy-dash` (`tools/agy_limits/agy_dashboard.py`)** resuelve esto combinando tres mecanismos sin parchear el binario cerrado:
   - **Panel único en una sola ventana de consola (`tmux -L agy-dash`)** con barra superior de pestañas (`0:⚡DASH`, `1:● Sesión`, `2:💤 Sesión [0MB]`) y atajos directos (`Alt+0`/`Alt+D` para el Command Center, `Alt+1..9` para cambiar de sesión, `Alt+N` nueva sesión, `Alt+H` hibernar ociosas).
   - **KSM del kernel Linux 7.0 (`prctl(PR_SET_MEMORY_MERGE, 1)`) + tope del GC de Go (`GOGC=35 GOMEMLIMIT=180MiB`):** heredado tras `execve`, fusiona páginas anónimas idénticas entre procesos y frena el crecimiento del heap de Go.
   - **Auto-Hibernate a 0 MB de RAM:** las pestañas en reposo (`IDLE`, `not_fully_idle=0`) se hibernan liberando ~250–369 MB por sesión a **0 MB** y quedan representadas por un stub ligero (`<2 MB`) que despierta instantáneamente en el mismo lugar al pulsar `Enter` (`agy --conversation <uuid>`).
3. **`isymem` (`tools/isymem/agent_memory.py`)** unifica la memoria nativa de Claude Code (`~/.claude/projects/<workspace>/memory/MEMORY.md`) con `docs/agent/memory/` e inyecta las reglas globales (`~/.gemini/GEMINI.md`, `~/.codex/AGENTS.md`, `~/.claude/CLAUDE.md`, `~/.qwen/QWEN.md`, `~/.config/opencode/AGENTS.md`) para que todas las TUIs compartan la misma memoria.

## Comandos (salida real ejecutada, 2026-11-17 22:49 -06:00)

### 1. Ver estado de RAM, KSM y sesiones (`agy-dash status` + `agy-fast-core`)

```
$ agy-dash status
╔══════════════════════════════════════════════════════════════════════════════════════════════╗
║  ⚡ AGY MULTI-SESSION DASHBOARD (Motor Zig + Parser Python — Estilo Codex / Grok)            ║
╚══════════════════════════════════════════════════════════════════════════════════════════════╝
  Escaneo Zig     : 8.89 ms (`agy-fast-core scan`) | Sesiones en RAM: 3 activas (5 procs) | 0 hibernadas (0 MB, ahorrados ~0 MB)
  Consumo actual  : PSS=1031 MB (Anon=913 MB) | Shared=35 MB + KSM=17 MB (3712 págs) | Swap=327 MB

  ESTADO      PID      UBICACIÓN      PSS  SHR+KSM   SWAP  ID        PASOS  TÍTULO / WORKSPACE
  ────────────────────────────────────────────────────────────────────────────────────────────
  RUNNING     1746960  tab:2+ext     130M      39M    77M  5f5d9aec   1012  Pulir Isycode Con Isymcp (ISyCo)
  RUNNING     1599625  ext:pts/0     206M      33M    53M  ab6bd0b4    321  Dashboard Multi-Sesión Para AGY (ISyCo) *
  BG-TASK     1740443  tab:1+ext     102M      39M    80M  2e8c8861    783  Orquestación De Paridad Piton (ISyCo)
  SAVED       -        disco           0M        -     0M  a53a3a0d     16  ISyMCP Agent Initialization And Se (ISyCo)
  SAVED       -        disco           0M        -     0M  6b24c72a   1065  Integración De Python Y Agentes (ISyCo)
  SAVED       -        disco           0M        -     0M  4bfc55f0    196  OpenSwift Code Audit And Tool (ISyCo)

  Comandos: `agy-dash` (Dashboard 1 ventana) | `agy-dash hibernate all-idle` | `isymem list`
```

### 2. Preparar o abrir el Dashboard de 1 sola ventana (`agy-dash`)

```
$ agy-dash --detach
Dashboard preparado en servidor tmux `agy-dash` (sesión `agy`).
Para adjuntarte en tu ventana de consola ejecuta: `agy-dash`
```

Para entrar desde tu consola interactiva:

```
$ agy-dash
```

### 3. Hibernar sesiones ociosas a 0 MB de RAM (`agy-dash hibernate all-idle`)

```
$ agy-dash hibernate all-idle
No hay sesiones AGY ociosas (IDLE con not_fully_idle=0) fuera de la sesión actual.
```

(Cuando `6b24c72a` estaba `IDLE` en `pts/3`, se hibernó liberando 369 MB de RAM+Swap y quedó anclada como pestaña `1:💤Integración-De-P` en `agy-dash` lista para despertar con `Enter`).

### 4. Consultar y diagnosticar la Memoria Compartida Multi-Harness (`isymem`)

```
$ isymem doctor
Memory directory : ~/.claude/projects/<workspace>/memory
Repo symlink     : <workspace>/docs/agent/memory -> ~/.claude/projects/<workspace>/memory
Indexed in MEMORY: 45 entries
Topic .md files  : 45 files
Broken links     : 0 (OK)
TUI rule [agy/gemini]: INSTALLED (~/.gemini/GEMINI.md)
TUI rule [codex     ]: INSTALLED (~/.codex/AGENTS.md)
TUI rule [claude    ]: INSTALLED (~/.claude/CLAUDE.md)
```

```
$ isymem list --limit 5
SHARED MEMORY (<workspace>/docs/agent/memory/MEMORY.md — 45 entradas):
  Detalle: `isymem show <slug>` | Buscar: `isymem search <patron>` | Guardar: `isymem save <slug> ...`
  - [Check long runs early](check-long-runs-early.md) — inspect process state/CPU within minutes; NVIDIA driver can wedge ggml-cuda binaries
  - [Slop pipeline for weights](slop-pipeline-weights.md) — Danny’s recurring idea: compress weights with Slop New York’s chain (filter, simplify, per-field precision, reorder+compress, seeded texture)
  - [Engine good neighbour](engine-good-neighbor.md) — engine yields under host pressure (OOM-first, PSI back-off); validate speedups also under repeatable realistic 16 GB load
  - [Dataflow pattern](dataflow-pattern.md) — Danny’s dataflow structure (persistent workers, adaptive path, process skipping, size lanes) mapped to inference/Olympics
  - [ISyMCP repo git rules](isymcp-repo-git.md) — public repo, protected main (PR + test check, merge commit), fetch before branching, load-flaky tests
  (+40 entradas mas: `isymem list`)
```

### 5. Guardar una memoria compartida (`isymem save`)

```
$ isymem save <slug> --name "<Título>" --desc "<Resumen 1 línea>" --type feedback|user|project|reference --why "<Motivo>" --how "<Cómo aplicarlo>"
```

## Cómo leer la salida

| Ves | Significa | Haz |
|---|---|---|
| `RUNNING` | `agy` ejecutando un turno activo del modelo | No tocar |
| `BG-TASK` | Turno en espera pero con subagentes o tareas shell vivas (`not_fully_idle=1`) | No hibernar; sigue trabajando en background |
| `IDLE` | 100% en reposo (`not_fully_idle=0`, sin hijos activos) pero ocupando ~230 MB RAM | Pulsa `h` / `H` en `agy-dash` o corre `agy-dash hibernate all-idle` |
| `HIBERNATED` (`💤0MB`) | Sesión congelada en disco (0 MB RAM); pestaña lista en `agy-dash` | Pulsa `Enter` sobre ella en `agy-dash` para despertarla en <1 s |
| `SAVED` | Conversación histórica en `conversation_summaries.db` (0 MB RAM) | Pulsa `Enter` en `agy-dash` para reabrirla en una pestaña |
| `Broken links: 0 (OK)` en `isymem doctor` | Todas las entradas de `MEMORY.md` tienen su archivo `.md` válido | Listo para usar |

## Atajos y Centro de Comando dentro de `agy-dash`

El panel `0:⚡DASH` muestra la lista de sesiones a la izquierda, la **Vista en Vivo Zig (<4 ms)** abajo y el **Centro de Comando interactivo** a la derecha (soporta **clic con el mouse** y **teclado**):

| Tecla / Botón | Dónde | Acción |
|---|---|---|
| `r` (`[r] 🔄 Actualizar`) | `0:⚡DASH` o `👁️ Espejo` | Fuerza escaneo Zig + recarga DB + refresca vista y barra |
| `Enter` (`[↵] 🚀 Abrir / Ir`) | `0:⚡DASH` o pestaña `💤` | Abre/despierta la sesión (si está ocupada afuera en `pts/*`, abre `👁️ Espejo Vivo`) |
| `v` (`[v] 👁 Espejo Vivo`) | `0:⚡DASH` | Abre pestaña de espejo en tiempo real (`<3 MB RAM`, refresca cada 0.4s vía Zig) |
| `R` (`[R] ♻ Recargar Tab`) | `0:⚡DASH` | Recarga una pestaña ya abierta para sincronizar su último estado en disco |
| `n` / `Alt+N` (`[n] ➕ Nueva`) | Global / `0:⚡DASH` | Abre nueva sesión `agy` optimizada (`KSM` + `GOMEMLIMIT=180MiB`) |
| `h` (`[h] 💤 Hibernar 0MB`) | `0:⚡DASH` | Hiberna la sesión `IDLE` seleccionada a `0 MB RAM` |
| `H` / `a` / `Alt+H` (`[H] ⚡ Hibernar IDLE`) | Global / `0:⚡DASH` | Hiberna todas las sesiones `IDLE` y adopta sus pestañas `💤` |
| `x` (`[x] 🧹 Deduplicar`) | `0:⚡DASH` | Cierra copias duplicadas inactivas fuera de `agy-dash` |
| `B` (`[B] 🔀 Baton Pass`) | `0:⚡DASH` / `👁️ Espejo` | Pasa la estafeta de la conversación + mismo modelo a otra cuenta Pro sin hacer logout |
| `u` (`[u] 💳 Cuentas`) | `0:⚡DASH` | Abre gestor de cuentas Pro, cuotas `/usage` (`+`: login aislado, `B`: baton, `R`: refrescar) |
| `U` | `0:⚡DASH` | Refresca en segundo plano las cuotas (`/usage`) de todas las cuentas |
| `c` (`[c] 🗜 Compactar NV`) | `0:⚡DASH` o pestaña `💤` | Compacta la conversación seleccionada con NVIDIA NIM (Nemotron Ultra) y la abre ligera ahorrando >95% de tokens de entrada |
| `t` (`[t] ✏️ Renombrar`) | `0:⚡DASH` o `Ctrl+B ,` | Renombra permanentemente la conversación en el dashboard, pestañas LED y bases de datos |
| `K` (`[K] 🔑 Key NVIDIA`) | `0:⚡DASH` o pestaña `💤` | Guarda tu NVIDIA API Key (`nvapi-...`, permisos `0600`) para compactación externa |
| `m` (`[m] 🧠 Memoria Shared`) | `0:⚡DASH` | Alterna el visor de Memoria Compartida Multi-Harness (`MEMORY.md`) |
| `Alt+0` / `Alt+D` / Clic en `⚡ AGY DASH` | Cualquier pestaña | Alterna entre el Command Center (`0:⚡DASH`) y tu pestaña activa |
| `Alt+1` .. `Alt+9` / `Alt+Left`/`Right` | Cualquier pestaña | Cambiar entre pestañas en la misma ventana |

### Multi-Cuenta Google Pro, Alertas LED, Compactación NVIDIA NIM y Baton Pass sin Logout

1. **`primary`** usa automáticamente tu sesión actual en el keyring del sistema (`agy-dash quota --refresh`).
2. **Agregar 2ª+ cuenta Pro sin cerrar la actual:** pulsa `u` -> `+` en el dashboard (o corre `agy-dash account add pro2`). Abre una pestaña aislada (`DBUS_SESSION_BUS_ADDRESS=unix:path=/nonexistent` + `HOME` aislado con symlinks a tus conversaciones/MCP) para completar OAuth una sola vez sin tocar `primary`.
3. **Baton Pass (`[B]` o `agy-dash baton <conv> [--to <slot>]`):** cuando la alerta de cuota baja (`🚨 <=5%` / `⚠️ <=15%`) avise en el banner del dashboard, pulsa `B` para relanzar exactamente `--conversation <uuid> --model <modelo>` bajo la cuenta con mayor cuota restante.
4. **Compactador Externo con NVIDIA NIM (`[c]` / `[K]` / `agy-dash nvapi set-key <nvapi-...>`):**
   - Guarda tu API key de NVIDIA una sola vez (`~/.gemini/antigravity-cli/nvapi_config.json`, `0600`).
   - Antes de reabrir una conversación `SAVED` o `HIBERNATED` pesada (`>=80` pasos o `>=120 KB`), aparece una pantalla que ofrece compactarla con **NVIDIA Nemotron Ultra** (`nvidia/llama-3.1-nemotron-ultra-253b-v1` / `nemotron-3-ultra-550b`), generando `compacted/<id>_handoff.md` y abriendo una sesión `agy` ligera con todo el estado técnico en ~1,000 tokens en lugar de >100,000 tokens de entrada.
5. **Pestañas tipo letrero LED y Renombrado Persistente (`[t]` / `agy-dash rename` / `Ctrl+B ,`):**
   - Las pestañas en `RUNNING` / `BG-TASK` muestran un spinner Braille animándose a la izquierda (`⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏`) junto con el título + la acción actual desplazándose estilo marquee (`Editing files...`, `Running cmd...`).
   - Si renombras una sesión con `[t]`, `agy-dash rename <id> "<nombre>"` o directamente en tmux (`Ctrl+B ,`), el nombre personalizado se guarda en `custom_titles.json` y en todas las `conversation_summaries.db` (incluyendo cuentas aisladas) sin regresar nunca al nombre anterior.
6. **Protección contra hibernación en medio de un turno (`is_transcript_turn_active`):**
   - Aunque `agy` reporte temporalmente `CASCADE_RUN_STATUS_IDLE` entre llamadas de herramientas (`read_file`, `edit_file`) sin subprocesos `bash`, `agy-dash` inspecciona `transcript.jsonl` (mtime reciente y `tool_calls` pendientes) para mantener la sesión en `RUNNING` y nunca enviarla a la pantalla `💤 SESIÓN EN MODO HIBERNADO`. Auto-hibernate por temporizador está desactivado por defecto (`AGY_IDLE_HIBERNATE_SEC=0`).
7. **Botón `×` en pestañas (estilo OpenCode) y Selección Freeze-Copy:**
   - Hacer clic en **`×`** (o clic central) sobre una pestaña cierra **solo el tab visual**: si esa sesión sigue en `RUNNING` o `BG-TASK`, se estaciona viva en la sesión oculta `agy-bg` sin matar el proceso (puedes traerla de vuelta al frente en cualquier momento desde `0:⚡DASH` con `Enter`).
   - Arrastrar con el mouse o usar la rueda arriba en cualquier pestaña `agy` entra automáticamente en `copy-mode` (congela el scroll mientras el modelo escribe para que **nunca se corte ni deseleccione lo subrayado**) y `MouseDragEnd1Pane` copia al portapapeles sin limpiar la selección (`Alt+C` activa el mismo modo por teclado; `Alt+S` y `Alt+X` quedan para AGY: board y dismiss del swarm).
   - Al abrir login aislado (`u -> +`), un wrapper `xdg-open` restaura tu `DBUS_SESSION_BUS_ADDRESS` real para abrir el navegador automáticamente y copia la URL OAuth completa en 1 línea al portapapeles (`Ctrl+V`).
8. **Seguridad al cerrar procesos (auditoría 2026-11-18):**
   - Hibernar, deduplicar, migrar o hacer Baton Pass **vuelve a leer el estado justo antes de cerrar** (la lista de la TUI puede tener segundos): si en ese momento hay un turno o tarea activa, no se toca.
   - Solo se manda `SIGTERM` si el PID sigue siendo el mismo `agy` del escaneo (misma hora de arranque del kernel; un PID reciclado nunca se toca) y nunca se escala a `SIGKILL`. La sesión solo se marca hibernada si el proceso de verdad terminó.
   - Las copias duplicadas **ya no se cierran solas**: el panel avisa ("está abierta N veces") y tú decides con `[x]`. Antes se cerraba cada 10 s cualquier copia externa sin hijos, aunque fuera la terminal donde estabas escribiendo.
   - Abrir en el panel una sesión que corre en otra terminal: si esa copia está ocupada, o no se cierra, se abre el espejo (`👁`) en vez de un segundo `agy` escribiendo la misma conversación.
   - `self-compact` espera a que el turno termine de verdad (`--wait`, 900 s por defecto) antes de reemplazar la pestaña. Si sigue ocupada, deja el handoff en el Baúl sin cortar nada. `self` se resuelve por la pestaña del propio agente (`$TMUX_PANE`), no por la que estás mirando, y si hay ambigüedad pide el `conv_id`.
   - Eliminar una cuenta aislada: si AGY reemplazó un symlink compartido por un archivo real (pasa con `conversation_summaries.db`), ese archivo se **mueve** a `accounts/.rescued/<cuenta>-<fecha>/` antes de purgar; nunca se borra.
   - El escaneo de sesiones (~50 ms) corre en su propio hilo: las teclas ya no esperan. El spinner de pestañas va cada 0.9 s (`AGY_LED_TICK_SEC`).

## Trampas

1. **`agy` lanzado fuera de `agy-dash` / `agy-limited`** no lleva `PR_SET_MEMORY_MERGE=1` ni `GOMEMLIMIT=180MiB`. Usa `agy-dash` (o pon `alias agy=agy-limited` en `~/.bashrc`).
2. **Codex ignora `@imports` en `AGENTS.md`:** por eso `isymem setup` escribe la regla **inline** en `~/.codex/AGENTS.md` y `~/.gemini/GEMINI.md`, y usa `@~/.config/isyco-memory/agents.md` en `~/.claude/CLAUDE.md`. Si editas la regla base, vuelve a correr `isymem setup`.
3. **Capturar stdout al lanzar `tmux new-session -d`** desde Python cuelga si no se redirige `stdout/stderr` a `DEVNULL`, porque el daemon del servidor `tmux` hereda el descriptor del pipe (`tmux_cmd` en `agy_dash/tmuxctl.py` ya lo aísla con `DEVNULL`).
4. **Dos copias del dash:** `~/.local/bin/agy-dash` y el hook de AGY apuntan al fork (`ISyCo Git/antigravity-cli/tools/agy-dash`), pero `tools/agy_limits/` del monorepo es una copia. Si el panel se arrancó desde la copia, corre ese código: arráncalo siempre con `agy-dash`.

### ⌨ Atajos

Arriba a la derecha de la barra hay un botón amarillo **⌨ Atajos**: clic y sale un popup con todas las combinaciones (dash, Command Center y AGY). Cualquier tecla lo cierra. Por terminal: `agy-dash keys`.

## Arquitectura por módulos (2026-11-18)

El código vive en el paquete `tools/agy-dash/agy_dash/`, partido mecánicamente desde el antiguo monolito `agy_dashboard.py` (último commit antes del split: `f255869`; el split: `a703354`, sin cambio de comportamiento). `agy_dashboard.py` ya no contiene lógica: es un shim (ver abajo). El plan y las puertas de verificación están en `tools/agy-dash/PLAN_OLA_MODULOS.md`.

### Módulos (nombres verificados con `grep` sobre el paquete)

| Módulo | Responsabilidad | Funciones y clases principales |
|---|---|---|
| `paths.py` | HOME real (`_resolve_real_home`), rutas y constantes de entorno/umbrales (`BRAIN_DIR`, `SUMMARIES_DB`, `ZIG_CORE_BIN`, `COMPACTED_DIR`, `ACCOUNTS_DIR`, `TMUX_SOCKET`, `PR_SET_MEMORY_MERGE`, `DEFAULT_GOMEMLIMIT`, etc.) y `SELF_PATH` (el shim). Se importa primero desde `__init__.py`. | `_resolve_real_home`, constantes |
| `procs.py` | Procesos: escaneo Zig de `/proc`, KSM, identidad de PID y terminación verificada | `zig_scan`, `ensure_zig_core`, `ProcMemInfo`, `enable_ksm_memory_merge`, `is_ancestor_pid`, `proc_start_ticks`, `terminate_verified`, `_proc_is_agy` |
| `sessions.py` | Sesiones AGY: carga desde `conversation_summaries.db`, refresco, transcript y turno activo, registros de hibernadas y títulos, hibernar y deduplicar | `AgySession`, `load_sessions`, `refresh_session`, `transcript_path_for`, `is_transcript_turn_active`, `zig_tail_transcript`, `load_hibernated_registry`, `save_hibernated_registry`, `load_custom_titles`, `save_custom_title`, `hibernate_session`, `deduplicate_session_procs`, `close_stale_dash_duplicates` |
| `tmuxctl.py` | Todo lo que habla con tmux: `tmux_cmd`, configuración, mapas de ventanas, pestañas, pestañas LED, pestaña de login, `close_tab_safely` | `tmux_cmd`, `write_tmux_config`, `open_session_in_dashboard_tab`, `launch_account_login_tab`, `sync_led_tmux_tabs`, `format_led_tab_title`, `ensure_dash_window_zero`, `session_live_activity`, `close_tab_safely` |
| `accounts.py` | Cuentas Pro: home aislado, registro de cuentas, cuota, baton pass, entorno de arranque de `agy`, retirada de cuentas | `load_accounts_registry`, `save_accounts_registry`, `ensure_isolated_home`, `refresh_account_quota`, `parse_usage_output`, `compute_quota_alert`, `choose_baton_target`, `baton_preflight`, `baton_pass_session`, `build_agy_env`, `exec_agy_optimized`, `inspect_account_removal`, `remove_account_slot`, `rescue_diverged_shared`, `purge_isolated_slot_dir` |
| `compact.py` | Compactación: NVIDIA NIM (clave y llamada), Baúl de handoffs, medición de contexto, heartbeat y respawn de la sesión compactada | `compact_conversation_with_nvapi`, `load_nvapi_config`, `save_nvapi_config`, `mask_api_key`, `measure_session_context`, `evaluate_context_heartbeat`, `should_offer_compaction`, `respawn_compacted_session_in_place`, `get_vault_handoff`, `list_compacted_vault`, `delete_vault_handoff`, `detect_caller_conversation_id`, `reload_session_tab` |
| `mirror.py` | Espejo en vivo (`👁`) de sesiones que corren en otra terminal | `run_live_mirror_curses`, `latest_active_task_log` |
| `tui.py` | Command Center en curses: botones, hit-test de ratón, prompts de texto y bucle principal (clase `Dashboard`: estado como atributos, un método por vista/acción) | `run_curses_tui` (delega en `Dashboard(stdscr).run()`), `Dashboard`, `CommandButton`, `build_command_buttons`, `hit_test_button`, `_prompt_curses_text`, `_ram_bar` |
| `cli.py` | Subcomandos de la CLI y punto de entrada | `build_parser`, `main`, los `cmd_*` (p. ej. `cmd_status`, `cmd_hibernate`, `cmd_accounts`, `cmd_baton`, `cmd_compact`, `cmd_self_compact`, `cmd_tui`, `cmd_dashboard`) |

`agy_dash/__init__.py` contiene además `OWNER`, la tabla generada por `scripts/split_agy.py` con una entrada `nombre → módulo` (ver siguiente sección).

### Cómo funciona el shim y por qué existe

- `agy_dashboard.py` hace `import agy_dash`, importa cada módulo dueño y reemplaza la clase de su propio módulo por `_ForwardingModule`. Así, `ad.NOMBRE` lee, escribe o borra `NOMBRE` en el módulo que lo define (según `OWNER`). Un nombre que no está en `OWNER` da `AttributeError`.
- `main` pertenece a `cli`: `python3 agy_dashboard.py ...` ejecuta `cli.main()`.
- Motivo: las pestañas se relanzan con `exec python3 agy_dashboard.py ...`, el hook PreInvocation, `~/.local/bin/agy-dash` y `agy-limited` apuntan a este archivo, y los tests hacen `ad.BRAIN_DIR = x` o `mock.patch.object(ad, "refresh_session")`. El shim mantiene todo eso sin tocar a los llamadores. Como `setattr` se reenvía al módulo dueño, un parche sobre `ad` cambia el global que leen también las llamadas internas del propio módulo.
- Si añades un nombre de nivel superior que un test, una pestaña o el hook necesitan vía `ad.NOMBRE`, añádelo también a `OWNER`. Si solo lo usa el paquete, no hace falta.
- `paths.SELF_PATH` = `tools/agy-dash/agy_dashboard.py`. Es el archivo que se relanza. En código movido, `Path(__file__)` se sustituyó por `_paths.SELF_PATH`; `paths.py` conserva su propio `Path(__file__)` (`SCRIPT_DIR`) porque está un nivel dentro del paquete.

### Regla del enlace tardío (`_alias`)

No existe ninguna función `_alias`: la convención es `from . import X as _X`, y el uso es siempre en el cuerpo de la función:

```python
from . import sessions as _sessions        # en el módulo que llama
...
def algo():
    _sessions.load_sessions()              # el atributo se resuelve al llamar
```

- **Nunca** `from .paths import BRAIN_DIR` ni `from .sessions import load_sessions`: copia el valor en el momento del import y un parche posterior no lo alcanza. Lee siempre `_paths.BRAIN_DIR`.
- Los módulos se importan entre sí en ciclos (`procs` ↔ `tmuxctl`, `sessions` ↔ `accounts`, `tui` ↔ `cli`, etc.). Con enlace tardío el orden de import no importa. La excepción es `paths`, que se importa primero porque otros módulos leen constantes al importarse (valores por defecto de argumentos, campos de dataclass).
- El guion bajo evita choques: las funciones tienen variables locales llamadas `sessions` o `accounts` (ruff F823 marcó 10 sitios antes del cambio). Por eso el alias es `_sessions`, no `sessions`.

### Dónde poner código nuevo

- Lógica nueva: en el módulo cuyo tema coincide con la tabla de arriba. Si no encaja en ninguno, valora un módulo nuevo antes de meterlo en `cli.py` o `tui.py`.
- Constantes de rutas, umbrales o variables de entorno: `paths.py`.
- Cualquier llamada a tmux: pásala por `tmux_cmd` (`tmuxctl.py`), que ya aísla stdin/stdout/stderr con `DEVNULL`. No llames `subprocess` a tmux directamente.
- Llamadas entre módulos: `from . import X as _X` y `_X.nombre()`. No añadas `from .X import nombre`.
- Subcomando nuevo: `cmd_*` en `cli.py` y registrarlo en `build_parser`.
- Rutas de la app: no uses `Path(__file__)` fuera de `paths.py`; usa `_paths.SELF_PATH` o `_paths.SCRIPT_DIR`.
- Tests nuevos: un archivo por módulo (`test_tui.py`, `test_autocompact.py`, …); parchea el módulo dueño (`mock.patch.object(agy_dash.paths, "BRAIN_DIR", …)`) o `ad` vía el shim. `test_agy_dashboard.py` queda para los tests históricos.
- No edites la copia del monorepo `tools/agy_limits/agy_dashboard.py` (sigue siendo el monolito de 5.3k líneas, ver trampa 4) para cambios de este código.

### Cómo verificar un cambio

Desde `tools/agy-dash/`:

```bash
python3 -m pytest -q test_*.py                 # toda la suite
ruff check --select F agy_dash                  # sin errores F (pyflakes)
python3 scripts/verify_split.py <raíz del worktree>   # ver nota
```

- `verify_split.py` no mide la lógica: compara todas las definiciones de `git show f255869:tools/agy-dash/agy_dashboard.py` con `<raíz>/tools/agy-dash/agy_dash/*.py` (normaliza el prefijo `_módulo.` y `Path(__file__)` → `_paths.SELF_PATH`). Sale con 0 solo si nada cambió; sirve como comprobación del split. Tras un cambio real de código dará `differing` no vacío a propósito; eso no es un fallo del script. Requiere que `f255869` exista en el repo.
- Para validar comportamiento, usa los tests y el `--help` de la CLI, que debe ser igual que antes del cambio; no ejecutes `agy`, `agy-dash` ni `tmux` como prueba.
