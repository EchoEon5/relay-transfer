
"""目标端代理的「参考实现」/ 本地联调替身。

作用有两个：
  1. 在本机（Windows/Linux）跑起来，验证中继 + 控制页全链路，不需要真机
  2. 作为目标端 bat 内嵌 PowerShell 的**协议规范参考**——PowerShell 版本照着实现即可

用法：
  python mock_target.py --base http://127.0.0.1:8080 --key <device_key> --machine PC-TEST
"""

import argparse
import json
import os
import shutil
import sys
import threading
import time
import uuid
import urllib.error
import urllib.request

TIMEOUT = 60

class CmdError(Exception):
    pass

class Agent:
    def __init__(self, base, key, machine, name=None):
        self.base = base.rstrip("/")
        self.key = key
        self.machine = machine
        self.name = name or os.environ.get("COMPUTERNAME") or machine
        self.user = os.environ.get("USERNAME") or os.environ.get("USER") or "user"
        self.admin = _is_admin()
        self.part_size = 64 * 1024 * 1024
        self.oss_ready = False
        self.last_seq = 0
        self.cancel_flag = threading.Event()
        self.job_lock = threading.Lock()
        self.job = {"state": "idle"}

    def post(self, path, obj, timeout=TIMEOUT):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.base + path, data=data,
            headers={"Content-Type": "application/json", "X-Device-Key": self.key},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise CmdError("HTTP %s: %s" % (e.code, e.read()[:300].decode("utf-8", "replace"))) from None

    def hello(self):
        disks, roots = scan_roots()
        r = self.post("/api/t/hello", {
            "machine": self.machine, "host": self.name, "user": self.user,
            "admin": self.admin, "agent": "mock/1.0", "disks": disks, "roots": roots,
        })
        self.part_size = r.get("part_size", self.part_size)
        self.oss_ready = bool(r.get("oss"))
        print("[agent] 已连接中继，part_size=%d oss=%s" % (self.part_size, self.oss_ready))

    def run(self):
        backoff = 1.0
        while True:
            try:
                self.hello()
                backoff = 1.0
                while True:
                    r = self.post("/api/t/poll", {"machine": self.machine, "after": self.last_seq, "wait": 25}, timeout=60)
                    for c in r.get("cmds") or []:
                        self.last_seq = max(self.last_seq, int(c.get("seq") or 0))
                        threading.Thread(target=self.handle, args=(c,), daemon=True).start()
            except Exception as exc:
                print("[agent] 断线重连:", exc)
                time.sleep(backoff)
                backoff = min(backoff * 1.6, 15.0)

    def handle(self, c):
        seq = c.get("seq")
        action = c.get("action")
        args = c.get("args") or {}
        print("[agent] <- #%s %s" % (seq, action))
        try:
            if action == "list":
                data = self.list_dir(args.get("path") or "")
            elif action == "size":
                data = self.size_of(args.get("paths") or [])
            elif action == "push":
                threading.Thread(target=self.run_push, args=(args,), daemon=True).start()
                data = {"accepted": True}
            elif action == "cancel":
                self.cancel_flag.set()
                data = {"cancelled": True}
            elif action == "ping":
                data = {"pong": int(time.time())}
            else:
                raise CmdError("未知指令 %s" % action)
            self.post("/api/t/report", {"machine": self.machine, "seq": seq, "ok": True, "data": data})
        except Exception as exc:
            self.post("/api/t/report", {"machine": self.machine, "seq": seq, "ok": False,
                                        "error": str(exc), "data": None})

    def list_dir(self, path):
        path = normalize(path)
        if not path:
            raise CmdError("路径为空")
        entries, skipped = [], 0
        try:
            with os.scandir(path) as it:
                for e in it:
                    try:
                        is_dir = e.is_dir(follow_symlinks=False)
                        st = e.stat(follow_symlinks=False)
                        entries.append({
                            "name": e.name, "is_dir": is_dir,
                            "size": 0 if is_dir else st.st_size,
                            "mtime": int(st.st_mtime), "path": e.path,
                        })
                    except (OSError, PermissionError):
                        skipped += 1
        except PermissionError:
            raise CmdError("无权访问该目录")
        except FileNotFoundError:
            raise CmdError("路径不存在")
        except OSError as exc:
            raise CmdError("读取失败: %s" % exc)
        entries.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
        return {"path": path, "parent": parent_of(path), "entries": entries, "skipped": skipped}

    def size_of(self, paths):
        total = files = dirs = skipped = 0
        for p in paths:
            p = normalize(p)
            if os.path.isdir(p):
                dirs += 1
                for root, dnames, fnames in os.walk(p, onerror=lambda e: None):
                    for f in fnames:
                        try:
                            total += os.path.getsize(os.path.join(root, f))
                            files += 1
                        except OSError:
                            skipped += 1
            elif os.path.isfile(p):
                try:
                    total += os.path.getsize(p)
                    files += 1
                except OSError:
                    skipped += 1
            else:
                skipped += 1
        return {"total": total, "files": files, "dirs": dirs, "skipped": skipped}

    def run_push(self, args):
        items = args.get("items") or []
        self.cancel_flag.clear()
        sizes = []
        for it in items:
            sz = it.get("size")
            if not sz:
                p = normalize(it.get("path") or "")
                sz = self.size_of([p])["total"] if os.path.exists(p) else 0
            sizes.append(int(sz or 0))
        total = sum(sizes)
        done = 0
        started = time.time()
        objects = []
        speed = 22 * 1024 * 1024
        self.job = {"state": "running", "phase": "上传中", "total": total, "done": 0,
                    "speed": 0, "eta": 0, "skipped": 0, "objects": []}
        self.push_job()
        for i, it in enumerate(items):
            if self.cancel_flag.is_set():
                self.job.update({"state": "cancelled", "message": "已取消"})
                self.push_job()
                return
            remain = sizes[i]
            while remain > 0:
                if self.cancel_flag.is_set():
                    self.job.update({"state": "cancelled", "message": "已取消"})
                    self.push_job()
                    return
                step = min(remain, 3 * 1024 * 1024)
                remain -= step
                done += step
                el = max(time.time() - started, 0.001)
                sp = done / el
                self.job.update({
                    "done": done, "speed": sp,
                    "eta": (total - done) / sp if sp > 0 else 0,
                    "phase": ("打包中" if it.get("is_dir") else "上传中"),
                })
                self.push_job()
                time.sleep(0.18)
            objects.append({
                "index": i, "key": "mock/%s" % uuid.uuid4().hex,
                "size": sizes[i], "kind": "zip" if it.get("is_dir") else "file",
                "name": it.get("label"),
            })
        self.job.update({"state": "done", "done": total, "speed": 0, "eta": 0,
                         "phase": "已完成", "objects": objects})
        self.push_job()

    def push_job(self):
        try:
            self.post("/api/t/report", {"machine": self.machine, "job": dict(self.job)})
        except Exception:
            pass

