
"""驱动真实浏览器，端到端验证反向上传界面（发到目标电脑）。

链路：浏览器选文件 → PUT 到 OSS（本地假 OSS，零真实流量） →
中继下发 pull 指令 → 目标机 Start-PullJob 拉回并落盘。

数据量控制在 ~151 KB：1 KB 走单点 PUT，150 KB 在 100 KB 分片下走 2 片，
既覆盖分片路径又不烧流量。
"""
import base64
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

import websocket

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(os.environ.get("TEMP", "/tmp"), "ui_env_state.json")
SHOT = os.path.join(BASE, "_shots")

def free_port():
    """占一个空闲端口再放开：固定端口会和历次残留进程撞车。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p

OSS_PORT = free_port()

os.makedirs(SHOT, exist_ok=True)

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

oss_dir = os.path.join(os.environ.get("TEMP", "/tmp"), "ui_rev_oss")
os.makedirs(oss_dir, exist_ok=True)
oss_env = dict(os.environ)
oss_env["PYTHONIOENCODING"] = "utf-8"

oss_env["FAKEOSS_VERBOSE"] = "1"
OSS_LOG = os.path.join(os.environ.get("TEMP", "/tmp"), "ui_rev_oss.log")
oss_log_f = open(OSS_LOG, "w", encoding="utf-8")
oss = subprocess.Popen(
    [sys.executable, os.path.join(BASE, "tools", "fake_oss.py"),
     "--port", str(OSS_PORT), "--dir", oss_dir],
    stdout=oss_log_f, stderr=subprocess.STDOUT, env=oss_env)
time.sleep(1.5)
if oss.poll() is not None:
    print("假 OSS 启动失败:\n%s" % open(OSS_LOG, encoding="utf-8").read()[:1200])
    sys.exit(1)
print("假 OSS 已启动 :%d  (trace: %s)" % (OSS_PORT, OSS_LOG))

env = dict(os.environ)
env["UI_OSS_PORT"] = str(OSS_PORT)
env["PYTHONIOENCODING"] = "utf-8"
dev = subprocess.run(
    [sys.executable, os.path.join(BASE, "tests", "dev_ui_env.py")],
    env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
print(dev.stdout.strip())
if dev.returncode != 0:
    print("开发环境启动失败:\n%s" % (dev.stderr or "")[:2000])
    oss.terminate()
    sys.exit(1)

ST = json.load(open(STATE, encoding="utf-8"))
URL = ST["url"]
SANDBOX = ST["sandbox"]
PASSWORD = ST["password"]
WORKDIR = os.path.join(ST["tmp"], "state")

box = os.path.join(SANDBOX, "收件箱")
os.makedirs(box, exist_ok=True)
src_dir = os.path.join(os.environ.get("TEMP", "/tmp"), "ui_rev_src")
os.makedirs(src_dir, exist_ok=True)

f_small = os.path.join(src_dir, "小文件.txt")
f_big = os.path.join(src_dir, "大文件.bin")
with open(f_small, "wb") as f:
    f.write(b"reverse upload via ui " * 47)
with open(f_big, "wb") as f:
    f.write(os.urandom(150 * 1024))

def sha(p):
    import hashlib
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()

ts = json.loads(urllib.request.urlopen("http://127.0.0.1:9222/json").read())
page = next((t for t in ts if t["type"] == "page"), None)
ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=180)
_mid = [0]

EVENTS = []

def send(method, params=None):
    _mid[0] += 1
    mine = _mid[0]
    ws.send(json.dumps({"id": mine, "method": method, "params": params or {}}))
    while True:
        r = json.loads(ws.recv())
        if r.get("id") == mine:
            return r.get("result", {})
        if r.get("method"):
            EVENTS.append(r)

def net_failures():
    """把所有与 OSS 相关的网络失败摊平成可读行。"""
    out = []
    urls = {}
    for e in EVENTS:
        m = e.get("method")
        p = e.get("params") or {}
        if m == "Network.requestWillBeSent":
            urls[p.get("requestId")] = p.get("request", {}).get("url", "")
        elif m == "Network.loadingFailed":
            rid = p.get("requestId")
            out.append({
                "url": urls.get(rid, "?"),
                "error": p.get("errorText"),
                "blocked": p.get("blockedReason"),
                "cors": p.get("corsErrorStatus"),
                "type": p.get("type"),
            })
    return out

def js(expr, await_promise=True):
    r = send("Runtime.evaluate", {
        "expression": "(async()=>{%s})()" % expr,
        "awaitPromise": await_promise,
        "returnByValue": True,
    })
    res = r.get("result", {})
    if res.get("subtype") == "error" or r.get("exceptionDetails"):
        det = r.get("exceptionDetails", {})
        return {"__error": det.get("exception", {}).get("description")
                or res.get("description")}
    return res.get("value")

def shot(name, clip=None):
    p = {"format": "png"}
    if clip:
        p["clip"] = clip
    d = send("Page.captureScreenshot", p)
    with open(os.path.join(SHOT, name + ".png"), "wb") as f:
        f.write(base64.b64decode(d["data"]))

send("Page.enable")
send("DOM.enable")
send("Runtime.enable")
send("Network.enable")

try:

    send("Page.navigate", {"url": URL + "/login"})
    time.sleep(2.5)
    js("""
      await fetch('/api/login',{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({username:'admin',password:%s}),
        credentials:'same-origin'});
      return true;
    """ % json.dumps(PASSWORD))
    send("Page.navigate", {"url": URL + "/"})
    time.sleep(4.0)

    ready = None
    for _ in range(40):
        ready = js("""
          const tab=document.querySelector('[data-tab="transfer"]');
          if(tab) tab.click();
          await new Promise(r=>setTimeout(r,800));
          return { pick: !!document.querySelector('#revPick'),
                   dev: !!(window.currentDev && currentDev()),
                   app: (typeof S !== 'undefined') };
        """)
        if (ready or {}).get("pick"):
            break
        time.sleep(1.0)

    check("页面已登录并渲染出反向上传区", (ready or {}).get("pick") is True, ready)
    check("页面脚本已加载（S 可用）", (ready or {}).get("app") is True, ready)

    online = False
    for _ in range(60):
        v = js("return !!(window.currentDev && currentDev() && currentDev().online);",
               await_promise=True)
        if v is True:
            online = True
            break
        time.sleep(1.0)
    check("目标设备在线", online)

    nav = js("""
      await navigate(%s);
      revRenderDest();
      return { path: S.path, dest: (document.querySelector('#revDest')||{}).textContent };
    """ % json.dumps(box))
    check("左侧导航到目标目录", (nav or {}).get("path") == box, nav)
    check("反向上传区显示目标目录", (nav or {}).get("dest") == box, nav)

    doc = send("DOM.getDocument", {"depth": -1})
    node = send("DOM.querySelector", {"nodeId": doc["root"]["nodeId"],
                                      "selector": "#revPick"})
    send("DOM.setFileInputFiles", {"files": [f_small, f_big],
                                   "nodeId": node["nodeId"]})
    time.sleep(0.6)
    js("""
      const p=document.querySelector('#revPick');
      p.dispatchEvent(new Event('change',{bubbles:true}));
      return 1;
    """)
    time.sleep(0.5)

    sel = js("""
      const box=document.querySelector('#revSel');
      const btn=document.querySelector('#btnRevSend');
      return { hidden: box.hidden, text: box.textContent, btnHidden: btn.hidden,
               count: REV.files.length,
               names: REV.files.map(f=>f.name) };
    """)
    check("已选文件数正确", (sel or {}).get("count") == 2, sel)
    check("已选列表显示两个文件名",
          "小文件.txt" in ((sel or {}).get("text") or "")
          and "大文件.bin" in ((sel or {}).get("text") or ""), sel)
    check("发送按钮已出现", (sel or {}).get("btnHidden") is False, sel)
    shot("rev_01_选择文件")

    js("window.confirm = () => true;")
    js("document.querySelector('#btnRevSend').click(); return 1;",
       await_promise=True)

    sent = False
    note = ""
    for _ in range(90):
        r = js("""
          const n=document.querySelector('#revNote');
          const btn=document.querySelector('#btnRevSend');
          return { note: n.textContent||'', hidden: n.hidden,
                   btn: btn.textContent, busy: !!REV.busy,
                   stat: (document.querySelector('#revStat')||{}).textContent||'' };
        """) or {}
        note = r.get("note") or ""
        if "已发送" in note:
            sent = True
            break
        if "失败" in note:
            break
        time.sleep(1.0)
    check("界面提示已发送", sent, note)
    check("提示里是目标目录", box in note, note)

    if not sent:
        fails = net_failures()
        print("  --- 浏览器网络失败 ---")
        if not fails:
            print("    (没有任何 loadingFailed：请求可能根本没发起)")
        for f in fails:
            print("    url     : %s" % f["url"][:140])
            print("    error   : %s" % f["error"])
            print("    blocked : %s" % f["blocked"])
            print("    cors    : %s" % f["cors"])
        olog = open(OSS_LOG, encoding="utf-8", errors="replace").read()
        print("  --- 假 OSS 收到过的请求 ---")
        print("    " + (olog.strip().replace("\n", "\n    ") or "(一条都没有)"))
        reqs = [e["params"]["request"]["url"]
                for e in EVENTS
                if e.get("method") == "Network.requestWillBeSent"]
        print("  --- 页面发出的 OSS 相关请求 ---")
        for u in reqs:
            if str(OSS_PORT) in u:
                print("    %s" % u[:160])
    shot("rev_02_上传完成")

    p1 = os.path.join(box, "小文件.txt")
    p2 = os.path.join(box, "大文件.bin")
    landed = False
    for _ in range(120):
        if os.path.isfile(p1) and os.path.isfile(p2):
            try:
                if os.path.getsize(p1) == os.path.getsize(f_small) \
                        and os.path.getsize(p2) == os.path.getsize(f_big):
                    landed = True
                    break
            except OSError:
                pass
        time.sleep(1.0)
    check("两个文件都落到目标目录", landed, (p1, p2))
    if landed:
        check("小文件字节一致", sha(p1) == sha(f_small))
        check("大文件字节一致（分片重组正确）", sha(p2) == sha(f_big))

    job = js("""
      const j = S.job || {};
      return { phase: (document.querySelector('#jobPhase')||{}).textContent,
               direction: j.direction, state: j.state, total: j.total,
               objects: (j.objects||[]).length };
    """)
    check("传输进度显示接收中/已完成",
          ((job or {}).get("phase") or "") not in ("", "空闲"), job)
    check("作业标记为 direction=in", (job or {}).get("direction") == "in", job)
    check("作业报了 2 个文件", (job or {}).get("objects") == 2, job)

    rect = js("""
      const el=document.querySelector('.revbox');
      el.scrollIntoView({block:'center'});
      await new Promise(r=>setTimeout(r,350));
      const b=el.getBoundingClientRect();
      const vh=window.innerHeight, y=Math.max(0,b.y);
      const h=Math.min(b.height, vh-y-4);
      return {x:b.x, y:y, width:b.width, height:h};
    """)
    if rect and rect.get("width"):
        shot("rev_03_反向区特写", {"x": rect["x"], "y": rect["y"],
                                   "width": rect["width"], "height": rect["height"],
                                   "scale": 2})
    shot("rev_04_整页")

    lg = os.path.join(ST["tmp"], "work", "agent.log")
    if os.path.isfile(lg):
        tail = open(lg, "r", encoding="utf-8", errors="replace").read()[-6000:]
        check("目标机日志有反向上传记录", "反向上传" in tail, tail[-400:])
        check("目标机日志有接收完成", "接收完成" in tail, tail[-400:])
    else:
        print("  [跳过] 找不到 %s" % lg)
    wl = os.path.join(ST["tmp"], "worker.log")
    if os.path.isfile(wl):
        wtxt = open(wl, "r", encoding="utf-8", errors="replace").read()
        if wtxt.strip():
            print("--- worker stdout ---")
            print(wtxt[-1500:])

    print("")
    print("=" * 70)
    print("结果：通过 %d / 失败 %d" % (PASS, FAIL))
    print("=" * 70)
    sys.exit(0 if FAIL == 0 else 1)
finally:
    try:
        ws.close()
    except Exception:
        pass

    try:
        oss.terminate()
        oss.wait(timeout=5)
    except Exception:
        try:
            oss.kill()
        except Exception:
            pass
    try:
        oss_log_f.close()
    except Exception:
        pass
    st = {}
    try:
        st = json.load(open(STATE, encoding="utf-8"))
    except Exception:
        pass
    for pid_key in ("relay_pid", "worker_pid"):
        pid = st.get(pid_key)
        if not pid:
            continue
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True)
        except Exception:
            pass
