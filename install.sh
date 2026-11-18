#!/usr/bin/env bash
# install.sh — Build agy-fast-core (Zig 0.16 ReleaseFast) and install agy-dash,
# agy-limited, and isymem symlinks into ~/.local/bin.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${HOME}/.local/bin"
mkdir -p "$BIN_DIR"

if command -v zig >/dev/null 2>&1; then
  echo "[install] Building agy-fast-core (ReleaseFast)..."
  (cd "$REPO_DIR/tools/agy-dash/zig_core" && zig build -Drelease)
  cp "$REPO_DIR/tools/agy-dash/zig_core/zig-out/bin/agy-fast-core" "$REPO_DIR/tools/agy-dash/agy-fast-core"
  chmod +x "$REPO_DIR/tools/agy-dash/agy-fast-core"
else
  echo "[install] zig not found in PATH — agy-dash will run with Python fallback." >&2
fi

chmod +x "$REPO_DIR/tools/agy-dash/agy_dashboard.py" "$REPO_DIR/tools/agy-dash/agy-limited" "$REPO_DIR/tools/isymem/agent_memory.py"
ln -sfn "$REPO_DIR/tools/agy-dash/agy_dashboard.py" "$BIN_DIR/agy-dash"
ln -sfn "$REPO_DIR/tools/agy-dash/agy-limited" "$BIN_DIR/agy-limited"
ln -sfn "$REPO_DIR/tools/isymem/agent_memory.py" "$BIN_DIR/isymem"

echo "[install] Installed:"
echo "  $BIN_DIR/agy-dash"
echo "  $BIN_DIR/agy-limited"
echo "  $BIN_DIR/isymem"
