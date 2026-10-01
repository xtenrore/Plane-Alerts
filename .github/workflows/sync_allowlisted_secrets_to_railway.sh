#!/usr/bin/env bash
set -euo pipefail

if (($# != 0)); then
  echo "error: this script accepts no arguments; the sync set is hard-coded" >&2
  exit 2
fi

: "${RAILWAY_TOKEN:?RAILWAY_TOKEN must contain the existing Plane Alerts Railway project token}"
: "${RAILWAY_SERVICE_ID:?RAILWAY_SERVICE_ID is required}"
: "${RAILWAY_ENVIRONMENT_ID:?RAILWAY_ENVIRONMENT_ID is required}"

readonly ALLOWLIST=(
  ADMIN_PASSWORD
  AI_OPS_GITHUB_DR_TOKEN
  BACK4APP_API
  CLOUDFLARE_ACCOUNT_ID
  CLOUDFLARE_ACCOUNT_ID_2
  CLOUDFLARE_API_TOKEN
  CLOUDFLARE_API_TOKEN_2
  COHERE_TRIAL_KEY
  GEMINI_API_KEY
  GEMINI_API_KEY_2
  GEMINI_API_KEY_3
  GEMINI_API_KEY_4
  GEMINI_API_KEY_5
  GOOGLE_CONTRAILS_API_KEY
  GROQ_KEY
  GROQ_KEY_2
  GROQ_KEY_3
  GROQ_KEY_4
  GROQ_KEY_5
  MISTRAL_API
  MISTRAL_API_2
  MISTRAL_API_3
  MISTRAL_API_4
  MISTRAL_API_5
  MONGO_URI
  NF_API_TOKEN
  OPENROUTER_API
  OPENSKY_1
  OPENSKY_2
  OPENSKY_3
  OPENSKY_4
  OPENSKY_5
)

values=()
missing=()
for name in "${ALLOWLIST[@]}"; do
  if [[ ! -v "$name" || -z "${!name}" ]]; then
    missing+=("$name")
    continue
  fi
  values+=("$name=${!name}")
done

if ((${#missing[@]} != 0)); then
  printf 'error: refusing partial sync; missing allowlisted secret(s):' >&2
  printf ' %s' "${missing[@]}" >&2
  printf '\n' >&2
  exit 3
fi

railway variable set \
  --service "$RAILWAY_SERVICE_ID" \
  --environment "$RAILWAY_ENVIRONMENT_ID" \
  --skip-deploys \
  "${values[@]}" \
  >/dev/null

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
railway variable list \
  --service "$RAILWAY_SERVICE_ID" \
  --environment "$RAILWAY_ENVIRONMENT_ID" \
  --json >"$tmp"

python - "$tmp" <<'PY'
import json
import sys
from pathlib import Path

expected = {
    "ADMIN_PASSWORD",
    "AI_OPS_GITHUB_DR_TOKEN",
    "BACK4APP_API",
    "CLOUDFLARE_ACCOUNT_ID",
    "CLOUDFLARE_ACCOUNT_ID_2",
    "CLOUDFLARE_API_TOKEN",
    "CLOUDFLARE_API_TOKEN_2",
    "COHERE_TRIAL_KEY",
    "GEMINI_API_KEY",
    "GEMINI_API_KEY_2",
    "GEMINI_API_KEY_3",
    "GEMINI_API_KEY_4",
    "GEMINI_API_KEY_5",
    "GOOGLE_CONTRAILS_API_KEY",
    "GROQ_KEY",
    "GROQ_KEY_2",
    "GROQ_KEY_3",
    "GROQ_KEY_4",
    "GROQ_KEY_5",
    "MISTRAL_API",
    "MISTRAL_API_2",
    "MISTRAL_API_3",
    "MISTRAL_API_4",
    "MISTRAL_API_5",
    "MONGO_URI",
    "NF_API_TOKEN",
    "OPENROUTER_API",
    "OPENSKY_1",
    "OPENSKY_2",
    "OPENSKY_3",
    "OPENSKY_4",
    "OPENSKY_5",
}

data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if isinstance(data, dict):
    present = set(data)
elif isinstance(data, list):
    present = {
        item.get("name") or item.get("key")
        for item in data
        if isinstance(item, dict) and (item.get("name") or item.get("key"))
    }
else:
    raise SystemExit("error: unrecognized Railway variable-list JSON shape")

missing = sorted(expected - present)
if missing:
    raise SystemExit("error: Railway name verification failed for: " + ", ".join(missing))
print(f"Verified {len(expected)} allowlisted Railway variable names; values were not printed.")
PY
