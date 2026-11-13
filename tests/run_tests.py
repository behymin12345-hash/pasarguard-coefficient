#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""run_tests.py — تست کامل pg_coef.py روی پنل شبیه‌سازی‌شده (بدون پنل واقعی)."""

import json
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PG = os.path.join(ROOT, "pg_coef.py")
TMP = os.path.join(HERE, "tmp")
os.makedirs(TMP, exist_ok=True)

PORT = 8977
BASE = "http://127.0.0.1:%d" % PORT
CONFIG = os.path.join(TMP, "config.json")
COEFS = os.path.join(TMP, "coefficients.json")

FAILURES = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print("  [%s] %s%s" % (status, name, (" — " + detail) if detail and not cond else ""))
    if not cond:
        FAILURES.append((name, detail))


def mock(method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read() or b"{}")


def state():
    return mock("GET", "/__test__/state")["users"]


def state_nodes():
    return mock("GET", "/__test__/state").get("nodes", {})


def pg(*args, expect_rc=0):
    cmd = [sys.executable, PG] + list(args)
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    if proc.returncode != expect_rc:
        print(proc.stdout)
        print(proc.stderr, file=sys.stderr)
        raise SystemExit("فرمان ناموفق بود: %s (rc=%d)" % (" ".join(args), proc.returncode))
    return proc.stdout


def write_config(**extra):
    cfg = {"panel_url": BASE, "admin_username": "admin", "admin_password": "admin",
           "mode": "disable", "auto_reenable": True}
    cfg.update(extra)
    with open(CONFIG, "w", encoding="utf-8") as f:
        json.dump(cfg, f)


def reset_files():
    for p in (COEFS,):
        if os.path.exists(p):
            os.remove(p)


