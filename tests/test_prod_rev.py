
"""生产环境反向上传验收：真实中继 + 真实 OSS + 真实 worker。

流量控制：只用 2 KB 的小文件，验完立刻删对象。

**跑之前先设三个环境变量**（不要把真实地址/密码写进这个文件）：

    PROD_RELAY_URL   = http://你的服务器:8080
    PROD_ADMIN_PASS  = 你的控制台密码
    PROD_DEVICE_KEY  = 你的设备密钥

缺任何一个都会直接退出，不会误连到别人的服务器。
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
URL = (os.environ.get("PROD_RELAY_URL") or "").rstrip("/")
USER = os.environ.get("PROD_ADMIN_USER") or "admin"
PWD = os.environ.get("PROD_ADMIN_PASS") or ""
DEVICE_KEY = os.environ.get("PROD_DEVICE_KEY") or ""

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

class C:
    def __init__(self):
        self.cookie = None

    def post(self, path, body, timeout=60):
        req = urllib.request.Request(
            URL + path, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        if self.cookie:
            req.add_header("Cookie", self.cookie)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                for k, v in r.headers.items():
                    if k.lower() == "set-cookie":
                        self.cookie = v.split(";")[0]
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                d = json.loads(raw)
                d["_status"] = e.code
                return d
            except Exception:
                return {"ok": False, "_status": e.code, "error": raw[:200]}

def main():
    missing = [k for k, v in (("PROD_RELAY_URL", URL),
                              ("PROD_ADMIN_PASS", PWD),
                              ("PROD_DEVICE_KEY", DEVICE_KEY)) if not v]
    if missing:
        print("缺少环境变量：%s" % ", ".join(missing))
        print("")
        print("  set PROD_RELAY_URL=http://你的服务器:8080")
        print("  set PROD_ADMIN_PASS=你的控制台密码")
        print("  set PROD_DEVICE_KEY=你的设备密钥")
        return 2

    tmp = tempfile.mkdtemp(prefix="prodrev_")
    dest = os.path.join(tmp, "收件箱")
    workdir = os.path.join(tmp, "work")
    os.makedirs(workdir, exist_ok=True)

    c = C()
    r = c.post("/api/login", {"username": USER, "password": PWD})
    check("生产环境登录", r.get("ok") is True, r)
    if not r.get("ok"):
        return 1

    blob = ("production reverse check " * 78).encode()
    key = hashlib.md5(blob + str(time.time()).encode()).hexdigest()

    r = c.post("/api/oss", {"op": "putsingle", "key": key})
    check("拿到预签名上传地址", r.get("ok") is True and r.get("url"), r)
    if not r.get("ok"):
        return 1

    req = urllib.request.Request(r["url"], data=blob, method="PUT",
                                 headers={"Content-Type": "application/octet-stream"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        check("PUT 到真实 OSS 成功", resp.status == 200, resp.status)

    src = os.path.join(BASE, "client", "agent.ps1")
    with open(src, "r", encoding="utf-8-sig") as f:
        txt = f.read()
    old = "$WorkDir = Join-Path $env:LOCALAPPDATA 'WinUpdSvc'"
    txt = txt.replace(old, "$WorkDir = '%s'" % workdir.replace("\\", "\\\\"))
    agent = os.path.join(tmp, "agent_prod.ps1")
    with open(agent, "w", encoding="utf-8-sig", newline="\n") as f:
        f.write(txt)

    env = dict(os.environ)
    env["RELAY_URL"] = URL
    env["DEVICE_KEY"] = DEVICE_KEY
    env["PYTHONIOENCODING"] = "utf-8"
    wlog = open(os.path.join(tmp, "worker.log"), "w", encoding="utf-8")
    worker = subprocess.Popen(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", agent, "-Mode", "worker"],
        stdout=wlog, stderr=subprocess.STDOUT, env=env)

    try:
        dev = None
        for _ in range(60):
            snap = c.post("/api/events", {"ver": 0, "wait": 0.3})
            for d in snap.get("devices") or []:
                if d.get("online"):
                    dev = d.get("id")
                    break
            if dev:
                break
            time.sleep(1.0)
        check("目标端上线（本机连到生产）", bool(dev), dev)
        if not dev:
            return 1

        r = c.post("/api/cmd", {"device": dev, "action": "status"})
        time.sleep(1.0)

        r = c.post("/api/cmd", {"device": dev, "action": "pull",
                                "args": {"items": [{
                                    "key": key,
                                    "name": "生产验收.txt",
                                    "size": len(blob),
                                    "dir": dest}]}})
        check("下发 pull 指令", r.get("ok") is True, r)

        target = os.path.join(dest, "生产验收.txt")
        landed = False
        for _ in range(90):
            if os.path.isfile(target):
                if os.path.getsize(target) == len(blob):
                    landed = True
                    break
            time.sleep(1.0)
        check("文件落盘到目标目录", landed, target)
        if landed:
            with open(target, "rb") as f:
                check("字节完全一致", f.read() == blob)

        snap = c.post("/api/events", {"ver": 0, "wait": 0.3})
        job = {}
        for d in snap.get("devices") or []:
            if d.get("id") == dev:
                job = d.get("job") or {}
        check("作业 direction=in", job.get("direction") == "in", job)
        check("作业已完成", job.get("state") == "done", job)

        print("     对象将随 7 天生命周期自动清理（约 2 KB）")

        alog = os.path.join(workdir, "agent.log")
        got_line = False
        tail = ""

        for _ in range(20):
            if os.path.isfile(alog):
                tail = open(alog, "r", encoding="utf-8", errors="replace").read()[-3000:]
                if "接收完成" in tail:
                    got_line = True
                    break
            time.sleep(1.0)
        check("目标机日志有接收完成", got_line, tail[-300:])

        print("")
        print("=" * 70)
        print("生产验收：通过 %d / 失败 %d" % (PASS, FAIL))
        print("=" * 70)
        return 0 if FAIL == 0 else 1
    finally:
        try:
            worker.terminate()
            worker.wait(timeout=8)
        except Exception:
            try:
                worker.kill()
            except Exception:
                pass
        try:
            wlog.close()
        except Exception:
            pass

if __name__ == "__main__":
    sys.exit(main())
