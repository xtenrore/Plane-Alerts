#!/usr/bin/env bash
# ============================================================================
# Aircraft Alert Telegram Bot — Management Script
# ============================================================================
# Usage:
#   ./manage.sh status        Show status of bot, worker, mongod, and health
#   ./manage.sh start         Start bot and worker background services
#   ./manage.sh stop          Stop bot and worker background services
#   ./manage.sh restart       Restart both services
#   ./manage.sh logs          View live combined logs from journalctl
#   ./manage.sh logs-bot      View live bot logs
#   ./manage.sh logs-worker   View live worker logs
#   ./manage.sh test          Run test suite inside virtualenv
#   ./manage.sh run-bot       Run bot foreground in current terminal
#   ./manage.sh run-worker    Run worker foreground in current terminal
# ============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_PYTHON="$SCRIPT_DIR/venv/bin/python"
VENV_PYTEST="$SCRIPT_DIR/venv/bin/pytest"

# Color helpers
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

cmd="${1:-status}"

case "$cmd" in
    status)
        echo -e "${CYAN}======================================================${NC}"
        echo -e "${CYAN}  Aircraft Alert System — Status Check${NC}"
        echo -e "${CYAN}======================================================${NC}"
        echo ""
        
        # Check systemd services
        for svc in mongod aircraft-bot aircraft-worker; do
            if systemctl is-active --quiet "$svc" 2>/dev/null; then
                echo -e "  [Service] ${GREEN}● ACTIVE${NC}   $svc"
            else
                echo -e "  [Service] ${RED}○ INACTIVE${NC} $svc"
            fi
        done
        
        echo ""
        echo -e "${BLUE}Testing Local Web Server (http://localhost:8000/health):${NC}"
        if command -v curl >/dev/null 2>&1; then
            HEALTH_OUTPUT=$(curl -s --max-time 3 http://localhost:8000/health 2>/dev/null || true)
            if [ -n "$HEALTH_OUTPUT" ]; then
                echo -e "  ${GREEN}✓ Server responding:${NC} $HEALTH_OUTPUT"
            else
                echo -e "  ${YELLOW}⚠ Server not responding on port 8000 (may still be starting or stopped)${NC}"
            fi
        fi
        echo ""
        ;;

    start)
        echo -e "${BLUE}Starting Aircraft Alert background services...${NC}"
        sudo systemctl start aircraft-bot aircraft-worker
        echo -e "${GREEN}✓ Services started. Run './manage.sh status' to verify.${NC}"
        ;;

    stop)
        echo -e "${YELLOW}Stopping Aircraft Alert background services...${NC}"
        sudo systemctl stop aircraft-bot aircraft-worker
        echo -e "${GREEN}✓ Services stopped.${NC}"
        ;;

    restart)
        echo -e "${BLUE}Restarting Aircraft Alert background services...${NC}"
        sudo systemctl restart aircraft-bot aircraft-worker
        echo -e "${GREEN}✓ Services restarted. Run './manage.sh status' to verify.${NC}"
        ;;

    logs)
        echo -e "${CYAN}Streaming logs for aircraft-bot and aircraft-worker (Ctrl+C to exit)...${NC}"
        journalctl -u aircraft-bot -u aircraft-worker -f --output=cat
        ;;

    logs-bot)
        echo -e "${CYAN}Streaming logs for aircraft-bot (Ctrl+C to exit)...${NC}"
        journalctl -u aircraft-bot -f
        ;;

    logs-worker)
        echo -e "${CYAN}Streaming logs for aircraft-worker (Ctrl+C to exit)...${NC}"
        journalctl -u aircraft-worker -f
        ;;

    test)
        if [ ! -f "$VENV_PYTEST" ]; then
            echo -e "${RED}Error: pytest not found in $SCRIPT_DIR/venv${NC}"
            echo "Install tests dependencies with: ./venv/bin/pip install pytest pytest-asyncio mongomock-motor"
            exit 1
        fi
        echo -e "${BLUE}Running test suite...${NC}"
        "$VENV_PYTEST" -v
        ;;

    run-bot)
        echo -e "${BLUE}Running bot in foreground with current .env...${NC}"
        "$VENV_PYTHON" -m app.main
        ;;

    run-worker)
        echo -e "${BLUE}Running worker in foreground with current .env...${NC}"
        "$VENV_PYTHON" worker.py
        ;;

    *)
        echo "Usage: ./manage.sh {status|start|stop|restart|logs|logs-bot|logs-worker|test|run-bot|run-worker}"
        exit 1
        ;;
esac
