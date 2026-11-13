
"""跨公网信息传输 —— 中继服务（Linux / Ubuntu 24.04，纯标准库，无第三方依赖）

职责（只做「控制面」，不搬数据）：
  1. 目标端 bat 与控制页之间的指令通道（长轮询，25s）
  2. 目录列举 / 容量统计 / 传输进度的转发
  3. 签发 OSS 预签名 URL —— 上传分片与下载都直连 OSS，数据不经过本机

启动： python3 relay.py --config config.json
"""

import argparse
import hashlib
import json
import mimetypes
import os
import platform
import re
import secrets
import signal
import socket
import sys
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from oss_client import OSSClient, OSSError, content_disposition

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")

DEFAULT_CONFIG = {
    "listen": "0.0.0.0",
    "port": 8080,
    "username": "admin",
    "password": "change-this-password",
    "device_key": "change-this-device-key",
    "session_hours": 12,
    "max_fail": 5,
    "lock_seconds": 900,
    "public_base": "",
    "oss": {
        "endpoint": "oss-cn-hangzhou.aliyuncs.com",
        "bucket": "",
        "access_key_id": "",
        "access_key_secret": "",
        "scheme": "https",
        "host_override": "",
        "part_size": 64 * 1024 * 1024,
        "download_expires": 86400,
    },
}

LOG_LOCK = threading.Lock()

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

def log(*args):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    with LOG_LOCK:
        print("[%s] %s" % (ts, " ".join(str(a) for a in args)), flush=True)

def merge(dst, src):
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            merge(dst[k], v)
        else:
            dst[k] = v
    return dst

def load_config(path):
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if path and os.path.exists(path):

        with open(path, "r", encoding="utf-8-sig") as f:
            merge(cfg, json.load(f))
    else:
        log("警告: 未找到配置文件 %s，使用默认配置" % path)
    for env, (sec, key) in {
        "RELAY_PASSWORD": (None, "password"),
        "RELAY_DEVICE_KEY": (None, "device_key"),
        "OSS_AK": ("oss", "access_key_id"),
        "OSS_SK": ("oss", "access_key_secret"),
        "OSS_BUCKET": ("oss", "bucket"),
    }.items():
        val = os.environ.get(env)
        if val:
            if sec:
                cfg[sec][key] = val
            else:
                cfg[key] = val
    return cfg

