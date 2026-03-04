#!/bin/bash
#
# nightwire installer
# Signal + Claude AI Bot
#
# Usage: ./install.sh [--skip-signal] [--skip-systemd] [--uninstall] [--restart]
#

set -e

# Flush any buffered stdin (prevents stray Enter from skipping prompts)
flush_stdin() {
    while read -t 0.1 -n 10000 discard 2>/dev/null; do :; done
}

# Portable sed -i (BSD/macOS sed requires backup extension arg)
sed_inplace() {
    if sed --version 2>/dev/null | grep -q GNU; then
        sed -i "$@"
    else
        sed -i '' "$@"
    fi
}

# Wait for Signal bridge QR code endpoint to be ready.
# signal-cli inside the container needs time to fully initialize —
# /v1/about returns OK first, but qrcodelink may not work yet.
# The endpoint returns a PNG image on success, JSON error on failure.
# Returns 0 on success, 1 on failure. Sets QR_READY=true on success.
wait_for_qrcode() {
    local max_wait=${1:-90}
    local elapsed=0
    local qr_url="http://127.0.0.1:8080/v1/qrcodelink?device_name=${DEVICE_NAME:-nightwire}"
    QR_READY=false

    echo -ne "  Waiting for Signal bridge to initialize"

    while [ $elapsed -lt $max_wait ]; do
        # First check if container is even running
        if ! docker ps | grep -q signal-api; then
            echo ""
            return 1
        fi

        # Single GET request — check content-type from the response itself
        # Success: content-type contains "image" (PNG QR code)
        # Failure: content-type contains "json" (error like "no data to encode")
        local ctype
        ctype=$(curl -s -o /dev/null -w "%{content_type}" "$qr_url" 2>/dev/null || true)

        if echo "$ctype" | grep -qi "image"; then
            QR_READY=true
            echo ""
            return 0
        fi

        sleep 3
        elapsed=$((elapsed + 3))
        printf "."
    done

    echo ""
    return 1
}

# =============================================================================
# Device linking — uses signal-cli inside the Docker container via docker exec.
# The upstream image (1772646660-ci) includes all necessary fixes.
# =============================================================================

# Starts the Signal bridge container for device linking.
# Sets: SIGNAL_CLI_CMD (docker exec command prefix)
# Returns 0 on success, 1 on failure.
prepare_link_tool() {
    local install_dir="$1"

    echo -e "  ${BLUE}Preparing device link tool...${NC}"

    # Ensure compose file exists
    if [ ! -f "$install_dir/docker-compose.yml" ]; then
        echo -e "  ${RED}docker-compose.yml not found${NC}"
        return 1
    fi

    # Start the signal-api container (needed for docker exec)
    cd "$install_dir"
    docker compose up -d signal-api 2>/dev/null || docker start signal-api 2>/dev/null || true
    cd - > /dev/null

    # Wait for container to be running
    local waited=0
    while [ $waited -lt 15 ]; do
        if docker ps --format '{{.Names}}' | grep -q '^signal-api$'; then
            break
        fi
        sleep 1
        waited=$((waited + 1))
    done

    if ! docker ps --format '{{.Names}}' | grep -q '^signal-api$'; then
        echo -e "  ${RED}signal-api container failed to start${NC}"
        return 1
    fi

    SIGNAL_CLI_CMD="docker exec signal-api signal-cli"

    echo -e "  ${GREEN}✓${NC} Link tool ready (docker exec)"
    return 0
}

# Runs device linking with QR code display and retry logic.
# Args: $1=remote_mode(true/false) $2=device_name(default: nightwire)
# Sets: LINKED_NUMBER on success
# Returns 0 on success, 1 on failure.
run_device_link() {
    local remote_mode="$1"
    local device_name="${2:-nightwire}"
    local max_attempts=3
    local attempt=0

    # Install qrcode Python package for QR display
    pip install -q qrcode 2>/dev/null || true

    while [ $attempt -lt $max_attempts ]; do
        attempt=$((attempt + 1))

        if [ $attempt -gt 1 ]; then
            echo ""
            echo -e "  ${YELLOW}Retrying (attempt $attempt of $max_attempts)...${NC}"
            echo -e "  Signal's provisioning window is 60 seconds — scan promptly."
        fi

        # Run signal-cli link in background via docker exec, capture output
        local link_log
        link_log=$(mktemp)
        $SIGNAL_CLI_CMD --config /home/.local/share/signal-cli link --name "$device_name" \
            > "$link_log" 2>&1 &
        LINK_PID=$!

        # Wait for URI to appear in output (up to 20s)
        local uri=""
        local QR_SERVER_PID=""
        for i in $(seq 1 20); do
            uri=$(grep -o 'sgnl://[^ ]*' "$link_log" 2>/dev/null | head -1)
            if [ -n "$uri" ]; then
                break
            fi
            if ! kill -0 $LINK_PID 2>/dev/null; then
                break
            fi
            sleep 1
        done

        if [ -z "$uri" ]; then
            echo -e "  ${YELLOW}Failed to generate link URI.${NC}"
            kill $LINK_PID 2>/dev/null
            wait $LINK_PID 2>/dev/null || true
            grep -i "error\|exception\|fail" "$link_log" 2>/dev/null | tail -3 | sed 's/^/    /'
            rm -f "$link_log"
            continue
        fi

        echo ""
        echo -e "  ${GREEN}Link your phone to ${device_name}:${NC}"
        echo ""

        # Generate QR code in terminal
        python3 -c "
import sys
try:
    import qrcode
    q = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L)
    q.add_data(sys.argv[1])
    q.make(fit=True)
    q.print_ascii(invert=True)
except ImportError:
    print('  URI: ' + sys.argv[1])
    print('  (Install qrcode for QR display: pip install qrcode)')
" "$uri" 2>/dev/null | sed 's/^/    /'

        # Serve QR as PNG via HTTP for remote scanning
        if [ "$remote_mode" = "true" ]; then
            local server_ip
            server_ip=$(hostname -I 2>/dev/null | awk '{print $1}')
            [ -z "$server_ip" ] && server_ip=$(ipconfig getifaddr en0 2>/dev/null)
            [ -z "$server_ip" ] && server_ip=$(ip route get 1 2>/dev/null | awk '{print $7; exit}')
            [ -z "$server_ip" ] && server_ip="<your-server-ip>"

            python3 - "$uri" << 'PYEOF' &
import http.server, socketserver, sys, io, signal as sig
sig.alarm(120)
uri = sys.argv[1]
try:
    import qrcode
    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    png_data = buf.getvalue()
except Exception:
    png_data = None
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if png_data:
            self.send_response(200)
            self.send_header('Content-type', 'image/png')
            self.end_headers()
            self.wfile.write(png_data)
        else:
            self.send_response(200)
            self.send_header('Content-type', 'text/plain')
            self.end_headers()
            self.wfile.write(uri.encode())
    def log_message(self, *a): pass
