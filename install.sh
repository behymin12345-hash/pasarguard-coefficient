#!/usr/bin/env bash
# ============================================================
#  Usage coefficient panel installer for PasarGuard
#  Usage:
#     bash install.sh
#  Or from GitHub (on the panel server):
#     curl -sL https://raw.githubusercontent.com/behymin12345-hash/pasarguard-coefficient/main/install.sh -o install.sh && sudo bash install.sh
# ============================================================
set -e

REPO_RAW="https://raw.githubusercontent.com/behymin12345-hash/pasarguard-coefficient/main"
DEST="/opt/pg-coef"
SERVICE="pg-coef"

# If piped (curl ... | bash), take interactive prompts from the terminal
if [ ! -t 0 ]; then
    exec < /dev/tty 2>/dev/null || true
fi

echo "=============================================="
echo "  Usage Coefficient Panel for PasarGuard"
echo "=============================================="

# ---------- requirements
if [ "$(id -u)" -ne 0 ]; then echo "ERROR: run as root (sudo bash install.sh)"; exit 1; fi
command -v python3 >/dev/null || { echo "ERROR: python3 is not installed"; exit 1; }
command -v curl >/dev/null || { echo "ERROR: curl is not installed (apt install curl)"; exit 1; }

# ---------- download files
mkdir -p "$DEST"
echo "Downloading files to $DEST ..."
for f in pg_coef.py pg_panel.py index.html; do
    curl -sL "$REPO_RAW/$f" -o "$DEST/$f"
    [ -s "$DEST/$f" ] || { echo "ERROR: download of $f failed"; exit 1; }
done
chmod 600 "$DEST"/*.py 2>/dev/null || true

# ---------- detect database container (for native mode)
# Note: pgbouncer usually has no psql client; prefer the actual DB container
DB_CONTAINER=""
if command -v docker >/dev/null; then
    DB_CONTAINER=$(docker ps --format '{{.Names}}' | grep -iE 'timescale|postgres' | head -1 || true)
    if [ -z "$DB_CONTAINER" ]; then
        DB_CONTAINER=$(docker ps --format '{{.Names}}' | grep -iE 'pgbouncer' | head -1 || true)
    fi
fi

# ---------- config
if [ -f "$DEST/config.json" ]; then
    echo "INFO: config.json already exists — kept untouched."
else
    echo "------------ Settings ------------"
    read -rp "Panel URL (e.g. https://panel.example.com:8000): " PANEL_URL
    read -rp "Panel API Key (empty = login with admin user/password): " API_KEY
    if [ -z "$API_KEY" ]; then
        read -rp "Panel admin username: " ADMIN_USER
        read -rsp "Panel admin password: " ADMIN_PASS; echo
    fi
    read -rp "Panel DB container name [${DB_CONTAINER:-empty=native mode disabled}]: " DB_INPUT
    [ -n "$DB_INPUT" ] && DB_CONTAINER="$DB_INPUT"

    # Validate: container must have psql and connect to the DB; otherwise scan candidates
    if [ -n "$DB_CONTAINER" ] && command -v docker >/dev/null; then
        db_ok() {
            docker exec "$1" sh -c 'command -v psql >/dev/null 2>&1' 2>/dev/null || return 1
            docker exec "$1" psql -U pasarguard -d pasarguard -c "SELECT 1" >/dev/null 2>&1
        }
        if ! db_ok "$DB_CONTAINER"; then
            echo "WARN: container '$DB_CONTAINER' did not answer (maybe no psql — like pgbouncer). Scanning candidates..."
            DB_CONTAINER=""
            for cand in $(docker ps --format '{{.Names}}' | grep -iE 'timescale|postgres|pgbouncer' || true); do
                if db_ok "$cand"; then DB_CONTAINER="$cand"; echo "OK: container '$cand' works — selected."; break; fi
            done
        fi
    fi
    if [ -n "$DB_CONTAINER" ]; then MODE="native"; else
        MODE="disable"
        [ -n "$DB_INPUT" ] && echo "WARN: no healthy DB container found -> installed in 'disable' mode."
    fi
    read -rp "Web panel password (empty = generate random): " WEB_PASS
    [ -z "$WEB_PASS" ] && WEB_PASS=$(python3 -c "import secrets;print(secrets.token_urlsafe(9))")
    read -rp "Web panel port [9012]: " WEB_PORT
    [ -z "$WEB_PORT" ] && WEB_PORT=9012

    python3 - "$DEST/config.json" <<PYEOF
import json, sys
cfg = {
    "panel_url": "${PANEL_URL}",
    "admin_username": "${ADMIN_USER}",
    "admin_password": "${ADMIN_PASS}",
    "api_key": "${API_KEY}",
    "mode": "${MODE}",
    "auto_reenable": True,
    "interval_seconds": 60,
    "default_coefficient": 1.0,
    "timeout_seconds": 20,
    "verify_tls": False,
    "db_container": "${DB_CONTAINER}",
    "db_user": "pasarguard",
    "db_name": "pasarguard",
    "web_password": "${WEB_PASS}",
    "web_port": int("${WEB_PORT}"),
}
json.dump(cfg, open(sys.argv[1], "w"), ensure_ascii=False, indent=2)
PYEOF
    chmod 600 "$DEST/config.json"
fi

WEB_PORT=$(python3 -c "import json;print(json.load(open('$DEST/config.json')).get('web_port',9012))")
WEB_PASS=$(python3 -c "import json;print(json.load(open('$DEST/config.json')).get('web_password',''))")

# ---------- systemd service
cat > /etc/systemd/system/${SERVICE}.service <<EOF
[Unit]
Description=PasarGuard per-user usage coefficient panel
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$DEST
ExecStart=/usr/bin/python3 $DEST/pg_panel.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now ${SERVICE}.service >/dev/null 2>&1 || systemctl restart ${SERVICE}.service
sleep 2

# ---------- test panel connection before announcing success
echo ""
echo "Testing panel connection..."
cd "$DEST"
if python3 pg_coef.py login-test >/dev/null 2>&1; then
    echo "OK: panel connection works."
else
    echo "FAILED: panel connection! Details:  cd $DEST && python3 pg_coef.py login-test"
fi

# ---------- native-mode warning
echo ""
echo "NOTE (native mode): multiplied usage is written directly into the panel database."
echo "      Take a backup of the PasarGuard database before first use."

# ---------- final summary (last output)
SERVER_IP=$(curl -s -m 5 https://api.ipify.org 2>/dev/null) || SERVER_IP=""
if [ -z "$SERVER_IP" ]; then SERVER_IP=$(curl -s -m 5 https://icanhazip.com 2>/dev/null); fi
[ -z "$SERVER_IP" ] && SERVER_IP="SERVER_IP"
PANEL_MODE=$(python3 -c "import json;print(json.load(open('$DEST/config.json')).get('mode'))")

echo ""
echo "=============================================="
echo "             INSTALLATION DONE"
echo "=============================================="
echo "  Web panel :  http://$SERVER_IP:$WEB_PORT"
echo "  Password  :  $WEB_PASS"
echo "  Mode      :  $PANEL_MODE"
echo "----------------------------------------------"
echo "  CLI commands :  cd $DEST && python3 pg_coef.py --help"
echo "  Service      :  systemctl status $SERVICE"
echo "  Update later :  cd $DEST && git pull && systemctl restart $SERVICE"
echo "=============================================="
