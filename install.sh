#!/usr/bin/env bash
# ============================================================
#  نصب پنل ضریب مصرفی برای پنل پاسارگارد (PasarGuard)
#  استفاده:
#     bash install.sh
#  یا نصب یک‌خطی از گیت‌هاب:
#     bash <(curl -sL https://raw.githubusercontent.com/behymin12345-hash/pasarguard-coefficient/main/install.sh)
# ============================================================
set -e

REPO_RAW="https://raw.githubusercontent.com/behymin12345-hash/pasarguard-coefficient/main"
DEST="/opt/pg-coef"
SERVICE="pg-coef"

echo "=============================================="
echo "  پنل ضریب مصرفی پاسارگارد — نصب‌کننده"
echo "=============================================="

# ---------- پیش‌نیازها
if [ "$(id -u)" -ne 0 ]; then echo "❌ با root اجرا کنید (sudo bash install.sh)"; exit 1; fi
command -v python3 >/dev/null || { echo "❌ python3 نصب نیست"; exit 1; }
command -v curl >/dev/null || { echo "❌ curl نصب نیست (apt install curl)"; exit 1; }

# ---------- دانلود فایل‌ها
mkdir -p "$DEST"
echo "⬇ دانلود فایل‌ها به $DEST ..."
for f in pg_coef.py pg_panel.py index.html; do
    curl -sL "$REPO_RAW/$f" -o "$DEST/$f"
    [ -s "$DEST/$f" ] || { echo "❌ دانلود $f ناموفق بود"; exit 1; }
done
chmod 600 "$DEST"/*.py 2>/dev/null || true

# ---------- تشخیص کانتینر دیتابیس (برای مود native)
DB_CONTAINER=""
if command -v docker >/dev/null; then
    DB_CONTAINER=$(docker ps --format '{{.Names}}' | grep -iE 'timescale|postgres|pgbouncer' | head -1 || true)
fi

# ---------- کانفیگ
if [ -f "$DEST/config.json" ]; then
    echo "ℹ config.json از قبل موجود است — دست‌نخورده ماند."
else
    echo "------------ تنظیمات ------------"
    read -rp "آدرس پنل (مثلاً https://panel.example.com:8000): " PANEL_URL
    read -rp "API Key پنل (خالی = احراز با یوزر/پسورد): " API_KEY
    if [ -z "$API_KEY" ]; then
        read -rp "یوزر ادمین پنل: " ADMIN_USER
        read -rsp "پسورد ادمین پنل: " ADMIN_PASS; echo
    fi
    read -rp "نام کانتینر دیتابیس پنل [${DB_CONTAINER:-خالی=مود native غیرفعال}]: " DB_INPUT
    [ -n "$DB_INPUT" ] && DB_CONTAINER="$DB_INPUT"
    if [ -n "$DB_CONTAINER" ]; then MODE="native"; else MODE="disable"; fi
    read -rp "رمز ورود پنل ضریب (خالی = تولید تصادفی): " WEB_PASS
    [ -z "$WEB_PASS" ] && WEB_PASS=$(python3 -c "import secrets;print(secrets.token_urlsafe(9))")
    read -rp "پورت پنل ضریب [9012]: " WEB_PORT
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

# ---------- سرویس systemd
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

echo ""
echo "=============================================="
echo "✅ نصب کامل شد"
echo "   پنل وب:  http://$(curl -s -m 5 ifconfig.me 2>/dev/null || echo 'SERVER_IP'):$WEB_PORT"
echo "   رمز:     $WEB_PASS"
echo "   مود:     $(python3 -c "import json;print(json.load(open('$DEST/config.json')).get('mode'))")"
echo ""
echo "   دستورات CLI:"
echo "     cd $DEST && python3 pg_coef.py --help"
echo "   وضعیت سرویس:"
echo "     systemctl status $SERVICE"
echo "=============================================="
echo ""
echo "⚠ نکته مود native: مصرف ضریبی مستقیم در دیتابیس پنل نوشته می‌شود؛"
echo "   قبل از استفاده اولیه از پنل یک بکاپ از دیتابیس پاسارگارد بگیرید."