class Hub:
    def __init__(self, cfg):
        self.cfg = cfg
        self.cv = threading.Condition()
        self.ver = 0
        self.boot_id = uuid.uuid4().hex
        self.devices = {}
        self.sessions = {}
        self.lockout = {}
        self.start = time.time()

        base = cfg.get("state_dir") or "state"
        if not os.path.isabs(base):
            base = os.path.join(os.path.dirname(os.path.abspath(__file__)), base)
        self.state_dir = base

        self.history_path = os.path.join(self.state_dir, "history.json")
        self.history = self._load_history()

        self.metrics = {"at": [], "cpu": [], "mem": [], "net_rx": [], "net_tx": []}
        self._last_net = None
        self._last_net_at = None

        self.dev_open = os.environ.get("RELAY_DEV_OPEN") == "1"
        self.oss = None
        o = cfg["oss"]
        if o.get("access_key_id") and o.get("access_key_secret") and o.get("bucket"):
            try:
                self.oss = OSSClient(o["endpoint"], o["bucket"], o["access_key_id"],
                                     o["access_key_secret"], o.get("sts_token"),
                                     scheme=o.get("scheme", "https"),
                                     host_override=o.get("host_override"))
                log("OSS 已就绪: %s / %s" % (o["endpoint"], o["bucket"]))
            except Exception as exc:
                log("OSS 初始化失败:", exc)
        else:
            log("警告: OSS 未配置，上传/下载链接功能将不可用（其余功能正常）")

    def bump(self):
        with self.cv:
            self.ver += 1
            self.cv.notify_all()

    def wait_change(self, since, timeout=25.0):
        deadline = time.time() + timeout
        with self.cv:
            while self.ver <= since:
                left = deadline - time.time()
                if left <= 0:
                    break
                self.cv.wait(left)
            return self.ver

    def device(self, dev_id):
        d = self.devices.get(dev_id)
        if d is None:
            d = {
                "id": dev_id, "host": dev_id, "user": "", "admin": False, "agent": "",
                "disks": [], "roots": {}, "seq": 0, "pending": {}, "results": {},
                "job": self.load_job(dev_id),
                "last_seen": 0.0, "connected_at": 0.0, "ip": "", "skipped_total": 0,

                "transport": "oss", "ipv6_enabled": False, "v6": None,
            }
            self.devices[dev_id] = d
        return d

    def job_path(self, dev_id):
        safe = re.sub(r"[^0-9A-Za-z_.:-]", "", dev_id)[:64]
        return os.path.join(self.state_dir, safe + ".json")

    def load_job(self, dev_id):
        try:
            p = self.job_path(dev_id)
            if os.path.isfile(p):
                with open(p, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            log("[警告] 读取作业状态失败 %s: %s" % (dev_id, e))
        return None

    def save_job(self, dev_id, job):
        try:
            os.makedirs(self.state_dir, exist_ok=True)
            p = self.job_path(dev_id)
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(job, f, ensure_ascii=False)
            os.replace(tmp, p)
        except Exception as e:
            log("[警告] 保存作业状态失败 %s: %s" % (dev_id, e))

    def _load_history(self):
        try:
            if os.path.isfile(self.history_path):
                with open(self.history_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    return data
        except Exception as e:
            log("[警告] 读取传输历史失败: %s" % e)
        return []

    def _save_history(self):
        try:
            os.makedirs(self.state_dir, exist_ok=True)
            tmp = self.history_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.history, f, ensure_ascii=False)
            os.replace(tmp, self.history_path)
        except Exception as e:
            log("[警告] 保存传输历史失败: %s" % e)

    def add_history(self, dev_id, job):
        """作业进入终态时把它的 objects 记进历史，按 key 去重"""
        if not isinstance(job, dict):
            return
        if job.get("state") != "done":
            return
        objs = job.get("objects") or []
        if not objs:
            return
        existing = {h.get("key") for h in self.history}
        added = 0
        now = int(time.time())
        for o in objs:
            key = o.get("key")
            if not key or key in existing:
                continue
            self.history.append({
                "key": key,
                "name": o.get("name") or key[:8],
                "size": int(o.get("size") or 0),
                "kind": o.get("kind") or "file",
                "device": dev_id,
                "at": now,
            })
            existing.add(key)
            added += 1
        if added:

            self.history.sort(key=lambda h: h.get("at") or 0, reverse=True)
            self._save_history()
            log("历史记录 +%d（共 %d 项可下载）" % (added, len(self.history)))

    def remove_history(self, key):
        before = len(self.history)
        self.history = [h for h in self.history if h.get("key") != key]
        if len(self.history) != before:
            self._save_history()
            return True
        return False

    def _read_net_bytes(self):
        """返回 (rx, tx) 累计字节数；拿不到就返回 None"""
        try:
            if os.name == "nt":
                return None
            rx = tx = 0
            with open("/proc/net/dev", "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if ":" not in line:
                        continue
                    name, _, rest = line.partition(":")
                    name = name.strip()
                    if name == "lo":
                        continue
                    parts = rest.split()
                    if len(parts) >= 9:
                        rx += int(parts[0])
                        tx += int(parts[8])
            return rx, tx
        except Exception:
            return None

    def sample_metrics(self, cpu_pct, mem_pct):
        """记录一个采样点，返回给前端画图的序列"""
        now = int(time.time())
        rx_bps = tx_bps = 0
        net = self._read_net_bytes()
        if net is not None:
            if self._last_net is not None and self._last_net_at:
                dt = now - self._last_net_at
                if dt > 0:
                    rx_bps = max(0, int((net[0] - self._last_net[0]) / dt))
                    tx_bps = max(0, int((net[1] - self._last_net[1]) / dt))
            self._last_net = net
            self._last_net_at = now

        m = self.metrics
        m["at"].append(now)
        m["cpu"].append(round(float(cpu_pct or 0), 1))
        m["mem"].append(round(float(mem_pct or 0), 1))
        m["net_rx"].append(rx_bps)
        m["net_tx"].append(tx_bps)
        for k in m:
            if len(m[k]) > 60:
                m[k] = m[k][-60:]
        return m

    def touch(self, dev_id, req_ip=""):
        d = self.device(dev_id)
        d["last_seen"] = time.time()
        d["ip"] = req_ip
        return d

    def enqueue(self, dev_id, action, args=None, timeout=30):
        with self.cv:
            d = self.device(dev_id)
            d["seq"] += 1
            seq = d["seq"]
            d["pending"][seq] = {
                "seq": seq, "action": action, "args": args or {},
                "at": time.time(), "sent": 0.0, "timeout": timeout,
            }
            self.ver += 1
            self.cv.notify_all()
            return seq

    def poll_device(self, dev_id, after, timeout=25.0):
        deadline = time.time() + timeout
        d = self.device(dev_id)
        with self.cv:
            while True:
                now = time.time()
                out = []
                for seq in sorted(d["pending"]):
                    cmd = d["pending"][seq]
                    if seq <= after:

                        if now - cmd["at"] > 300:
                            d["pending"].pop(seq, None)
                        continue
                    if cmd["sent"] and now - cmd["sent"] < 45:
                        continue
                    cmd["sent"] = now
                    out.append({"seq": seq, "action": cmd["action"], "args": cmd["args"]})
                if out:
                    return out
                left = deadline - time.time()
                if left <= 0:
                    return []
                self.cv.wait(min(left, 5.0))

    def complete_cmd(self, dev_id, seq):
        with self.cv:
            d = self.device(dev_id)
            d["pending"].pop(int(seq), None)
            self.ver += 1
            self.cv.notify_all()

    def put_result(self, dev_id, seq, ok, data, error=""):
        with self.cv:
            d = self.device(dev_id)
            d["pending"].pop(int(seq), None)
            d["results"][str(seq)] = {"seq": int(seq), "ok": ok, "data": data, "error": error}
            if len(d["results"]) > 10:
                for k in sorted(d["results"], key=lambda x: int(x))[:-10]:
                    d["results"].pop(k, None)
            self.ver += 1
            self.cv.notify_all()

    def put_job(self, dev_id, job):
        with self.cv:
            d = self.device(dev_id)
            d["job"] = job

            now = time.time()
            terminal = job.get("state") != "running"
            if terminal or now - d.get("job_saved", 0) > 5:
                d["job_saved"] = now
                self.save_job(dev_id, job)

            if terminal:
                self.add_history(dev_id, job)
            self.ver += 1
            self.cv.notify_all()

    def cancel_pending(self, dev_id):
        """目标端正在长时间上传时不轮询，靠这个标记把"取消"带回去"""
        with self.cv:
            d = self.devices.get(dev_id)
            if not d:
                return False
            for seq in d["pending"].values():
                if seq["action"] == "cancel":
                    return True
            return False

    def new_session(self):
        sid = secrets.token_urlsafe(24)
        with self.cv:
            self.sessions[sid] = time.time() + self.cfg["session_hours"] * 3600
        return sid

    def valid_session(self, sid):
        if not sid:
            return False
        with self.cv:
            exp = self.sessions.get(sid)
            if not exp:
                return False
            if exp < time.time():
                self.sessions.pop(sid, None)
                return False
            return True

    def drop_session(self, sid):
        with self.cv:
            self.sessions.pop(sid, None)

    def is_locked(self, ip):
        with self.cv:
            rec = self.lockout.get(ip)
            if not rec:
                return 0
            if rec["until"] > time.time():
                return int(rec["until"] - time.time())
            self.lockout.pop(ip, None)
            return 0

    def note_fail(self, ip):
        with self.cv:
            rec = self.lockout.setdefault(ip, {"count": 0, "until": 0})
            rec["count"] += 1
            if rec["count"] >= self.cfg["max_fail"]:
                rec["until"] = time.time() + self.cfg["lock_seconds"]
                rec["count"] = 0
                return self.cfg["lock_seconds"]
            return 0

    def clear_fail(self, ip):
        with self.cv:
            self.lockout.pop(ip, None)

    def snapshot(self):
        now = time.time()
        devs = []
        for d in self.devices.values():
            devs.append({
                "id": d["id"], "host": d["host"], "user": d["user"], "admin": d["admin"],
                "agent": d["agent"], "disks": d["disks"], "job": d["job"], "roots": d["roots"],
                "online": (now - d["last_seen"]) < 50,
                "last_seen": int(d["last_seen"]), "results": d["results"],
                "connected_at": int(d["connected_at"]), "ip": d["ip"],
                "transport": d.get("transport") or "oss",
                "ipv6_enabled": bool(d.get("ipv6_enabled")),
                "v6": d.get("v6"),
            })
        devs.sort(key=lambda x: (not x["online"], x["id"]))
        return {
            "ok": True, "ver": self.ver, "now": int(now), "uptime": int(now - self.start),
            "devices": devs, "oss": bool(self.oss),
            "oss_error": "" if self.oss else "OSS 未配置",
            "history": list(self.history),
        }

class Api:
    def __init__(self, hub):
        self.hub = hub

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    hub = None
    server_version = "Relay/1.0"

    def log_message(self, fmt, *args):
        pass

    def client_ip(self):
        xff = self.headers.get("X-Forwarded-For")
        if xff:
            return xff.split(",")[0].strip()
        return self.client_address[0]

    def send_json(self, obj, status=200, cookie=None):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(raw)

    def send_error_json(self, msg, status=400):
        self.send_json({"ok": False, "error": msg}, status)

    def _read_body(self, limit=16 * 1024 * 1024):
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            raise ValueError("请求体过大")
        raw = self.rfile.read(n) if n else b""
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def body_json(self, limit=16 * 1024 * 1024):

        cached = getattr(self, "_body_cache", None)
        if cached is not None:
            return cached
        self._body_cache = self._read_body(limit)
        return self._body_cache

    def session_id(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "relay_sid":
                return v
        return ""

    def require_login(self):
        if self.hub.valid_session(self.session_id()):
            return True
        self.send_error_json("未登录", 401)
        return False

    def dev_session(self):
        """开发模式：仅回环地址允许免登录，返回新建的 sid"""
        if not self.hub.dev_open:
            return ""
        if self.client_ip() not in ("127.0.0.1", "::1"):
            return ""
        return self.hub.new_session()

    def send_file(self, rel):
        path = os.path.normpath(os.path.join(WEB_DIR, rel.lstrip("/")))
        if not path.startswith(WEB_DIR) or not os.path.isfile(path):
            self.send_error_json("Not Found", 404)
            return
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith(("text/", "application/javascript", "application/json")) else ""))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, loc):
        self.send_response(302)
        self.send_header("Location", loc)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def route(self, method):
        path = urlparse(self.path).path
        try:

            self._body_cache = None
            if method == "POST":
                try:
                    self._body_cache = self._read_body()
                except Exception:
                    self._body_cache = {}

            if method == "GET":
                if path in ("/", "/index.html"):
                    if not self.hub.valid_session(self.session_id()):
                        sid = self.dev_session()
                        if sid:
                            self.send_response(302)
                            self.send_header("Location", "/")
                            self.send_header("Set-Cookie", "relay_sid=%s; Path=/; HttpOnly; Max-Age=3600; SameSite=Lax" % sid)
                            self.send_header("Content-Length", "0")
                            self.end_headers()
                            return
                        return self.redirect("/login")
                    return self.send_file("index.html")
                if path in ("/login", "/login.html"):
                    return self.send_file("login.html")

                if path in ("/favicon.ico", "/favicon-256.png", "/icon-192.png",
                            "/apple-touch-icon.png", "/manifest.json"):
                    return self.send_file(path.lstrip("/"))
                if path in ("/app.js", "/style.css"):
                    return self.send_file(path.lstrip("/"))
                if path == "/api/ping":
                    return self.send_json({"ok": True, "t": int(time.time())})

            if path == "/api/login" and method == "POST":
                return self.api_login()
            if path == "/api/logout" and method == "POST":
                self.hub.drop_session(self.session_id())
                return self.send_json({"ok": True}, cookie="relay_sid=; Path=/; Max-Age=0")
            if path == "/api/state" and method == "GET":
                if not self.require_login():
                    return
                return self.send_json(self.hub.snapshot())
            if path == "/api/events":
                if not self.require_login():
                    return
                return self.api_events()
            if path == "/api/cmd" and method == "POST":
                if not self.require_login():
                    return
                return self.api_cmd()
            if path == "/api/link" and method == "POST":
                if not self.require_login():
                    return
                return self.api_link()
            if path == "/api/sysinfo" and method == "GET":
                if not self.require_login():
                    return
                return self.send_json(self.api_sysinfo())
            if path == "/api/account" and method == "POST":
                if not self.require_login():
                    return
                return self.api_account()
            if path == "/api/history" and method == "POST":
                if not self.require_login():
                    return
                return self.api_history()
            if path == "/api/disconnect" and method == "POST":
                if not self.require_login():
                    return
                return self.api_disconnect()
            if path == "/api/oss" and method == "POST":
                if not self.require_login():
                    return
                return self.api_web_oss()

            if path == "/api/t/hello" and method == "POST":
                return self.api_hello()
            if path == "/api/t/poll" and method == "POST":
                return self.api_poll()
            if path == "/api/t/report" and method == "POST":
                return self.api_report()
            if path == "/api/t/oss" and method == "POST":
                return self.api_target_oss()

            return self.send_error_json("Not Found", 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            log("ERR", self.path, repr(exc))
            traceback.print_exc()
            try:
                self.send_error_json("服务器内部错误: %s" % exc, 500)
            except Exception:
                pass

    def api_login(self):
        ip = self.client_ip()
        left = self.hub.is_locked(ip)
        if left:
            return self.send_json({"ok": False, "error": "失败次数过多，请 %d 秒后再试" % left}, 429)
        body = self.body_json()
        user = (body.get("username") or "").strip()
        pwd = body.get("password") or ""
        cfg = self.hub.cfg

        def _eq(a, b):
            return secrets.compare_digest(str(a).encode("utf-8"), str(b).encode("utf-8"))

        def _norm(s):
            """去掉首尾空白和控制字符。

            历史版本的 install.sh 曾把换行符写进密码（变成 "\\n真实密码"），
            而浏览器的密码框根本输入不了换行，导致用户输对也永远登不上。
            这里两边都归一化，配置里带了脏空白也能登录。
            """
            return str(s).strip().strip("\r\n\t\v\f\0")

        ok = _eq(_norm(user), _norm(cfg["username"])) and _eq(_norm(pwd), _norm(cfg["password"]))
        if not ok:
            locked = self.hub.note_fail(ip)
            log("登录失败 ip=%s user=%r" % (ip, user))
            msg = "账号或密码错误"
            if locked:
                msg = "连续失败 %d 次，已锁定 %d 分钟" % (cfg["max_fail"], locked // 60)
            return self.send_json({"ok": False, "error": msg}, 401)
        self.hub.clear_fail(ip)
        sid = self.hub.new_session()
        log("登录成功 ip=%s" % ip)
        cookie = "relay_sid=%s; Path=/; HttpOnly; Max-Age=%d; SameSite=Lax" % (
            sid, cfg["session_hours"] * 3600)
        return self.send_json({"ok": True}, cookie=cookie)

    def api_events(self):
        body = self.body_json()
        since = int(body.get("ver") or 0)
        wait = min(float(body.get("wait") or 25), 30.0)
        if self.hub.ver <= since:
            self.hub.wait_change(since, wait)
        return self.send_json(self.hub.snapshot())

    CMD_ACTIONS = (
        "list", "size", "push", "cancel", "ping", "zip_preview", "disconnect",

        "exists", "mkdir", "newfile", "rename", "copy", "move", "delete",

        "v6prep", "v6stop", "v6stat",

        "pull",
    )

    def api_cmd(self):
        body = self.body_json()
        dev = (body.get("device") or "").strip()
        action = (body.get("action") or "").strip()
        args = body.get("args") or {}
        if not dev or not action:
            return self.send_error_json("缺少 device / action")
        if dev not in self.hub.devices:
            return self.send_error_json("设备不存在")
        if action not in self.CMD_ACTIONS:
            return self.send_error_json("不支持的指令: %s" % action)
        seq = self.hub.enqueue(dev, action, args)
        log("→ 指令 %s #%d %s" % (dev, seq, action))
        return self.send_json({"ok": True, "seq": seq})

    def api_link(self):
        if not self.hub.oss:
            return self.send_error_json("OSS 未配置", 503)
        body = self.body_json()
        key = (body.get("key") or "").strip()
        if not key:
            return self.send_error_json("缺少 key")
        if body.get("op") == "delete":
            self.hub.oss.delete(key)
            self.hub.remove_history(key)
            log("已删除对象 %s" % key[:12])
            return self.send_json({"ok": True, "deleted": key})
        expires = int(body.get("expires") or self.hub.cfg["oss"]["download_expires"])
        url = self.hub.oss.presign_get(key, expires=expires, filename=body.get("filename"))
        return self.send_json({"ok": True, "url": url, "expires_in": expires,
                               "expires_at": int(time.time()) + expires})

    def api_disconnect(self):
        """让目标端下线：下发一条 disconnect 指令，目标端轮询到就自行退出。
        不删除历史记录，已传完的文件仍然可以下载。"""
        body = self.body_json()
        dev = (body.get("device") or "").strip()
        if not dev:
            return self.send_error_json("缺少 device")
        if dev not in self.hub.devices:
            return self.send_error_json("设备不存在")

        with self.hub.cv:
            d = self.hub.device(dev)
            for seq in list(d["pending"]):
                d["pending"].pop(seq, None)
            self.hub.ver += 1
            self.hub.cv.notify_all()

        seq = self.hub.enqueue(dev, "disconnect", {})
        log("→ 断开设备 %s (#%d)" % (dev, seq))
        return self.send_json({"ok": True, "device": dev, "seq": seq})

    def api_history(self):
        body = self.body_json()
        if body.get("op") == "clear":
            n = len(self.hub.history)
            self.hub.history = []
            self.hub._save_history()
            return self.send_json({"ok": True, "cleared": n})
        key = (body.get("key") or "").strip()
        if body.get("op") == "remove" and key:

            ok = self.hub.remove_history(key)
            return self.send_json({"ok": True, "removed": ok})
        return self.send_error_json("不支持的操作")

    def api_sysinfo(self):
        hub = self.hub
        cfg = hub.cfg
        o = cfg["oss"]
        now = time.time()

        def read_file(p, default=""):
            try:
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    return f.read().strip()
            except Exception:
                return default

        os_name = os_version = ""
        try:
            for line in read_file("/etc/os-release").splitlines():
                if line.startswith("PRETTY_NAME="):
                    os_name = line.split("=", 1)[1].strip('"')
                elif line.startswith("VERSION_ID="):
                    os_version = line.split("=", 1)[1].strip('"')
        except Exception:
            pass
        if not os_name:
            os_name = platform.platform()

        cpu_model = ""
        cpu_cores = os.cpu_count() or 0
        try:
            for line in read_file("/proc/cpuinfo").splitlines():
                if line.startswith("model name"):
                    cpu_model = line.split(":", 1)[1].strip()
                    break
        except Exception:
            pass
        if not cpu_model:
            cpu_model = platform.processor() or "未知"

        load1 = load5 = load15 = 0.0
        try:
            load1, load5, load15 = os.getloadavg()
        except Exception:
            pass

        mem_total = mem_avail = mem_used = 0
        swap_total = swap_used = 0
        if os.name == "nt":

            try:
                import ctypes

                class MEMSTAT(ctypes.Structure):
                    _fields_ = [
                        ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                    ]

                ms = MEMSTAT()
                ms.dwLength = ctypes.sizeof(MEMSTAT)
                if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms)):
                    mem_total = ms.ullTotalPhys
                    mem_avail = ms.ullAvailPhys
                    mem_used = mem_total - mem_avail
                    swap_total = ms.ullTotalPageFile
                    swap_used = max(0, swap_total - ms.ullAvailPageFile)
            except Exception:
                pass
        else:
            try:
                info = {}
                for line in read_file("/proc/meminfo").splitlines():
                    k, _, v = line.partition(":")
                    info[k.strip()] = int(v.strip().split()[0]) * 1024
                mem_total = info.get("MemTotal", 0)
                mem_avail = info.get("MemAvailable", info.get("MemFree", 0))
                mem_used = max(0, mem_total - mem_avail)
                swap_total = info.get("SwapTotal", 0)
                swap_used = max(0, swap_total - info.get("SwapFree", 0))
            except Exception:
                pass

        disks = []
        for mount in ("/" if os.name != "nt" else "C:\\",):
            try:
                if os.name == "nt":
                    import shutil
                    tu = shutil.disk_usage(mount)
                    disks.append({"mount": mount, "total": tu.total,
                                  "free": tu.free, "used": tu.used})
                else:
                    st = os.statvfs(mount)
                    total = st.f_blocks * st.f_frsize
                    free = st.f_bavail * st.f_frsize
                    disks.append({"mount": mount, "total": total,
                                  "free": free, "used": total - free})
            except Exception:
                pass
        if os.name != "nt":

            try:
                st = os.statvfs(os.path.dirname(hub.state_dir) or "/")
                total = st.f_blocks * st.f_frsize
                free = st.f_bavail * st.f_frsize
                if not disks or abs(total - disks[0]["total"]) > 1024 * 1024:
                    disks.append({"mount": os.path.dirname(hub.state_dir) or "/",
                                  "total": total, "free": free, "used": total - free})
            except Exception:
                pass

        hostname = socket.gethostname()
        try:
            local_ip = socket.gethostbyname(hostname)
        except Exception:
            local_ip = ""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
            s.close()
        except Exception:
            pass

        rss = 0
        threads = 0
        if os.name == "nt":
            try:
                import ctypes
                from ctypes import wintypes

                PROCESS_QUERY_INFORMATION = 0x0400
                PROCESS_VM_READ = 0x0010
                hproc = ctypes.windll.kernel32.OpenProcess(
                    PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, os.getpid())

                class PMC(ctypes.Structure):
                    _fields_ = [
                        ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t),
                    ]

                if hproc:
                    pmc = PMC()
                    pmc.cb = ctypes.sizeof(PMC)
                    if ctypes.windll.psapi.GetProcessMemoryInfo(
                            hproc, ctypes.byref(pmc), ctypes.sizeof(PMC)):
                        rss = int(pmc.WorkingSetSize)
                    ctypes.windll.kernel32.CloseHandle(hproc)
            except Exception:
                pass
        else:
            try:
                with open("/proc/self/status", "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        if line.startswith("VmRSS:"):
                            rss = int(line.split()[1]) * 1024
                        elif line.startswith("Threads:"):
                            threads = int(line.split()[1])
            except Exception:
                pass
        if not threads:
            try:
                threads = threading.active_count()
            except Exception:
                threads = 0

        online = sum(1 for d in hub.devices.values() if (now - d["last_seen"]) < 50)
        dev_list = []
        for d in hub.devices.values():
            dev_list.append({
                "id": d["id"], "host": d["host"], "user": d["user"],
                "admin": d["admin"], "agent": d["agent"], "ip": d["ip"],
                "online": (now - d["last_seen"]) < 50,
                "last_seen": int(d["last_seen"]),
                "connected_at": int(d["connected_at"]),
                "disks": d["disks"],
            })
        dev_list.sort(key=lambda x: (not x["online"], x["id"]))

        ak = str(o.get("access_key_id") or "")
        ak_masked = (ak[:8] + "…" + ak[-4:]) if len(ak) > 14 else ("已配置" if ak else "未配置")
        sk = str(o.get("access_key_secret") or "")
        sk_masked = "已配置（%d 位）" % len(sk) if sk else "未配置"

        oss_live = False
        oss_err = ""
        if hub.oss:
            try:
                hub.oss.head("__probe_missing__")
                oss_live = True
            except Exception as e:
                msg = str(e)

                if "NoSuchKey" in msg or "404" in msg:
                    oss_live = True
                else:
                    oss_err = msg.split("<Code>")[1].split("</Code>")[0] if "<Code>" in msg else msg[:120]

        public_base = (cfg.get("public_base") or "").strip().rstrip("/")
        public_base_auto = False
        if not public_base:
            host_hdr = (self.headers.get("Host") or "").strip()
            if host_hdr:
                proto = (self.headers.get("X-Forwarded-Proto") or "").strip().lower()
                if proto not in ("http", "https"):
                    proto = "http"
                public_base = "%s://%s" % (proto, host_hdr)
                public_base_auto = True

        return {
            "ok": True,
            "server": {
                "hostname": hostname,
                "local_ip": local_ip,
                "public_base": public_base,
                "public_base_auto": public_base_auto,
                "port": cfg.get("port"),
                "listen": cfg.get("listen"),
                "boot_id": hub.boot_id,
                "uptime": int(now - hub.start),
                "started_at": int(hub.start),
                "now": int(now),
                "pid": os.getpid(),
                "python": platform.python_version(),
                "python_bin": sys.executable,
                "cwd": os.getcwd(),
                "rss": rss,
                "threads": threads,
            },
            "os": {
                "name": os_name,
                "version": os_version,
                "kernel": platform.release(),
                "arch": platform.machine(),
                "platform": platform.platform(),
            },
            "cpu": {
                "model": cpu_model,
                "cores": cpu_cores,
                "load1": round(load1, 2),
                "load5": round(load5, 2),
                "load15": round(load15, 2),
                "usage_pct": min(100, round(load1 / cpu_cores * 100, 1)) if cpu_cores else 0,
            },
            "memory": {
                "total": mem_total, "used": mem_used, "available": mem_avail,
                "usage_pct": round(mem_used / mem_total * 100, 1) if mem_total else 0,
                "swap_total": swap_total, "swap_used": swap_used,
            },
            "disks": disks,
            "oss": {
                "endpoint": o.get("endpoint", ""),
                "bucket": o.get("bucket", ""),
                "scheme": o.get("scheme", "https"),
                "part_size": o.get("part_size", 0),
                "download_expires": o.get("download_expires", 0),
                "ak_masked": ak_masked,
                "sk_masked": sk_masked,
                "configured": bool(hub.oss),
                "live": oss_live,
                "error": oss_err,
            },
            "devices": dev_list,
            "devices_online": online,
            "devices_total": len(hub.devices),
            "history_count": len(hub.history),
            "history_bytes": sum(int(h.get("size") or 0) for h in hub.history),
            "sessions": len(hub.sessions),
            "config_path": getattr(hub, "config_path", ""),
            "state_dir": hub.state_dir,
            "account": {
                "username": cfg.get("username", ""),
                "device_key": cfg.get("device_key", ""),
            },
            "trend": hub.sample_metrics(
                min(100, round(load1 / cpu_cores * 100, 1)) if cpu_cores else 0,
                round(mem_used / mem_total * 100, 1) if mem_total else 0,
            ),
        }

    def api_account(self):
        body = self.body_json()
        hub = self.hub
        cfg = hub.cfg

        old_pwd = str(body.get("old_password") or "")
        new_user = str(body.get("username") or "").strip()
        new_pwd = str(body.get("password") or "")

        if not secrets.compare_digest(old_pwd.encode("utf-8"),
                                      str(cfg["password"]).encode("utf-8")):
            return self.send_error_json("当前密码不正确", 403)

        if not new_user and not new_pwd:
            return self.send_error_json("用户名和新密码都是空的，没有要改的内容")

        if new_user:
            if len(new_user) < 3 or len(new_user) > 32:
                return self.send_error_json("用户名长度需要在 3-32 位之间")
            if not re.match(r"^[0-9A-Za-z_.@-]+$", new_user):
                return self.send_error_json("用户名只能包含字母、数字、下划线、点、@、短横线")

        if new_pwd:
            if len(new_pwd) < 12:
                return self.send_error_json("新密码至少 12 位")
            if new_pwd == old_pwd:
                return self.send_error_json("新密码和当前密码一样")

        try:
            path = getattr(hub, "config_path", "")
            if not path or not os.path.isfile(path):
                return self.send_error_json("找不到配置文件，无法保存", 500)
            with open(path, "r", encoding="utf-8-sig") as f:
                raw = json.load(f)
            if new_user:
                raw["username"] = new_user
                cfg["username"] = new_user
            if new_pwd:
                raw["password"] = new_pwd
                cfg["password"] = new_pwd
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                json.dump(raw, f, ensure_ascii=False, indent=2)
                f.write("\n")
            try:
                os.chmod(tmp, 0o600)
            except Exception:
                pass
            os.replace(tmp, path)
        except Exception as e:
            return self.send_error_json("保存失败: %s" % e, 500)

        if new_pwd:
            hub.sessions.clear()
        log("账号已更新：用户名=%s 密码=%s" % (new_user or "(未改)", "已改" if new_pwd else "(未改)"))

        return self.send_json({
            "ok": True,
            "username": cfg["username"],
            "relogin": bool(new_pwd),
        })

    def check_device_key(self):
        key = self.headers.get("X-Device-Key") or ""
        if not secrets.compare_digest(key.encode("utf-8"),
                                      str(self.hub.cfg["device_key"]).encode("utf-8")):
            self.send_error_json("device key 无效", 403)
            return False
        return True

    def api_hello(self):
        if not self.check_device_key():
            return
        body = self.body_json()
        dev_id = re.sub(r"[^0-9A-Za-z_.:-]", "", str(body.get("machine") or ""))[:64]
        if not dev_id:
            return self.send_error_json("缺少 machine")
        with self.hub.cv:
            d = self.hub.touch(dev_id, self.client_ip())
            first = not d["connected_at"]
            d["host"] = str(body.get("host") or dev_id)[:64]
            d["user"] = str(body.get("user") or "")[:64]
            d["admin"] = bool(body.get("admin"))
            d["agent"] = str(body.get("agent") or "")[:32]
            d["disks"] = body.get("disks") or d["disks"]
            d["roots"] = body.get("roots") or d["roots"]
            if first:
                d["connected_at"] = time.time()
            self.hub.ver += 1
            self.hub.cv.notify_all()
        log("目标端连入 %s (%s\\%s)%s" % (dev_id, d["host"], d["user"], " [管理员]" if d["admin"] else ""))
        return self.send_json({
            "ok": True, "device": dev_id, "server_time": int(time.time()),
            "part_size": self.hub.cfg["oss"]["part_size"],
            "oss": bool(self.hub.oss),
            "boot_id": self.hub.boot_id,
        })

    def api_poll(self):
        if not self.check_device_key():
            return
        body = self.body_json()
        dev_id = str(body.get("machine") or "")
        if not dev_id:
            return self.send_error_json("缺少 machine")
        self.hub.touch(dev_id, self.client_ip())

        d = self.hub.device(dev_id)
        changed = False
        if body.get("transport"):
            tr = str(body["transport"]).strip().lower()
            if tr in ("oss", "auto", "ipv6") and d.get("transport") != tr:
                d["transport"] = tr
                changed = True
        if body.get("ipv6_enabled") is not None:
            en = bool(body["ipv6_enabled"])
            if bool(d.get("ipv6_enabled")) != en:
                d["ipv6_enabled"] = en
                changed = True

        if "v6" in body:
            v6 = body["v6"]
            if isinstance(v6, dict) and v6.get("port"):
                new_v6 = {"port": int(v6["port"]),
                          "addrs": [str(a) for a in (v6.get("addrs") or [])][:8],
                          "files": int(v6.get("files") or 0),
                          "at": int(time.time())}
                old_v6 = d.get("v6") or {}
                if (old_v6.get("port") != new_v6["port"]
                        or old_v6.get("files") != new_v6["files"]
                        or old_v6.get("addrs") != new_v6["addrs"]):
                    d["v6"] = new_v6
                    changed = True
            elif d.get("v6"):
                d["v6"] = None
                changed = True

        if changed:
            with self.hub.cv:
                self.hub.ver += 1
                self.hub.cv.notify_all()
        after = int(body.get("after") or 0)
        wait = min(float(body.get("wait") or 25), 28.0)
        cmds = self.hub.poll_device(dev_id, after, wait)
        return self.send_json({"ok": True, "cmds": cmds, "server_time": int(time.time()),
                               "boot_id": self.hub.boot_id})

    def api_report(self):
        if not self.check_device_key():
            return
        body = self.body_json()
        dev_id = str(body.get("machine") or "")
        if not dev_id:
            return self.send_error_json("缺少 machine")
        self.hub.touch(dev_id, self.client_ip())
        if body.get("job") is not None:
            self.hub.put_job(dev_id, body["job"])
        seq = body.get("seq")
        if seq:
            self.hub.put_result(dev_id, int(seq), bool(body.get("ok", True)),
                                body.get("data"), body.get("error") or "")
        return self.send_json({"ok": True, "cancel": self.hub.cancel_pending(dev_id)})

    def api_web_oss(self):
        """浏览器端直传 OSS 的签名服务（登录态）。

        字节由浏览器直接 PUT 到 OSS，中继只签发 URL，不碰文件内容。
        用于反向上传：用户把本机文件传上 OSS，再让目标电脑拉下去。
        """
        if not self.hub.oss:
            return self.send_error_json("OSS 未配置，无法反向上传", 503)
        body = self.body_json()
        op = body.get("op")
        oss = self.hub.oss
        key = str(body.get("key") or "").strip()
        if op not in ("putinit", "putsign", "putcomplete", "putabort", "putsingle", "putfinish"):
            return self.send_error_json("未知 op")
        if not key:
            return self.send_error_json("缺少 key")

        if "/" in key or "\\" in key or key.startswith("."):
            return self.send_error_json("key 不合法")
        log("web OSS op=%s key=%s" % (op, key[:12]))
        try:
            if op == "putsingle":
                url = oss.presign("PUT", key, expires=7200,
                                  content_type="application/octet-stream")
                return self.send_json({"ok": True, "url": url})
            if op == "putinit":
                upload_id = oss.initiate_multipart(key)
                part_size = self.hub.cfg["oss"]["part_size"]
                return self.send_json({"ok": True, "upload_id": upload_id,
                                       "part_size": part_size})
            if op == "putsign":
                nums = [int(n) for n in (body.get("parts") or [])]
                if not nums or len(nums) > 200:
                    return self.send_error_json("parts 数量不合法")
                urls = {str(n): oss.presign_part(key, body.get("upload_id"), n)
                        for n in nums}
                return self.send_json({"ok": True, "urls": urls})
            if op == "putcomplete":
                oss.complete_multipart(key, body.get("upload_id"),
                                       body.get("parts") or [])
                return self.send_json({"ok": True})
            if op == "putfinish":

                upload_id = body.get("upload_id")
                parts = oss.list_parts(key, upload_id)
                if not parts:
                    return self.send_json({"ok": False, "error": "没有已上传的分片"}, 400)
                expect = int(body.get("count") or 0)
                if expect and len(parts) != expect:
                    return self.send_json(
                        {"ok": False,
                         "error": "分片数量不符：应有 %d 片，实际 %d 片" % (expect, len(parts))},
                        400)
                oss.complete_multipart(
                    key, upload_id,
                    [{"PartNumber": p["n"], "ETag": p["etag"]} for p in parts])
                return self.send_json({"ok": True, "parts": len(parts)})
            if op == "putabort":
                oss.abort_multipart(key, body.get("upload_id"))
                return self.send_json({"ok": True})
        except OSSError as exc:
            log("web OSS 错误:", exc)
            return self.send_json({"ok": False, "error": str(exc)}, 502)

    def api_target_oss(self):
        if not self.check_device_key():
            return
        if not self.hub.oss:
            return self.send_error_json("OSS 未配置", 503)
        body = self.body_json()
        op = body.get("op")
        log("OSS op=%s key=%s" % (op, str(body.get("key"))[:12]))
        oss = self.hub.oss
        key = str(body.get("key") or "")
        try:
            if op == "init":
                upload_id = oss.initiate_multipart(key)
                return self.send_json({"ok": True, "upload_id": upload_id,
                                       "part_size": self.hub.cfg["oss"]["part_size"]})
            if op == "sign":
                nums = [int(n) for n in (body.get("parts") or [])]
                urls = {str(n): oss.presign_part(key, body.get("upload_id"), n) for n in nums}
                return self.send_json({"ok": True, "urls": urls})
            if op == "single":
                return self.send_json({"ok": True,
                                       "url": oss.presign("PUT", key, expires=7200,
                                                          content_type="application/octet-stream")})
            if op == "parts":
                return self.send_json({"ok": True, "parts": oss.list_parts(key, body.get("upload_id"))})
            if op == "complete":
                raw = oss.complete_multipart(key, body.get("upload_id"), body.get("parts") or [])
                return self.send_json({"ok": True, "raw": raw[:300].decode("utf-8", "replace")})
            if op == "abort":
                oss.abort_multipart(key, body.get("upload_id"))
                return self.send_json({"ok": True})
            if op == "head":
                return self.send_json({"ok": True, "head": oss.head(key)})
            if op == "get":

                keys = body.get("keys")
                if isinstance(keys, list) and keys:
                    if len(keys) > 100:
                        return self.send_error_json("keys 太多")
                    urls = {str(k): oss.presign_get(str(k), expires=7200)
                            for k in keys}
                    return self.send_json({"ok": True, "urls": urls})
                return self.send_json({"ok": True,
                                       "url": oss.presign_get(key, expires=7200)})
            return self.send_error_json("未知 op")
        except OSSError as exc:
            log("OSS 错误:", exc)
            return self.send_json({"ok": False, "error": str(exc)}, 502)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(BASE_DIR, "config.json"))
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    args = ap.parse_args()

    cfg = load_config(args.config)
    host = args.host or cfg["listen"]
    port = args.port or cfg["port"]

    hub = Hub(cfg)
    hub.config_path = os.path.abspath(args.config)
    Handler.hub = hub

    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    log("中继已启动 http://%s:%d  (web 目录: %s)" % (host, port, WEB_DIR))
    if cfg["password"] == DEFAULT_CONFIG["password"]:
        log("[注意] 仍在使用默认密码，请修改 config.json")
    if cfg["device_key"] == DEFAULT_CONFIG["device_key"]:
        log("[注意] 仍在使用默认 device_key，请修改 config.json")
    if hub.dev_open:
        log("[警告] RELAY_DEV_OPEN=1 已开启：本机回环地址可免登录，切勿在生产环境使用")

    def stop(signum, _frame):
        log("收到信号 %s，正在退出" % signum)
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    try:
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
    except Exception:
        pass

    try:
        httpd.serve_forever(poll_interval=0.3)
    finally:
        httpd.server_close()
        log("已退出")

if __name__ == "__main__":
    main()
