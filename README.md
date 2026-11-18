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

This fork adds companion tooling around the closed `agy` binary to run multiple Antigravity CLI sessions inside a single terminal window with shared RAM, real-time live mirroring, and cross-harness shared memory:

- **`agy-dash` (`tools/agy-dash/`)**: Single-window `tmux` Command Center (`tmux -L agy-dash`) powered by a **Zig 0.16 (`ReleaseFast`) core** (`agy-fast-core`) + Python `curses` parser/UI:
  - **Dual-pane Command Center**: Left session list + right clickable mouse/hotkey Command Center (`[r] Actualizar`, `[↵] Abrir/Ir`, `[v] Espejo Vivo`, `[R] Recargar Tab`, `[n] Nueva`, `[h/H] Hibernar 0MB`, `[x] Deduplicar`, `[B] Baton Pass`, `[u] Cuentas`, `[m] Memoria`) + live bottom transcript preview (`<4 ms`).
  - **Multi-Account Pro Vault, Low-Quota Alerts & 1-Click Baton Pass**: Track multiple Google Pro accounts (`primary` keyring + isolated virtual `HOME` slots sharing local conversations/MCPs without logging out), poll live `/usage` quotas in the background, show critical/low banners on the active model family, and hand off `--conversation <uuid> --model <slug>` to another account in one click (`[B]` / `agy-dash baton`).
  - **LED Marquee Tab Bar & OpenCode-Style `×` Close**: Active tmux tabs show animated Braille spinner frames (`⠋..⠏`) + scrolling action status (`Editing files...`, `Running cmd...`) and a clickable `×` button that parks still-running (`RUNNING`/`BG-TASK`) sessions into a hidden `agy-bg` session without terminating the agent.
  - **Streaming-Safe Freeze Selection & OAuth Browser Shim**: Mouse drag / wheel-up inside `agy` tabs freezes the viewport in tmux `copy-mode` (`copy-pipe-no-clear` so streaming output never clears your highlight), and isolated OAuth logins restore real D-Bus for `xdg-open` + auto-copy unbroken URLs to the clipboard.
  - **Shared RAM & GC Capping**: Enables Linux 6.4+/7.0 KSM (`prctl(PR_SET_MEMORY_MERGE, 1)`) inherited across `execvp` + `GOGC=35 GOMEMLIMIT=180MiB`.
  - **Real-Time `<3 MB` Live Mirror**: Streams external busy sessions (`transcript.jsonl` + active `task-*.log`) every `0.4 s` without spawning duplicate `agy` processes (`⚠ Conversation already open`).
  - **Safe `0 MB` Auto-Hibernate**: Parks 100% idle sessions (`not_fully_idle=0` and no active children) to `0 MB RAM` with instant `<1 s` wake stubs.
- **`agy-limited` (`tools/agy-dash/agy-limited`)**: Runs `agy` inside a `systemd --user` cgroup scope with CPU/memory caps + KSM + `GOMEMLIMIT`.
- **`isymem` (`tools/isymem/agent_memory.py`)**: Shared Multi-Harness Memory System compatible with Claude Code's `MEMORY.md` across AGY, Codex, Qwen, OpenCode, and Claude Code (`recall`, `show`, `save`, `archive`, `doctor`, `setup`).

### Quick Install

```bash
./install.sh
agy-dash
```