s = socketserver.TCPServer(('0.0.0.0', 9090), H)
s.socket.setsockopt(1, 2, 1)
s.handle_request()
PYEOF
            QR_SERVER_PID=$!
            echo ""
            echo -e "    Or open in browser: ${CYAN}http://${server_ip}:9090/${NC}"
        fi

        echo ""
        echo "    1. Open Signal on your phone"
        echo "    2. Settings > Linked Devices > Link New Device"
        echo "    3. Scan the QR code"
        echo ""
        echo -e "  ${BLUE}Waiting for link to complete...${NC}"

        # Wait for signal-cli link process to finish (it blocks until linked or timeout)
        local timeout=90
        local waited=0
        while kill -0 $LINK_PID 2>/dev/null && [ $waited -lt $timeout ]; do
            sleep 2
            waited=$((waited + 2))
        done

        # Clean up QR server
        [ -n "$QR_SERVER_PID" ] && kill $QR_SERVER_PID 2>/dev/null
        wait $QR_SERVER_PID 2>/dev/null || true

        # Check result
        if ! kill -0 $LINK_PID 2>/dev/null; then
            local link_exit=0
            wait $LINK_PID 2>/dev/null || link_exit=$?

            if [ $link_exit -eq 0 ] && grep -q "Associated with" "$link_log" 2>/dev/null; then
                LINKED_NUMBER=$(grep -o '+[0-9]*' "$link_log" | head -1)
                rm -f "$link_log"
                echo -e "  ${GREEN}✓${NC} Device linked: ${LINKED_NUMBER:-successfully}"
                return 0
            fi

            echo -e "  ${YELLOW}Link did not complete (exit code $link_exit).${NC}"
            grep -i "error\|fail\|exception" "$link_log" 2>/dev/null | tail -3 | sed 's/^/    /'
        else
            kill $LINK_PID 2>/dev/null
            wait $LINK_PID 2>/dev/null || true
            echo -e "  ${YELLOW}Link timed out.${NC}"
        fi

        rm -f "$link_log"
    done

    echo -e "  ${YELLOW}Could not complete device link after $max_attempts attempts.${NC}"
    echo -e "  You can re-run the installer to try again."
    return 1
}

# Spinner — shows a thinking animation while a background command runs
# Usage: run_with_spinner "message" command arg1 arg2 ...
run_with_spinner() {
    local msg="$1"; shift
    local spin_chars='⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'
    local log
    log=$(mktemp)

    # Run command in background, capture output
    "$@" > "$log" 2>&1 &
    local pid=$!

    # Animate spinner
    printf "  %s" "$msg"
    local i=0
    while kill -0 "$pid" 2>/dev/null; do
        printf "\r  %s %s" "${spin_chars:i%${#spin_chars}:1}" "$msg"
        i=$((i + 1))
        sleep 0.1
    done

    # Check result
    wait "$pid"
    local rc=$?
    if [ $rc -eq 0 ]; then
        printf "\r  ${GREEN}✓${NC} %s\n" "$msg"
    else
        printf "\r  ${RED}✗${NC} %s\n" "$msg"
        cat "$log" >&2
    fi
    rm -f "$log"
    return $rc
}

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Configuration — default to the repo directory (where install.sh lives)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${NIGHTWIRE_DIR:-$SCRIPT_DIR}"
VENV_DIR="$INSTALL_DIR/venv"
CONFIG_DIR="$INSTALL_DIR/config"
DATA_DIR="$INSTALL_DIR/data"
LOGS_DIR="$INSTALL_DIR/logs"
SIGNAL_DATA_DIR="$INSTALL_DIR/signal-data"

# Flags
SKIP_SIGNAL=false
SKIP_SYSTEMD=false
UNINSTALL=false
RESTART=false
QUICK_MODE=false
PHONE_NUMBER_ARG=""

# Parse arguments
for arg in "$@"; do
    case $arg in
        --skip-signal)
            SKIP_SIGNAL=true
            ;;
        --skip-systemd)
            SKIP_SYSTEMD=true
            ;;
        --uninstall)
            UNINSTALL=true
            ;;
        --restart)
            RESTART=true
            ;;
        --quick)
            QUICK_MODE=true
            ;;
        --phone=*)
            PHONE_NUMBER_ARG="${arg#*=}"
            ;;
        --help|-h)
            echo "Usage: ./install.sh [options]"
            echo ""
            echo "Options:"
            echo "  --quick            Minimal prompts — uses smart defaults"
            echo "  --phone=NUMBER     Set phone number (e.g., --phone=+15551234567)"
            echo "  --skip-signal      Skip Signal pairing (configure later)"
            echo "  --skip-systemd     Skip service installation"
            echo "  --uninstall        Remove nightwire service and containers"
            echo "  --restart          Restart the nightwire service"
            echo "  --help, -h         Show this help message"
            echo ""
            echo "Quick install: ./install.sh --quick --phone=+15551234567"
            exit 0
            ;;
    esac
done

