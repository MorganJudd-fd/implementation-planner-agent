#!/usr/bin/env bash
# PreToolUse guard for Bash. Logic lives in scripts/plannerlib/hooks.py.
# Denies via hookSpecificOutput JSON on stdout with exit 0 so the reason reaches the model.
set -euo pipefail
DIR="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
exec python3 -I -c 'import sys; sys.path.insert(0, sys.argv[1]); from plannerlib.hooks import main; sys.exit(main(sys.argv[2]))' "$DIR/scripts" bash
