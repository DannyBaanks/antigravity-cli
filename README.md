# Antigravity CLI

Antigravity CLI understands your codebase, makes edits with your permission, and executes commands — right from your terminal.

- **Official Docs**: [antigravity.google/docs/cli/overview](https://antigravity.google/docs/cli/overview)
- **Official Website**: [antigravity.google/product/antigravity-cli](https://antigravity.google/product/antigravity-cli)

![Antigravity CLI Demo](agy-cli-demo.gif)

---

Antigravity CLI brings the core capabilities of Antigravity 2.0 (multi-step reasoning, multi-file editing, tool calling, and persistent history) directly to your terminal. It is optimized for keyboard-driven workflows and remote SSH sessions with minimal resource overhead.

---

## Features at a Glance

| Feature | Antigravity CLI | Antigravity 2.0 |
| :--- | :--- | :--- |
| **Primary Focus** | Speed, keyboard efficiency, low overhead | Comprehensiveness, visual orchestration, project management |
| **Interface** | Terminal User Interface (TUI) | Full Rich GUI Application |
| **Workflows** | SSH/Remote sessions, keyboard-first | Local workspaces, heavy orchestration |
| **Agent Engine** | Shared Core Agent Engine | Shared Core Agent Engine |

---

## Integration

- **Shared Agent Engine**: Both interfaces run on the same core agent engine. Improvements automatically apply to both.
- **Shared Settings**: Preferences and permissions sync bidirectionally.
- **Session Export**: Export terminal sessions to the Antigravity 2.0 GUI to continue working.

---

## Installation

### macOS / Linux
```bash
curl -fsSL https://antigravity.google/cli/install.sh | bash
```

### Windows PowerShell
```powershell
irm https://antigravity.google/cli/install.ps1 | iex
```

### Windows CMD
```cmd
curl -fsSL https://antigravity.google/cli/install.cmd -o install.cmd && install.cmd && del install.cmd
```

## Usage

After installation, start Antigravity CLI by running:

```bash
agy
```

---

## Authentication

The CLI authenticates via the system keyring, falling back to Google Sign-In if no active session exists.

- **Local**: Automatically opens your default browser.
- **Remote / SSH**: Detects SSH sessions and prints an authorization URL to complete login locally.
- **Sign Out**: Run `/logout` to clear saved credentials.

> [!NOTE]
> For enterprise access, connect your GCP project during onboarding. See the Enterprise page for details.

---

## Terms of Service & Data Use

> [!WARNING]
> AI coding agents are known to have certain security risks, including autonomous code execution, data exfiltration, prompt injection, and supply chain risks. Ensure that you monitor and verify all actions taken by the agent.

By using Antigravity CLI, you agree to help improve the product by allowing Google to collect and use your Interactions data, subject to the Google Terms of Service and Google Privacy Policy. You can choose to opt out at any time via your settings.

### Legal & Privacy Links

- **Terms of Service**: [antigravity.google/terms](https://antigravity.google/terms)
- **Privacy Policy**: [policies.google.com/privacy](https://policies.google/privacy)

---

## Fork Extensions (`DannyBaanks/antigravity-cli`)

This fork adds companion tooling around the closed `agy` binary to run multiple Antigravity CLI sessions inside a single terminal window with shared RAM, multi-account Baton Pass, streaming-safe copy mode, real-time live mirroring, and cross-harness shared memory:

### 1. `agy-dash` (`tools/agy-dash/`) — Single-Window Command Center & RAM Optimizer
Powered by a **Zig 0.16 (`ReleaseFast`) core** (`agy-fast-core`) + Python `curses` parser/UI (`tmux -L agy-dash`):

- **Clickable `⚡ AGY DASH` Badge & OpenCode-Style `×` Tab Close**:
  - Top-left red **`⚡ AGY DASH`** badge (turns green `▾` inside the dashboard) toggles between the Command Center and your last tab (`MouseDown1StatusLeft` / `Alt+D` / `Alt+0`) without cluttering the tab strip with a `0:⚡DASH` tab.
  - Clicking **`×`** (or middle-click) on any session tab runs `close-tab`: active `RUNNING` / `BG-TASK` tabs are parked alive in a hidden background `agy-bg` session (zero process termination) and restored transparently when reopened from the dashboard.
- **LED Marquee Tab Bar**: Active tabs animate Braille spinners (`⠋..⠏`) on the left alongside a scrolling status ticker (`Editing files...`, `Running cmd...`, `Task running...`) read from `transcript.jsonl`.
- **Dual-Pane Command Center + `<4 ms` Zig Transcript Preview**:
  - Left session table (`RUNNING` / `BG-TASK` / `IDLE` / `HIBERNATED` / `SAVED`), clickable right Command Center (`[r] Actualizar`, `[↵] Abrir/Ir`, `[v] Espejo Vivo`, `[R] Recargar Tab`, `[n] Nueva`, `[h/H] Hibernar 0MB`, `[x] Deduplicar`, `[B] Baton Pass`, `[u] Cuentas`, `[m] Memoria`), and bottom real-time transcript tail.
- **Streaming-Safe Freeze Selection (`copy-pipe-no-clear`)**:
  - Mouse dragging or scrolling up (`WheelUpPane` / `Alt+S`) inside any `agy` chat tab enters tmux `copy-mode` to freeze the viewport while the model streams output, keeping the highlight visible and copying automatically to the system clipboard (`wl-copy` / `xclip` / `xsel`).
- **Multi-Account Google Pro Vault, Preflight & 1-Click Baton Pass (`[u]` / `[B]`)**:
  - Keep `primary` on the OS keyring while adding isolated virtual `HOME` slots (`pro2`, `pro3`, ...) with separate OAuth/config storage (`0700`) and automatic symlinks for `conversations/`, `brain/`, `conversation_summaries.db`, `settings.json`, `mcp/`, `.ssh`, `.gitconfig`, and `.config/gh`.
  - Background `/usage --output-format json` quota polling with `AGY_NO_BROWSER=1` (never opens stray browser tabs) and critical/low banners per model family (`Argon`, `Gemini`, `Claude/GPT`); interactive login restores real D-Bus via an `xdg-open` wrapper and copies the unbroken OAuth URL to the clipboard.
  - **`baton_preflight` (`agy-dash baton <session> --check [--json]`)**: Verifies authorized distinct identity, disjoint credential storage, quota headroom, single active process (`no_duplicate_operation`), and preserved conversation state before handing off `--conversation <uuid> --model <slug>`.
  - **Confirmed Local-Only Profile Removal (`[x]` / `Del`)**: Displays profile identity, duplicate-email detection, associated sessions, and live `/proc` PID checks; refuses `primary` or in-use profiles; removes only regular files inside `accounts/<slot>/` without traversing shared symlinks.
- **Shared RAM (`KSM` + `GOMEMLIMIT`), `<3 MB` Live Mirror & `0 MB` Auto-Hibernate**:
  - Enables Linux 6.4+/7.0 KSM (`prctl(PR_SET_MEMORY_MERGE, 1)`) inherited across `execvp` + `GOGC=35 GOMEMLIMIT=180MiB`.
  - Streams busy external sessions (`transcript.jsonl` + active `task-*.log`) every `0.4 s` in `<3 MB RAM` without opening duplicate `agy` processes, and hibernates 100% idle tabs to `0 MB RAM` with `<1 s` wake stubs.

### 2. `agy-limited` (`tools/agy-dash/agy-limited`)
Runs `agy` inside a `systemd --user` cgroup scope with configurable CPU/memory caps + KSM + `GOMEMLIMIT`.

### 3. `isymem` (`tools/isymem/agent_memory.py`)
Shared Multi-Harness Memory System compatible with Claude Code's `MEMORY.md` across AGY, Codex, Qwen, OpenCode, and Claude Code (`recall`, `show`, `save`, `add`, `close`, `archive`, `doctor`, `setup`).

### Quick Install & Commands

```bash
./install.sh
agy-dash                                         # Launch / attach single-window Command Center
agy-dash quota --refresh                         # Check /usage quotas across all accounts
agy-dash account add pro2                        # Isolated OAuth login without signing out primary
agy-dash baton <conv-or-title> --check --json    # Verify preflight safety (isolation, quota, 0 dup)
agy-dash baton <conv-or-title> [--to pro2]       # Hand off conversation + active model seamlessly
agy-dash hibernate all-idle                      # Freeze 100% idle sessions to 0 MB RAM
isymem doctor                                    # Verify cross-harness shared memory links
```
