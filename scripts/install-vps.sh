#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${1:-/opt/kael-thread-rebuild-mcp}"
CONFIG_PATH="${2:-/etc/kael-thread-rebuild/config.toml}"

if [[ ! -f "$REPO_DIR/pyproject.toml" ]]; then
  echo "repository not found: $REPO_DIR" >&2
  exit 1
fi

python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else "Python 3.11+ is required")'

python3 -m venv "$REPO_DIR/.venv"
"$REPO_DIR/.venv/bin/pip" install --upgrade pip
"$REPO_DIR/.venv/bin/pip" install "$REPO_DIR"

install -d -m 0700 "$(dirname "$CONFIG_PATH")"
if [[ ! -f "$CONFIG_PATH" ]]; then
  install -m 0600 "$REPO_DIR/examples/config.toml" "$CONFIG_PATH"
  echo "created $CONFIG_PATH — edit project_dir before continuing"
fi

echo
echo "Next:"
echo "  1. edit $CONFIG_PATH"
echo "  2. run: $REPO_DIR/.venv/bin/kael-thread-rebuild --config $CONFIG_PATH doctor"
echo "  3. run: $REPO_DIR/.venv/bin/kael-thread-rebuild --config $CONFIG_PATH dirty"
echo "  4. run: $REPO_DIR/.venv/bin/kael-thread-rebuild --config $CONFIG_PATH plan"
echo "     check selected_turns == source_turns and dropped_oldest_turns == 0"
echo "  5. follow README.md to add the MCP server and Stop hook"
