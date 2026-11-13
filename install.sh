#!/usr/bin/env bash
# ============================================================
#  نصب پنل ضریب مصرفی برای پنل پاسارگارد (PasarGuard)
#  استفاده:
#     bash install.sh
#  یا نصب از گیت‌هاب (روی سرور):
#     curl -sL https://raw.githubusercontent.com/behymin12345-hash/pasarguard-coefficient/main/install.sh -o install.sh && sudo bash install.sh
# ============================================================
set -e

REPO_RAW="https://raw.githubusercontent.com/behymin12345-hash/pasarguard-coefficient/main"
DEST="/opt/pg-coef"
SERVICE="pg-coef"

# اگر از پایپ اجرا شده (curl ... | bash) سوال‌های تعاملی از ترمینال گرفته شوند
if [ ! -t 0 ]; then
    exec < /dev/tty 2>/dev/null || true
fi

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
# توجه: pgbouncer معمولاً psql ندارد؛ اول کانتینر خود دیتابیس (timescale/postgres)
DB_CONTAINER=""
if command -v docker >/dev/null; then
    DB_CONTAINER=$(docker ps --format '{{.Names}}' | grep -iE 'timescale|postgres' | head -1 || true)
    if [ -z "$DB_CONTAINER" ]; then
        DB_CONTAINER=$(docker ps --format '{{.Names}}' | grep -iE 'pgbouncer' | head -1 || true)
    fi
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

    # اعتبارسنجی: کانتینر باید psql داشته باشد و به دیتابیس وصل شود؛ وگرنه بین کاندیدها بگرد
    if [ -n "$DB_CONTAINER" ] && command -v docker >/dev/null; then
        db_ok() {
            docker exec "$1" sh -c 'command -v psql >/dev/null 2>&1' 2>/dev/null || return 1
            docker exec "$1" psql -U pasarguard -d pasarguard -c "SELECT 1" >/dev/null 2>&1
        }
        if ! db_ok "$DB_CONTAINER"; then
            echo "⚠ کانتینر «$DB_CONTAINER» پاسخ نداد (شاید psql ندارد — مثل pgbouncer). بین کاندیدها می‌گردم..."
            DB_CONTAINER=""
            for cand in $(docker ps --format '{{.Names}}' | grep -iE 'timescale|postgres|pgbouncer' || true); do
                if db_ok "$cand"; then DB_CONTAINER="$cand"; echo "✔ کانتینر «$cand» سالم است — انتخاب شد."; break; fi
            done
        fi
    fi
    if [ -n "$DB_CONTAINER" ]; then MODE="native"; else
        MODE="disable"
        [ -n "$DB_INPUT" ] && echo "⚠ هیچ کانتینر دیتابیس سالمی پیدا نشد → مود disable نصب شد."
    fi
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

# ---------- تست اتصال به پنل قبل از اعلام موفقیت
echo ""
echo "🔎 تست اتصال به پنل..."
cd "$DEST"
if python3 pg_coef.py login-test >/dev/null 2>&1; then
    echo "✔ اتصال به پنل پاسارگارد سالم است."
else
    echo "❌ اتصال به پنل ناموفق! با این فرمان جزئیات را ببینید:  cd $DEST && python3 pg_coef.py login-test"
fi

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
SERVER_IP=$(curl -s -m 5 https://api.ipify.org 2>/dev/null) || SERVER_IP=""
[ -z "$SERVER_IP" ] && SERVER_IP=$(curl -s -m 5 https://icanhazip.com 2>/dev/null) || SERVER_IP="$SERVER_IP"
[ -z "$SERVER_IP" ] && SERVER_IP="آیپی-سرور"
echo "✅ نصب کامل شد"
echo "   پنل وب:  http://$SERVER_IP:$WEB_PORT"
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
