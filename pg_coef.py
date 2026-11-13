#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pg_coef.py — مدیریت ضریب مصرفی برای هر کاربر در پنل پاسارگارد (PasarGuard)

پنل پاسارگارد فقط «ضریب مصرف» per-node دارد؛ این اسکریپت ضریب مصرف per-user را
از طریق API پنل شبیه‌سازی می‌کند:

  مصرف مؤثر = ضریب × مصرف واقعی

حجم کاربر (data_limit) در مود پیش‌فرض دست‌نخورده می‌ماند و فقط وقتی
«مصرف مؤثر» از حجم گذشت، کاربر توسط اسکریپت قطع می‌شود (مود disable).
مود limit هم هست که پنل خودش قطع می‌کند ولی حجم نمایشی تغییر می‌کند.

پیش‌نیاز: Python 3.8+ (فقط کتابخانه استاندارد؛ هیچ pip install لازم نیست)

مستندات کامل: README.md
"""

import argparse
import json
import os
import secrets
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCHEMA_VERSION = 1
MODES = ("native", "disable", "limit", "report")
PAGE_SIZE = 100
VERSION = "1.1.0"
REPO_RAW = "https://raw.githubusercontent.com/behymin12345-hash/pasarguard-coefficient/main"
SERVICE_NAME = "pg-coef"

# وضعیت‌هایی که اجازه داریم رویشان کار کنیم؛ بقیه (expired / on_hold / inactive ...) رد می‌شوند
WORKABLE_STATUSES = ("active", "disabled", "limited")

DEFAULT_CONFIG = {
    "panel_url": "",
    "admin_username": "",
    "admin_password": "",
    "api_key": "",
    "mode": "native",            # native | disable | limit | report
    "auto_reenable": True,       # اگر اسکریپت قطع کرده و مصرف زیر حد آمد، دوباره وصل کند؟
    "interval_seconds": 60,      # فاصله هر دور همگام‌سازی در مود run
    "default_coefficient": 1.0,  # ضریب پیش‌فرض همه کاربران (۱ یعنی غیرفعال)
    "timeout_seconds": 20,
    "verify_tls": True,
    # فقط برای مود native (نوشتن مستقیم مصرف ضریبی در دیتابیس پنل):
    "db_container": "",          # نام کانتینر دیتابیس، مثل pasarguard-timescaledb-1
    "db_user": "pasarguard",
    "db_name": "pasarguard",
}

DEFAULT_COEF_FILE = {
    "version": SCHEMA_VERSION,
    "coefficients": {},   # {"username": 2.0}
    "state": {},          # {"username": {"disabled_by_script":..., "last_used":..., "written_limit":..., "volume_base":...}}
}


# ---------------------------------------------------------------- helpers

def force_utf8_console():
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream.encoding and stream.encoding.lower().replace("-", "") != "utf8":
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def now_str():
    return datetime.now().strftime("%H:%M:%S")


def log(msg, quiet=False):
    if not quiet:
        print("[%s] %s" % (now_str(), msg), flush=True)


def human_size(n):
    if n is None:
        return "—"
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "—"
    if n < 1024:
        return "%d B" % int(n)
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024.0
        if n < 1024 or unit == "TB":
            return ("%.2f %s" % (n, unit)).rstrip("0").rstrip(".") if n < 100 else "%.1f %s" % (n, unit)
    return str(n)


def parse_float(text, what):
    try:
        v = float(str(text).strip())
    except ValueError:
        raise SystemExit("Error: %s must be a number (e.g. 2 or 1.5), got: %s" % (what, text))
    if v <= 0:
        raise SystemExit("Error: %s must be greater than zero (coefficient 1 means no multiplier)" % what)
    return v


def load_json(path, default):
    if not os.path.exists(path):
        return json.loads(json.dumps(default))
    with open(path, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except ValueError as e:
            raise SystemExit("Error: file %s is corrupt (invalid JSON): %s" % (path, e))


def save_json_atomic(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


class CoefStore:
    """ذخیره ضریب‌ها و وضعیت هر کاربر در یک فایل JSON کنار اسکریپت."""

    def __init__(self, path):
        self.path = path
        self.data = load_json(path, DEFAULT_COEF_FILE)
        if not isinstance(self.data.get("coefficients"), dict):
            self.data["coefficients"] = {}
        if not isinstance(self.data.get("state"), dict):
            self.data["state"] = {}
        self.data["version"] = SCHEMA_VERSION

    def coefficient_of(self, username, default=1.0):
        v = self.data["coefficients"].get(username)
        return default if v is None else float(v)

    def set(self, username, k):
        if k == 1.0:
            self.data["coefficients"].pop(username, None)
        else:
            self.data["coefficients"][username] = k

    def state_of(self, username):
        return self.data["state"].setdefault(
            username,
            {"disabled_by_script": False, "last_used": None, "written_limit": None,
             "volume_base": None, "coef_applied": None, "baseline_used": None,
             "real_used": None, "native_written": None},
        )

    def drop_state(self, username):
        self.data["state"].pop(username, None)

    def save(self):
        save_json_atomic(self.path, self.data)


# ---------------------------------------------------------------- panel API

class ApiError(Exception):
    pass


class Panel:
    """کلاینت API پاسارگارد (سازگار با Marzban) — فقط کتابخانه استاندارد."""

    def __init__(self, base_url, admin_username="", admin_password="", api_key="",
                 timeout=20, verify_tls=True, db_container="", db_user="pasarguard",
                 db_name="pasarguard"):
        base = (base_url or "").strip().rstrip("/")
        if not base:
            raise SystemExit("Error: panel_url is empty in config.json. Set the panel URL (e.g. https://panel.example.com)")
        if not base.lower().startswith(("http://", "https://")):
            base = "https://" + base
        self.base = base
        self.admin_username = (admin_username or "").strip()
        self.admin_password = admin_password or ""
        self.api_key = (api_key or "").strip()
        self.timeout = timeout
        if verify_tls:
            self.ssl_ctx = ssl.create_default_context()
        else:
            self.ssl_ctx = ssl.create_default_context()
            self.ssl_ctx.check_hostname = False
            self.ssl_ctx.verify_mode = ssl.CERT_NONE
        self.token = None
        self.db_container = (db_container or "").strip()
        self.db_user = (db_user or "pasarguard").strip()
        self.db_name = (db_name or "pasarguard").strip()

    # --- مود native: نوشتن مستقیم در دیتابیس پنل

    def set_used_traffic(self, username, value):
        """نوشتن مطلق users.used_traffic (مصرف انباشتی است و پنل دلتای واقعی را رویش اضافه می‌کند)."""
        if not self.db_container:
            raise ApiError("native mode requires db_container in config.json (the panel DB container name)")
        safe_user = username.replace("'", "''")
        sql = "UPDATE users SET used_traffic = %d WHERE username = '%s';" % (int(value), safe_user)
        try:
            proc = subprocess.run(
                ["docker", "exec", self.db_container, "psql", "-U", self.db_user, "-d", self.db_name, "-c", sql],
                capture_output=True, timeout=15)
        except FileNotFoundError:
            raise ApiError("docker is not available on this server; native mode needs DB container access")
        except subprocess.TimeoutExpired:
            raise ApiError("timeout writing to panel database")
        if proc.returncode != 0:
            raise ApiError("panel database error: %s" % proc.stderr.decode("utf-8", "replace")[:200])

    # --- پایه

    def _request(self, method, path, payload=None, form=None, retry_auth=True, _transient_tries=2):
        headers = {"Accept": "application/json", "User-Agent": "pg_coef/1.0"}
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload).encode("utf-8")
        if form is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            body = urllib.parse.urlencode(form).encode("utf-8")
        if self.api_key:
            headers["X-Api-Key"] = self.api_key
        elif self.token:
            headers["Authorization"] = "Bearer " + self.token
        req = urllib.request.Request(self.base + path, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self.ssl_ctx) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            if e.code == 401 and retry_auth and not self.api_key:
                self.token = None
                self.ensure_token()
                return self._request(method, path, payload=payload, form=form, retry_auth=False)
            # خطاهای گذرای پنل (شلوغی/ری‌استارت لحظه‌ای) → تلاش مجدد با فاصله
            if e.code in (502, 503, 504) and _transient_tries > 0:
                time.sleep(1.5 if _transient_tries == 2 else 3.0)
                return self._request(method, path, payload=payload, form=form,
                                     retry_auth=retry_auth, _transient_tries=_transient_tries - 1)
            raise ApiError("HTTP %s from %s%s" % (e.code, path, (" — " + detail) if detail else ""))
        except urllib.error.URLError as e:
            if _transient_tries > 0:
                time.sleep(1.5 if _transient_tries == 2 else 3.0)
                return self._request(method, path, payload=payload, form=form,
                                     retry_auth=retry_auth, _transient_tries=_transient_tries - 1)
            raise ApiError("Cannot reach panel (%s): %s" % (self.base, getattr(e, "reason", e)))
        except OSError as e:
            raise ApiError("Network error connecting to panel: %s" % e)
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            return {"_raw": raw.decode("utf-8", "replace")}

    def ensure_token(self):
        if self.api_key:
            return
        if self.token:
            return
        if not self.admin_username or not self.admin_password:
            raise SystemExit(
                "خطا: احراز هویت تنظیم نشده. یا admin_username/admin_password را در config.json پر کنید "
                "یا api_key بسازید (داخل پنل: تنظیمات → API Keys) و در config.json بگذارید."
            )
        resp = self._request("POST", "/api/admin/token",
                             form={"username": self.admin_username, "password": self.admin_password},
                             retry_auth=False)
        token = resp.get("access_token")
        if not token:
            raise ApiError("Panel login failed; response: %s" % json.dumps(resp, ensure_ascii=False)[:200])
        self.token = token

    # --- کاربران

    def get_users(self):
        self.ensure_token()
        users, offset = [], 0
        while True:
            resp = self._request("GET", "/api/users?offset=%d&limit=%d" % (offset, PAGE_SIZE))
            items = resp.get("users") if isinstance(resp, dict) else resp
            if items is None:
                items = []
            users.extend(items)
            total = resp.get("total") if isinstance(resp, dict) else None
            if not items or (total is not None and len(users) >= total) or len(items) < PAGE_SIZE:
                break
            offset += PAGE_SIZE
        return users

    def get_user(self, username):
        self.ensure_token()
        return self._request("GET", "/api/user/" + urllib.parse.quote(username, safe=""))

    def modify_user(self, username, patch):
        self.ensure_token()
        return self._request("PUT", "/api/user/" + urllib.parse.quote(username, safe=""), payload=patch)

    def set_disabled(self, username, disabled):
        self.ensure_token()
        return self._request(
            "PUT",
            "/api/user/%s/disabled" % urllib.parse.quote(username, safe=""),
            payload={"disabled": bool(disabled)},
        )

    # --- نودها (ضریب native خود پنل)

    def get_nodes(self):
        self.ensure_token()
        try:
            resp = self._request("GET", "/api/nodes")
        except ApiError as e:
            if "404" in str(e):
                return []  # نسخه‌های قدیمی‌تر بدون endpoint نود
            raise
        if isinstance(resp, dict):
            return resp.get("nodes") or []
        if isinstance(resp, list):
            return resp
        return []

    def modify_node(self, node_id, patch):
        self.ensure_token()
        return self._request("PUT", "/api/node/%s" % node_id, payload=patch)


# ---------------------------------------------------------------- منطق ضریب

def effective_total(k, real, baseline):
    """مصرف مؤثر با مبنا: فقط مصرفِ بعد از ثبت ضریب ضرب می‌شود."""
    if baseline is None or real < baseline:
        baseline = real
    return int(baseline + (real - baseline) * k)


def row_for_user(u, k, st, mode):
    """ساخت یک ردیف گزارش برای کاربر."""
    used = u.get("used_traffic") or 0
    limit = u.get("data_limit")
    baseline = st.get("baseline_used")
    if k == 1.0 or baseline is None or st.get("coef_applied") != k:
        eff = used if k == 1.0 else k * used
    else:
        eff = effective_total(k, used, baseline)
    if limit:
        eff_remaining = max(0, limit - eff)
        quota_text = human_size(limit / k)
    else:
        eff_remaining = None
        quota_text = "Unlimited"
    return {
        "username": u.get("username", "?"),
        "status": u.get("status", "?"),
        "k": k,
        "used": used,
        "limit": limit,
        "effective_used": eff,
        "effective_remaining": eff_remaining,
        "effective_quota": quota_text,
        "script_cut": bool(st.get("disabled_by_script")),
        "mode_used": mode,
    }


def sync_once(panel, store, cfg, quiet=False, report_only=False):
    """یک دور همگام‌سازی روی همه کاربران پنل. خروجی: ردیف‌های گزارش."""
    mode = (cfg.get("mode") or "disable").lower()
    if mode not in MODES:
        raise SystemExit("Error: config mode must be one of %s (current: %s)" % ("/".join(MODES), mode))
    default_k = float(cfg.get("default_coefficient") or 1.0)
    auto_reenable = bool(cfg.get("auto_reenable", True))

    users = panel.get_users()
    rows = []

    for u in users:
        username = u.get("username")
        if not username:
            continue
        k = store.coefficient_of(username, default_k)
        st = store.state_of(username)
        status = (u.get("status") or "").lower()
        used = u.get("used_traffic") or 0
        limit = u.get("data_limit")

        # اگر کاربر دیگر در پنل وضعیت قابل‌کار ندارد، فلگ قطع اسکریپت را پاک کن
        if status not in WORKABLE_STATUSES:
            if st.get("disabled_by_script"):
                st["disabled_by_script"] = False
            continue

        if k == 1.0:
            # بدون ضریب — برگرداندن تغییرات اسکریپت و پاک‌سازی فلگ‌های کهنه
            native_written = st.get("native_written")
            if native_written is not None and not report_only:
                real_used = int(st.get("real_used") or 0)
                if real_used != used:
                    panel.set_used_traffic(username, real_used)
                    log("RESTORE: real usage of %s (%s) written back to panel." % (username, human_size(real_used)), quiet)
                base = st.get("volume_base")
                if base:
                    # ارسال همان حجم → پنل وضعیت را دوباره ارزیابی و در صورت نیاز فعال می‌کند
                    panel.modify_user(username, {"data_limit": int(base)})
                st["native_written"] = None
                st["real_used"] = None
            if st.get("disabled_by_script"):
                if status == "disabled" and not report_only:
                    panel.set_disabled(username, False)
                    log("ON: user %s re-enabled after coefficient removal." % username, quiet)
                st["disabled_by_script"] = False
            written = st.get("written_limit")
            base = st.get("volume_base")
            native_written = st.get("native_written")
            real = st.get("real_used")
            if native_written is not None and not report_only:
                # مود native بوده → مصرف واقعی به پنل برگردد
                if real is not None and used != real:
                    panel.set_used_traffic(username, real)
                    log("RESTORE: real usage of %s (%s) written back to panel." % (username, human_size(real)), quiet)
                if base:
                    panel.modify_user(username, {"data_limit": int(base)})
                st["native_written"] = None
                st["real_used"] = None
            if written is not None:
                if base is not None and limit == written and base != limit and not report_only:
                    # آخرین حجم‌نویسی مال اسکریپت بوده → حجم پایه را برگردان
                    panel.modify_user(username, {"data_limit": int(base)})
                    log("RESTORE: volume of %s restored to base %s." % (username, human_size(base)), quiet)
                st["written_limit"] = None
                st["volume_base"] = None
            st["coef_applied"] = None
            st["baseline_used"] = None
            continue

        if not limit:
            log("WARN: user %s has unlimited volume; coefficient %.2f applies to reports only." % (username, k), quiet)
            rows.append(row_for_user(u, k, st, "report-only"))
            st["last_used"] = used
            continue

        if mode == "native":
            chosen = "native"
        elif k < 1:
            # ضریب کمتر از ۱ یعنی به کاربر حجم بیشتری می‌رسد؛ پنل خودش در حجم واقعی قطع می‌کند
            # پس باید حجم را به V/k بالا ببریم (حجم کم نمی‌شود، بیشتر می‌شود)
            chosen = "limit"
            if not report_only:
                log("INFO: user %s has coefficient %.2f (<1) -> handled via the limit mechanism." % (username, k), quiet)
        else:
            chosen = mode if mode in ("disable", "limit") else "disable"

        if report_only or mode == "report":
            rows.append(row_for_user(u, k, st, "report"))
            st["last_used"] = used
            continue

        if chosen == "native":
            # مود native: مصرف ضریبی مستقیم در پاسارگارد نوشته می‌شود (نمایش و قطع، هر دو خود پنل)
            base = st.get("volume_base")
            written = st.get("written_limit")
            if written is not None and base is not None and limit == written and int(base) != limit:
                # از مود limit آمده‌ایم و حجم پایه هنوز برگردانده نشده
                panel.modify_user(username, {"data_limit": int(base)})
                limit = int(base)
                log("RESTORE: base volume of %s (%s) written back." % (username, human_size(base)), quiet)
            st["written_limit"] = None
            if base is None or (limit is not None and int(limit) != int(base)):
                base = limit  # اولین بار یا تغییر دستی حجم توسط ادمین → مبنای جدید

            prev_written = st.get("native_written")
            real = st.get("real_used")
            if prev_written is None or real is None:
                real = used  # اولین sync: مصرف فعلی را واقعی فرض کن
            else:
                delta = used - prev_written
                real = int(real + delta) if delta >= 0 else used  # ریست مصرف

            if st.get("coef_applied") != k:
                # ضریب تازه ثبت/تغییر کرده → از همین لحظه: گذشته ضرب نمی‌شود
                baseline = real
                if used != real:
                    panel.set_used_traffic(username, real)  # پاک کردن ضربِ قبلی
                    used = real
                if status == "limited" and limit:
                    panel.modify_user(username, {"data_limit": int(limit)})  # ارزیابی دوباره وضعیت
            else:
                baseline = st.get("baseline_used")
                if baseline is None or real < baseline:
                    baseline = real  # ریست مصرف → مبنا از نو

            if k >= 1:
                inflated = int(baseline + (real - baseline) * k)
            else:
                inflated = int(round(k * real))
            if inflated != used:
                panel.set_used_traffic(username, inflated)
                log("SET: panel usage of %s = %s (baseline %s + new usage x%.2f)"
                    % (username, human_size(inflated), human_size(baseline), k), quiet)
            if status == "limited" and limit and inflated < limit:
                panel.modify_user(username, {"data_limit": int(limit)})  # ریست مصرف → فعال دوباره
            st["native_written"] = inflated
            st["real_used"] = real
            st["baseline_used"] = baseline
            st["coef_applied"] = k
            st["volume_base"] = base
            st["last_used"] = used
            rows.append({
                "username": username, "status": status, "k": k,
                "used": real, "limit": limit,
                "effective_used": inflated,
                "effective_remaining": (max(0, limit - inflated) if limit else None),
                "effective_quota": (limit / k if limit else None),
                "script_cut": False, "mode_used": "native",
            })
            continue

        if chosen == "disable":
            # مود disable: حجم دست‌نخورده؛ فقط قطع/وصل با endpoint مخصوص
            if st.get("coef_applied") != k:
                baseline = used  # ضریب تازه ثبت/تغییر کرده → مبنا = مصرف فعلی
            else:
                baseline = st.get("baseline_used")
                if baseline is None or used < baseline:
                    baseline = used  # ریست مصرف
            eff = int(baseline + (used - baseline) * k)
            if eff >= limit:
                if status == "active":
                    panel.set_disabled(username, True)
                    st["disabled_by_script"] = True
                    log("CUT: user %s disabled (effective %s >= volume %s, coefficient %.2f)"
                        % (username, human_size(eff), human_size(limit), k), quiet)
                elif status == "limited":
                    log("INFO: user %s is already limited by the panel; nothing to do." % username, quiet)
            else:
                if st.get("disabled_by_script") and status == "disabled" and auto_reenable:
                    panel.set_disabled(username, False)
                    st["disabled_by_script"] = False
                    log("ON: user %s re-enabled (effective %s < volume %s)"
                        % (username, human_size(eff), human_size(limit)), quiet)
                elif status != "disabled":
                    # فلگ قطع کهنه است ولی کاربر قطع نیست (مثلاً ادمین دستی وصلش کرده)
                    st["disabled_by_script"] = False
            st["coef_applied"] = k
            st["baseline_used"] = baseline
        else:
            # مود limit: data_limit طوری نوشته می‌شود که پنل خودش دقیقاً سر ضریب قطع کند
            base = st.get("volume_base")
            written = st.get("written_limit")
            if written is not None and limit != written:
                base = limit  # ادمین دستی حجم را عوض کرده → مبنای جدید
                log("INFO: volume of %s changed manually (%s); adopting new base." % (username, human_size(limit)), quiet)
            if base is None:
                base = limit
            if st.get("coef_applied") != k:
                baseline = used  # ضریب تازه ثبت/تغییر کرده → مبنا = مصرف فعلی
            else:
                baseline = st.get("baseline_used")
                if baseline is None or used < baseline:
                    baseline = used  # ریست مصرف
            if k < 1:
                target = int(round(base / k))          # بالا بردن ثابت حجم به V/k
            else:
                target = int(base - (k - 1) * (used - baseline))  # فقط مصرفِ بعد از مبنا ضرب می‌شود
                if target < used:
                    # از حد ضریب گذشته (پرش مصرف بین دو sync) → سقف را همین حالا روی مصرف فعلی ببند
                    target = int(used)
            if target != limit and target >= 1:
                panel.modify_user(username, {"data_limit": target})
                log("SET: effective volume of %s = %s (base %s, coefficient %.2f)"
                    % (username, human_size(target), human_size(base), k), quiet)
            st["written_limit"] = target if target >= 1 else st.get("written_limit")
            st["volume_base"] = base
            st["coef_applied"] = k
            st["baseline_used"] = baseline
            if st.get("disabled_by_script"):
                st["disabled_by_script"] = False

        st["last_used"] = used
        rows.append(row_for_user(u, k, st, chosen))

    return rows


def print_report(rows):
    if not rows:
        print("No users with a coefficient (other than 1) found.")
        return
    headers = ["User", "Coef", "Volume", "Real usage", "Effective usage", "Eff. remaining", "Effective quota", "Status"]
    table = []
    for r in sorted(rows, key=lambda x: x["username"]):
        status = r["status"]
        if r["script_cut"]:
            status += " (script-cut)"
        table.append([
            r["username"],
            ("%.2f" % r["k"]).rstrip("0").rstrip(".") if r["k"] != int(r["k"]) else str(int(r["k"])),
            human_size(r["limit"]),
            human_size(r["used"]),
            human_size(r["effective_used"]),
            human_size(r["effective_remaining"]),
            r["effective_quota"],
            status,
        ])
    widths = [max(len(headers[i]), *(len(row[i]) for row in table)) for i in range(len(headers))]
    fmt = "  ".join("%%-%ds" % w for w in widths)
    print(fmt % tuple(headers))
    print("-" * (sum(widths) + 2 * (len(headers) - 1)))
    for row in table:
        print(fmt % tuple(row))
    print()
    print("Effective usage = coefficient x real usage; effective remaining = volume - effective usage")


# ---------------------------------------------------------------- مدیریت سرویس (x-pasarguard)

def _config_path():
    return os.path.join(SCRIPT_DIR, "config.json")


def _run_systemctl(action):
    proc = subprocess.run(["systemctl", action, SERVICE_NAME], capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit("Error: systemctl %s %s failed: %s" % (action, SERVICE_NAME, proc.stderr.strip()[:200]))
    print("OK: service %s %s" % (SERVICE_NAME, action))


def _ensure_bin_link():
    """ساخت فرمان ثابت x-pasarguard در PATH."""
    link = "/usr/local/bin/x-pasarguard"
    target = os.path.join(SCRIPT_DIR, "pg_coef.py")
    content = '#!/bin/sh\nexec python3 "%s" "$@"\n' % target
    try:
        old = ""
        if os.path.exists(link):
            with open(link, "r", encoding="utf-8", errors="replace") as f:
                old = f.read()
        if old != content:
            with open(link, "w", encoding="utf-8", newline="\n") as f:
                f.write(content)
            os.chmod(link, 0o755)
            print("OK: command installed -> %s" % link)
        else:
            print("OK: command already installed -> %s" % link)
    except PermissionError:
        print("WARN: could not write %s (need root). Run as root or create it manually." % link)


def _fresh_version():
    proc = subprocess.run([sys.executable, "-c", "import pg_coef;print(pg_coef.VERSION)"],
                          cwd=SCRIPT_DIR, capture_output=True, text=True)
    return proc.stdout.strip() or "?"


def cmd_update(args):
    dest = SCRIPT_DIR
    changed = False
    git_bin = shutil.which("git")
    if git_bin and os.path.isdir(os.path.join(dest, ".git")):
        proc = subprocess.run([git_bin, "pull", "--ff-only"], cwd=dest, capture_output=True, text=True)
        out = (proc.stdout + proc.stderr).strip()
        print(out[-400:] if out else "git pull done")
        changed = proc.returncode == 0 and "Already up to date" not in out and "up to date" not in out
        if proc.returncode != 0:
            raise SystemExit("Error: git pull failed (local changes?). Fix manually or re-run install.")
    else:
        print("No git repo — downloading files from GitHub...")
        for f in ("pg_coef.py", "pg_panel.py", "index.html"):
            try:
                with urllib.request.urlopen(REPO_RAW + "/" + f, timeout=30) as r:
                    raw = r.read()
            except Exception as e:
                raise SystemExit("Error downloading %s: %s" % (f, e))
            local = os.path.join(dest, f)
            if os.path.exists(local):
                with open(local, "rb") as fobj:
                    if fobj.read() == raw:
                        continue
            with open(local, "wb") as fobj:
                fobj.write(raw)
            print("updated:", f)
            changed = True
    _ensure_bin_link()
    if changed:
        proc = subprocess.run(["systemctl", "is-active", SERVICE_NAME], capture_output=True, text=True)
        if proc.stdout.strip() == "active":
            _run_systemctl("restart")
            print("OK: service restarted with the new version.")
    print("Version: %s" % _fresh_version())


def cmd_status(args):
    cfg = load_config(_config_path())
    print("X-PasarGuard v%s" % VERSION)
    print("  Install dir  : %s" % SCRIPT_DIR)
    print("  Panel URL    : %s" % cfg.get("panel_url"))
    print("  Mode         : %s" % cfg.get("mode"))
    print("  Sync interval: %ss" % cfg.get("interval_seconds"))
    print("  Web panel    : port %s (%s)" % (cfg.get("web_port", 9012), "password set" if cfg.get("web_password") else "NO PASSWORD"))
    store = CoefStore(os.path.join(SCRIPT_DIR, "coefficients.json"))
    print("  Coefficients : %d users" % len(store.data["coefficients"]))
    proc = subprocess.run(["systemctl", "is-active", SERVICE_NAME], capture_output=True, text=True)
    print("  Service      : %s" % (proc.stdout.strip() or "unknown"))
    try:
        panel = make_panel(cfg, args)
        n = len(panel.get_users())
        print("  Panel API    : OK (%d users)" % n)
    except SystemExit as e:
        print("  Panel API    : %s" % e)
    except ApiError as e:
        print("  Panel API    : FAILED (%s)" % str(e)[:120])


def cmd_backup(args):
    dest = os.path.join(SCRIPT_DIR, "backups")
    os.makedirs(dest, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    cfg = load_config(_config_path())
    db_container = cfg.get("db_container") or ""
    created = []
    if db_container:
        proc = subprocess.run(
            ["docker", "exec", db_container, "pg_dump", "-U", cfg.get("db_user") or "pasarguard",
             "-d", cfg.get("db_name") or "pasarguard"],
            capture_output=True, timeout=120)
        if proc.returncode == 0 and proc.stdout:
            path = os.path.join(dest, "panel-db-%s.sql.gz" % ts)
            with open(path, "wb") as f:
                f.write(proc.stdout)
            created.append(path)
        else:
            print("WARN: pg_dump failed: %s" % proc.stderr.decode("utf-8", "replace")[:200])
    else:
        print("WARN: db_container not set — panel DB not backed up.")
    for name in ("config.json", "coefficients.json"):
        src = os.path.join(SCRIPT_DIR, name)
        if os.path.exists(src):
            path = os.path.join(dest, "%s-%s.bak" % (name, ts))
            with open(src, "rb") as fin, open(path, "wb") as fout:
                fout.write(fin.read())
            created.append(path)
    # نگه‌داشتن ۱۰ بکاپ آخر
    for prefix in ("panel-db-", "config.json-", "coefficients.json-"):
        olds = sorted(f for f in os.listdir(dest) if f.startswith(prefix))
        for old in olds[:-10]:
            os.remove(os.path.join(dest, old))
    for path in created:
        print("created: %s" % path)
    if not created:
        raise SystemExit("Error: nothing was backed up.")


def cmd_logs(args):
    cmd = ["journalctl", "-u", SERVICE_NAME, "--no-pager"]
    if args.follow:
        cmd.append("-f")
    else:
        cmd += ["-n", str(args.n)]
    proc = subprocess.run(cmd)
    sys.exit(proc.returncode)


def cmd_service(args):
    _run_systemctl(args.action)


def cmd_uninstall(args):
    if not args.yes:
        raise SystemExit("This will stop and remove the service. Add --yes to confirm (use --purge to also delete /opt files).")
    subprocess.run(["systemctl", "stop", SERVICE_NAME], capture_output=True)
    subprocess.run(["systemctl", "disable", SERVICE_NAME], capture_output=True)
    unit = "/etc/systemd/system/%s.service" % SERVICE_NAME
    if os.path.exists(unit):
        os.remove(unit)
    subprocess.run(["systemctl", "daemon-reload"], capture_output=True)
    link = "/usr/local/bin/x-pasarguard"
    if os.path.exists(link):
        os.remove(link)
    print("OK: service and command removed.")
    if args.purge:
        shutil.rmtree(SCRIPT_DIR, ignore_errors=True)
        print("OK: install dir deleted (%s)." % SCRIPT_DIR)
    else:
        print("Install dir kept (%s) — delete manually if you want a full wipe." % SCRIPT_DIR)


def cmd_password_token(args):
    path = _config_path()
    cfg = load_json(path, DEFAULT_CONFIG)
    token = secrets.token_hex(8)
    cfg["pending_password_token"] = token
    cfg["pending_password_expires"] = int(time.time()) + 900
    save_json_atomic(path, cfg)
    print("One-time password reset token (valid for 15 minutes):")
    print("")
    print("  %s" % token)
    print("")
    print("Open the web panel login page -> 'Forgot password?' -> enter this token and a new password.")


def cmd_version(args):
    print("X-PasarGuard v%s" % VERSION)


def cmd_password_reset(args):
    path = _config_path()
    cfg = load_json(path, DEFAULT_CONFIG)
    new_pass = args.set_pass or secrets.token_urlsafe(9)
    if len(new_pass) < 6:
        raise SystemExit("Error: password must be at least 6 characters.")
    cfg["web_password"] = new_pass
    cfg.pop("pending_password_token", None)
    cfg.pop("pending_password_expires", None)
    save_json_atomic(path, cfg)
    print("OK: web panel password updated (effective immediately).")
    print("New password: %s" % new_pass)


# ---------------------------------------------------------------- فرمان‌ها

def make_panel(cfg, args):
    url = args.url or cfg.get("panel_url")
    username = args.username if args.username is not None else cfg.get("admin_username")
    password = args.password if args.password is not None else cfg.get("admin_password")
    api_key = args.api_key if args.api_key is not None else cfg.get("api_key")
    if args.mode:
        cfg["mode"] = args.mode
    return Panel(
        url,
        admin_username=username,
        admin_password=password,
        api_key=api_key,
        timeout=float(cfg.get("timeout_seconds") or 20),
        verify_tls=bool(cfg.get("verify_tls", True)),
        db_container=cfg.get("db_container") or "",
        db_user=cfg.get("db_user") or "pasarguard",
        db_name=cfg.get("db_name") or "pasarguard",
    )


def load_config(path):
    cfg = load_json(path, DEFAULT_CONFIG)
    merged = dict(DEFAULT_CONFIG)
    merged.update({k: v for k, v in cfg.items() if v not in (None, "")})
    return merged


def ensure_config_template(path):
    if os.path.exists(path):
        return
    save_json_atomic(path, DEFAULT_CONFIG)
    print("A config.json was created next to the script. Fill in panel_url and admin credentials, then run again.")
    print(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2))


def cmd_login_test(args):
    cfg = load_config(args.config)
    panel = make_panel(cfg, args)
    panel.ensure_token()
    users = panel.get_users()
    print("OK: connected and authenticated: %s" % panel.base)
    print("  Panel users: %d" % len(users))
    nodes = panel.get_nodes()
    if nodes:
        print("  Nodes: " + ", ".join("%s (native coefficient: %s)" % (n.get("name"), n.get("usage_coefficient", 1)) for n in nodes))


def cmd_set(args):
    k = parse_float(args.coefficient, "ضریب")
    store = CoefStore(args.file)
    store.set(args.username, k)
    store.save()
    if k == 1.0:
        print("OK: coefficient of %s removed (equivalent to 1)." % args.username)
    else:
        print("OK: usage coefficient of %s = %s registered." % (args.username, ("%.3f" % k).rstrip("0").rstrip(".")))
        print("  From now on: new usage counts x %.2f; previous usage stays untouched." % k)
        print("  To apply: python pg_coef.py sync  (or run for a permanent loop)")


def cmd_set_all(args):
    cfg = load_config(args.config)
    k = parse_float(args.coefficient, "ضریب")
    panel = make_panel(cfg, args)
    users = panel.get_users()
    if not users:
        raise SystemExit("No users found in the panel.")
    store = CoefStore(args.file)
    for u in users:
        store.set(u.get("username"), k)
    store.save()
    print("OK: coefficient %s registered for %d users." % (("%.3f" % k).rstrip("0").rstrip("."), len(users)))
    print("  To apply: python pg_coef.py sync")


def cmd_set_default(args):
    k = parse_float(args.coefficient, "ضریب")
    cfg = load_json(args.config, DEFAULT_CONFIG)
    cfg["default_coefficient"] = k
    save_json_atomic(args.config, cfg)
    if k == 1.0:
        print("OK: default coefficient disabled.")
    else:
        print("OK: default coefficient for users without one = %s" % (("%.3f" % k).rstrip("0").rstrip(".")))
        print("  WARN: affects ALL panel users; applied on sync.")


def cmd_remove(args):
    store = CoefStore(args.file)
    if args.username in store.data["coefficients"]:
        del store.data["coefficients"][args.username]
        store.save()
        print("OK: coefficient of %s removed." % args.username)
    else:
        print("%s had no coefficient." % args.username)


def cmd_remove_all(args):
    store = CoefStore(args.file)
    n = len(store.data["coefficients"])
    store.data["coefficients"] = {}
    store.save()
    print("OK: coefficients cleared for %d users." % n)


def cmd_list(args):
    store = CoefStore(args.file)
    coefs = store.data["coefficients"]
    cfg = load_config(args.config)
    if not coefs and float(cfg.get("default_coefficient") or 1.0) == 1.0:
        print("No coefficients registered yet. Example: python pg_coef.py set ali 2")
        return
    if coefs:
        print("Registered coefficients:")
        for u in sorted(coefs):
            print("  %s → %s" % (u, ("%.3f" % coefs[u]).rstrip("0").rstrip(".")))
    default_k = float(cfg.get("default_coefficient") or 1.0)
    if default_k != 1.0:
        print("Default coefficient (users without one): %.2f" % default_k)


def cmd_report(args):
    cfg = load_config(args.config)
    panel = make_panel(cfg, args)
    store = CoefStore(args.file)
    rows = sync_once(panel, store, cfg, quiet=args.quiet, report_only=True)
    store.save()
    print_report(rows)


def cmd_sync(args):
    cfg = load_config(args.config)
    panel = make_panel(cfg, args)
    store = CoefStore(args.file)
    rows = sync_once(panel, store, cfg, quiet=args.quiet)
    store.save()
    changed = [r for r in rows if r["script_cut"] or r["mode_used"] in ("disable", "limit")]
    log("OK: sync finished; %d users checked." % len(rows), args.quiet)
    if not args.quiet:
        print_report(rows)


def cmd_run(args):
    cfg = load_config(args.config)
    if args.interval:
        cfg["interval_seconds"] = args.interval
    panel = make_panel(cfg, args)
    store = CoefStore(args.file)
    interval = max(5, int(cfg.get("interval_seconds") or 60))
    log("Permanent watch started (every %ds, mode %s). Ctrl+C to stop" % (interval, cfg.get("mode")), args.quiet)
    while True:
        started = time.time()
        try:
            rows = sync_once(panel, store, cfg, quiet=args.quiet)
            store.save()
            log("Sync cycle done (%d users)." % len(rows), args.quiet)
        except ApiError as e:
            log("Cycle error: %s — retrying next cycle." % e, args.quiet)
        except SystemExit as e:
            raise e
        elapsed = time.time() - started
        time.sleep(max(1, interval - elapsed))


def cmd_node_list(args):
    cfg = load_config(args.config)
    panel = make_panel(cfg, args)
    nodes = panel.get_nodes()
    if not nodes:
        print("No nodes found.")
        return
    print("Nodes and the panel's native usage coefficient (applies to all users on that node):")
    for n in nodes:
        print("  [%s] %s -> coefficient: %s | status: %s" % (
            n.get("id"), n.get("name"), n.get("usage_coefficient", 1), n.get("status")))


def cmd_node_set(args):
    k = parse_float(args.coefficient, "ضریب")
    cfg = load_config(args.config)
    panel = make_panel(cfg, args)
    nodes = panel.get_nodes()
    target = None
    for n in nodes:
        if str(n.get("id")) == str(args.node) or (n.get("name") or "").lower() == args.node.lower():
            target = n
            break
    if target is None:
        raise SystemExit("Node '%s' not found. Use node-list to see exact names." % args.node)
    panel.modify_node(target["id"], {"usage_coefficient": k})
    print("OK: native usage coefficient of node '%s' = %s registered." % (target.get("name"), k))
    print("  This coefficient multiplies usage of ALL users on that node (no permanent sync needed).")


# ---------------------------------------------------------------- argparse

def _add_common_args(parser, sub_level=False):
    """فلگ‌های عمومی؛ در سطح sub-parser با SUPPRESS تا مقدار سطح بالا را پاک نکنند."""
    sup = argparse.SUPPRESS if sub_level else None
    parser.add_argument("--config", default=sup if sub_level else os.path.join(SCRIPT_DIR, "config.json"), help="config file path (default: config.json next to the script)")
    parser.add_argument("--file", default=sup if sub_level else os.path.join(SCRIPT_DIR, "coefficients.json"), help="coefficients/state file path")
    parser.add_argument("--url", default=sup, help="panel URL; overrides config")
    parser.add_argument("--username", default=sup, help="admin username; overrides config")
    parser.add_argument("--password", default=sup, help="admin password; overrides config")
    parser.add_argument("--api-key", default=sup, help="panel X-Api-Key; overrides config")
    parser.add_argument("--mode", default=sup, choices=MODES, help="coefficient mode; overrides config")
    parser.add_argument("--interval", default=sup, type=int, help="watch interval (seconds) for the run command")
    parser.add_argument("--quiet", default=sup if sub_level else False, action="store_true", help="quiet output")


def build_parser():
    p = argparse.ArgumentParser(
        prog="x-pasarguard",
        description="X-PasarGuard v%s - per-user usage coefficient for PasarGuard panels" % VERSION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  x-pasarguard login-test\n"
            "  x-pasarguard set ali 2               (ali's NEW usage counts double)\n"
            "  x-pasarguard set reza 0.5            (reza's new usage counts half = double volume)\n"
            "  x-pasarguard sync                    (apply once)\n"
            "  x-pasarguard report                  (effective-usage table)\n"
            "  x-pasarguard status                  (service and panel status)\n"
            "  x-pasarguard backup                  (backup panel DB + configs)\n"
            "  x-pasarguard update                  (update from GitHub)\n"
            "  x-pasarguard password reset --set X  (reset web panel password)\n"
        ),
    )
    _add_common_args(p)

    sub = p.add_subparsers(dest="command")

    def sp(name, help_text):
        parser = sub.add_parser(name, help=help_text)
        _add_common_args(parser, sub_level=True)
        return parser

    sp("login-test", "test panel connection and auth")
    sp("report", "effective-usage table for all users (read-only)")

    s = sp("set", "set coefficient for one user")
    s.add_argument("username")
    s.add_argument("coefficient")

    s = sp("set-all", "set one coefficient for all panel users")
    s.add_argument("coefficient")

    s = sp("set-default", "default coefficient for users without one (affects everyone)")
    s.add_argument("coefficient")

    s = sp("remove", "remove one user's coefficient")
    s.add_argument("username")

    sp("remove-all", "clear all coefficients")
    sp("list", "show registered coefficients")
    sp("sync", "apply coefficients to the panel once")
    sp("run", "permanent watch (sync every few seconds)")

    sp("node-list", "nodes and the panel's native coefficient")
    s = sp("node-set", "set a node's native coefficient (applies to all its users)")
    s.add_argument("node", help="node name or id")
    s.add_argument("coefficient")

    # ---- management commands
    sp("update", "update X-PasarGuard from GitHub and restart the service")
    sp("status", "show service, config and panel status")
    sp("version", "show X-PasarGuard version")
    sp("backup", "backup panel database + config + coefficients (keeps last 10)")
    s = sp("logs", "show service logs")
    s.add_argument("--follow", action="store_true", help="follow logs live (like tail -f)")
    s.add_argument("-n", type=int, default=100, help="number of lines (default 100)")
    s = sp("service", "control the pg-coef systemd service")
    s.add_argument("action", choices=("start", "stop", "restart"))
    s = sp("uninstall", "remove the service and command (files kept unless --purge)")
    s.add_argument("--yes", action="store_true", help="do not ask, just do it")
    s.add_argument("--purge", action="store_true", help="also delete the install directory")

    s = sub.add_parser("password", help="web panel password management")
    _add_common_args(s, sub_level=True)
    psub = s.add_subparsers(dest="password_cmd", required=True)
    pt = psub.add_parser("token", help="print a one-time reset token (use on the login page)")
    pt.set_defaults(func=cmd_password_token)
    pr = psub.add_parser("reset", help="reset the web panel password directly (root)")
    pr.add_argument("--set", dest="set_pass", default=None, help="new password (default: generate a random one)")
    pr.set_defaults(func=cmd_password_reset)

    return p


def main(argv=None):
    force_utf8_console()
    args = build_parser().parse_args(argv)
    if not args.command:
        build_parser().print_help()
        return 0
    ensure_config_template(args.config)
    handlers = {
        "login-test": cmd_login_test,
        "set": cmd_set,
        "set-all": cmd_set_all,
        "set-default": cmd_set_default,
        "remove": cmd_remove,
        "remove-all": cmd_remove_all,
        "list": cmd_list,
        "report": cmd_report,
        "sync": cmd_sync,
        "run": cmd_run,
        "node-list": cmd_node_list,
        "node-set": cmd_node_set,
        "update": cmd_update,
        "status": cmd_status,
        "version": cmd_version,
        "backup": cmd_backup,
        "logs": cmd_logs,
        "service": cmd_service,
        "uninstall": cmd_uninstall,
    }
    try:
        if args.command == "password":
            args.func(args)
            return 0
        handlers[args.command](args)
        return 0
    except ApiError as e:
        print("Error: %s" % e, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
