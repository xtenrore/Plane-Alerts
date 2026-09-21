#!/usr/bin/env bash
set -euo pipefail

STATE_DIR="${AGY_STATE_DIR:-/agy-state}"
export HOME="$STATE_DIR/home"
export XDG_DATA_HOME="$HOME/.local/share"
export XDG_CONFIG_HOME="$HOME/.config"
export XDG_CACHE_HOME="$HOME/.cache"

mkdir -p \
  "$HOME/.gemini/antigravity-cli" \
  "$XDG_DATA_HOME/keyrings" \
  "$XDG_CONFIG_HOME" \
  "$XDG_CACHE_HOME" \
  "$STATE_DIR/prediction-lab/context"

if [[ -z "${AGY_KEYRING_PASSWORD:-}" ]]; then
  echo "FATAL: AGY_KEYRING_PASSWORD is required so the persistent Linux Secret Service can be unlocked after a Railway restart." >&2
  exit 78
fi

# Antigravity account sessions are stored through Linux Secret Service. Start
# one D-Bus session for the lifetime of this container, then unlock/create the
# login keyring using a Railway secret. The encrypted keyring files themselves
# live under the persistent volume via XDG_DATA_HOME.
eval "$(dbus-launch --sh-syntax)"
KEYRING_ENV="$(printf '%s' "$AGY_KEYRING_PASSWORD" | gnome-keyring-daemon --unlock --components=secrets 2>/tmp/agy-keyring-error.log || true)"
if [[ -n "$KEYRING_ENV" ]]; then
  eval "$KEYRING_ENV"
fi

if ! secret-tool store --label='Plane Alerts keyring probe' plane-alerts probe <<<"ok" >/dev/null 2>&1; then
  echo "FATAL: Linux Secret Service is not usable; refusing to start because AGY authentication would not reliably survive restarts." >&2
  cat /tmp/agy-keyring-error.log >&2 || true
  exit 78
fi
if [[ "$(secret-tool lookup plane-alerts probe 2>/dev/null || true)" != "ok" ]]; then
  echo "FATAL: persistent keyring probe failed." >&2
  exit 78
fi

# Never let an inherited API key silently switch this worker onto billable API
# usage. Account-based Google AI Pro authentication is the only allowed path.
unset GEMINI_API_KEY GOOGLE_API_KEY GOOGLE_GEMINI_API_KEY GOOGLE_GEMINI_BASE_URL || true

export PATH="/usr/local/bin:$PATH"
export PYTHONPATH="/app${PYTHONPATH:+:$PYTHONPATH}"
export AGY_CLI_DISABLE_AUTO_UPDATE=true

# Seed first-launch choices and the minimum headless permissions on the
# persistent volume. Do not use the dangerous global bypass: AGY may read the
# Plane Alerts source and its redacted Prediction Lab context, and may only run
# the explicitly allowlisted git/test/python/read-only inspection commands.
python - <<'PY'
import json, os, time
from pathlib import Path

home = Path(os.environ['HOME'])
settings_path = home / '.gemini' / 'antigravity-cli' / 'settings.json'
settings_path.parent.mkdir(parents=True, exist_ok=True)
try:
    data = json.loads(settings_path.read_text())
except Exception:
    data = {}
data.pop('modelProvider', None)
data['useG1Credits'] = False
data['colorScheme'] = data.get('colorScheme') or 'terminal'
data['altScreenMode'] = 'never'
data['agentMode'] = 'accept-edits'
data['enableTelemetry'] = False
trusted = list(data.get('trustedWorkspaces') or [])
if '/app' not in trusted:
    trusted.append('/app')
data['trustedWorkspaces'] = trusted
permissions = data.setdefault('permissions', {})
# jq is not installed in the AGY image. Remove the stale permission so the
# model is not encouraged to choose a command that can never succeed.
allow = [rule for rule in list(permissions.get('allow') or []) if rule != 'command(jq)']
required = [
    'read_file(/app)',
    'read_file(/agy-state/prediction-lab/context)',
    'write_file(/agy-state/prediction-lab)',
    'command(git)',
    'command(pytest)',
    'command(python)',
    'command(python3)',
    'command(grep)',
    'command(ls)',
    'command(regex:python /app/scripts/agy_record_finding.py.*)',
    'command(regex:python3 /app/scripts/agy_record_finding.py.*)',
]
for rule in required:
    if rule not in allow:
        allow.append(rule)
permissions['allow'] = allow
tmp = settings_path.with_suffix('.tmp')
tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
tmp.replace(settings_path)

