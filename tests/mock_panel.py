#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mock_panel.py — پنل ساختگی شبیه API پاسارگارد برای تست pg_coef.py بدون پنل واقعی.

اجرا:
    python tests/mock_panel.py --port 8977

endpoint های تستی:
    POST /__test__/add_user   {"username": "ali", "data_limit_gb": 10}
    POST /__test__/use        {"username": "ali", "mb": 500}     (مصرف اضافه می‌کند)
    POST /__test__/reset      {"username": "ali"}                 (ریست مصرف)
    GET  /__test__/state                                          (_dump وضعیت)
    GET  /__test__/ping
"""

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

ADMIN_USER = "admin"
ADMIN_PASS = "admin"
VALID_API_KEYS = {"test-api-key"}

LOCK = threading.Lock()
USERS = {}  # username -> dict(username, status, used_traffic, data_limit, expire, note)
NODES = {}  # id -> dict(id, name, usage_coefficient, status)
TOKENS = {"mock-token-123"}


def gb(v):
    return int(v * 1024 * 1024 * 1024)


def add_user(username, data_limit_gb=10.0):
    USERS[username] = {
        "username": username,
        "status": "active",
        "used_traffic": 0,
        "data_limit": gb(data_limit_gb),
        "expire": None,
        "note": None,
        "lifetime_used_traffic": 0,
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # بی‌صدا
        pass

    # ---------- helpers

    def _send(self, code, payload=None):
        raw = b"" if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        if raw:
            self.wfile.write(raw)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        ctype = (self.headers.get("Content-Type") or "").lower()
        if not raw:
            return {}
        if "x-www-form-urlencoded" in ctype:
            from urllib.parse import parse_qsl
            return dict(parse_qsl(raw.decode("utf-8")))
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            return {}

    def _authed(self):
        if self.headers.get("X-Api-Key") in VALID_API_KEYS:
            return True
        auth = self.headers.get("Authorization") or ""
        return auth.startswith("Bearer ") and auth[7:] in TOKENS

    # ---------- panel-native status resolution (مثل crud خود پنل)

    def _apply_limit_change(self, user, new_limit):
        user["data_limit"] = new_limit or None
        if user["status"] in ("expired", "disabled"):
            return
        if not user["data_limit"] or user["used_traffic"] < user["data_limit"]:
            user["status"] = "active"
        else:
            user["status"] = "limited"

    # ---------- routes

    def do_GET(self):
        parsed = urlparse(self.path)
        path, qs = parsed.path, parse_qs(parsed.query)
        with LOCK:
            if path == "/__test__/ping":
                return self._send(200, {"ok": True})
            if path == "/__test__/state":
                return self._send(200, {"users": USERS, "nodes": {str(k): v for k, v in NODES.items()}})
            if path == "/api/nodes":
                if not self._authed():
                    return self._send(401, {"detail": "unauthorized"})
                return self._send(200, {"nodes": list(NODES.values()), "total": len(NODES)})
            if path.startswith("/api/users"):
                if not self._authed():
                    return self._send(401, {"detail": "unauthorized"})
                offset = int((qs.get("offset") or ["0"])[0])
                limit = int((qs.get("limit") or ["100"])[0])
                items = sorted(USERS.values(), key=lambda u: u["username"])[offset:offset + limit]
                return self._send(200, {"users": items, "total": len(USERS)})
            if path.startswith("/api/user/"):
                if not self._authed():
                    return self._send(401, {"detail": "unauthorized"})
                username = path[len("/api/user/"):]
                user = USERS.get(username)
                if not user:
                    return self._send(404, {"detail": "not found"})
                return self._send(200, dict(user))
            return self._send(404, {"detail": "no route"})

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        body = self._body()
        with LOCK:
            if path.startswith("/__test__/"):
                if path == "/__test__/add_user":
                    add_user(body["username"], float(body.get("data_limit_gb") or 10))
                    return self._send(200, {"ok": True})
                if path == "/__test__/add_node":
                    node_id = int(body["id"])
                    NODES[node_id] = {"id": node_id, "name": body["name"],
                                      "usage_coefficient": float(body.get("usage_coefficient") or 1),
                                      "status": "connected"}
                    return self._send(200, dict(NODES[node_id]))
                if path == "/__test__/use":
                    user = USERS.get(body["username"])
                    if not user:
                        return self._send(404, {"detail": "not found"})
                    user["used_traffic"] += int(body["mb"]) * 1024 * 1024
                    user["lifetime_used_traffic"] += user["used_traffic"]
                    # پنل خودش وقتی مصرف از حجم گذشت limited می‌کند
                    if user["data_limit"] and user["used_traffic"] >= user["data_limit"] and user["status"] == "active":
                        user["status"] = "limited"
                    return self._send(200, dict(user))
                if path == "/__test__/reset":
                    user = USERS.get(body["username"])
                    if not user:
                        return self._send(404, {"detail": "not found"})
                    user["used_traffic"] = 0
                    if user["status"] == "limited":
                        user["status"] = "active"
                    return self._send(200, dict(user))
                return self._send(404, {"detail": "no route"})
            if path == "/api/admin/token":
                if body.get("username") == ADMIN_USER and body.get("password") == ADMIN_PASS:
                    return self._send(200, {"access_token": "mock-token-123", "token_type": "bearer"})
                return self._send(401, {"detail": "bad credentials"})
            if not self._authed():
                return self._send(401, {"detail": "unauthorized"})
            if path == "/api/user/%s/reset" % body.get("username", ""):
                user = USERS.get(body["username"])
                if not user:
                    return self._send(404, {"detail": "not found"})
                user["used_traffic"] = 0
                if user["status"] == "limited":
                    user["status"] = "active"
                return self._send(200, dict(user))
            return self._send(404, {"detail": "no route"})

    def do_PUT(self):
        parsed = urlparse(self.path)
        path = parsed.path
        body = self._body()
        with LOCK:
            if not self._authed():
                return self._send(401, {"detail": "unauthorized"})
            if path.startswith("/api/node/"):
                try:
                    node_id = int(path[len("/api/node/"):])
                except ValueError:
                    return self._send(404, {"detail": "not found"})
                node = NODES.get(node_id)
                if not node:
                    return self._send(404, {"detail": "not found"})
                if "usage_coefficient" in body:
                    node["usage_coefficient"] = float(body["usage_coefficient"])
                return self._send(200, dict(node))
            if path.startswith("/api/user/") and path.endswith("/disabled"):
                username = path[len("/api/user/"):-len("/disabled")]
                user = USERS.get(username)
                if not user:
                    return self._send(404, {"detail": "not found"})
                if body.get("disabled"):
                    user["status"] = "disabled"
                else:
                    # مثل پنل: بعد از وصل کردن، وضعیت درست را resolve کن
                    if user["status"] == "disabled":
                        if user["data_limit"] and user["used_traffic"] >= user["data_limit"]:
                            user["status"] = "limited"
                        else:
                            user["status"] = "active"
                return self._send(200, dict(user))
            if path.startswith("/api/user/"):
                username = path[len("/api/user/"):]
                user = USERS.get(username)
                if not user:
                    return self._send(404, {"detail": "not found"})
                if "data_limit" in body:
                    self._apply_limit_change(user, int(body["data_limit"]))
                if "status" in body:
                    user["status"] = body["status"]
                return self._send(200, dict(user))
            return self._send(404, {"detail": "no route"})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8977)
    ap.add_argument("--seed", type=int, default=3, help="چند کاربر تستی ali/reza/sara ساخته شود")
    args = ap.parse_args()
    if args.seed:
        for name in ("ali", "reza", "sara"):
            add_user(name, 10.0)
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print("mock panel on http://127.0.0.1:%d (admin/admin, api key: test-api-key)" % args.port, flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