# =============================================================================
# UNINSTALL MODE
# =============================================================================
if [ "$UNINSTALL" = true ]; then
    echo ""
    echo -e "${CYAN}╔══════════════════════════════════════╗${NC}"
    echo -e "${CYAN}║       nightwire uninstaller          ║${NC}"
    echo -e "${CYAN}╚══════════════════════════════════════╝${NC}"
    echo ""

    REMOVED=()

    # ── Step 1: Stop and remove services ──────────────────────────────────

    # User-level systemd service (Linux)
    if [ "$(uname)" = "Linux" ] && command -v systemctl &> /dev/null; then
        USER_SERVICE_FILE="$HOME/.config/systemd/user/nightwire.service"
        if systemctl --user is-active nightwire &> /dev/null || [ -f "$USER_SERVICE_FILE" ]; then
            echo -e "${BLUE}Stopping user systemd service...${NC}"
            systemctl --user stop nightwire 2>/dev/null || true
            systemctl --user disable nightwire 2>/dev/null || true
            rm -f "$USER_SERVICE_FILE"
            systemctl --user daemon-reload
            echo -e "  ${GREEN}✓${NC} User service stopped and removed"
            REMOVED+=("user systemd service")
        fi

        # System-level systemd service (may exist from older installs)
        SYS_SERVICE=""
        for name in signal-claude-bot nightwire; do
            if [ -f "/etc/systemd/system/${name}.service" ]; then
                SYS_SERVICE="/etc/systemd/system/${name}.service"
                SYS_SERVICE_NAME="${name}"
                break
            fi
        done
        if [ -n "$SYS_SERVICE" ]; then
            echo -e "${YELLOW}Found system-level service: ${SYS_SERVICE_NAME}.service${NC}"
            flush_stdin
            read -p "  Remove it? (requires sudo) [y/N] " -n 1 -r
            echo ""
            if [[ $REPLY =~ ^[Yy]$ ]]; then
                sudo systemctl stop "$SYS_SERVICE_NAME" 2>/dev/null || true
                sudo systemctl disable "$SYS_SERVICE_NAME" 2>/dev/null || true
                sudo rm -f "$SYS_SERVICE"
                sudo systemctl daemon-reload
                echo -e "  ${GREEN}✓${NC} System service ${SYS_SERVICE_NAME} removed"
                REMOVED+=("system service ($SYS_SERVICE_NAME)")
            else
                echo "  Skipped system service removal"
            fi
        fi
    fi

    # macOS launchd service
    if [ "$(uname)" = "Darwin" ]; then
        PLIST_FILE="$HOME/Library/LaunchAgents/com.nightwire.bot.plist"
        if [ -f "$PLIST_FILE" ]; then
            echo -e "${BLUE}Removing launchd service...${NC}"
            launchctl unload "$PLIST_FILE" 2>/dev/null || true
            rm -f "$PLIST_FILE"
            echo -e "  ${GREEN}✓${NC} launchd service removed"
            REMOVED+=("launchd service")
        fi

        # Unset launchctl environment variables that were injected during install
        ENV_FILE="$CONFIG_DIR/.env"
        if [ -f "$ENV_FILE" ]; then
            while IFS='=' read -r key value; do
                [[ "$key" =~ ^#.*$ || -z "$key" ]] && continue
                key=$(echo "$key" | xargs)
                [ -n "$key" ] && launchctl unsetenv "$key" 2>/dev/null || true
            done < "$ENV_FILE"
            REMOVED+=("launchctl environment variables")
        fi
    fi

    # Kill any orphaned bot processes (if service stop didn't catch them)
    BOT_PIDS=$(pgrep -f 'python.*-m nightwire' 2>/dev/null || true)
    if [ -n "$BOT_PIDS" ]; then
        echo -e "${BLUE}Stopping orphaned bot process(es)...${NC}"
        echo "$BOT_PIDS" | xargs kill 2>/dev/null || true
        sleep 2
        # Force-kill if still running
        BOT_PIDS=$(pgrep -f 'python.*-m nightwire' 2>/dev/null || true)
        [ -n "$BOT_PIDS" ] && echo "$BOT_PIDS" | xargs kill -9 2>/dev/null || true
        echo -e "  ${GREEN}✓${NC} Orphaned processes stopped"
        REMOVED+=("orphaned processes")
    fi

    # ── Step 2: Stop and remove Docker containers and networks ──────────

    if command -v docker &> /dev/null && docker info &> /dev/null 2>&1; then
        # Use docker compose down to cleanly remove containers + networks
        COMPOSE_DOWN=false
        if [ -f "$INSTALL_DIR/docker-compose.yml" ]; then
            echo -e "${BLUE}Stopping Docker compose project...${NC}"
            (cd "$INSTALL_DIR" && docker compose down 2>/dev/null) || true
            COMPOSE_DOWN=true
        fi

        # Also remove any containers by name (in case compose file is missing)
        for CONTAINER in signal-api nightwire; do
            if docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
                echo -e "${BLUE}Stopping Docker container: ${CONTAINER}...${NC}"
                docker stop "$CONTAINER" 2>/dev/null || true
                docker rm "$CONTAINER" 2>/dev/null || true
                echo -e "  ${GREEN}✓${NC} Container ${CONTAINER} removed"
                REMOVED+=("container: $CONTAINER")
            fi
        done

        if [ "$COMPOSE_DOWN" = true ]; then
            echo -e "  ${GREEN}✓${NC} Docker compose project stopped (containers + networks)"
            REMOVED+=("docker compose project")
        fi

        # Clean up any orphaned compose networks
        for net in $(docker network ls --filter name=nightwire --format '{{.Name}}' 2>/dev/null); do
            docker network rm "$net" 2>/dev/null || true
            REMOVED+=("docker network: $net")
        done

        # ── Step 3: Optionally remove Docker images ───────────────────────

        DOCKER_IMAGES=()
        for img in bbernhard/signal-cli-rest-api:1772646660-ci nightwire-sandbox:latest; do
            if docker image inspect "$img" &> /dev/null; then
                DOCKER_IMAGES+=("$img")
            fi
        done
        if [ ${#DOCKER_IMAGES[@]} -gt 0 ]; then
            echo ""
            echo -e "${YELLOW}Found Docker images:${NC}"
            for img in "${DOCKER_IMAGES[@]}"; do
                size=$(docker image inspect "$img" --format '{{.Size}}' 2>/dev/null || echo "0")
                size_mb=$((size / 1024 / 1024))
                echo "  - $img (~${size_mb}MB)"
            done
            flush_stdin
            read -p "Remove these Docker images? [y/N] " -n 1 -r
            echo ""
            if [[ $REPLY =~ ^[Yy]$ ]]; then
                for img in "${DOCKER_IMAGES[@]}"; do
                    docker rmi "$img" 2>/dev/null || true
                done
                echo -e "  ${GREEN}✓${NC} Docker images removed"
                REMOVED+=("docker images")
            else
                echo "  Kept Docker images"
            fi
        fi
    fi

    # ── Step 4: Remove signal-data backups ──────────────────────────────

    # Remove signal-data backup directories
    backup_count=0
    for bak in "$INSTALL_DIR"/signal-data.bak.*; do
        if [ -d "$bak" ]; then
            rm -rf "$bak"
            backup_count=$((backup_count + 1))
        fi
    done
    if [ $backup_count -gt 0 ]; then
        echo -e "  ${GREEN}✓${NC} Removed $backup_count signal-data backup(s)"
        REMOVED+=("signal-data backups")
    fi

    # ── Step 5: Data preservation prompt ──────────────────────────────────

    HAS_DATA=false
    [ -d "$SIGNAL_DATA_DIR" ] && HAS_DATA=true
    [ -d "$DATA_DIR" ] && HAS_DATA=true

    if [ "$HAS_DATA" = true ]; then
        echo ""
        echo -e "${YELLOW}Your data directories:${NC}"
        [ -d "$SIGNAL_DATA_DIR" ] && echo "  - $SIGNAL_DATA_DIR  (Signal account)"
        [ -d "$DATA_DIR" ] && echo "  - $DATA_DIR  (bot memory database)"
        echo ""
        flush_stdin
        read -p "Keep this data? [Y/n] " -n 1 -r
        echo ""
        if [[ $REPLY =~ ^[Nn]$ ]]; then
            [ -d "$SIGNAL_DATA_DIR" ] && rm -rf "$SIGNAL_DATA_DIR"
            [ -d "$DATA_DIR" ] && rm -rf "$DATA_DIR"
            echo -e "  ${GREEN}✓${NC} Data directories removed"
            REMOVED+=("signal data" "bot data")
        else
            echo "  Kept data directories — remove manually later if needed"
        fi
    fi

    # ── Step 6: Remove runtime artifacts ──────────────────────────────────

    # Virtual environment
    if [ -d "$VENV_DIR" ]; then
        echo -e "${BLUE}Removing Python virtual environment...${NC}"
        rm -rf "$VENV_DIR"
        echo -e "  ${GREEN}✓${NC} Removed venv/"
        REMOVED+=("venv")
    fi

    # Logs
    if [ -d "$LOGS_DIR" ]; then
        rm -rf "$LOGS_DIR"
        echo -e "  ${GREEN}✓${NC} Removed logs/"
        REMOVED+=("logs")
    fi

    # Marker and temp files
    for f in run.sh; do
        if [ -f "$INSTALL_DIR/$f" ]; then
            rm -f "$INSTALL_DIR/$f"
            REMOVED+=("$f")
        fi
    done

    # Python build artifacts
    for d in "$INSTALL_DIR"/nightwire.egg-info "$INSTALL_DIR"/src/*.egg-info; do
        if [ -d "$d" ]; then
            rm -rf "$d"
            REMOVED+=("$(basename "$d")")
        fi
    done
    # Remove __pycache__ directories
    pycache_count=$(find "$INSTALL_DIR" -type d -name '__pycache__' 2>/dev/null | wc -l)
    if [ "$pycache_count" -gt 0 ]; then
        find "$INSTALL_DIR" -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
        echo -e "  ${GREEN}✓${NC} Removed $pycache_count __pycache__ directories"
        REMOVED+=("__pycache__ directories")
    fi

    # Disable loginctl linger if no other user services remain (Linux only)
    if [ "$(uname)" = "Linux" ] && command -v loginctl &> /dev/null; then
        USER_SERVICE_DIR="$HOME/.config/systemd/user"
        if loginctl show-user "$USER" --property=Linger 2>/dev/null | grep -q "Linger=yes"; then
            remaining=$(find "$USER_SERVICE_DIR" -name '*.service' 2>/dev/null | wc -l)
            if [ "$remaining" -eq 0 ]; then
                echo -e "${BLUE}Disabling loginctl linger (no other user services)...${NC}"
                sudo loginctl disable-linger "$USER" 2>/dev/null || true
                echo -e "  ${GREEN}✓${NC} Linger disabled"
                REMOVED+=("loginctl linger")
            fi
        fi
    fi

    # ── Step 7: Remove install directory ──────────────────────────────────

    if [ -d "$INSTALL_DIR" ]; then
        echo ""
        # Show what's left
        remaining_items=$(ls -A "$INSTALL_DIR" 2>/dev/null | head -20)
        if [ -n "$remaining_items" ]; then
            echo -e "${YELLOW}Remaining files in ${INSTALL_DIR}:${NC}"
            ls -A "$INSTALL_DIR" | while read -r item; do
                if [ -d "$INSTALL_DIR/$item" ]; then
                    echo "  📁 $item/"
                else
                    echo "  📄 $item"
                fi
            done
            echo ""
        fi
        flush_stdin
        read -p "Remove entire install directory ($INSTALL_DIR)? [y/N] " -n 1 -r
        echo ""
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            rm -rf "$INSTALL_DIR"
            echo -e "  ${GREEN}✓${NC} Removed $INSTALL_DIR"
            REMOVED+=("install directory")
        else
            echo "  Kept $INSTALL_DIR"
        fi
    fi

    # ── Summary ───────────────────────────────────────────────────────────

    echo ""
    if [ ${#REMOVED[@]} -gt 0 ]; then
        echo -e "${GREEN}nightwire uninstalled.${NC} Removed:"
        for item in "${REMOVED[@]}"; do
            echo -e "  ${GREEN}✓${NC} $item"
        done
    else
        echo -e "${YELLOW}Nothing to uninstall.${NC} No services, containers, or install directory found."
        echo ""
        echo "  Expected install dir: $INSTALL_DIR"
        echo "  Set NIGHTWIRE_DIR if installed elsewhere."
    fi
    echo ""
    exit 0
fi

# =============================================================================
# RESTART MODE
# =============================================================================
if [ "$RESTART" = true ]; then
    echo ""
    echo -e "${CYAN}Restarting nightwire...${NC}"
    echo ""

    RESTARTED=false

    # Linux: systemd
    if [ "$(uname)" = "Linux" ] && command -v systemctl &> /dev/null; then
        if systemctl --user is-active nightwire &> /dev/null || systemctl --user is-enabled nightwire &> /dev/null; then
            systemctl --user restart nightwire
            sleep 2
            if systemctl --user is-active nightwire &>/dev/null; then
                echo -e "  ${GREEN}✓${NC} nightwire restarted (systemd)"
            else
                echo -e "  ${YELLOW}!${NC} Restart issued but service not active yet"
                echo -e "  Check: ${CYAN}journalctl --user -u nightwire -f${NC}"
            fi
            RESTARTED=true
        fi
    fi

    # macOS: launchd
    if [ "$(uname)" = "Darwin" ] && [ "$RESTARTED" = false ]; then
        PLIST_FILE="$HOME/Library/LaunchAgents/com.nightwire.bot.plist"
        if [ -f "$PLIST_FILE" ]; then
            launchctl unload "$PLIST_FILE" 2>/dev/null || true
            launchctl load "$PLIST_FILE" 2>/dev/null
            sleep 2
            if launchctl list | grep -q com.nightwire.bot; then
                echo -e "  ${GREEN}✓${NC} nightwire restarted (launchd)"
            else
                echo -e "  ${YELLOW}!${NC} Restart issued but service not running"
                echo -e "  Check: ${CYAN}tail -f $LOGS_DIR/nightwire.log${NC}"
            fi
            RESTARTED=true
        fi
    fi

    if [ "$RESTARTED" = false ]; then
        echo -e "  ${YELLOW}No service found.${NC} Start manually:"
        echo -e "  ${CYAN}$INSTALL_DIR/run.sh${NC}"
    fi

    echo ""
    exit 0
fi

# Banner
VERSION="2.4.1"
echo -e "${CYAN}"
cat << 'EOF'
       _       _     _            _
 _ __ (_) __ _| |__ | |___      _(_)_ __ ___
| '_ \| |/ _` | '_ \| __\ \ /\ / / | '__/ _ \
| | | | | (_| | | | | |_ \ V  V /| | | |  __/
|_| |_|_|\__, |_| |_|\__| \_/\_/ |_|_|  \___|
         |___/

EOF
echo -e "${NC}"
echo -e "  ${GREEN}Signal + Claude AI Bot${NC} — v${VERSION}"
echo -e "  By ${CYAN}hackingdave${NC} — ${CYAN}https://github.com/hackingdave/nightwire${NC}"
echo ""

# -----------------------------------------------------------------------------
# Prerequisite checks
# -----------------------------------------------------------------------------
echo -e "${BLUE}Checking prerequisites...${NC}"

# Python 3.9+
if command -v python3 &> /dev/null; then
    PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    MAJOR=$(echo $PYTHON_VERSION | cut -d. -f1)
    MINOR=$(echo $PYTHON_VERSION | cut -d. -f2)
    if [ "$MAJOR" -lt 3 ] || ([ "$MAJOR" -eq 3 ] && [ "$MINOR" -lt 9 ]); then
        echo -e "${RED}Error: Python 3.9+ required (found $PYTHON_VERSION)${NC}"
        exit 1
    fi
    echo -e "  ${GREEN}✓${NC} Python $PYTHON_VERSION"
else
    echo -e "${RED}Error: Python 3 not found${NC}"
    exit 1
fi

# Claude CLI
if command -v claude &> /dev/null; then
    echo -e "  ${GREEN}✓${NC} Claude CLI"
elif [ -f "$HOME/.local/bin/claude" ]; then
    echo -e "  ${GREEN}✓${NC} Claude CLI ($HOME/.local/bin/claude)"
else
    echo -e "  ${YELLOW}!${NC} Claude CLI not found"
    echo -e "    nightwire requires Claude CLI for code commands (/ask, /do, /complex)."
    echo -e "    Install: ${CYAN}https://docs.anthropic.com/en/docs/claude-code${NC}"
    if [ "$QUICK_MODE" = true ]; then
        echo -e "    ${BLUE}(--quick: continuing without Claude CLI)${NC}"
    else
        read -p "    Continue anyway? [y/N] " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            exit 1
        fi
    fi
fi

# curl (needed for Signal pairing verification)
if ! command -v curl &> /dev/null; then
    echo -e "  ${YELLOW}!${NC} curl not found — installing..."
    if command -v apt-get &> /dev/null; then
        sudo apt-get install -y -qq curl > /dev/null 2>&1
    elif command -v dnf &> /dev/null; then
        sudo dnf install -y -q curl > /dev/null 2>&1
    fi
    if command -v curl &> /dev/null; then
        echo -e "  ${GREEN}✓${NC} curl installed"
    else
        echo -e "  ${YELLOW}!${NC} curl not found — Signal pairing verification may fail"
    fi
else
    echo -e "  ${GREEN}✓${NC} curl"
fi

# Docker (required for Signal bridge — one small container)
DOCKER_OK=false
if [ "$SKIP_SIGNAL" = true ]; then
    DOCKER_OK=true  # Don't need Docker if skipping Signal
elif command -v docker &> /dev/null; then
    if docker info &> /dev/null; then
        echo -e "  ${GREEN}✓${NC} Docker"
        DOCKER_OK=true
    else
        echo -e "  ${YELLOW}!${NC} Docker installed but not running"
        if [ "$QUICK_MODE" = true ]; then
            echo -e "    ${BLUE}(--quick: skipping Signal setup — start Docker and re-run)${NC}"
            SKIP_SIGNAL=true
            DOCKER_OK=true
        fi
        if [ "$DOCKER_OK" = false ]; then
        echo ""
        if [ "$(uname)" = "Darwin" ]; then
            echo -e "    Start Docker Desktop: ${CYAN}open -a Docker${NC}"
        else
            echo -e "    Start Docker: ${CYAN}sudo systemctl start docker${NC}"
        fi
        echo ""
        echo "    1) Wait — I'll start Docker now"
        echo "    2) Skip Signal setup for now"
        echo ""
        read -p "    > " DOCKER_WAIT_CHOICE
        echo ""
        if [ "$DOCKER_WAIT_CHOICE" = "2" ]; then
            SKIP_SIGNAL=true
            DOCKER_OK=true
        else
            flush_stdin
            read -p "    Press Enter when Docker is running..."
            echo ""
            echo -e "    Waiting for Docker..."
            TRIES=0
            while [ $TRIES -lt 30 ]; do
                if docker info &> /dev/null; then
                    break
                fi
                sleep 2
                TRIES=$((TRIES + 1))
            done
            if docker info &> /dev/null; then
                echo -e "  ${GREEN}✓${NC} Docker is running"
                DOCKER_OK=true
            else
                echo -e "  ${YELLOW}Docker still not ready.${NC} Skipping Signal setup."
                SKIP_SIGNAL=true
                DOCKER_OK=true
            fi
        fi
        fi # end DOCKER_OK=false check
    fi
else
    echo -e "  ${YELLOW}!${NC} Docker not found"
    echo ""
    echo "    nightwire needs one small Docker container for Signal messaging."
    echo ""
    if [ "$(uname)" = "Darwin" ]; then
        echo -e "    Install Docker Desktop: ${CYAN}https://docs.docker.com/desktop/install/mac-install/${NC}"
    elif command -v apt-get &> /dev/null; then
        read -p "    Install Docker now? [Y/n] " -n 1 -r
        echo ""
        if [[ ! $REPLY =~ ^[Nn]$ ]]; then
            echo -e "  ${CYAN}Installing Docker...${NC}"
            sudo apt-get update -qq && sudo apt-get install -y -qq docker.io > /dev/null
            sudo usermod -aG docker "$USER" 2>/dev/null || true
            sudo systemctl start docker 2>/dev/null || true
            sudo systemctl enable docker 2>/dev/null || true
            if docker info &> /dev/null; then
                echo -e "  ${GREEN}✓${NC} Docker installed"
                DOCKER_OK=true
            else
                echo -e "  ${YELLOW}!${NC} Docker installed but may need a re-login for group permissions"
                echo "    Run: ${CYAN}newgrp docker${NC} then re-run this installer"
                exit 1
            fi
        fi
    elif command -v dnf &> /dev/null; then
        read -p "    Install Docker now? [Y/n] " -n 1 -r
        echo ""
        if [[ ! $REPLY =~ ^[Nn]$ ]]; then
            echo -e "  ${CYAN}Installing Docker...${NC}"
            sudo dnf install -y -q docker > /dev/null
            sudo usermod -aG docker "$USER" 2>/dev/null || true
            sudo systemctl start docker 2>/dev/null || true
            sudo systemctl enable docker 2>/dev/null || true
            if docker info &> /dev/null; then
                echo -e "  ${GREEN}✓${NC} Docker installed"
                DOCKER_OK=true
            else
                echo -e "  ${YELLOW}!${NC} Docker installed but may need a re-login for group permissions"
                echo "    Run: ${CYAN}newgrp docker${NC} then re-run this installer"
                exit 1
            fi
        fi
    else
        echo -e "    Install Docker: ${CYAN}https://docs.docker.com/get-docker/${NC}"
    fi

    if [ "$DOCKER_OK" = false ]; then
        if [ "$QUICK_MODE" = true ]; then
            echo -e "    ${BLUE}(--quick: skipping Signal setup — install Docker and re-run)${NC}"
            SKIP_SIGNAL=true
            DOCKER_OK=true
        else
            echo ""
            read -p "    Skip Signal setup and continue? [y/N] " -n 1 -r
            echo ""
            if [[ $REPLY =~ ^[Yy]$ ]]; then
                SKIP_SIGNAL=true
                DOCKER_OK=true
            else
                echo ""
                echo "  Install Docker, then re-run this installer."
                exit 1
            fi
        fi
    fi
fi

echo ""

# -----------------------------------------------------------------------------
# Verify source and create data directories
# -----------------------------------------------------------------------------
echo -e "${BLUE}Setting up directories...${NC}"

if [ ! -d "$INSTALL_DIR/nightwire" ]; then
    echo -e "${RED}Error: nightwire package not found in $INSTALL_DIR${NC}"
    echo "  Run this installer from the nightwire repo directory."
    exit 1
fi

mkdir -p "$CONFIG_DIR"
mkdir -p "$DATA_DIR"
mkdir -p "$LOGS_DIR"
mkdir -p "$SIGNAL_DATA_DIR"

# Copy config templates if not already present
if [ -d "$INSTALL_DIR/config" ]; then
    cp -n "$INSTALL_DIR/config/"*.example "$CONFIG_DIR/" 2>/dev/null || true
fi

echo -e "  ${GREEN}✓${NC} Ready ($INSTALL_DIR)"

# -----------------------------------------------------------------------------
# Create virtual environment
# -----------------------------------------------------------------------------
echo -e "${BLUE}Setting up Python environment...${NC}"

if [ ! -d "$VENV_DIR" ]; then
    run_with_spinner "Creating virtual environment" python3 -m venv "$VENV_DIR"
fi

source "$VENV_DIR/bin/activate"

if "$VENV_DIR/bin/pip" freeze 2>/dev/null | grep -q aiohttp; then
    echo -e "  ${GREEN}✓${NC} Dependencies already installed"
else
    run_with_spinner "Installing dependencies (this may take a minute)" bash -c "pip install --upgrade pip -q && pip install -e '$INSTALL_DIR' -q"
fi

# Fix sqlite-vec on aarch64 — pip wheel v0.1.6 ships a 32-bit ARM binary
# for the "aarch64" platform. Replace it with the proper 64-bit build.
ARCH=$(uname -m)
if [ "$ARCH" = "aarch64" ]; then
    VEC_SO=$(python3 -c "import sqlite_vec; print(sqlite_vec.loadable_path())" 2>/dev/null)
    if [ -n "$VEC_SO" ] && file "${VEC_SO}.so" 2>/dev/null | grep -q "32-bit"; then
        echo -e "  ${YELLOW}⚠${NC}  sqlite-vec pip wheel has wrong architecture, fixing..."
        VEC_URL="https://github.com/asg017/sqlite-vec/releases/download/v0.1.7-alpha.10/sqlite-vec-0.1.7-alpha.10-loadable-linux-aarch64.tar.gz"
        TMPDIR=$(mktemp -d)
        if curl -sL "$VEC_URL" | tar xz -C "$TMPDIR" 2>/dev/null; then
            if file "$TMPDIR/vec0.so" | grep -q "64-bit"; then
                cp "$TMPDIR/vec0.so" "${VEC_SO}.so"
                echo -e "  ${GREEN}✓${NC} sqlite-vec aarch64 binary fixed"
            else
                echo -e "  ${YELLOW}⚠${NC}  Downloaded binary still not 64-bit, skipping"
            fi
        else
            echo -e "  ${YELLOW}⚠${NC}  Could not download sqlite-vec fix (vector search will use fallback)"
        fi
        rm -rf "$TMPDIR"
    fi
fi

# -----------------------------------------------------------------------------
# Interactive configuration
# -----------------------------------------------------------------------------
echo ""
echo -e "${BLUE}Configuration${NC}"
echo ""

# Create settings.yaml from template
SETTINGS_FILE="$CONFIG_DIR/settings.yaml"
if [ ! -f "$SETTINGS_FILE" ]; then
    if [ -f "$CONFIG_DIR/settings.yaml.example" ]; then
        cp "$CONFIG_DIR/settings.yaml.example" "$SETTINGS_FILE"
    else
        cat > "$SETTINGS_FILE" << 'YAML'
# nightwire configuration

# Phone numbers authorized to use the bot (E.164 format)
allowed_numbers:
  - "+1XXXXXXXXXX"  # Replace with your number

# Signal CLI REST API
signal_api_url: "http://127.0.0.1:8080"

# Memory System
memory:
  session_timeout: 30
  max_context_tokens: 1500

# Autonomous Tasks
autonomous:
  enabled: true
  poll_interval: 30
  quality_gates: true

# Optional: nightwire AI assistant (OpenAI or Grok)
nightwire_assistant:
  enabled: false
YAML
    fi
fi

# Get phone number (accept from --phone flag or prompt)
if [ -n "$PHONE_NUMBER_ARG" ]; then
    PHONE_NUMBER="$PHONE_NUMBER_ARG"
    echo -e "  Phone number: ${GREEN}$PHONE_NUMBER${NC}"
else
    echo -e "  Enter your phone number (e.g., +15551234567):"
    read -p "  > " PHONE_NUMBER
fi

if [ -n "$PHONE_NUMBER" ]; then
    if [[ ! "$PHONE_NUMBER" =~ ^\+[1-9][0-9]{6,14}$ ]]; then
        echo -e "  ${YELLOW}Warning: doesn't look like E.164 format (e.g., +15551234567)${NC}"
        if [ "$QUICK_MODE" = true ]; then
            echo -e "  ${BLUE}(--quick: using phone number as-is)${NC}"
        else
            read -p "  Continue anyway? [y/N] " -n 1 -r
            echo
            if [[ ! $REPLY =~ ^[Yy]$ ]]; then
                echo "  Please re-run the installer with a valid phone number."
                exit 1
            fi
        fi
    fi
    sed_inplace "s/+1XXXXXXXXXX/$PHONE_NUMBER/" "$SETTINGS_FILE"
    echo -e "  ${GREEN}✓${NC} Phone number set"
fi

# Create .env file
ENV_FILE="$CONFIG_DIR/.env"
if [ ! -f "$ENV_FILE" ]; then
    cat > "$ENV_FILE" << EOF
# nightwire environment variables

# Optional: OpenAI API key (for nightwire AI assistant)
# OPENAI_API_KEY=

# Optional: Grok API key (for nightwire AI assistant)
# GROK_API_KEY=
EOF
fi

# Optional AI assistant (not required — Claude handles all code tasks)
if [ "$QUICK_MODE" = true ]; then
    echo -e "  ${BLUE}(--quick: skipping optional AI assistant — enable later in settings.yaml)${NC}"
    REPLY="n"
else
    echo ""
    echo -e "  ${BLUE}Optional:${NC} nightwire can use OpenAI or Grok as a lightweight"
    echo "  assistant for general knowledge questions (\"nightwire: what is X?\")."
    echo "  This is NOT required — Claude handles all code commands (/ask, /do, /complex)."
    echo ""
    read -p "  Enable optional AI assistant? [y/N] " -n 1 -r
    echo ""
fi
if [[ $REPLY =~ ^[Yy]$ ]]; then
    sed_inplace "s/enabled: false/enabled: true/" "$SETTINGS_FILE"
    echo ""
    echo "    Which provider? (1) OpenAI  (2) Grok"
    read -p "    > " PROVIDER_CHOICE
    echo ""
    if [ "$PROVIDER_CHOICE" = "1" ]; then
        echo -e "  Enter your OpenAI API key:"
        read -p "  > " -s OPENAI_KEY
        echo ""
        if [ -n "$OPENAI_KEY" ]; then
            # Use python to safely replace the line (avoids sed special char issues in keys)
            python3 -c "
import re, sys
p = sys.argv[1]
k = sys.argv[2]
txt = open(p).read()
txt = re.sub(r'^#?\s*OPENAI_API_KEY=.*', 'OPENAI_API_KEY=' + k, txt, flags=re.MULTILINE)
open(p, 'w').write(txt)
" "$ENV_FILE" "$OPENAI_KEY"
            echo -e "  ${GREEN}✓${NC} OpenAI configured"
        fi
    else
        echo -e "  Enter your Grok API key:"
        read -p "  > " -s GROK_KEY
        echo ""
        if [ -n "$GROK_KEY" ]; then
            python3 -c "
import re, sys
p = sys.argv[1]
k = sys.argv[2]
txt = open(p).read()
txt = re.sub(r'^#?\s*GROK_API_KEY=.*', 'GROK_API_KEY=' + k, txt, flags=re.MULTILINE)
open(p, 'w').write(txt)
" "$ENV_FILE" "$GROK_KEY"
            echo -e "  ${GREEN}✓${NC} Grok configured"
        fi
    fi
fi

# -----------------------------------------------------------------------------
# Projects directory
# -----------------------------------------------------------------------------
DEFAULT_PROJECTS="$HOME/projects"
if [ "$QUICK_MODE" = true ]; then
    PROJECTS_PATH="$DEFAULT_PROJECTS"
    echo -e "  ${BLUE}(--quick: using default projects path: $PROJECTS_PATH)${NC}"
else
    echo ""
    echo -e "  ${BLUE}Projects directory:${NC} Where your code projects live."
    echo "  Claude will be able to work on any project registered from this folder."
    echo ""
    read -p "  Projects path [$DEFAULT_PROJECTS]: " PROJECTS_PATH
    PROJECTS_PATH="${PROJECTS_PATH:-$DEFAULT_PROJECTS}"
fi

# Expand ~ if used
PROJECTS_PATH="${PROJECTS_PATH/#\~/$HOME}"

if [ -d "$PROJECTS_PATH" ]; then
    # Set projects_base_path in settings.yaml
    if grep -q "^# projects_base_path:" "$SETTINGS_FILE" 2>/dev/null; then
        sed_inplace "s|^# projects_base_path:.*|projects_base_path: \"$PROJECTS_PATH\"|" "$SETTINGS_FILE"
    elif grep -q "^projects_base_path:" "$SETTINGS_FILE" 2>/dev/null; then
        sed_inplace "s|^projects_base_path:.*|projects_base_path: \"$PROJECTS_PATH\"|" "$SETTINGS_FILE"
    else
        echo "" >> "$SETTINGS_FILE"
        echo "projects_base_path: \"$PROJECTS_PATH\"" >> "$SETTINGS_FILE"
    fi
    echo -e "  ${GREEN}✓${NC} Projects path set: $PROJECTS_PATH"

    # Scan for subdirectories and offer to auto-register
    SUBDIRS=()
    while IFS= read -r dir; do
        SUBDIRS+=("$(basename "$dir")")
    done < <(find "$PROJECTS_PATH" -mindepth 1 -maxdepth 1 -type d | sort)

    if [ ${#SUBDIRS[@]} -gt 0 ]; then
        echo ""
        echo "  Found ${#SUBDIRS[@]} project(s) in $PROJECTS_PATH:"
        for d in "${SUBDIRS[@]}"; do
            echo "    - $d"
        done
        if [ "$QUICK_MODE" = true ]; then
            REPLY="y"
            echo -e "  ${BLUE}(--quick: auto-registering ${#SUBDIRS[@]} project(s))${NC}"
        else
            echo ""
            read -p "  Auto-register all as projects? [Y/n] " -n 1 -r
            echo ""
        fi
        if [[ ! $REPLY =~ ^[Nn]$ ]]; then
            # Create projects.yaml with all subdirectories
            PROJECTS_FILE="$CONFIG_DIR/projects.yaml"
            cat > "$PROJECTS_FILE" << PROJEOF
# nightwire Projects Registry — auto-generated by installer
projects:
PROJEOF
            for d in "${SUBDIRS[@]}"; do
                echo "  - name: \"$d\"" >> "$PROJECTS_FILE"
                echo "    path: \"$PROJECTS_PATH/$d\"" >> "$PROJECTS_FILE"
            done
            echo -e "  ${GREEN}✓${NC} Registered ${#SUBDIRS[@]} project(s)"
        fi
    else
        echo "  No subdirectories found — add projects later with /add"
    fi
else
    echo -e "  ${YELLOW}!${NC} Directory not found: $PROJECTS_PATH"
    echo "    You can set projects_base_path later in config/settings.yaml"
fi

# -----------------------------------------------------------------------------
# Docker Sandbox (optional)
# -----------------------------------------------------------------------------
if [ "$DOCKER_OK" = true ]; then
    if [ "$QUICK_MODE" = true ]; then
        echo -e "  ${BLUE}(--quick: skipping Docker sandbox — enable later in settings.yaml)${NC}"
        REPLY="n"
    else
        echo ""
        echo -e "  ${BLUE}Optional:${NC} Docker sandbox runs Claude CLI inside a container."
        echo "  This adds complexity and is only needed if you're working on sensitive"
        echo "  projects that you don't want Claude to have access to — Claude is already"
        echo "  restricted to the projects folder. Requires building a Docker image (~400MB)."
        echo ""
        echo -e "  Most users should say ${GREEN}no${NC}."
        echo ""
        read -p "  Enable Docker sandbox? [y/N] " -n 1 -r
        echo ""
    fi
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        if run_with_spinner "Building sandbox image (this may take a few minutes)" docker build -t nightwire-sandbox:latest -f "$INSTALL_DIR/Dockerfile.sandbox" "$INSTALL_DIR"; then
            echo -e "  ${GREEN}✓${NC} Sandbox image built (nightwire-sandbox:latest)"

            # Enable sandbox in settings.yaml — append if not already present
            if ! grep -q "^sandbox:" "$SETTINGS_FILE" 2>/dev/null; then
                cat >> "$SETTINGS_FILE" << 'SANDBOXEOF'

# Docker Sandbox
sandbox:
  enabled: true
  image: "nightwire-sandbox:latest"
  network: false
  memory_limit: "2g"
  cpu_limit: 2.0
  tmpfs_size: "256m"
SANDBOXEOF
            fi
            echo -e "  ${GREEN}✓${NC} Sandbox enabled in config"
        else
            echo "    You can retry later:"
            echo "    cd $INSTALL_DIR && docker build -t nightwire-sandbox:latest -f Dockerfile.sandbox ."
        fi
    fi
fi

# -----------------------------------------------------------------------------
# Signal Pairing — uses signal-cli inside the Docker container
# -----------------------------------------------------------------------------
SIGNAL_PAIRED=false

if [ "$SKIP_SIGNAL" = false ]; then
    echo ""
    echo -e "${BLUE}Signal Pairing${NC}"
    echo ""

    mkdir -p "$SIGNAL_DATA_DIR"

    # Device name prompt (shown in Signal's linked devices list)
    DEVICE_NAME="nightwire"
    if [ "$QUICK_MODE" != true ]; then
        flush_stdin
        read -p "  Device name [nightwire]: " DEVICE_NAME_INPUT
        DEVICE_NAME="${DEVICE_NAME_INPUT:-nightwire}"
    fi

    # Clean stale signal-data from previous failed link attempts
    if [ -d "$SIGNAL_DATA_DIR/data" ]; then
        ACCT_FILE="$SIGNAL_DATA_DIR/data/accounts.json"
        STALE_DATA=false
        if [ -f "$ACCT_FILE" ]; then
            acct_content=$(cat "$ACCT_FILE" 2>/dev/null || echo "")
            # Empty/trivial accounts.json = stale from failed link
            if [ -z "$acct_content" ] || [ "$acct_content" = "[]" ] || [ "$acct_content" = "{}" ]; then
                STALE_DATA=true
            fi
            # "multi-account" marker = corrupt data from bbernhard's multi-account mode
            if echo "$acct_content" | grep -q "multi-account" 2>/dev/null; then
                STALE_DATA=true
            fi
        fi
        if [ "$STALE_DATA" = true ]; then
            echo -e "  ${YELLOW}Cleaning stale signal data from previous attempt...${NC}"
            rm -rf "$SIGNAL_DATA_DIR/data"
            echo -e "  ${GREEN}✓${NC} Stale data removed"
        fi
    fi

    # Ask about remote access for QR code scanning
    REMOTE_MODE=false
    if [ -n "$SSH_CONNECTION" ]; then
        echo -e "  ${YELLOW}Remote session detected.${NC}"
        REMOTE_MODE=true
        echo -e "  QR code will be served on port 9090 for remote scanning."
    elif [ "$QUICK_MODE" = true ]; then
        echo -e "  ${BLUE}(--quick: QR code will display in terminal)${NC}"
    else
        read -p "  Will you scan the QR code from another device (e.g., SSH'd in)? [y/N] " -n 1 -r
        echo ""
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            REMOTE_MODE=true
            echo -e "  QR code will be served on port 9090 for remote scanning."
            echo -e "  After pairing, the port is ${GREEN}automatically closed${NC}."
        fi
        echo ""
    fi

    # Start signal-api container and prepare docker exec for device linking
    if prepare_link_tool "$INSTALL_DIR"; then
        echo ""

        # Run device linking
        if run_device_link "$REMOTE_MODE" "$DEVICE_NAME"; then
            SIGNAL_PAIRED=true

            if [ -n "$LINKED_NUMBER" ] && [ "$LINKED_NUMBER" != "$PHONE_NUMBER" ]; then
                sed_inplace "s/$PHONE_NUMBER/$LINKED_NUMBER/" "$SETTINGS_FILE" 2>/dev/null || true
            fi
        fi
    else
        echo ""
        echo -e "  ${YELLOW}Could not prepare link tool.${NC}"
        echo -e "  You can pair later by re-running the installer."
    fi
fi

# -----------------------------------------------------------------------------
# Start Signal bridge in json-rpc mode (required for WebSocket messaging)
# -----------------------------------------------------------------------------
if command -v docker &> /dev/null && docker info &> /dev/null; then
    echo ""
    echo -e "${BLUE}Starting Signal bridge...${NC}"

    mkdir -p "$SIGNAL_DATA_DIR"

    # Back up signal data before starting (prevents session loss on container issues)
    if [ -f "$SIGNAL_DATA_DIR/data/accounts.json" ]; then
        BACKUP_DIR="$SIGNAL_DATA_DIR.bak.$(date +%Y%m%d_%H%M%S)"
        cp -a "$SIGNAL_DATA_DIR" "$BACKUP_DIR" 2>/dev/null || true
        echo -e "  ${GREEN}✓${NC} Signal data backed up"
    fi

    # Detect Docker Compose command (v2 plugin or v1 standalone)
    COMPOSE=""
    if docker compose version &>/dev/null; then
        COMPOSE="docker compose"
    elif command -v docker-compose &>/dev/null; then
        COMPOSE="docker-compose"
    fi

    if [ -n "$COMPOSE" ] && [ -f "$INSTALL_DIR/docker-compose.yml" ]; then
        cd "$INSTALL_DIR"
        $COMPOSE up -d --force-recreate
        cd - > /dev/null
    else
        # Fallback: direct docker run (when docker compose is unavailable)
        docker rm -f signal-api 2>/dev/null || true
        sleep 1

        docker run -d \
            --name signal-api \
            --restart unless-stopped \
            --health-cmd "curl -sf http://127.0.0.1:8080/v1/about || exit 1" \
            --health-interval 60s \
            --health-timeout 10s \
            --health-retries 3 \
            --health-start-period 30s \
            -p "127.0.0.1:8080:8080" \
            -v "$SIGNAL_DATA_DIR:/home/.local/share/signal-cli" \
            -e MODE=json-rpc \
            bbernhard/signal-cli-rest-api:1772646660-ci
    fi

    sleep 3
    if docker ps | grep -q signal-api; then
        echo -e "  ${GREEN}✓${NC} Signal bridge running (json-rpc mode, with health checks)"
    else
        echo -e "  ${YELLOW}Signal bridge did not start. Check: docker logs signal-api${NC}"
    fi
fi

# -----------------------------------------------------------------------------
# Auto-start service
# -----------------------------------------------------------------------------
INSTALLED_SERVICE=false
STARTED_SERVICE=false

# Create run script (always, as a fallback)
RUN_SCRIPT="$INSTALL_DIR/run.sh"
cat > "$RUN_SCRIPT" << EOF
#!/bin/bash
set -e
cd "$INSTALL_DIR" || exit 1
source "$VENV_DIR/bin/activate"
[ -f "$CONFIG_DIR/.env" ] && source "$CONFIG_DIR/.env"
exec python3 -m nightwire
EOF
chmod +x "$RUN_SCRIPT"

if [ "$SKIP_SYSTEMD" = false ]; then
    echo ""

    if [ "$(uname)" = "Linux" ] && command -v systemctl &> /dev/null; then
        # --- Linux: systemd service ---
        if [ "$QUICK_MODE" = true ]; then
            REPLY="y"
            echo -e "  ${BLUE}(--quick: installing systemd service)${NC}"
        else
            read -p "Start nightwire as a service (auto-starts on boot)? [Y/n] " -n 1 -r
            echo ""
        fi

        if [[ ! $REPLY =~ ^[Nn]$ ]]; then
            SERVICE_FILE="$HOME/.config/systemd/user/nightwire.service"
            mkdir -p "$HOME/.config/systemd/user"

            cat > "$SERVICE_FILE" << EOF
[Unit]
Description=nightwire - Signal Claude Bot
After=network.target docker.service

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
Environment="PATH=$VENV_DIR/bin:/usr/local/bin:/usr/bin:/bin"
EnvironmentFile=-$CONFIG_DIR/.env
ExecStartPre=/bin/bash -c 'cd $INSTALL_DIR && docker compose up -d 2>/dev/null || docker start signal-api 2>/dev/null || true'
ExecStart=$VENV_DIR/bin/python3 -m nightwire
StandardOutput=append:$LOGS_DIR/nightwire.log
StandardError=append:$LOGS_DIR/nightwire.log
Restart=on-failure
RestartSec=10
RestartForceExitStatus=75
StartLimitIntervalSec=300
StartLimitBurst=5

[Install]
WantedBy=default.target
EOF

            systemctl --user daemon-reload
            systemctl --user enable nightwire
            loginctl enable-linger "$USER" 2>/dev/null || true

            INSTALLED_SERVICE=true
            echo -e "  ${GREEN}✓${NC} Service installed and enabled"

            # Start it now
            systemctl --user start nightwire 2>/dev/null
            sleep 2
            if systemctl --user is-active nightwire &>/dev/null; then
                echo -e "  ${GREEN}✓${NC} nightwire is running!"
                STARTED_SERVICE=true
            else
                echo -e "  ${YELLOW}Service installed but not started yet.${NC}"
                echo -e "  Start with: ${CYAN}systemctl --user start nightwire${NC}"
            fi
        fi

    elif [ "$(uname)" = "Darwin" ]; then
        # --- macOS: launchd plist ---
        if [ "$QUICK_MODE" = true ]; then
            REPLY="y"
            echo -e "  ${BLUE}(--quick: installing launchd service)${NC}"
        else
            read -p "Start nightwire as a service (auto-starts on login)? [Y/n] " -n 1 -r
            echo ""
        fi

        if [[ ! $REPLY =~ ^[Nn]$ ]]; then
            PLIST_DIR="$HOME/Library/LaunchAgents"
            PLIST_FILE="$PLIST_DIR/com.nightwire.bot.plist"
            mkdir -p "$PLIST_DIR"

            cat > "$PLIST_FILE" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.nightwire.bot</string>
    <key>ProgramArguments</key>
    <array>
        <string>$VENV_DIR/bin/python3</string>
        <string>-m</string>
        <string>nightwire</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$INSTALL_DIR</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$VENV_DIR/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>StandardOutPath</key>
    <string>$LOGS_DIR/nightwire.log</string>
    <key>StandardErrorPath</key>
    <string>$LOGS_DIR/nightwire.err</string>
</dict>
</plist>
EOF

            INSTALLED_SERVICE=true
            echo -e "  ${GREEN}✓${NC} Service installed"

            # Load .env into the plist environment
            if [ -f "$ENV_FILE" ]; then
                while IFS='=' read -r key value; do
                    [[ "$key" =~ ^#.*$ || -z "$key" ]] && continue
                    # Strip surrounding whitespace
                    key=$(echo "$key" | xargs)
                    value=$(echo "$value" | xargs)
                    [ -n "$value" ] && launchctl setenv "$key" "$value" 2>/dev/null || true
                done < "$ENV_FILE"
            fi

            launchctl unload "$PLIST_FILE" 2>/dev/null || true
            launchctl load "$PLIST_FILE" 2>/dev/null

            sleep 2
            if launchctl list | grep -q com.nightwire.bot; then
                echo -e "  ${GREEN}✓${NC} nightwire is running!"
                STARTED_SERVICE=true
            else
                echo -e "  ${YELLOW}Service installed but not started yet.${NC}"
                echo -e "  Start with: ${CYAN}launchctl load $PLIST_FILE${NC}"
            fi
        fi
    fi
fi

# -----------------------------------------------------------------------------
# Summary
# -----------------------------------------------------------------------------
echo ""
if [ "$SIGNAL_PAIRED" = true ] && [ "$STARTED_SERVICE" = true ]; then
    # Everything worked — clean success
    echo -e "${GREEN}╔════════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${GREEN}║                    nightwire is ready!                          ║${NC}"
    echo -e "${GREEN}╚════════════════════════════════════════════════════════════════╝${NC}"
    echo ""
    echo -e "  Send a message to ${CYAN}${LINKED_NUMBER:-your Signal number}${NC} to test!"
    echo -e "  Try: ${CYAN}/help${NC}"
    echo ""
    if [ "$(uname)" = "Linux" ]; then
        echo -e "  View logs:  ${CYAN}journalctl --user -u nightwire -f${NC}"
        echo -e "  Stop:       ${CYAN}systemctl --user stop nightwire${NC}"
        echo -e "  Restart:    ${CYAN}systemctl --user restart nightwire${NC}"
    elif [ "$(uname)" = "Darwin" ]; then
        echo -e "  View logs:  ${CYAN}tail -f $LOGS_DIR/nightwire.log${NC}"
        echo -e "  Stop:       ${CYAN}launchctl unload ~/Library/LaunchAgents/com.nightwire.bot.plist${NC}"
        echo -e "  Restart:    ${CYAN}launchctl unload ~/Library/LaunchAgents/com.nightwire.bot.plist && launchctl load ~/Library/LaunchAgents/com.nightwire.bot.plist${NC}"
    fi
else
    echo -e "${GREEN}╔════════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${GREEN}║              nightwire installation complete!                   ║${NC}"
    echo -e "${GREEN}╚════════════════════════════════════════════════════════════════╝${NC}"
    echo ""
    echo -e "  Install dir: ${CYAN}$INSTALL_DIR${NC}"
    echo -e "  Config:      ${CYAN}$CONFIG_DIR/settings.yaml${NC}"

    # Only show remaining steps
    STEP=1

    if ! command -v claude &> /dev/null && ! [ -f "$HOME/.local/bin/claude" ]; then
        echo ""
        echo -e "  ${STEP}. Install Claude CLI: ${CYAN}https://docs.anthropic.com/en/docs/claude-code${NC}"
        STEP=$((STEP + 1))
    fi

    if [ "$SIGNAL_PAIRED" = false ] && [ "$SKIP_SIGNAL" = true ]; then
        echo ""
        echo "  $STEP. Set up Signal (re-run installer without --skip-signal)"
        STEP=$((STEP + 1))
    fi

    if [ "$STARTED_SERVICE" = false ]; then
        echo ""
        echo -e "  $STEP. Start nightwire: ${CYAN}$RUN_SCRIPT${NC}"
        STEP=$((STEP + 1))
    fi

    echo ""
    echo -e "  Send a test message on Signal: ${CYAN}/help${NC}"
fi

echo ""
echo -e "  Config:  ${CYAN}$CONFIG_DIR/settings.yaml${NC}"
echo -e "  Docs:    ${CYAN}https://github.com/hackingdave/nightwire${NC}"
echo ""
