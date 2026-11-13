
"""反向上传端到端：假 OSS + 真实中继 + 真实 worker。

模拟浏览器的行为（真实浏览器里是 fetch/XHR 直传 OSS，这里用 urllib）：
  1. 登录中继
  2. 调 /api/oss 拿预签名 URL，把文件 PUT 到假 OSS
  3. 调 /api/cmd 发 pull 指令
  4. 目标端从假 OSS 拉下来落盘
  5. 校验落盘文件的 SHA256 与源文件一致

覆盖：
  小文件单点上传 / 大文件分片上传 / 落盘内容一致 /
  同名自动改名 / 非法文件名被拒 / 目标目录自动创建 /
  分片数不符被拒 / 未登录不能调 /api/oss / key 校验
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.join(BASE, "server")
PORT = 8098
OSS_PORT = 9100
TMP = tempfile.mkdtemp(prefix="reve2e_")
DEVICE_KEY = "rev-e2e-key-0123456789abcdef"
PASSWORD = "rev-pass"
PART = 100 * 1024

PASS = 0
FAIL = 0

def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        print("  [PASS] %s" % name)
        PASS += 1
    else:
        print("  [FAIL] %s %s" % (name, extra))
        FAIL += 1

def sha_bytes(b):
    return hashlib.sha256(b).hexdigest()

def sha_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()

class Client:
    def __init__(self, base):
        self.base = base
        self.cookie = None

    def post(self, path, body, timeout=60, reraise=False):
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data,
                                     headers={"Content-Type": "application/json"})
        if self.cookie:
            req.add_header("Cookie", self.cookie)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                for k, v in r.headers.items():
                    if k.lower() == "set-cookie":
                        self.cookie = v.split(";")[0]
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            if reraise:
                raise
            try:
                d = json.loads(raw)
                d.setdefault("_status", e.code)
                return d
            except Exception:
                return {"ok": False, "_status": e.code,
                        "error": "HTTP %d: %s" % (e.code, raw[:200])}

    def get(self, path, timeout=30):
        req = urllib.request.Request(self.base + path)
        if self.cookie:
            req.add_header("Cookie", self.cookie)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()

def put_url(url, blob):
    req = urllib.request.Request(url, data=blob, method="PUT",
                                 headers={"Content-Type": "application/octet-stream"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.status, (r.headers.get("ETag") or "")

def wait_cmd(c, dev, action, args, timeout=120):
    r = c.post("/api/cmd", {"device": dev, "action": action, "args": args})
    if not r.get("ok"):
        return {"ok": False, "error": r.get("error")}
    seq = r["seq"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        snap = c.post("/api/events", {"ver": 0, "wait": 0.4})
        for d in snap.get("devices") or []:
            if d.get("id") != dev:
                continue
            res = (d.get("results") or {}).get(str(seq))
            if res and res.get("seq") == seq:
                return res
        time.sleep(0.2)
    return {"ok": False, "error": "等待结果超时(seq=%s)" % seq}

def wait_job_done(c, dev, timeout=180):
    """等目标端把作业上报成 done / error"""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        snap = c.post("/api/events", {"ver": 0, "wait": 0.4})
        for d in snap.get("devices") or []:
            if d.get("id") != dev:
                continue
            job = d.get("job") or {}
            last = job
            if job.get("phase") in ("已接收", "准备失败") or job.get("state") in ("done", "error"):
                return job
        time.sleep(0.3)
    return last or {}

def main():
    print("临时目录: %s" % TMP)

    cfg = {
        "listen": "127.0.0.1", "port": PORT,
        "username": "admin", "password": PASSWORD,
        "device_key": DEVICE_KEY,
        "session_hours": 12, "max_fail": 5, "lock_seconds": 900,
        "public_base": "http://127.0.0.1:%d" % PORT,

        "state_dir": os.path.join(TMP, "state"),
        "oss": {"endpoint": "127.0.0.1:%d" % OSS_PORT, "bucket": "test",
                "access_key_id": "fake-ak", "access_key_secret": "fake-sk",
                "scheme": "http", "host_override": "127.0.0.1:%d" % OSS_PORT,
                "part_size": PART, "download_expires": 3600},
    }
    cfg_path = os.path.join(TMP, "config.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

    with open(os.path.join(BASE, "client", "agent.ps1"), "r", encoding="utf-8-sig") as f:
        agent_text = f.read()
    workdir = os.path.join(TMP, "work")
    os.makedirs(workdir, exist_ok=True)
    old_line = "$WorkDir = Join-Path $env:LOCALAPPDATA 'WinUpdSvc'"
    if old_line not in agent_text:
        print("找不到 $WorkDir 定义")
        return 1
    agent_text = agent_text.replace(
        old_line, "$WorkDir = '%s'" % workdir.replace("\\", "\\\\"))
    agent_test = os.path.join(TMP, "agent_test.ps1")
    with open(agent_test, "w", encoding="utf-8-sig") as f:
        f.write(agent_text)

    env = dict(os.environ)
    env["RELAY_URL"] = "http://127.0.0.1:%d" % PORT
    env["DEVICE_KEY"] = DEVICE_KEY
    env["TRANSPORT"] = "oss"
    env["PYTHONIOENCODING"] = "utf-8"

    oss_dir = os.path.join(TMP, "ossdata")
    os.makedirs(oss_dir, exist_ok=True)
    oss = subprocess.Popen(
        [sys.executable, os.path.join(BASE, "tools", "fake_oss.py"),
         "--port", str(OSS_PORT), "--dir", oss_dir],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, text=True,
        encoding="utf-8", errors="replace")
    time.sleep(1.5)
    if oss.poll() is not None:
        print("假 OSS 启动失败:\n%s" % oss.stdout.read()[:1500])
        return 1

    relay = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "relay.py"), "--config", cfg_path],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, text=True,
        encoding="utf-8", errors="replace")
    time.sleep(2.0)
    if relay.poll() is not None:
        print("中继启动失败:\n%s" % relay.stdout.read()[:2000])
        oss.terminate()
        return 1

    worker = None
    try:
        c = Client("http://127.0.0.1:%d" % PORT)
        for _ in range(30):
            try:
                c.get("/login", timeout=3)
                break
            except Exception:
                time.sleep(0.3)

        anon = Client("http://127.0.0.1:%d" % PORT)
        r = anon.post("/api/oss", {"op": "putinit", "key": "x" * 32})
        check("未登录 /api/oss 被拒", r.get("ok") is not True, r)

        login = c.post("/api/login", {"username": "admin", "password": PASSWORD})
        check("登录中继", login.get("ok") is True, login)

        r = c.post("/api/oss", {"op": "putinit", "key": "a/b"})
        check("key 含斜杠被拒", r.get("ok") is not True, r)
        r = c.post("/api/oss", {"op": "putinit", "key": ".hidden"})
        check("key 以点开头被拒", r.get("ok") is not True, r)
        r = c.post("/api/oss", {"op": "nope", "key": "x" * 32})
        check("未知 op 被拒", r.get("ok") is not True, r)

        worker = subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", agent_test, "-Mode", "worker"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, text=True,
            encoding="utf-8", errors="replace")

        dev = None
        deadline = time.time() + 60
        while time.time() < deadline:
            snap = c.post("/api/events", {"ver": 0, "wait": 0.3})
            online = [d for d in (snap.get("devices") or []) if d.get("online")]
            if online:
                dev = online[0]["id"]
                break
            time.sleep(0.4)
        check("目标端上线", bool(dev), dev)
        if not dev:
            return 1

        small = b"hello reverse upload " * 10
        big = os.urandom(300 * 1024 + 1234)
        small_key = "a" * 32
        big_key = "b" * 32

        r = c.post("/api/oss", {"op": "putsingle", "key": small_key})
        check("putsingle 返回预签名 URL", r.get("ok") is True and r.get("url"), r)
        st, _ = put_url(r["url"], small)
        check("小文件 PUT 成功", st == 200, st)

        r = c.post("/api/oss", {"op": "putinit", "key": big_key})
        check("putinit 返回 upload_id", r.get("ok") is True and r.get("upload_id"), r)
        upload_id = r.get("upload_id")
        count = (len(big) + PART - 1) // PART
        for n in range(1, count + 1):
            s = c.post("/api/oss", {"op": "putsign", "key": big_key,
                                    "upload_id": upload_id, "parts": [n]})
            if not (s.get("ok") and s.get("urls", {}).get(str(n))):
                check("putsign 第 %d 片" % n, False, s)
                return 1
            put_url(s["urls"][str(n)], big[(n - 1) * PART: n * PART])
        check("大文件 %d 片全部 PUT 成功" % count, True)

        r = c.post("/api/oss", {"op": "putfinish", "key": big_key,
                                "upload_id": upload_id, "count": count + 5})
        check("putfinish 分片数不符被拒", r.get("ok") is not True, r)

        r = c.post("/api/oss", {"op": "putfinish", "key": big_key,
                                "upload_id": upload_id, "count": count})
        check("putfinish 合并成功", r.get("ok") is True, r)

        dest1 = os.path.join(TMP, "收件箱")
        dest2 = os.path.join(TMP, "已有目录")
        os.makedirs(dest2, exist_ok=True)

        with open(os.path.join(dest2, "big.bin"), "wb") as f:
            f.write(b"old")

        items = [
            {"key": small_key, "name": "小文件.txt", "size": len(small), "dir": dest1},
            {"key": big_key, "name": "big.bin", "size": len(big), "dir": dest2},
            {"key": small_key, "name": "bad|name.txt", "size": len(small), "dir": dest2},
        ]

        snap0 = c.post("/api/events", {"ver": 0, "wait": 0.3})
        for d in snap0.get("devices") or []:
            if d.get("id") == dev:
                print("  [诊断] 发 pull 前设备作业 = %r" % (d.get("job"),))

        r = wait_cmd(c, dev, "pull", {"items": items}, timeout=60)
        check("pull 指令被接受", r.get("ok") is True, r)
        alive = worker.poll()
        check("worker 收到指令后仍存活", alive is None, "退出码 %s" % alive)

        job = wait_job_done(c, dev, timeout=180)
        check("作业完成", job.get("phase") == "已接收", job)

        p1 = os.path.join(dest1, "小文件.txt")
        check("目录被自动创建且文件落地", os.path.isfile(p1), p1)
        if os.path.isfile(p1):
            with open(p1, "rb") as f:
                check("小文件内容一致", f.read() == small, "sha 不符")

        p2 = os.path.join(dest2, "big.bin")
        p2b = os.path.join(dest2, "big (1).bin")
        check("同名自动改名，原文件未被覆盖",
              os.path.isfile(p2b) and open(p2, "rb").read() == b"old", (p2, p2b))
        if os.path.isfile(p2b):
            check("大文件分片重组后内容一致", sha_file(p2b) == sha_bytes(big),
                  "%s vs %s" % (sha_file(p2b)[:16], sha_bytes(big)[:16]))

        check("非法文件名被跳过（未落盘）",
              not os.path.exists(os.path.join(dest2, "bad|name.txt")), "出现了非法文件名")

        objs = job.get("objects") or []
        check("作业 objects 报告了 2 个文件", len(objs) == 2, objs)
        check("作业 direction 标记为 in", job.get("direction") == "in", job.get("direction"))

        leftovers = [n for n in os.listdir(dest2) if n.endswith(".relaypart")]
        check("没有 .relaypart 残留", not leftovers, leftovers)

        print("")
        print("=" * 70)
        print("结果：通过 %d / 失败 %d" % (PASS, FAIL))
        print("=" * 70)
        return 0 if FAIL == 0 else 1
    finally:
        if worker:
            try:
                worker.terminate()
                worker.wait(timeout=10)
            except Exception:
                try:
                    worker.kill()
                except Exception:
                    pass
        relay.terminate()
        oss.terminate()
        try:
            relay.wait(timeout=8)
        except Exception:
            relay.kill()
        try:
            oss.wait(timeout=8)
        except Exception:
            oss.kill()

        for name, proc in (("中继", relay), ("目标端", worker)):
            if not proc:
                continue
            try:
                tail = proc.stdout.read() or ""
            except Exception:
                tail = ""
            print("")
            print("=" * 70)
            print("  %s 日志（全量）" % name)
            print("=" * 70)
            for line in (tail.strip().splitlines() or ["(空)"]):
                print("  " + line)

        try:
            for fn in sorted(os.listdir(workdir)):
                p = os.path.join(workdir, fn)
                if not os.path.isfile(p):
                    continue
                try:
                    txt = open(p, "r", encoding="utf-8", errors="replace").read()
                except Exception:
                    continue
                print("")
                print("=" * 70)
                print("  worker 文件 %s（末 30 行）" % fn)
                print("=" * 70)
                for line in (txt.strip().splitlines()[-30:] or ["(空)"]):
                    print("  " + line)
        except Exception as exc:
            print("  读取 workdir 失败: %s" % exc)

if __name__ == "__main__":
    sys.exit(main())
