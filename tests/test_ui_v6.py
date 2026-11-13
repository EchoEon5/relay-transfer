
"""C3/C4 界面端到端：真实浏览器 + 真实 worker，验证 IPv6 直连的界面行为。

覆盖：
  C3-1 设备面板显示「传输方式 = IPv6 优先」
  C3-2 IPv6 直连行「待命中」→ 起任务后显示端口
  C3-3 选文件→创建传输任务→走 IPv6，出现直连提示条与带「IPv6 直连」标签的链接
  C3-4 直链可真实下载且字节一致
  C4-1 选文件夹时 auto 模式回落 OSS
  C4-2 仅 IPv6 模式选文件夹被明确拒绝
  C4-3 仅 IPv6 模式 IPv6 不可达时报错且不回落
"""
import base64
import json
import os
import shutil
import sys
import time
import urllib.request

import websocket

STATE = os.path.join(os.environ.get("TEMP", "/tmp"), "ui_env_state.json")
ST = json.load(open(STATE, encoding="utf-8"))
URL = ST["url"]
SANDBOX = ST["sandbox"]
LEAF = os.path.basename(SANDBOX)
SHOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "_shots")
os.makedirs(SHOT, exist_ok=True)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dev_ui_env import build_sandbox

shutil.rmtree(SANDBOX, ignore_errors=True)
build_sandbox(SANDBOX)

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

ts = json.loads(urllib.request.urlopen("http://127.0.0.1:9222/json").read())
page = next((t for t in ts if t["type"] == "page"), None)
ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=180)
_mid = [0]

def send(method, params=None):
    _mid[0] += 1
    ws.send(json.dumps({"id": _mid[0], "method": method, "params": params or {}}))
    while True:
        r = json.loads(ws.recv())
        if r.get("id") == _mid[0]:
            return r.get("result", {})

def js(expr, await_promise=True):
    r = send("Runtime.evaluate", {
        "expression": "(async()=>{%s})()" % expr,
        "awaitPromise": await_promise,
        "returnByValue": True,
    })
    res = r.get("result", {})
    if res.get("subtype") == "error" or r.get("exceptionDetails"):
        det = r.get("exceptionDetails", {})
        return {"__error": det.get("exception", {}).get("description") or res.get("description")}
    return res.get("value")

def shot(name, clip=None):
    p = {"format": "png"}
    if clip:
        p["clip"] = clip
    d = send("Page.captureScreenshot", p)
    with open(os.path.join(SHOT, name + ".png"), "wb") as f:
        f.write(base64.b64decode(d["data"]))

HELPERS = r"""
const $ = (s)=>document.querySelector(s);
const $$ = (s)=>[...document.querySelectorAll(s)];
const sleep = (ms)=>new Promise(r=>setTimeout(r,ms));
async function waitFor(fn, ms=20000){
  const t0=Date.now();
  while(Date.now()-t0<ms){ try{ if(fn()) return true; }catch(e){} await sleep(150); }
  return false;
}
async function clearSel(){
  const boxes=[...document.querySelectorAll('#fileBody input[type=checkbox]')].filter(b=>b.checked);
  for(const b of boxes){ b.click(); await sleep(40); }
  await sleep(150);
}
function rowByName(n){
  return $$('#fileBody tr').find(tr=>{
    const s=tr.querySelector('.fname span[title]');
    return s && s.getAttribute('title')===n;
  });
}
async function selRow(n){
  const tr=rowByName(n); if(!tr) return false;
  const cb=tr.querySelector('input[type=checkbox]'); if(!cb) return false;
  if(!cb.checked){ cb.click(); await sleep(220); }
  return true;
}
async function dbl(name, ms=40000){
  const tr = rowByName(name);
  if(!tr) return false;
  const before = ($('#pathInfo')||{}).textContent || '';
  const el = tr.querySelector('.fname');
  el.dispatchEvent(new MouseEvent('dblclick',{bubbles:true,cancelable:true}));
  const ok = await waitFor(()=>{ const p=($('#pathInfo')||{}).textContent||''; return p && p!==before; }, ms);
  await sleep(400);
  return ok;
}
"""

def step(expr):
    return js(HELPERS + "\n" + expr)

