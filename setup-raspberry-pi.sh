#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="${PLANE_ALERTS_INSTALL_DIR:-/opt/plane-alerts}"
REPO_URL="https://github.com/xtenrore/Plane-Alerts.git"
API_URL="https://api.github.com/repos/xtenrore/Plane-Alerts/releases/latest"

if [[ "${1:-}" == "--self-test" ]]; then
  echo "INSTALLER_SELF_TEST=ok platform=raspberry-pi-arm64"
  exit 0
fi

ARCH="$(uname -m)"
MODEL=""
if [[ -r /proc/device-tree/model ]]; then MODEL="$(tr -d '\0' </proc/device-tree/model)"; fi
if [[ "${PLANE_ALERTS_INSTALLER_TEST:-}" == "1" ]]; then
  ARCH="${PLANE_ALERTS_TEST_ARCH:-$ARCH}"
  MODEL="${PLANE_ALERTS_TEST_MODEL:-$MODEL}"
fi
if [[ "$ARCH" != "aarch64" && "$ARCH" != "arm64" ]]; then
  echo "ERROR: Raspberry Pi Plane Alerts requires a 64-bit ARM OS. Detected: $ARCH" >&2
  exit 2
fi
if [[ "$MODEL" != *"Raspberry Pi"* ]]; then
  echo "ERROR: This entry point is reserved for Raspberry Pi OS / Raspberry Pi hardware." >&2
  echo "Use setup-linux-arm64.sh for other ARM64 computers." >&2
  exit 3
fi
if [[ "$MODEL" != *"Raspberry Pi 4"* && "$MODEL" != *"Raspberry Pi 5"* ]]; then
  echo "WARNING: $MODEL is not one of the primary Pi 4/Pi 5 validation targets." >&2
fi
if [[ "${1:-}" == "--check-platform" ]]; then
  echo "PLATFORM_CHECK=ok platform=raspberry-pi-arm64 arch=$ARCH model=$MODEL"
  exit 0
fi

if [[ "$(id -u)" -ne 0 ]]; then
  if command -v sudo >/dev/null 2>&1; then exec sudo -E bash "$0" "$@"; fi
  echo "ERROR: Root permission is required to install packages and the system service." >&2
  exit 5
fi
if [[ ! -r /etc/os-release ]]; then
  echo "ERROR: Could not identify Raspberry Pi OS/Debian version." >&2
  exit 6
fi
# shellcheck disable=SC1091
. /etc/os-release
if [[ "${ID:-}" != "raspbian" && "${ID:-}" != "debian" && "${ID:-}" != "ubuntu" && " ${ID_LIKE:-} " != *" debian "* ]]; then
  echo "ERROR: Unsupported Raspberry Pi distribution: ${PRETTY_NAME:-${ID:-unknown}}" >&2
  exit 6
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ca-certificates curl git python3 python3-venv python3-pip build-essential >/dev/null
python3 - <<'PY'
import sys
if sys.version_info < (3, 11):
    raise SystemExit("ERROR: Plane Alerts requires Python 3.11 or newer")
PY

RAM_MB="$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo 2>/dev/null || echo 0)"
DISK_MB="$(df -Pm "$(dirname "$INSTALL_DIR")" 2>/dev/null | awk 'NR==2 {print $4}' || echo 0)"
echo "Raspberry Pi: $MODEL"
echo "RAM: ${RAM_MB} MB; available install filesystem: ${DISK_MB} MB"
if (( RAM_MB > 0 && RAM_MB < 1500 )); then echo "WARNING: Low RAM may reduce self-host reliability." >&2; fi
if (( DISK_MB > 0 && DISK_MB < 1500 )); then echo "ERROR: At least about 1.5 GB free disk is required." >&2; exit 7; fi

for svc in readsb dump1090-fa dump1090 ultrafeeder; do
  if systemctl list-unit-files 2>/dev/null | grep -q "^${svc}\.service"; then
    echo "Detected local ADS-B service: $svc"
  fi
done
if [[ -d /run/readsb || -d /run/dump1090-fa || -d /run/dump1090 ]]; then
  echo "Local ADS-B runtime data was detected. The setup wizard can configure its HTTP aircraft feed."
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
if [[ ! -x "$INSTALL_DIR/.venv/bin/python" ]]; then python3 -m venv "$INSTALL_DIR/.venv"; fi
exec "$INSTALL_DIR/.venv/bin/python" "$INSTALL_DIR/scripts/community_installer_v55.py" --install-dir "$INSTALL_DIR"
