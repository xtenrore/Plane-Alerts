#!/usr/bin/env bash
# Forwarding wrapper: execute root setup.sh
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "$SCRIPT_DIR/../setup.sh" "$@"
