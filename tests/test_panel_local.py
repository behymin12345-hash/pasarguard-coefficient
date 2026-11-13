#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""تست محلی pg_panel.py: کل استک (mock panel + web panel) در پوشه موقت."""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TMP = os.path.join(HERE, "tmp_panel")
BASE = "http://127.0.0.1:8978"

FAIL = []


def check(name, cond, detail=""):
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name, (" — " + detail) if detail and not cond else ""))
    if not cond:
        FAIL.append(name)


def req(method, path, payload=None, token=None):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    r = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    with urllib.request.urlopen(r, timeout=15) as resp:
        return resp.status, json.loads(resp.read() or b"{}")


def mock_api(method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request("http://127.0.0.1:8977" + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=10) as resp:
        return json.loads(resp.read() or b"{}")


def main():
    if os.path.exists(TMP):
        shutil.rmtree(TMP)
    os.makedirs(TMP)
    for f in ("pg_coef.py", "pg_panel.py", "index.html"):
        shutil.copy(os.path.join(ROOT, f), os.path.join(TMP, f))
    with open(os.path.join(TMP, "config.json"), "w", encoding="utf-8") as f:
        json.dump({"panel_url": "http://127.0.0.1:8977", "admin_username": "admin",
                   "admin_password": "admin", "mode": "disable", "auto_reenable": True,
                   "interval_seconds": 30, "default_coefficient": 1.0,
                   "web_password": "test123", "web_port": 8978}, f)

    mock = subprocess.Popen([sys.executable, os.path.join(HERE, "mock_panel.py"), "--port", "8977", "--seed", "0"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    panel = subprocess.Popen([sys.executable, os.path.join(TMP, "pg_panel.py")],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        for _ in range(40):
            try:
                req("GET", "/api/overview")
                break
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    break
            except Exception:
                time.sleep(0.25)
        else:
            print(panel.stdout.read())
            raise SystemExit("web panel بالا نیامد")

        print("== auth ==")
        try:
            req("POST", "/api/login", {"password": "wrong"})
            check("رمز غلط رد می‌شود", False)
        except urllib.error.HTTPError as e:
            check("رمز غلط رد می‌شود", e.code == 401, str(e.code))
        try:
            req("GET", "/api/users")
            check("بدون توکن رد می‌شود", False)
        except urllib.error.HTTPError as e:
            check("بدون توکن رد می‌شود", e.code == 401)
        _, d = req("POST", "/api/login", {"password": "test123"})
        tok = d["token"]
        check("ورود موفق", bool(tok))

        print("== users ==")
        mock_api("POST", "/__test__/add_user", {"username": "ali", "data_limit_gb": 10})
        _, d = req("GET", "/api/users", token=tok)
        check("لیست کاربران می‌آید", any(u["username"] == "ali" for u in d["users"]))
        ali = [u for u in d["users"] if u["username"] == "ali"][0]
        check("ضریب پیش‌فرض ۱", ali["coefficient"] == 1)

        print("== set coefficient via panel ==")
        _, d = req("POST", "/api/coefficient", {"username": "ali", "coefficient": 2}, token=tok)
        check("ثبت ضریب ok", d.get("ok") and d.get("sync", {}).get("ok"))
        users = mock_api("GET", "/__test__/state")["users"]
        check("حجم ali دست نخورده (مود disable)", users["ali"]["data_limit"] == 10 * 1024**3)
        check("ali فعال ماند", users["ali"]["status"] == "active")

        print("== cut at threshold ==")
        mock_api("POST", "/__test__/use", {"username": "ali", "mb": 6 * 1024})  # مؤثر=12G>=10G
        req("POST", "/api/sync", {}, token=tok)
        users = mock_api("GET", "/__test__/state")["users"]
        check("ali قطع شد", users["ali"]["status"] == "disabled")
        _, d = req("GET", "/api/users", token=tok)
        ali = [u for u in d["users"] if u["username"] == "ali"][0]
        check("پرچم script_cut در UI", ali["script_cut"] is True)
        check("مصرف مؤثر دوبرابر واقعی", ali["effective_used"] == 2 * ali["used"])

        print("== reset → auto reenable ==")
        mock_api("POST", "/__test__/reset", {"username": "ali"})
        req("POST", "/api/sync", {}, token=tok)
        users = mock_api("GET", "/__test__/state")["users"]
        check("ali خودکار وصل شد", users["ali"]["status"] == "active")

        print("== settings ==")
        _, d = req("POST", "/api/settings", {"mode": "limit", "interval_seconds": 45}, token=tok)
        check("تغییر مود ok", d.get("ok"))
        _, d = req("GET", "/api/overview", token=tok)
        check("مود جدید در overview", d["mode"] == "limit" and d["interval_seconds"] == 45)
        req("POST", "/api/settings", {"mode": "disable", "interval_seconds": 30}, token=tok)

        print("== remove coefficient ==")
        _, d = req("POST", "/api/coefficient/remove", {"username": "ali"}, token=tok)
        check("حذف ok", d.get("ok"))
        _, d = req("GET", "/api/users", token=tok)
        ali = [u for u in d["users"] if u["username"] == "ali"][0]
        check("ضریب برگشت به ۱", ali["coefficient"] == 1)

        print("== index.html ==")
        with urllib.request.urlopen(BASE + "/", timeout=10) as r:
            html = r.read().decode("utf-8")
        check("صفحه UI سرو می‌شود", "X-PasarGuard" in html)

        print("== password reset via token ==")
        # توکن معتبر بساز (مثل کاری که x-pasarguard password token می‌کند)
        cfg_path = os.path.join(TMP, "config.json")
        cfg = json.load(open(cfg_path, encoding="utf-8"))
        import secrets as _s
        tok_reset = _s.token_hex(8)
        cfg["pending_password_token"] = tok_reset
        cfg["pending_password_expires"] = int(time.time()) + 900
        json.dump(cfg, open(cfg_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        try:
            req("POST", "/api/password-reset", {"token": "wrong", "new_password": "newpass1"}, token=tok)
            check("توکن غلط رد می‌شود", False)
        except urllib.error.HTTPError as e:
            check("توکن غلط رد می‌شود", e.code == 400)
        _, d = req("POST", "/api/password-reset", {"token": tok_reset, "new_password": "newpass1"}, token=tok)
        check("ریست با توکن درست ok", d.get("ok") is True)
        # پسورد قدیمی باید باطل شده باشد
        try:
            req("POST", "/api/login", {"password": "test123"})
            check("رمز قبلی باطل شد", False)
        except urllib.error.HTTPError as e:
            check("رمز قبلی باطل شد", e.code == 401)
        _, d = req("POST", "/api/login", {"password": "newpass1"})
        check("ورود با رمز جدید", bool(d.get("token")))

        print()
        if FAIL:
            print("❌ %d شکست: %s" % (len(FAIL), ", ".join(FAIL)))
            return 1
        print("✔ همه تست‌های پنل وب پاس شدند.")
        return 0
    finally:
        for p in (panel, mock):
            p.terminate()
            try:
                p.wait(timeout=5)
            except Exception:
                p.kill()
        shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
