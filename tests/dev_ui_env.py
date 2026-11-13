
"""本地 UI 开发环境：起真实中继 + 真实 PowerShell worker，
在临时沙箱目录里造好样本文件，供浏览器端手工/自动验证文件操作界面。

只操作临时沙箱目录，绝不碰真实安装目录与用户文件。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.join(BASE, "server")
PORT = 8096
DEVICE_KEY = "ui-test-key-0123456789abcdef"
PASSWORD = "uitest123456"

STATE = os.path.join(tempfile.gettempdir(), "ui_env_state.json")

def build_sandbox(root):
    """造样本：嵌套目录、普通文件、只读文件、中文名、空目录"""

    if os.path.isdir(root):
        for dirpath, dirnames, filenames in os.walk(root):
            for fn in filenames:
                p = os.path.join(dirpath, fn)
                try:
                    os.chmod(p, 0o666)
                except OSError:
                    pass
    os.makedirs(root, exist_ok=True)

    def w(rel, text):
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)

    w("readme.txt", "示例文件，用于验证复制/移动/重命名/删除。\n")
    w("报告-2026.txt", "中文文件名测试\n")
    w("sub1/a.txt", "a\n")
    w("sub1/b.txt", "b\n")
    w("sub1/deep/c.txt", "c\n")
    w("sub2/only.txt", "only\n")
    w("copy-target/.keep", "")
    w("move-target/.keep", "")
    os.makedirs(os.path.join(root, "empty-dir"), exist_ok=True)

    ro = os.path.join(root, "readonly.txt")
    with open(ro, "w", encoding="utf-8") as f:
        f.write("只读\n")
    os.chmod(ro, 0o444)

    w("很长的文件名" + "测试" * 20 + ".txt", "long\n")
    return root

def kill_previous():
    """清掉上一轮留下的中继/worker。

    不清会出大问题：残留 worker 用旧的 $WorkDir，却连着同一个中继设备 ID，
    它会抢走指令并把日志写到旧目录，本轮断言全看不到。
    """
    try:
        st = json.load(open(STATE, encoding="utf-8"))
    except Exception:
        return
    for key in ("worker_pid", "relay_pid"):
        pid = st.get(key)
        if not pid:
            continue
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True, timeout=30)
            print("  清理上一轮 %s=%s" % (key, pid))
        except Exception:
            pass

def main():
    kill_previous()
    tmp = tempfile.mkdtemp(prefix="uiarena_")
    sandbox = os.path.join(tmp, "arena")
    build_sandbox(sandbox)
    workdir = os.path.join(tmp, "work")
    os.makedirs(workdir, exist_ok=True)

    with open(os.path.join(BASE, "client", "agent.ps1"), "r", encoding="utf-8-sig") as f:
        agent_text = f.read()
    old = "$WorkDir = Join-Path $env:LOCALAPPDATA 'WinUpdSvc'"
    if old not in agent_text:
        print("找不到 $WorkDir 定义，中止")
        return 1
    agent_text = agent_text.replace(old, "$WorkDir = '%s'" % workdir.replace("\\", "\\\\"))

    anchor = "$out = New-Object System.Collections.ArrayList"
    if anchor in agent_text:
        agent_text = agent_text.replace(
            anchor, anchor + "\n    [void]$out.Add('::1')   # <TEST-ONLY>")
    agent_test = os.path.join(tmp, "agent_test.ps1")
    with open(agent_test, "w", encoding="utf-8-sig") as f:
        f.write(agent_text)

    oss_port = os.environ.get("UI_OSS_PORT")
    if oss_port:
        oss_cfg = {
            "endpoint": "127.0.0.1:%s" % oss_port, "bucket": "test",
            "access_key_id": "fake-ak", "access_key_secret": "fake-sk",
            "scheme": "http", "host_override": "127.0.0.1:%s" % oss_port,
            "part_size": 100 * 1024, "download_expires": 3600,
        }
    else:
        oss_cfg = {
            "endpoint": "", "bucket": "", "access_key_id": "", "access_key_secret": "",
            "scheme": "https", "host_override": "",
            "part_size": 8388608, "download_expires": 86400,
        }

    cfg = {
        "listen": "127.0.0.1",
        "port": PORT,
        "username": "admin",
        "password": PASSWORD,
        "device_key": DEVICE_KEY,
        "session_hours": 12,
        "public_base": "",

        "state_dir": os.path.join(tmp, "state"),
        "oss": oss_cfg,
    }
    cfg_path = os.path.join(tmp, "config.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

    env = dict(os.environ)
    env["RELAY_URL"] = "http://127.0.0.1:%d" % PORT
    env["DEVICE_KEY"] = DEVICE_KEY
    env["TRANSPORT"] = os.environ.get("UI_TRANSPORT", "auto")
    env["PYTHONIOENCODING"] = "utf-8"

    relay_out = open(os.path.join(tmp, "relay.log"), "wb")
    relay = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "relay.py"), "--config", cfg_path],
        cwd=ROOT, stdout=relay_out, stderr=subprocess.STDOUT, env=env,
    )
    time.sleep(2.0)
    if relay.poll() is not None:
        print("中继启动失败")
        return 1

    worker_out = open(os.path.join(tmp, "worker.log"), "wb")
    worker = subprocess.Popen(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", agent_test, "-Mode", "worker"],
        stdout=worker_out, stderr=subprocess.STDOUT, env=env,
    )

    dev = None
    deadline = time.time() + 90
    while time.time() < deadline:
        try:
            req = urllib.request.Request(
                "http://127.0.0.1:%d/api/login" % PORT,
                data=json.dumps({"username": "admin", "password": PASSWORD}).encode(),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as r:
                cookie = ""
                for k, v in r.headers.items():
                    if k.lower() == "set-cookie":
                        cookie = v.split(";")[0]
                r.read()
            req2 = urllib.request.Request(
                "http://127.0.0.1:%d/api/events" % PORT,
                data=json.dumps({"ver": 0, "wait": 0.3}).encode(),
                headers={"Content-Type": "application/json", "Cookie": cookie})
            with urllib.request.urlopen(req2, timeout=10) as r2:
                snap = json.loads(r2.read().decode())
            devs = snap.get("devices") or []
            if devs and devs[0].get("online"):
                dev = devs[0]["id"]
                break
        except Exception:
            pass
        time.sleep(1.0)

    state = {
        "port": PORT, "tmp": tmp, "sandbox": sandbox,
        "relay_pid": relay.pid, "worker_pid": worker.pid,
        "device": dev, "url": "http://127.0.0.1:%d" % PORT,
        "password": PASSWORD,
    }
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

    print("中继地址 : %s" % state["url"])
    print("登录     : admin / %s" % PASSWORD)
    print("沙箱目录 : %s" % sandbox)
    print("设备 ID  : %s" % dev)
    print("状态文件 : %s" % STATE)
    return 0 if dev else 2

if __name__ == "__main__":
    sys.exit(main())
