#!/usr/bin/env sh
# imagegen-relay launcher (macOS / Linux)
#
# Reads config.json from the skill root, injects IMAGE_RELAY_URL / IMAGE_RELAY_KEY,
# then runs the relay CLI with Python 3 (or uv when Pillow is missing).
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

PY_BIN=""
if command -v python3 >/dev/null 2>&1; then
  PY_BIN="python3"
elif command -v python >/dev/null 2>&1; then
  PY_BIN="python"
fi

if [ -f "$CFG" ] && [ -n "$PY_BIN" ]; then
  URL_FROM_CFG=$("$PY_BIN" - "$CFG" <<'PY' 2>/dev/null || true
import json, sys
try:
    with open(sys.argv[1], 'r', encoding='utf-8-sig') as fh:
        cfg = json.load(fh)
    print((cfg.get('api_url') or '').strip())
except Exception:
    print('')
PY
)
  KEY_FROM_CFG=$("$PY_BIN" - "$CFG" <<'PY' 2>/dev/null || true
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
  if [ -n "${CODEX_API_URL:-}" ]; then
    export IMAGE_RELAY_URL="$CODEX_API_URL"
  elif [ -n "${OPENAI_BASE_URL:-}" ]; then
    export IMAGE_RELAY_URL="$OPENAI_BASE_URL"
  fi
fi
if [ -z "${IMAGE_RELAY_KEY:-}" ]; then
  if [ -n "${CODEX_API_KEY:-}" ]; then
    export IMAGE_RELAY_KEY="$CODEX_API_KEY"
  elif [ -n "${OPENAI_API_KEY:-}" ]; then
    export IMAGE_RELAY_KEY="$OPENAI_API_KEY"
  fi
fi

if [ -n "$PY_BIN" ]; then
  if "$PY_BIN" -c "import PIL" >/dev/null 2>&1 || ! command -v uv >/dev/null 2>&1; then
    exec "$PY_BIN" "$CLI" "$@"
  fi
fi

if command -v uv >/dev/null 2>&1; then
  exec uv run --quiet --no-project --with pillow python "$CLI" "$@"
fi

echo "[imagegen-relay] Python 3 was not found on PATH." >&2
exit 5
