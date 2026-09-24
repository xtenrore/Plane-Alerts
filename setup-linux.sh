#!/usr/bin/env bash
set -euo pipefail

TARGET_ARCH="x86_64"
TARGET_LABEL="Linux x64 / AMD64"
INSTALL_DIR="${PLANE_ALERTS_INSTALL_DIR:-/opt/plane-alerts}"
REPO_URL="https://github.com/xtenrore/Plane-Alerts.git"
API_URL="https://api.github.com/repos/xtenrore/Plane-Alerts/releases/latest"

if [[ "${1:-}" == "--self-test" ]]; then
  echo "INSTALLER_SELF_TEST=ok platform=linux-x64"
  exit 0
fi

ARCH="$(uname -m)"
if [[ "$ARCH" != "$TARGET_ARCH" && "$ARCH" != "amd64" ]]; then
  echo "ERROR: This installer is for $TARGET_LABEL. Detected architecture: $ARCH" >&2
  echo "Use setup-linux-arm64.sh on ARM64 systems." >&2
  exit 2
fi
if [[ "${1:-}" == "--check-platform" ]]; then
  echo "PLATFORM_CHECK=ok platform=linux-x64 arch=$ARCH"
  exit 0
fi

if [[ "$(id -u)" -ne 0 ]]; then
  if command -v sudo >/dev/null 2>&1; then
    exec sudo -E bash "$0" "$@"
  fi
  echo "ERROR: Root permission is required to install packages and the system service." >&2
  exit 5
fi

if [[ ! -r /etc/os-release ]]; then
  echo "ERROR: This installer supports tested Debian/Ubuntu-family Linux systems." >&2
  exit 6
fi
# shellcheck disable=SC1091
. /etc/os-release
case "${ID:-}" in
  ubuntu|debian|raspbian) ;;
  *)
    if [[ " ${ID_LIKE:-} " != *" debian "* ]]; then
      echo "ERROR: Unsupported distribution: ${PRETTY_NAME:-${ID:-unknown}}" >&2
      echo "Use the documented manual installation path for other distributions." >&2
      exit 6
    fi
    ;;
esac

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ca-certificates curl git python3 python3-venv python3-pip build-essential >/dev/null
git --version
python3 --version

PY_OK="$(python3 - <<'PY'
import sys
print('yes' if sys.version_info >= (3, 11) else 'no')
PY
)"
if [[ "$PY_OK" != "yes" ]]; then
  echo "ERROR: Plane Alerts requires Python 3.11 or newer. This distribution supplied $(python3 --version 2>&1)." >&2
  exit 7
fi

if [[ -d "$INSTALL_DIR/.git" ]]; then
  echo "Existing Plane Alerts installation detected. Opening setup/repair menu..."
elif [[ -e "$INSTALL_DIR" ]]; then
  echo "ERROR: $INSTALL_DIR exists but is not a Plane Alerts Git installation." >&2
  exit 8
else
  RELEASE_TAG="$(curl -fsSL -H 'Accept: application/vnd.github+json' "$API_URL" | python3 -c 'import json,sys; r=json.load(sys.stdin); assert not r.get("draft") and not r.get("prerelease"); print(r["tag_name"])')"
  if [[ ! "$RELEASE_TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "ERROR: GitHub did not return a valid stable Plane Alerts release tag." >&2
    exit 9
  fi
  echo "Installing stable release $RELEASE_TAG..."
  git clone --branch "$RELEASE_TAG" --single-branch --depth 1 "$REPO_URL" "$INSTALL_DIR"
fi

ORIGIN="$(git -C "$INSTALL_DIR" remote get-url origin)"
if [[ "$ORIGIN" != *"github.com/xtenrore/Plane-Alerts"* ]]; then
  echo "ERROR: Repository identity check failed. Existing code will not be modified." >&2
  exit 10
fi

if [[ ! -x "$INSTALL_DIR/.venv/bin/python" ]]; then
  python3 -m venv "$INSTALL_DIR/.venv"
fi
exec "$INSTALL_DIR/.venv/bin/python" "$INSTALL_DIR/scripts/community_installer_v55.py" --install-dir "$INSTALL_DIR"
