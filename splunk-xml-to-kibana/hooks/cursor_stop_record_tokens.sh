#!/usr/bin/env bash
# Cursor `stop` / `afterAgentResponse` hook: append token counts to PACKAGE_ROOT/metrics/.
#
# Requires SX2K_PACKAGE_ROOT pointing at the active conversion package, e.g.:
#   export SX2K_PACKAGE_ROOT=/path/to/elastic/my_dash
#
# Wire via hooks/hooks.json.example → project or ~/.cursor/hooks.json
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RECORDER="${SCRIPT_DIR}/../scripts/record_token_usage.py"

if [[ -z "${SX2K_PACKAGE_ROOT:-}" ]]; then
  # No package bound — fail open (do not block the agent)
  echo '{}'
  exit 0
fi

if [[ ! -f "$RECORDER" ]]; then
  echo '{}'
  exit 0
fi

python3 "$RECORDER" --from-hook --package "$SX2K_PACKAGE_ROOT" || echo '{}'
exit 0