def main():
    send("Page.enable")
    send("Runtime.enable")
    send("Emulation.setDeviceMetricsOverride", {"width": 1536, "height": 960,
                                               "deviceScaleFactor": 1, "mobile": False})

    print("=" * 70)
    print("IPv6 直连界面端到端测试")
    print("  地址:", URL)
    print("  沙箱:", SANDBOX)
    print("=" * 70)

    send("Page.navigate", {"url": URL + "/login"})
    time.sleep(2.5)
    step("""
    await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({username:'admin',password:'%s'}),credentials:'same-origin'});
    return true;
    """ % ST["password"])
    send("Page.navigate", {"url": URL + "/"})
    time.sleep(4)

    r = step("""
    document.querySelector('[data-tab="transfer"]').click();
    await sleep(1200);
    await waitFor(()=>document.querySelectorAll('#fileBody tr').length>0, 25000);
    return { path: ($('#pathInfo')||{}).textContent,
             rows: document.querySelectorAll('#fileBody tr').length,
             transVisible: !document.getElementById('pageTransfer').hidden };
    """)
    check("已进入信息传输页", r.get("transVisible") is True, r)

    dev = step("""
    const ok = await waitFor(()=>{
      const el=$('#pDev'); return el && el.textContent && el.textContent!=='—';
    }, 25000);
    const d = (typeof currentDev==='function') ? currentDev() : null;
    return { ok, transport:(d||{}).transport, ipv6:(d||{}).ipv6_enabled,
             pT:($('#pTransport')||{}).textContent,
             row:(()=>{const r=$('#pV6Row'); return r?{hidden:r.hidden, txt:$('#pV6').textContent}:null})() };
    """)
    check("设备已上线", dev.get("ok") is True, dev)
    check("C3-1 面板显示「IPv6 优先」", dev.get("pT") == "IPv6 优先", dev.get("pT"))
    check("C3-2 IPv6 行可见", (dev.get("row") or {}).get("hidden") is False, dev.get("row"))
    print("     起任务前 IPv6 状态:", (dev.get("row") or {}).get("txt"))

    segments = SANDBOX.split("\\")
    r = step("""
    const segs = %s;
    const log = [];
    for (let i=0;i<segs.length;i++){
      const s = segs[i];
      if (i===0) {
        const li = [...document.querySelectorAll('#diskTree li')].find(x=>x.textContent.indexOf(s)>=0);
        if(!li){ log.push('磁盘树找不到 '+s); break; }
        li.click(); await sleep(1500);
      } else {
        const ok = await dbl(s);
        if(!ok){ log.push('未找到目录 '+s); break; }
        await sleep(500);
      }
      log.push(s+' -> '+($('#pathInfo').textContent||''));
    }
    return { log, path:($('#pathInfo').textContent||''), rows:document.querySelectorAll('#fileBody tr').length };
    """ % json.dumps(segments, ensure_ascii=False))
    for line in r.get("log", []):
        print("     ", line)
    check("成功进入沙箱目录",
          r.get("path", "").rstrip("\\").lower().endswith(LEAF.lower()), r.get("path"))
    check("沙箱内容已加载", r.get("rows", 0) >= 8, "rows=%s" % r.get("rows"))

    picked = step("return await selRow('readme.txt');")
    check("选中 readme.txt", picked is True, picked)

    pushed = step(r"""
    const btn = $('#btnPush');
    const oc = window.confirm; window.confirm = ()=>true;
    btn.click();
    const done = await waitFor(()=>{
      const n=$('#v6Note');
      return n && !n.hidden && n.textContent.indexOf('IPv6 直连') >= 0;
    }, 90000);
    window.confirm = oc;
    return { done, note:($('#v6Note')||{}).textContent||'', btn:btn.textContent };
    """)
    check("C3-3 出现 IPv6 直连提示", pushed.get("done") is True, pushed)
    print("     提示:", (pushed.get("note") or "")[:140])
    shot("C1_IPv6直连提示")

    links = step("""
    return $$('#linkList .link-item').map(d=>({
      name: d.querySelector('.nm').textContent.trim(),
      tag: !!d.querySelector('.tagv6'),
      sz: d.querySelector('.sz').textContent.trim(),
      del: !!d.querySelector('[data-act=del]'),
    }));
    """)
    check("C3-3 生成 1 条直连链接", isinstance(links, list) and len(links) == 1, links)
    if isinstance(links, list) and links:
        check("C3-3 链接带「IPv6 直连」标签", links[0].get("tag") is True, links[0])
        check("C3-3 直连链接不显示删除按钮", links[0].get("del") is False, links[0])
        check("C3-3 链接说明含「不经过服务器」",
              "不经过服务器" in (links[0].get("sz") or ""), links[0].get("sz"))

    url = step("const l=S.links.find(x=>x.v6); return l?l.url:null;")
    print("     直连地址:", url)
    check("C3-3 直链形如 http://[addr]:port/f/…",
          bool(url) and url.startswith("http://[") and "/f/" in url, url)

    if url:
        src = os.path.join(SANDBOX, "readme.txt")
        out = os.path.join(os.environ["TEMP"], "v6_ui_dl.txt")
        if os.path.isfile(out):
            os.remove(out)
        rc = os.system('curl.exe -s -o "%s" "%s"' % (out, url))
        same = False
        if rc == 0 and os.path.isfile(out):
            with open(src, "rb") as f:
                a = f.read()
            with open(out, "rb") as f:
                b = f.read()
            same = (a == b)
        check("C3-4 直链下载字节一致", same, "rc=%s" % rc)

    r2 = step("""
    await waitFor(()=>{ const t=($('#pV6')||{}).textContent||''; return t.indexOf('端口')>=0; }, 20000);
    const r=$('#pV6Row');
    return { hidden:(r||{}).hidden, txt:($('#pV6')||{}).textContent };
    """)
    print("     起任务后 IPv6 状态:", r2.get("txt"))
    check("C3-2 起任务后显示端口", "端口" in (r2.get("txt") or ""), r2)
    shot("C2_IPv6直连链接")

    step("await clearSel();")
    fok = step("return await selRow('sub1');")
    check("选中文件夹 sub1", fok is True, fok)
    r = step(r"""
    const oc=window.confirm; window.confirm=()=>true;
    $('#btnPush').click();
    const done = await waitFor(()=>{
      const n=$('#v6Note');
      if(!n || n.hidden) return false;
      const t=n.textContent||'';
      return t.indexOf('已自动走') >= 0 || (t.indexOf('文件夹') >= 0 && t.indexOf('OSS') >= 0);
    }, 30000);
    window.confirm=oc;
    return { done, note:($('#v6Note')||{}).textContent||'' };
    """)
    check("C4-1 含文件夹时回落 OSS 并提示", r.get("done") is True, r)
    print("     提示:", (r.get("note") or "")[:120])
    shot("C3_含文件夹回落OSS")

    r = step(r"""
    await clearSel();
    await selRow('sub1');
    const d = currentDev();
    const bak = d.transport;
    d.transport = 'ipv6';
    if (typeof v6RenderDevice==='function') v6RenderDevice(d);
    const oc=window.confirm; window.confirm=()=>true;
    await doPush();
    window.confirm=oc;
    const note = ($('#v6Note')||{}).textContent || '';
    d.transport = bak;
    if (typeof v6RenderDevice==='function') v6RenderDevice(d);
    return { note, blocked: note.indexOf('不支持文件夹') >= 0, disabled:$('#btnPush').disabled };
    """)
    check("C4-2 仅 IPv6 选文件夹被明确拒绝", r.get("blocked") is True, r)
    check("C4-2 拒绝后按钮恢复可用", r.get("disabled") is False, r)
    print("     提示:", (r.get("note") or "")[:120])
    shot("C4_仅IPv6拒绝文件夹")

    r = step(r"""
    await clearSel();
    await selRow('readme.txt');
    const d = currentDev();
    const bak = d.transport;
    d.transport = 'ipv6';
    if (typeof v6RenderDevice==='function') v6RenderDevice(d);
    const origCmd = window.cmd;
    let called = 0;
    window.cmd = async (a,args,t)=>{
      if (a === 'v6prep') { called++; return { ok:true, port:9, addrs:['2001:db8::1234'], files:1 }; }
      return origCmd(a,args,t);
    };
    const oc=window.confirm; window.confirm=()=>true;
    await doPush();
    window.confirm=oc;
    window.cmd = origCmd;
    const note = ($('#v6Note')||{}).textContent || '';
    d.transport = bak;
    if (typeof v6RenderDevice==='function') v6RenderDevice(d);
    return { note, hit: note.indexOf('无法传输')>=0, fellback: note.indexOf('回落')>=0, called };
    """)
    check("C4-3 仅 IPv6 不可达时报错", r.get("hit") is True, r)
    check("C4-3 报错且没有回落 OSS", r.get("fellback") is False, r)
    print("     提示:", (r.get("note") or "")[:140])
    shot("C5_仅IPv6不可达报错")

    step("await clearSel();")

    print("")
    print("=" * 70)
    print("结果：通过 %d / 失败 %d" % (PASS, FAIL))
    print("=" * 70)
    return 0 if FAIL == 0 else 1

if __name__ == "__main__":
    sys.exit(main())