def main():
    if os.path.exists(CONFIG):
        os.remove(CONFIG)
    reset_files()
    write_config()  # کانفیگ تستی قبل از همه فرمان‌ها آماده باشد

    proc = subprocess.Popen([sys.executable, os.path.join(HERE, "mock_panel.py"), "--port", str(PORT)],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        for _ in range(50):
            try:
                mock("GET", "/__test__/ping")
                break
            except Exception:
                time.sleep(0.1)
        else:
            raise SystemExit("mock panel بالا نیامد")

        print("== 1) ورود با یوزر/پسورد ==")
        pg("login-test", "--config", CONFIG)
        check("login با ادمین موفق", True)

        print("== 2) ورود با API Key ==")
        pg("login-test", "--config", CONFIG, "--api-key", "test-api-key")
        check("login با X-Api-Key موفق", True)

        print("== 3) ثبت ضریب ali=2 و sync (باید فعال بماند و حجم دست نخورد) ==")
        mock("POST", "/__test__/add_user", {"username": "ali", "data_limit_gb": 10})
        pg("set", "ali", "2", "--file", COEFS)
        pg("sync", "--config", CONFIG, "--file", COEFS, "--quiet")
        ali = state()["ali"]
        check("ali فعال ماند", ali["status"] == "active", ali["status"])
        check("حجم ali دست نخورد (10GB)", ali["data_limit"] == 10 * 1024**3, str(ali["data_limit"]))

        print("== 4) ali شش گیگ مصرف می‌کند (مصرف مؤثر 12 >= 10 → قطع) ==")
        mock("POST", "/__test__/use", {"username": "ali", "mb": 6 * 1024})
        pg("sync", "--config", CONFIG, "--file", COEFS, "--quiet")
        ali = state()["ali"]
        check("ali قطع شد", ali["status"] == "disabled", ali["status"])
        check("حجم ali هنوز دست نخورده", ali["data_limit"] == 10 * 1024**3)
        check("state: disabled_by_script=True",
              json.load(open(COEFS, encoding="utf-8"))["state"]["ali"]["disabled_by_script"] is True)

        print("== 5) ریست مصرف ali → باید خودکار وصل شود ==")
        mock("POST", "/__test__/reset", {"username": "ali"})
        pg("sync", "--config", CONFIG, "--file", COEFS, "--quiet")
        ali = state()["ali"]
        check("ali دوباره وصل شد", ali["status"] == "active", ali["status"])
        check("state: disabled_by_script=False",
              json.load(open(COEFS, encoding="utf-8"))["state"]["ali"]["disabled_by_script"] is False)

        print("== 6) reza با ضریب 0.5 (بخشندگی) → حجم به 20GB بالا می‌رود ==")
        mock("POST", "/__test__/add_user", {"username": "reza", "data_limit_gb": 10})
        pg("set", "reza", "0.5", "--file", COEFS)
        pg("sync", "--config", CONFIG, "--file", COEFS, "--quiet")
        reza = state()["reza"]
        check("حجم reza = 20GB (10 ÷ 0.5)", reza["data_limit"] == 20 * 1024**3, str(reza["data_limit"]))
        check("reza فعال", reza["status"] == "active")

        print("== 7) مود limit برای sara: حجم پویا کم می‌شود و پنل خودش قطع می‌کند ==")
        mock("POST", "/__test__/add_user", {"username": "sara", "data_limit_gb": 10})
        write_config(mode="limit")
        pg("set", "sara", "2", "--file", COEFS)
        pg("sync", "--config", CONFIG, "--file", COEFS, "--quiet")  # مبنا ثبت می‌شود (مصرف صفر)
        mock("POST", "/__test__/use", {"username": "sara", "mb": 3 * 1024})
        pg("sync", "--config", CONFIG, "--file", COEFS, "--quiet")
        sara = state()["sara"]
        check("حجم مؤثر sara = 7GB (10 - 1×3)", sara["data_limit"] == 7 * 1024**3, str(sara["data_limit"]))
        check("sara هنوز فعال", sara["status"] == "active")

        mock("POST", "/__test__/use", {"username": "sara", "mb": 4 * 1024})  # used=7GB >= 7GB → limited توسط پنل
        pg("sync", "--config", CONFIG, "--file", COEFS, "--quiet")
        sara = state()["sara"]
        check("sara limited شد (پنل native)", sara["status"] == "limited", sara["status"])
        check("سقف sara روی نقطه عبور (7GB) قفل شد — clamp",
              sara["data_limit"] == 7 * 1024**3, str(sara["data_limit"]))

        print("== 8) حذف ضریب sara → حجم پایه 10GB برمی‌گردد و وضعیت درست می‌شود ==")
        pg("remove", "sara", "--file", COEFS)
        pg("sync", "--config", CONFIG, "--file", COEFS, "--quiet")
        sara = state()["sara"]
        check("حجم sara به 10GB برگشت", sara["data_limit"] == 10 * 1024**3, str(sara["data_limit"]))
        check("وضعیت sara active شد (مصرف 7 < 10)", sara["status"] == "active", sara["status"])

        print("== 9) حذف ضریب ali در حالت قطع‌شده → باید وصل شود ==")
        write_config(mode="disable")  # تست ۹ در مود disable
        mock("POST", "/__test__/use", {"username": "ali", "mb": 6 * 1024})
        pg("sync", "--config", CONFIG, "--file", COEFS, "--quiet")
        check("ali دوباره قطع شد", state()["ali"]["status"] == "disabled")
        pg("remove", "ali", "--file", COEFS)
        pg("sync", "--config", CONFIG, "--file", COEFS, "--quiet")
        ali = state()["ali"]
        check("ali بعد از حذف ضریب وصل شد", ali["status"] == "active", ali["status"])
        check("حجم ali همچنان 10GB", ali["data_limit"] == 10 * 1024**3)

        print("== 10) reza با ریست مصرف؛ حجم بالا می‌ماند ==")
        mock("POST", "/__test__/use", {"username": "reza", "mb": 19 * 1024})
        pg("sync", "--config", CONFIG, "--file", COEFS, "--quiet")
        reza = state()["reza"]
        check("reza فعال (19GB < 20GB)", reza["status"] == "active", reza["status"])
        mock("POST", "/__test__/reset", {"username": "reza"})
        pg("sync", "--config", CONFIG, "--file", COEFS, "--quiet")
        reza = state()["reza"]
        check("بعد از ریست هم حجم reza = 20GB", reza["data_limit"] == 20 * 1024**3, str(reza["data_limit"]))
        check("reza فعال", reza["status"] == "active")

        print("== 11) ضریب پیش‌فرض ==")
        write_config(mode="disable")
        pg("set-default", "1.5", "--config", CONFIG)
        out = pg("report", "--config", CONFIG, "--file", COEFS)
        check("گزارش شامل ali با ضریب 1.5 (پیش‌فرض)", "ali" in out and "1.5" in out, "")
        pg("set-default", "1", "--config", CONFIG)

        print("== 12) report بدون تغییر پنل ==")
        before = json.dumps(state(), sort_keys=True)
        pg("report", "--config", CONFIG, "--file", COEFS)
        check("گزارش چیزی را تغییر نداد", json.dumps(state(), sort_keys=True) == before)

        print("== 13) ضریب native نود (node-set / node-list) ==")
        mock("POST", "/__test__/add_node", {"id": 1, "name": "de-node", "usage_coefficient": 1})
        pg("node-set", "de-node", "3", "--config", CONFIG)
        check("ضریب نود شد 3", state_nodes()["1"]["usage_coefficient"] == 3.0)
        out = pg("node-list", "--config", CONFIG)
        check("node-list ضریب را نشان می‌دهد", "de-node" in out and "3" in out)

        print("== 14) مبنا: ضریب فقط روی مصرفِ بعد از ثبت اعمال شود ==")
        mock("POST", "/__test__/add_user", {"username": "basel", "data_limit_gb": 10})
        mock("POST", "/__test__/use", {"username": "basel", "mb": 5 * 1024})  # ۵ گیگ مصرف قبلی
        pg("set", "basel", "2", "--file", COEFS)
        pg("sync", "--config", CONFIG, "--file", COEFS, "--quiet")
        basel = state()["basel"]
        check("با مصرف قبلی ۵ گیگ، کاربر قطع نمی‌شود (مبنا)", basel["status"] == "active", basel["status"])
        mock("POST", "/__test__/use", {"username": "basel", "mb": 2 * 1024})  # +۲ گیگ بعد از ضریب
        pg("sync", "--config", CONFIG, "--file", COEFS, "--quiet")
        basel = state()["basel"]
        check("حالا هم فعال (۵ + ۲×۲ = ۹ < ۱۰)", basel["status"] == "active", basel["status"])
        mock("POST", "/__test__/use", {"username": "basel", "mb": 2 * 1024})  # +۲ گیگ دیگر → مؤثر ۱۳ ≥ ۱۰
        pg("sync", "--config", CONFIG, "--file", COEFS, "--quiet")
        basel = state()["basel"]
        check("قطع دقیقاً سر ضریبِ مصرفِ جدید", basel["status"] == "disabled", basel["status"])

        print()
        if FAILURES:
            print("❌ %d تست شکست خورد:" % len(FAILURES))
            for name, detail in FAILURES:
                print("   - %s %s" % (name, detail))
            return 1
        print("✔ همه تست‌ها پاس شدند.")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