state_dir = Path(os.environ.get('AGY_STATE_DIR', '/agy-state'))
supervisor_path = state_dir / 'prediction-lab' / 'supervisor.json'
supervisor_path.parent.mkdir(parents=True, exist_ok=True)
try:
    supervisor = json.loads(supervisor_path.read_text())
except Exception:
    supervisor = {}
enable = os.environ.get('AGY_GOAL_ENABLED', '').strip().lower() in {'1', 'true', 'yes', 'on'}
if enable:
    was_enabled = bool(supervisor.get('enabled', False))
    supervisor['enabled'] = True
    configured_goal = os.environ.get('AGY_GOAL', '').strip()
    goal = configured_goal or str(supervisor.get('goal') or '').strip()
    tooling_rules = (
        '[HEADLESS_TOOLING_RULES] In headless audits, prefer view_file, list_dir, grep_search, and write_to_file. '
        'For run_command, use exactly one supported command beginning with python3, python, grep, ls, git, pytest, '
        'or /app/scripts/agy_record_finding.py. NEVER use run_command to create or modify a file. For any multi-line '
        'analysis script, call write_to_file first, then make a separate run_command call containing only '
        '`python3 /path/to/script.py`. Do not use jq, sed, cat/heredocs, find, head, tail, shell redirection, pipes, '
        '&&, ||, semicolon command chains, sh, bash, or other compound shell syntax. The AGY image is intentionally '
        'minimal: use the Python standard library only unless a package import has already been proven to work. '
        'Do not assume numpy, pandas, scipy, or other optional packages exist, and do not pip-install packages at runtime. '
        'If an optional import fails, immediately rewrite the analysis with the standard library. A permission denial '
        'is NOT task completion. The Plane Alerts supervisor will immediately resume the same conversation with '
        'corrective tooling guidance after a soft denial. Never retry a denied operation through an equivalent shell '
        'workaround; continue using only the supported built-in tools and single allowlisted commands.'
    )
    if goal:
        # Replace any persisted older tooling block on every restart so the
        # supervisor cannot keep stale command guidance from a previous image.
        marker = '\n\n[HEADLESS_TOOLING_RULES]'
        if marker in goal:
            goal = goal.split(marker, 1)[0].rstrip()
        goal = f'{goal}\n\n{tooling_rules}'
        supervisor['goal'] = goal

    # A persisted quota hold dominates every startup override. A deployment,
    # enable toggle, tooling-policy migration, or force token may never shorten
    # a future guarded quota deadline. Unknown/unverified resets remain held
    # indefinitely until explicitly cleared after verified recovery.
    quota_status = str(supervisor.get('last_status') or '')
    quota_deadline = float(supervisor.get('next_run_at', 0) or 0)
    quota_hold_active = quota_status == 'quota_wait_unverified' or (
        quota_status == 'quota_wait' and quota_deadline > time.time()
    )

    # A tooling-policy change may run immediately only when no quota hold is
    # active. The policy version is still persisted while waiting.
    tooling_policy_version = 3
    if int(supervisor.get('tooling_policy_version', 0) or 0) != tooling_policy_version:
        supervisor['tooling_policy_version'] = tooling_policy_version
        if not quota_hold_active:
            supervisor['next_run_at'] = 0

    if not was_enabled and not quota_hold_active:
        supervisor['next_run_at'] = 0
    # Changing this token deliberately requests one immediate run, but it may
    # never bypass an active quota hold. Consume/persist the token while waiting
    # so an old request cannot fire later simply because the deadline expires.
    force_token = os.environ.get('AGY_FORCE_RUN_TOKEN', '').strip()
    if force_token and supervisor.get('last_force_run_token') != force_token:
        supervisor['last_force_run_token'] = force_token
        if not quota_hold_active:
            supervisor['next_run_at'] = 0
    stmp = supervisor_path.with_suffix('.tmp')
    stmp.write_text(json.dumps(supervisor, indent=2, sort_keys=True))
    stmp.replace(supervisor_path)
PY

# Parent-side bridge keeps Mongo credentials. AGY itself never receives them.
# It refreshes redacted truth every 30s and publishes findings every 3s.
python /app/scripts/agy_bridge_daemon.py &
BRIDGE_PID=$!
echo "Prediction Lab bridge started pid=$BRIDGE_PID"

exec uvicorn app.agy_worker_ext:app --host 0.0.0.0 --port "${PORT:-8090}" --workers 1
