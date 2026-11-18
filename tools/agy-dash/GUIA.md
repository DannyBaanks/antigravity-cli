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
| `m` (`[m] 🧠 Memoria Shared`) | `0:⚡DASH` | Alterna el visor de Memoria Compartida Multi-Harness (`MEMORY.md`) |
| `Alt+0` / `Alt+D` | Cualquier pestaña | Volver al Command Center (`0:⚡DASH`) |
| `Alt+1` .. `Alt+9` / `Alt+Left`/`Right` | Cualquier pestaña | Cambiar entre pestañas en la misma ventana |

## Trampas

1. **`agy` lanzado fuera de `agy-dash` / `agy-limited`** no lleva `PR_SET_MEMORY_MERGE=1` ni `GOMEMLIMIT=180MiB`. Usa `agy-dash` (o pon `alias agy=agy-limited` en `~/.bashrc`).
2. **Codex ignora `@imports` en `AGENTS.md`:** por eso `isymem setup` escribe la regla **inline** en `~/.codex/AGENTS.md` y `~/.gemini/GEMINI.md`, y usa `@~/.config/isyco-memory/agents.md` en `~/.claude/CLAUDE.md`. Si editas la regla base, vuelve a correr `isymem setup`.
3. **Capturar stdout al lanzar `tmux new-session -d`** desde Python cuelga si no se redirige `stdout/stderr` a `DEVNULL`, porque el daemon del servidor `tmux` hereda el descriptor del pipe (`tmux_cmd` en `agy_dashboard.py` ya lo aísla con `DEVNULL`).
