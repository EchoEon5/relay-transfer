
"""端到端集成测试：本地起中继 + 真实 PowerShell worker，验证新增的文件操作指令。

不碰生产环境，全部在本地临时目录跑。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8099
ROOT = os.path.join(BASE, "server")
TMP = tempfile.mkdtemp(prefix="e2e_")
DEVICE_KEY = "e2e-test-key-0123456789abcdef"
PASSWORD = "e2e-pass"

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

class Client:
    def __init__(self, base):
        self.base = base
        self.cookie = None

    def post(self, path, body, timeout=40):
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
            try:
                return json.loads(raw)
            except Exception:
                return {"ok": False, "error": "HTTP %d: %s" % (e.code, raw[:200])}

    def get(self, path, timeout=40):
        req = urllib.request.Request(self.base + path)
        if self.cookie:
            req.add_header("Cookie", self.cookie)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            for k, v in r.headers.items():
                if k.lower() == "set-cookie":
                    self.cookie = v.split(";")[0]
            return r.read()

def wait_cmd(c, dev, action, args, seq_wait=40):
    """发指令并等结果（靠 /api/events 拉快照拿 results）"""
    r = c.post("/api/cmd", {"device": dev, "action": action, "args": args})
    if not r.get("ok"):
        return {"ok": False, "error": r.get("error")}
    seq = r["seq"]
    deadline = time.time() + seq_wait
    seen = False
    while time.time() < deadline:
        snap = c.post("/api/events", {"ver": 0, "wait": 0.4})
        for d in snap.get("devices") or []:
            if d.get("id") != dev:
                continue
            res = (d.get("results") or {}).get(str(seq))
            if res and res.get("seq") == seq:
                seen = True
                return res
        if seen:
            break
    return {"ok": False, "error": "等待结果超时(seq=%s)" % seq}

def main():
    print("临时工作目录: %s" % TMP)

    cfg = {
        "listen": "127.0.0.1",
        "port": PORT,
        "username": "admin",
        "password": PASSWORD,
        "device_key": DEVICE_KEY,
        "session_hours": 12,
        "max_fail": 5,
        "lock_seconds": 900,
        "public_base": "http://127.0.0.1:%d" % PORT,
        "oss": {
            "endpoint": "", "bucket": "", "access_key_id": "",
            "access_key_secret": "", "scheme": "https",
            "host_override": "", "part_size": 67108864, "download_expires": 86400,
        },
    }
    cfg_path = os.path.join(TMP, "config.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

    agent_src = os.path.join(BASE, "client", "agent.ps1")
    with open(agent_src, "r", encoding="utf-8-sig") as f:
        agent_text = f.read()
    workdir = os.path.join(TMP, "work")
    os.makedirs(workdir, exist_ok=True)
    old_line = "$WorkDir = Join-Path $env:LOCALAPPDATA 'WinUpdSvc'"
    if old_line not in agent_text:
        print("找不到 $WorkDir 定义，测试中止")
        return 1
    agent_text = agent_text.replace(old_line, "$WorkDir = '%s'" % workdir.replace("\\", "\\\\"))
    agent_test = os.path.join(TMP, "agent_test.ps1")
    with open(agent_test, "w", encoding="utf-8-sig") as f:
        f.write(agent_text)

    env = dict(os.environ)
    env["RELAY_URL"] = "http://127.0.0.1:%d" % PORT
    env["DEVICE_KEY"] = DEVICE_KEY
    env["PYTHONIOENCODING"] = "utf-8"

    relay = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "relay.py"), "--config", cfg_path],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
    )
    time.sleep(2.0)
    if relay.poll() is not None:
        out = relay.stdout.read().decode("utf-8", "replace")
        print("中继启动失败：\n%s" % out[:2000])
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

        login = c.post("/api/login", {"username": "admin", "password": PASSWORD})
        check("登录中继", login.get("ok") is True, login)

        worker = subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", agent_test, "-Mode", "worker"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
        )

        dev = None
        for _ in range(60):
            snap = c.post("/api/events", {"ver": 0, "wait": 0.3})
            devs = snap.get("devices") or []
            if devs:
                dev = devs[0]["id"]
                break
            time.sleep(0.5)
        check("worker 上线", dev is not None, "未在 30 秒内连上中继")
        if not dev:
            return 1
        print("     设备ID: %s" % dev)

        sandbox = os.path.join(TMP, "sandbox")
        os.makedirs(sandbox, exist_ok=True)
        d_new = os.path.join(sandbox, "新建目录")

        r = wait_cmd(c, dev, "mkdir", {"path": d_new})
        check("mkdir 生效", r.get("ok") and os.path.isdir(d_new), r)

        f_new = os.path.join(sandbox, "note.txt")
        r = wait_cmd(c, dev, "newfile", {"path": f_new, "text": "端到端测试"})
        check("newfile 生效", r.get("ok") and os.path.isfile(f_new), r)
        if os.path.isfile(f_new):
            with open(f_new, "r", encoding="utf-8") as f:
                check("newfile 内容正确", f.read() == "端到端测试")

        r = wait_cmd(c, dev, "rename", {"path": f_new, "name": "改名.txt"})
        f_ren = os.path.join(sandbox, "改名.txt")
        check("rename 生效", r.get("ok") and os.path.isfile(f_ren), r)

        dest = os.path.join(sandbox, "子目录")
        os.makedirs(dest, exist_ok=True)
        r = wait_cmd(c, dev, "copy", {"paths": [f_ren], "dest": dest})
        check("copy 生效", r.get("ok") and os.path.isfile(os.path.join(dest, "改名.txt")), r)

        r = wait_cmd(c, dev, "move", {"paths": [os.path.join(dest, "改名.txt")], "dest": sandbox})
        check("move 生效", r.get("ok"), r)

        r = wait_cmd(c, dev, "exists", {"path": d_new})
        check("exists 正确", r.get("ok") and r.get("data", {}).get("exists") is True, r)

        r = wait_cmd(c, dev, "list", {"path": sandbox})
        data = r.get("data") or {}
        check("list 普通目录", r.get("ok") and len(data.get("entries") or []) > 0, r)
        check("list 返回 denied 字段", "denied" in data, list(data.keys()))

        r = wait_cmd(c, dev, "list", {"path": "C:\\System Volume Information"})
        data = r.get("data") or {}
        check("list 受限目录不报错且标记 denied",
              r.get("ok") is True and data.get("denied") is True and bool(data.get("denied_reason")), r)

        r = wait_cmd(c, dev, "list", {"path": "C:"})
        check("list 盘符根保留尾反斜杠", (r.get("data") or {}).get("path") == "C:\\", r)

        r = wait_cmd(c, dev, "delete", {"paths": [d_new]})
        check("delete 生效", r.get("ok") and not os.path.isdir(d_new), r)

        r = wait_cmd(c, dev, "delete", {"paths": ["C:\\"]})
        failed = ((r.get("data") or {}).get("failed") or [])
        check("delete 盘符根被拒绝", r.get("ok") and len(failed) == 1, r)

        r = c.post("/api/cmd", {"device": dev, "action": "rm_rf_everything", "args": {}})
        check("未知指令被中继拒绝", r.get("ok") is False, r)

        print()
        print("结果：通过 %d / 失败 %d" % (PASS, FAIL))
        return 0 if FAIL == 0 else 1

    finally:
        for p in (worker, relay):
            if p and p.poll() is None:
                try:
                    p.terminate()
                    p.wait(timeout=5)
                except Exception:
                    try:
                        p.kill()
                    except Exception:
                        pass
        shutil.rmtree(TMP, ignore_errors=True)

if __name__ == "__main__":
    sys.exit(main())