def _is_admin():
    try:
        if os.name == "nt":
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        return os.geteuid() == 0
    except Exception:
        return False

def normalize(path):
    if not path:
        return ""
    p = os.path.expandvars(os.path.expanduser(path))
    return os.path.abspath(p)

def parent_of(path):
    p = os.path.dirname(path.rstrip("\\/"))
    if not p or p == path:
        return ""
    return p

def scan_roots():
    disks, roots = [], {}
    if os.name == "nt":
        import string
        for ch in string.ascii_uppercase:
            root = ch + ":\\"
            if os.path.exists(root):
                try:
                    usage = shutil.disk_usage(root)
                    disks.append({"name": ch + ":", "path": root, "label": "",
                                  "total": usage.total, "free": usage.free})
                except OSError:
                    disks.append({"name": ch + ":", "path": root, "label": "",
                                  "total": None, "free": None})
        home = os.path.expanduser("~")
        roots = {
            "userprofile": home,
            "desktop": os.path.join(home, "Desktop"),
            "downloads": os.path.join(home, "Downloads"),
            "documents": os.path.join(home, "Documents"),
        }
    else:
        try:
            usage = shutil.disk_usage("/")
            disks.append({"name": "/", "path": "/", "label": "", "total": usage.total, "free": usage.free})
        except OSError:
            pass
        home = os.path.expanduser("~")
        roots = {"userprofile": home, "desktop": os.path.join(home, "Desktop"),
                 "downloads": os.path.join(home, "Downloads"), "documents": os.path.join(home, "Documents")}
    return disks, roots

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8080")
    ap.add_argument("--key", default="change-this-device-key")
    ap.add_argument("--machine", default="PC-MOCK")
    ap.add_argument("--name", default=None)
    a = ap.parse_args()
    Agent(a.base, a.key, a.machine, a.name).run()

if __name__ == "__main__":
    main()
