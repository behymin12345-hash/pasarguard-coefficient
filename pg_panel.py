#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pg_panel.py — پنل وب مدیریت ضریب مصرفی کاربران پاسارگارد

یه رابط فارسی/RTL برای pg_coef.py:
  • لیست همه کاربران با مصرف واقعی و مؤثر
  • ست کردن ضریب برای هر کاربر (یا همه)
  • تغییر مود / ضریب پیش‌فرض / فاصله همگام‌سازی
  • همگام‌سازی خودکار در پس‌زمینه + دکمه دستی

اجرا:  python3 pg_panel.py   (تنظیمات از config.json کنار اسکریپت)
فقط کتابخانه استاندارد پایتون.
"""

import json
import os
import secrets
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pg_coef

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
COEFS_PATH = os.path.join(BASE_DIR, "coefficients.json")
INDEX_PATH = os.path.join(BASE_DIR, "index.html")


# ---------------------------------------------------------------- state

class State:
    def __init__(self):
        self.lock = threading.RLock()
        self.tokens = {}          # token -> issued_ts
        self.last_sync = None
        self.last_error = None
        self.sync_count = 0
        self.syncing = False
        self.reload()

    def reload(self):
        cfg = pg_coef.load_config(CONFIG_PATH)
        cfg.setdefault("web_password", "")
        cfg.setdefault("web_port", 9012)
        self.cfg = cfg
        self.store = pg_coef.CoefStore(COEFS_PATH)
        self.panel = pg_coef.Panel(
            cfg.get("panel_url"),
            admin_username=cfg.get("admin_username"),
            admin_password=cfg.get("admin_password"),
            api_key=cfg.get("api_key"),
            timeout=float(cfg.get("timeout_seconds") or 20),
            verify_tls=bool(cfg.get("verify_tls", True)),
            db_container=cfg.get("db_container") or "",
            db_user=cfg.get("db_user") or "pasarguard",
            db_name=cfg.get("db_name") or "pasarguard",
        )
        return cfg

    def save_cfg(self):
        # مقادیر web_* و بقیه را عیناً نگه دار؛ load_config کلیدهای خالی را حذف می‌کند
        raw = pg_coef.load_json(CONFIG_PATH, pg_coef.DEFAULT_CONFIG)
        raw.update({k: v for k, v in self.cfg.items() if v not in (None, "")})
        pg_coef.save_json_atomic(CONFIG_PATH, raw)


STATE = State()


def do_sync(report_only=False):
    """اجرای امن یک دور همگام‌سازی (با قفل)."""
    with STATE.lock:
        if STATE.syncing:
            return {"ok": False, "error": "همگام‌سازی قبلی هنوز در جریان است"}
        STATE.syncing = True
    try:
        with STATE.lock:
            # تغییرات خارجی (CLI یا دست‌کاری فایل) را قبل از هر دور ببین
            STATE.reload()
            rows = pg_coef.sync_once(STATE.panel, STATE.store, STATE.cfg, quiet=True,
                                     report_only=report_only)
            STATE.store.save()
            STATE.last_sync = datetime.now().strftime("%H:%M:%S")
            STATE.last_error = None
            STATE.sync_count += 1
        return {"ok": True, "users": len(rows)}
    except Exception as e:  # ApiError و هر چیز دیگر — پنل نباید بخوابد
        STATE.last_error = str(e)
        return {"ok": False, "error": str(e)}
    finally:
        STATE.syncing = False


def background_loop():
    while True:
        started = time.time()
        do_sync()
        interval = max(5, int(STATE.cfg.get("interval_seconds") or 60))
        try:
            interval = max(5, int(STATE.cfg.get("interval_seconds") or 60))
        except (TypeError, ValueError):
            interval = 60
        time.sleep(max(1, interval - (time.time() - started)))


# ---------------------------------------------------------------- helpers

def merged_users():
    """لیست کاربران پنل + ضریب/وضعیت اسکریپت — بدون تغییر چیزی."""
    with STATE.lock:
        default_k = float(STATE.cfg.get("default_coefficient") or 1.0)
        users = STATE.panel.get_users()
        out = []
        for u in users:
            username = u.get("username") or "?"
            st = STATE.store.state_of(username)
            k = STATE.store.coefficient_of(username, default_k)
            used = u.get("used_traffic") or 0
            limit = u.get("data_limit")
            native_active = st.get("native_written") is not None
            if native_active:
                # در مود native پنل مقدار ضرب‌شده را نگه می‌دارد؛ مصرف واقعی در state است
                real_used = int(st.get("real_used") or 0)
                eff = used
            elif k == 1.0:
                real_used = used
                eff = used
            else:
                real_used = used
                baseline = st.get("baseline_used")
                if st.get("coef_applied") == k and baseline is not None:
                    eff = pg_coef.effective_total(k, used, baseline)
                else:
                    eff = used  # هنوز مبنا ثبت نشده → بدون ضرب نمایش داده شود
            out.append({
                "username": username,
                "status": u.get("status", "?"),
                "coefficient": k,
                "used": real_used,
                "limit": limit,
                "effective_used": eff,
                "effective_remaining": (max(0, limit - eff) if limit else None),
                "effective_quota": (limit / k if limit else None),
                "script_cut": bool(st.get("disabled_by_script")),
                "native": native_active,
                "online_at": u.get("online_at"),
                "expire": u.get("expire"),
            })
            if k == 1.0 and username not in STATE.store.data["coefficients"]:
                STATE.store.data["state"].pop(username, None)
        return out


def overview_payload():
    with STATE.lock:
        coefs = dict(STATE.store.data["coefficients"])
        cfg = STATE.cfg
    return {
        "panel_url": cfg.get("panel_url"),
        "mode": cfg.get("mode", "disable"),
        "interval_seconds": int(cfg.get("interval_seconds") or 60),
        "default_coefficient": float(cfg.get("default_coefficient") or 1.0),
        "auto_reenable": bool(cfg.get("auto_reenable", True)),
        "last_sync": STATE.last_sync,
        "last_error": STATE.last_error,
        "syncing": STATE.syncing,
        "sync_count": STATE.sync_count,
        "coefficient_count": len(coefs),
        "coefficient_users": coefs,
        "web_password_set": bool(cfg.get("web_password")),
    }


# ---------------------------------------------------------------- handler

class Handler(BaseHTTPRequestHandler):
    server_version = "pg-panel/1.0"

    def log_message(self, fmt, *args):
        pass  # لاگ اضافه نده

    # --- پایه

    def _send(self, code, payload=None, raw=None, ctype="application/json"):
        body = raw.encode("utf-8") if raw is not None else (
            json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else b"")
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if "json" in ctype or "html" in ctype else ""))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            return {}

    def _authed(self):
        auth = self.headers.get("Authorization") or ""
        if auth.startswith("Bearer ") and auth[7:] in STATE.tokens:
            return True
        return False

    # --- مسیرها

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/" or path == "/index.html":
            try:
                with open(INDEX_PATH, "r", encoding="utf-8") as f:
                    return self._send(200, raw=f.read(), ctype="text/html")
            except OSError:
                return self._send(500, {"detail": "index.html پیدا نشد"})
        if path == "/api/overview":
            if not self._authed():
                return self._send(401, {"detail": "unauthorized"})
            return self._send(200, overview_payload())
        if path == "/api/users":
            if not self._authed():
                return self._send(401, {"detail": "unauthorized"})
            try:
                users = merged_users()
            except pg_coef.ApiError as e:
                return self._send(502, {"detail": str(e)})
            with STATE.lock:
                mode = STATE.cfg.get("mode")
            return self._send(200, {"users": users, "mode": mode})
        return self._send(404, {"detail": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        body = self._body()

        if path == "/api/login":
            password = str(body.get("password") or "").strip()
            with STATE.lock:
                real = (STATE.cfg.get("web_password") or "").strip()
            if real and secrets.compare_digest(password, real):
                token = secrets.token_hex(20)
                STATE.tokens[token] = time.time()
                return self._send(200, {"token": token})
            time.sleep(0.4)  # ضد بروت‌فورس ساده
            return self._send(401, {"detail": "رمز اشتباه است"})

        if path == "/api/password-reset":
            token = str(body.get("token") or "").strip()
            new_pass = str(body.get("new_password") or "").strip()
            if len(new_pass) < 6:
                return self._send(400, {"detail": "رمز جدید حداقل ۶ کاراکتر است"})
            fresh = pg_coef.load_json(CONFIG_PATH, pg_coef.DEFAULT_CONFIG)
            pending = str(fresh.get("pending_password_token") or "")
            expires = int(fresh.get("pending_password_expires") or 0)
            if not pending or not secrets.compare_digest(token, pending) or time.time() > expires:
                return self._send(400, {"detail": "توکن نامعتبر یا منقضی — روی سرور: x-pasarguard password token"})
            with STATE.lock:
                STATE.cfg["web_password"] = new_pass
                STATE.cfg.pop("pending_password_token", None)
                STATE.cfg.pop("pending_password_expires", None)
                STATE.save_cfg()
            STATE.tokens.clear()  # همه نشست‌ها باطل — ورود مجدد با رمز جدید
            return self._send(200, {"ok": True})

        if not self._authed():
            return self._send(401, {"detail": "unauthorized"})

        if path == "/api/logout":
            auth = (self.headers.get("Authorization") or "")[7:]
            STATE.tokens.pop(auth, None)
            return self._send(200, {"ok": True})

        if path == "/api/sync":
            r = do_sync()
            return self._send(200 if r.get("ok") else 502, r)

        if path == "/api/coefficient":
            username = str(body.get("username") or "").strip()
            if not username:
                return self._send(400, {"detail": "username لازم است"})
            try:
                k = float(body.get("coefficient"))
                if k <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                return self._send(400, {"detail": "ضریب باید عدد بزرگ‌تر از صفر باشد (مثلاً 2 یا 0.5)"})
            with STATE.lock:
                STATE.store.set(username, k)
                STATE.store.save()
            r = do_sync()  # اعمال فوری
            return self._send(200 if r.get("ok") else 502, {
                "ok": True, "username": username, "coefficient": k, "sync": r})

        if path == "/api/coefficient/remove":
            username = str(body.get("username") or "").strip()
            with STATE.lock:
                STATE.store.data["coefficients"].pop(username, None)
                STATE.store.save()
            r = do_sync()
            return self._send(200 if r.get("ok") else 502, {
                "ok": True, "username": username, "sync": r})

        if path == "/api/coefficient/remove-all":
            with STATE.lock:
                STATE.store.data["coefficients"] = {}
                STATE.store.save()
            r = do_sync()
            return self._send(200, {"ok": True, "sync": r})

        if path == "/api/settings":
            updates = {}
            if "mode" in body:
                if body["mode"] not in pg_coef.MODES:
                    return self._send(400, {"detail": "مود نامعتبر"})
                updates["mode"] = body["mode"]
            if "default_coefficient" in body:
                try:
                    k = float(body["default_coefficient"])
                    if k <= 0:
                        raise ValueError
                    updates["default_coefficient"] = k
                except (TypeError, ValueError):
                    return self._send(400, {"detail": "ضریب پیش‌فرض عدد نامعتبر"})
            if "interval_seconds" in body:
                try:
                    iv = int(body["interval_seconds"])
                    if iv < 10:
                        raise ValueError
                    updates["interval_seconds"] = iv
                except (TypeError, ValueError):
                    return self._send(400, {"detail": "فاصله همگام‌سازی حداقل ۱۰ ثانیه"})
            if "auto_reenable" in body:
                updates["auto_reenable"] = bool(body["auto_reenable"])
            if "web_password" in body:
                npass = str(body["web_password"] or "").strip()
                if len(npass) < 6:
                    return self._send(400, {"detail": "رمز پنل حداقل ۶ کاراکتر"})
                updates["web_password"] = npass
            if not updates:
                return self._send(400, {"detail": "چیزی برای تغییر داده نشد"})
            with STATE.lock:
                STATE.cfg.update(updates)
                STATE.save_cfg()
            do_sync()
            return self._send(200, {"ok": True, "updated": updates})

        return self._send(404, {"detail": "not found"})


def main():
    port = int(STATE.cfg.get("web_port") or 9012)
    if not STATE.cfg.get("web_password"):
        raise SystemExit("خطا: web_password در config.json خالی است؛ قبل از اجرا یک رمز بگذارید.")
    t = threading.Thread(target=background_loop, daemon=True)
    t.start()
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    pg_coef.log("Coefficient panel started on port %d (mode %s, sync every %ss)"
                % (port, STATE.cfg.get("mode"), STATE.cfg.get("interval_seconds")))
    srv.serve_forever()


if __name__ == "__main__":
    main()
