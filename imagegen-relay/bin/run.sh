#!/usr/bin/env sh
# imagegen-relay launcher (macOS / Linux)
#
# Reads config.json from the skill root, injects IMAGE_RELAY_URL / IMAGE_RELAY_KEY,
# then runs the relay CLI with the system Python.
#
# Usage:
#   sh run.sh generate --prompt "..." --size 1672x941 --out "out.png"

set -eu

BIN_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT_DIR=$(CDPATH= cd -- "$BIN_DIR/.." && pwd)
CFG="$ROOT_DIR/config.json"
CLI="$ROOT_DIR/scripts/image_gen.py"

if [ ! -f "$CLI" ]; then
  echo "[imagegen-relay] CLI not found: $CLI" >&2
  exit 4
fi

if [ -f "$CFG" ]; then
  URL_FROM_CFG=$(python3 - "$CFG" <<'PY' 2>/dev/null || true
import json, sys
try:
    with open(sys.argv[1], 'r', encoding='utf-8-sig') as fh:
        cfg = json.load(fh)
    print((cfg.get('api_url') or '').strip())
except Exception:
    print('')
PY
)
  KEY_FROM_CFG=$(python3 - "$CFG" <<'PY' 2>/dev/null || true
import json, sys
try:
    with open(sys.argv[1], 'r', encoding='utf-8-sig') as fh:
        cfg = json.load(fh)
    print((cfg.get('api_key') or '').strip())
except Exception:
    print('')
PY
)
  [ -n "$URL_FROM_CFG" ] && export IMAGE_RELAY_URL="$URL_FROM_CFG"
  [ -n "$KEY_FROM_CFG" ] && export IMAGE_RELAY_KEY="$KEY_FROM_CFG"
fi

if [ -z "${IMAGE_RELAY_URL:-}" ]; then
  [ -n "${CODEX_API_URL:-}" ] && export IMAGE_RELAY_URL="$CODEX_API_URL" || true
fi
if [ -z "${IMAGE_RELAY_KEY:-}" ]; then
  [ -n "${CODEX_API_KEY:-}" ] && export IMAGE_RELAY_KEY="$CODEX_API_KEY" || true
fi
if [ -z "${IMAGE_RELAY_KEY:-}" ]; then
  [ -n "${OPENAI_API_KEY:-}" ] && export IMAGE_RELAY_KEY="$OPENAI_API_KEY" || true
fi

if command -v python3 >/dev/null 2>&1; then
  exec python3 "$CLI" "$@"
elif command -v python >/dev/null 2>&1; then
  exec python "$CLI" "$@"
else
  echo "[imagegen-relay] Python 3 was not found on PATH." >&2
  exit 5
fi
