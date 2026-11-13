
"""C2/C3 端到端：本地中继 + 真实 worker，验证 IPv6 直连指令链路。

不碰生产环境。为了让本机（没有公网 IPv6）也能验证，测试副本里
把 ::1 追加进地址列表——这是**仅存在于测试副本**的注入，
生产 agent.ps1 不含此行。
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
PORT = 8097
ROOT = os.path.join(BASE, "server")
TMP = tempfile.mkdtemp(prefix="v6e2e_")
DEVICE_KEY = "v6-e2e-key-0123456789abcdef"
PASSWORD = "v6-pass"

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

    def post(self, path, body, timeout=60):
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

    def get(self, path, timeout=30):
        req = urllib.request.Request(self.base + path)
        if self.cookie:
            req.add_header("Cookie", self.cookie)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()

def wait_cmd(c, dev, action, args, timeout=90):
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

def curl(args, timeout=120):
    p = subprocess.run(["curl.exe"] + args, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout)
    return p.stdout.strip(), p.stderr.strip()

def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()

def main():
    print("临时目录: %s" % TMP)

    cfg = {
        "listen": "127.0.0.1", "port": PORT,
        "username": "admin", "password": PASSWORD,
        "device_key": DEVICE_KEY,
        "session_hours": 12, "max_fail": 5, "lock_seconds": 900,
        "public_base": "http://127.0.0.1:%d" % PORT,
        "oss": {"endpoint": "", "bucket": "", "access_key_id": "",
                "access_key_secret": "", "scheme": "https", "host_override": "",
                "part_size": 67108864, "download_expires": 86400},
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
        print("找不到 $WorkDir 定义")
        return 1
    agent_text = agent_text.replace(
        old_line, "$WorkDir = '%s'" % workdir.replace("\\", "\\\\"))

    anchor = "$out = New-Object System.Collections.ArrayList"
    if anchor not in agent_text:
        print("找不到 Get-V6Addresses 锚点")
        return 1
    agent_text = agent_text.replace(
        anchor, anchor + "\n    [void]$out.Add('::1')   # <TEST-ONLY>")

    agent_test = os.path.join(TMP, "agent_test.ps1")
    with open(agent_test, "w", encoding="utf-8-sig") as f:
        f.write(agent_text)

    env = dict(os.environ)
    env["RELAY_URL"] = "http://127.0.0.1:%d" % PORT
    env["DEVICE_KEY"] = DEVICE_KEY
    env["TRANSPORT"] = "auto"
    env["PYTHONIOENCODING"] = "utf-8"

    relay = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "relay.py"), "--config", cfg_path],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, text=True,
        encoding="utf-8", errors="replace")
    time.sleep(2.0)
    if relay.poll() is not None:
        print("中继启动失败:\n%s" % relay.stdout.read()[:2000])
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
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, text=True,
            encoding="utf-8", errors="replace")

        dev = None
        snap = {}
        deadline = time.time() + 60
        while time.time() < deadline:
            snap = c.post("/api/events", {"ver": 0, "wait": 0.3})
            devs = snap.get("devices") or []
            if devs:
                dev = devs[0]["id"]
                break
            time.sleep(0.5)
        check("worker 上线", dev is not None, snap)
        if not dev:
            return 1
        print("     设备ID: %s" % dev)

        devinfo = {}
        for _ in range(40):
            snap = c.post("/api/events", {"ver": 0, "wait": 0.3})
            devinfo = next((d for d in (snap.get("devices") or [])
                            if d["id"] == dev), {})
            if devinfo.get("transport") == "auto":
                break
            time.sleep(0.25)
        print("     设备字段: %s" % json.dumps(
            {k: devinfo.get(k) for k in ("id", "transport", "ipv6_enabled", "v6")},
            ensure_ascii=False))
        if worker.poll() is not None:
            print("     worker 已退出，输出:\n%s"
                  % (worker.stdout.read() or "")[:2000])
        check("中继记录了 transport=auto", devinfo.get("transport") == "auto",
              devinfo.get("transport"))
        check("中继记录了 ipv6_enabled=True", devinfo.get("ipv6_enabled") is True,
              devinfo.get("ipv6_enabled"))
        check("尚未起任务时 v6 为空", devinfo.get("v6") in (None, {}), devinfo.get("v6"))

        box = os.path.join(TMP, "box")
        os.makedirs(box, exist_ok=True)
        f_small = os.path.join(box, "小文件.txt")
        with open(f_small, "w", encoding="utf-8") as f:
            f.write("IPv6 直连测试内容 0123456789" * 100)

        f_big = os.path.join(box, "大文件.bin")
        with open(f_big, "wb") as f:
            f.write(os.urandom(8 * 1024 * 1024))

        tok = "toke2e0123456789"
        files = [
            {"id": "s1", "token": tok, "path": f_small, "name": "小文件.txt"},
            {"id": "b1", "token": tok, "path": f_big, "name": "大文件.bin"},
        ]

        r = wait_cmd(c, dev, "v6prep", {"files": files, "port": 0}, timeout=180)
        data = r.get("data") or {}
        check("v6prep 成功", r.get("ok") and data.get("ok") is True, r)
        port = data.get("port")
        addrs = data.get("addrs") or []
        check("拿到监听端口", isinstance(port, int) and port > 1024, port)
        check("地址列表含 ::1（测试注入）", "::1" in addrs, addrs)
        check("登记 2 个文件", data.get("files") == 2, data.get("files"))
        if not port:
            return 1

        base = "http://[::1]:%d" % port

        snap = c.post("/api/events", {"ver": 0, "wait": 0.4})
        devinfo = next((d for d in (snap.get("devices") or []) if d["id"] == dev), {})
        v6 = devinfo.get("v6") or {}
        check("中继记录了 v6.port", v6.get("port") == port, v6)
        check("中继记录了 v6.files", v6.get("files") == 2, v6)

        out1 = os.path.join(TMP, "dl_small.txt")
        code, err = curl(["-s", "-o", out1, "-w", "%{http_code}", base + "/f/%s/s1" % tok])
        check("小文件下载 200", code == "200", (code, err))
        check("小文件字节一致", os.path.isfile(out1) and sha(out1) == sha(f_small), "")

        out2 = os.path.join(TMP, "dl_big.bin")
        code, err = curl(["-s", "-o", out2, "-w", "%{http_code}", base + "/f/%s/b1" % tok],
                         timeout=300)
        check("8MB 下载 200", code == "200", (code, err))
        check("8MB 字节一致", os.path.isfile(out2) and sha(out2) == sha(f_big), "")

        out3 = os.path.join(TMP, "dl_r.bin")
        code, err = curl(["-s", "-o", out3, "-r", "1048576-2097151",
                          "-w", "%{http_code}", base + "/f/%s/b1" % tok])
        check("Range 下载 206", code == "206", (code, err))
        if os.path.isfile(out3):
            with open(f_big, "rb") as f:
                f.seek(1048576)
                want = f.read(1048576)
            with open(out3, "rb") as f:
                got = f.read()
            check("Range 内容正确", got == want, "%d vs %d" % (len(got), len(want)))

        code, err = curl(["-s", "-o", "NUL", "-w", "%{http_code}", base + "/f/badtoken/s1"])
        check("错误令牌 403", code == "403", (code, err))

        code, err = curl(["-s", "-o", "NUL", "-w", "%{http_code}", base + "/f/%s/zzz" % tok])
        check("未知编号 404", code == "404", (code, err))

        r = wait_cmd(c, dev, "v6stat", {})
        st = r.get("data") or {}
        check("v6stat 运行中", st.get("running") is True, st)
        check("v6stat 端口一致", st.get("port") == port, st)
        check("v6stat 已统计命中", (st.get("hits") or 0) >= 3, st)
        check("v6stat 已统计下发字节", (st.get("served") or 0) >= 8 * 1024 * 1024, st.get("served"))
        print("     累计下发 %.2f MB / 命中 %s 次"
              % ((st.get("served") or 0) / 1048576.0, st.get("hits")))

        r = wait_cmd(c, dev, "v6stop", {})
        check("v6stop 返回", (r.get("data") or {}).get("stopped") is True, r)
        code, err = curl(["-s", "--max-time", "3", "-o", "NUL", "-w", "%{http_code}",
                          base + "/ping"])
        check("停止后连不上", code in ("", "000") or code.startswith("0"), (code, err))

        snap = c.post("/api/events", {"ver": 0, "wait": 0.4})
        devinfo = next((d for d in (snap.get("devices") or []) if d["id"] == dev), {})
        if devinfo.get("v6") not in (None, {}):

            for _ in range(20):
                time.sleep(0.3)
                snap = c.post("/api/events", {"ver": 0, "wait": 0.3})
                devinfo = next((d for d in (snap.get("devices") or [])
                                if d["id"] == dev), {})
                if devinfo.get("v6") in (None, {}):
                    break
        check("停止后中继清空 v6", devinfo.get("v6") in (None, {}), devinfo.get("v6"))

    finally:
        for p in (worker, relay):
            if p is None:
                continue
            try:
                p.kill()
            except Exception:
                pass

    print("")
    print("=" * 70)
    print("结果：通过 %d / 失败 %d" % (PASS, FAIL))
    print("=" * 70)
    return 0 if FAIL == 0 else 1

if __name__ == "__main__":
    sys.exit(main())
