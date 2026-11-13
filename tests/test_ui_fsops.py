
"""驱动真实浏览器，端到端验证文件操作界面（B1/B2/B3）。

全程只操作临时沙箱目录。
"""
import base64
import json
import os
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

import shutil

shutil.rmtree(SANDBOX, ignore_errors=True)
build_sandbox(SANDBOX)
print("已重建沙箱:", SANDBOX)

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
ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=120)
_mid = [0]

def send(method, params=None):
    _mid[0] += 1
    ws.send(json.dumps({"id": _mid[0], "method": method, "params": params or {}}))
    while True:
        r = json.loads(ws.recv())
        if r.get("id") == _mid[0]:
            return r.get("result", {})

def js(expr, await_promise=True):
    """执行 JS，返回解析后的值；表达式里用 return 返回结果"""
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
async function waitFor(fn, ms=15000){
  const t0=Date.now();
  while(Date.now()-t0<ms){ try{ if(fn()) return true; }catch(e){} await sleep(120); }
  return false;
}
function closeModal(){
  const m = document.getElementById('mdMask');
  if (m && !m.hidden) { const c=document.getElementById('mdCancel'); if(c) c.click(); }
}
async function clearSel(){
  const boxes = [...document.querySelectorAll('#fileBody input[type=checkbox]')].filter(b=>b.checked);
  for (const b of boxes){ b.click(); await sleep(40); }
  await sleep(120);
}
function rowByName(n){
  return $$('#fileBody tr').find(tr=>{
    const s = tr.querySelector('.fname span[title]');
    return s && s.getAttribute('title')===n;
  });
}
async function dbl(name, ms=40000){
  const tr = rowByName(name);
  if(!tr) return false;
  const before = ($('#pathInfo')||{}).textContent || '';
  const el = tr.querySelector('.fname');
  el.dispatchEvent(new MouseEvent('dblclick',{bubbles:true,cancelable:true}));
  // 目录很大时（如 Temp 有 5000+ 项）需要等路径真正变化
  const ok = await waitFor(()=>{ const p=($('#pathInfo')||{}).textContent||''; return p && p!==before; }, ms);
  await sleep(400);
  return ok;
}
"""

def step(expr):
    return js(HELPERS + "\ncloseModal(); await sleep(150); await clearSel();\n" + expr)

print("=" * 70)
print("文件操作界面端到端测试")
print("  地址:", URL)
print("  沙箱:", SANDBOX)
print("=" * 70)

send("Page.enable")
send("Runtime.enable")
send("Emulation.setDeviceMetricsOverride", {"width": 1440, "height": 900,
                                           "deviceScaleFactor": 1, "mobile": False})

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
return {rows: document.querySelectorAll('#fileBody tr').length,
        path: ($('#pathInfo')||{}).textContent,
        transferVisible: !document.getElementById('pageTransfer').hidden,
        diskItems: document.querySelectorAll('#diskTree li').length};
""")
print("\n【1】首屏加载:", r)
check("已切到信息传输页", r.get("transferVisible") is True)
check("磁盘树已渲染", r.get("diskItems", 0) > 0, r.get("diskItems"))

print("\n【2】逐层下钻到沙箱目录")
segments = SANDBOX.split("\\")
r = step("""
const segs = %s;
const log = [];
for (let i=0;i<segs.length;i++){
  const s = segs[i];
  if (i===0) {
    // 第一段是盘符：点左侧磁盘树
    const li = [...document.querySelectorAll('#diskTree li')].find(x=>x.textContent.indexOf(s)>=0);
    if(!li) { log.push('磁盘树找不到 '+s); break; }
    li.click(); await sleep(1500);
  } else {
    const ok = await dbl(s);
    if(!ok){ log.push('未找到目录 '+s); break; }
    await sleep(500);
  }
  log.push(s+' -> '+($('#pathInfo').textContent||''));
}
return {log, path: ($('#pathInfo').textContent||''), rows: document.querySelectorAll('#fileBody tr').length};
""" % json.dumps(segments, ensure_ascii=False))
for line in r.get("log", []):
    print("    ", line)
check("成功进入沙箱目录", r.get("path", "").rstrip("\\").lower().endswith(LEAF.lower()), r.get("path"))
check("目录内容非空", r.get("rows", 0) >= 8, "rows=%s" % r.get("rows"))

shot("01_进入目录")

r = step("""
const b = id => { const e=document.getElementById(id); return e? {disabled:e.disabled, vis:!!e.offsetParent}:null; };
return {
  bar: !!document.querySelector('#fsBar'),
  newDir: b('btnNewDir'), newFile: b('btnNewFile'),
  rename: b('btnRename'), copy: b('btnCopy'), move: b('btnMove'), del: b('btnDelete'),
  denyHidden: document.getElementById('denyBar').hidden,
  names: [...document.querySelectorAll('#fileBody .fname span[title]')].map(x=>x.getAttribute('title')),
};
""")
print("\n【3】工具栏初始状态")
print("     按钮:", {k: (v or {}).get("disabled") for k, v in r.items() if isinstance(v, dict)})
print("     目录项:", r.get("names", [])[:10])
check("工具栏存在", r.get("bar") is True)
check("新建按钮可用", r.get("newDir", {}).get("disabled") is False)
check("无选中时重命名禁用", r.get("rename", {}).get("disabled") is True)
check("无选中时删除禁用", r.get("del", {}).get("disabled") is True)
check("受限提示条隐藏", r.get("denyHidden") is True)
check("样本文件齐全", "readme.txt" in r.get("names", []) and "sub1" in r.get("names", []))

print("\n【4】新建文件夹")
r = step("""
document.getElementById('btnNewDir').click();
const ok = await waitFor(()=>!document.getElementById('mdMask').hidden, 5000);
if(!ok) return {err:'弹窗没出现'};
await sleep(200);
const title = document.getElementById('mdTitle').textContent;
const desc = document.getElementById('mdDesc').textContent;
document.getElementById('mdInput').value = '新建的文件夹';
document.getElementById('mdOk').click();
await waitFor(()=>document.getElementById('mdMask').hidden, 5000);
await waitFor(()=>!!rowByName('新建的文件夹'), 15000);
return {title, desc, found: !!rowByName('新建的文件夹'),
        toast: (document.getElementById('toast')||{}).textContent};
""")
print("     ", r)
check("弹窗标题正确", r.get("title") == "新建文件夹", r.get("title"))
check("文件夹已出现在列表", r.get("found") is True)
check("磁盘上确实创建了", os.path.isdir(os.path.join(SANDBOX, "新建的文件夹")))
shot("02_新建文件夹后")

print("\n【5】新建文件")
r = step("""
document.getElementById('btnNewFile').click();
await waitFor(()=>!document.getElementById('mdMask').hidden, 5000);
await sleep(150);
document.getElementById('mdInput').value = '新建文件.txt';
document.getElementById('mdOk').click();
await sleep(400);
const t2 = document.getElementById('mdTitle').textContent;
document.getElementById('mdInput').value = '这是通过网页写入的内容';
document.getElementById('mdOk').click();
await waitFor(()=>document.getElementById('mdMask').hidden, 5000);
await waitFor(()=>!!rowByName('新建文件.txt'), 15000);
return {secondTitle: t2, found: !!rowByName('新建文件.txt'),
        toast: (document.getElementById('toast')||{}).textContent};
""")
print("     ", r)
check("第二段标题正确", r.get("secondTitle") and "内容" in r.get("secondTitle"), r.get("secondTitle"))
check("文件已出现", r.get("found") is True)
p = os.path.join(SANDBOX, "新建文件.txt")
check("磁盘内容正确", os.path.isfile(p) and "网页写入的内容" in open(p, encoding="utf-8").read())

print("\n【6】重命名")
r = step("""
const tr = rowByName('新建文件.txt');
tr.querySelector('input[type=checkbox]').click();
await sleep(300);
const enabled = !document.getElementById('btnRename').disabled;
document.getElementById('btnRename').click();
await waitFor(()=>!document.getElementById('mdMask').hidden, 5000);
await sleep(150);
const prefill = document.getElementById('mdInput').value;
document.getElementById('mdInput').value = '改名后.txt';
document.getElementById('mdOk').click();
await waitFor(()=>document.getElementById('mdMask').hidden, 5000);
await waitFor(()=>!!rowByName('改名后.txt'), 15000);
return {enabled, prefill, found: !!rowByName('改名后.txt'), old: !!rowByName('新建文件.txt')};
""")
print("     ", r)
check("选中后重命名按钮可用", r.get("enabled") is True)
check("输入框预填原名", r.get("prefill") == "新建文件.txt", r.get("prefill"))
check("重命名成功", r.get("found") is True and r.get("old") is False)
check("磁盘上已改名", os.path.isfile(os.path.join(SANDBOX, "改名后.txt")))

print("\n【7】复制到指定目录")
r = step("""
const src = rowByName('改名后.txt');
if(!src) return {err:'找不到 改名后.txt'};
src.querySelector('input[type=checkbox]').click();
await sleep(300);
const enabled = !document.getElementById('btnCopy').disabled;
document.getElementById('btnCopy').click();
await waitFor(()=>!document.getElementById('mdMask').hidden, 8000);
await waitFor(()=>document.querySelectorAll('#mdList .mdrow[data-go]').length>0, 15000);
const title = document.getElementById('mdTitle').textContent;
const dirs = [...document.querySelectorAll('#mdList .mdrow .mn')].map(x=>x.textContent);
const errTxt = document.getElementById('mdErr').textContent;
// 进入 copy-target
const btn = [...document.querySelectorAll('#mdList .mdrow[data-go]')].find(b=>b.textContent.indexOf('copy-target')>=0);
if(!btn) return {err:'选择器里没有 copy-target', dirs, enabled, errTxt, title};
btn.click();
await sleep(1200);
const cur = document.getElementById('mdDesc').textContent;
document.getElementById('mdOk').click();
await waitFor(()=>document.getElementById('mdMask').hidden, 10000);
await sleep(1500);
return {title, dirs:dirs.slice(0,8), cur, enabled, errTxt,
        toast:(document.getElementById('toast')||{}).textContent};
""")
print("     ", r)
check("勾选后复制按钮可用", r.get("enabled") is True)
check("选择器列出子目录", "sub1" in (r.get("dirs") or []), r.get("dirs") or r.get("err"))
check("可进入子目录", r.get("cur", "").rstrip("\\").lower().endswith("copy-target"), r.get("cur"))
check("磁盘上复制成功", os.path.isfile(os.path.join(SANDBOX, "copy-target", "改名后.txt")))

print("\n【8】移动到指定目录")
r = step("""
const tr = rowByName('改名后.txt');
if(!tr) return {err:'找不到源文件'};
tr.querySelector('input[type=checkbox]').click();
await sleep(300);
document.getElementById('btnMove').click();
await waitFor(()=>!document.getElementById('mdMask').hidden, 8000);
await waitFor(()=>document.querySelectorAll('#mdList .mdrow[data-go]').length>0, 15000);
const btn = [...document.querySelectorAll('#mdList .mdrow[data-go]')].find(b=>b.textContent.indexOf('move-target')>=0);
if(!btn) return {err:'选择器里没有 move-target'};
btn.click(); await sleep(1200);
document.getElementById('mdOk').click();
await waitFor(()=>document.getElementById('mdMask').hidden, 10000);
await sleep(1800);
return {toast:(document.getElementById('toast')||{}).textContent,
        stillHere: !!rowByName('改名后.txt')};
""")
print("     ", r)
check("移动后原处不再有", r.get("stillHere") is False)
check("磁盘上已移动", os.path.isfile(os.path.join(SANDBOX, "move-target", "改名后.txt")))

print("\n【9】删除 + 二次确认")
r = step("""
const tr = rowByName('move-target');
if(!tr) return {err:'找不到 move-target'};
tr.querySelector('input[type=checkbox]').click();
await sleep(300);
document.getElementById('btnDelete').click();
await waitFor(()=>!document.getElementById('mdMask').hidden, 6000);
await sleep(200);
const title = document.getElementById('mdTitle').textContent;
const desc  = document.getElementById('mdDesc').textContent;
const okTxt = document.getElementById('mdOk').textContent;
const okCls = document.getElementById('mdOk').className;
// 先点取消，确认不删
document.getElementById('mdCancel').click();
await sleep(600);
const cancelled = !!rowByName('move-target');
// 再删一次，这次确认
document.getElementById('btnDelete').click();
await waitFor(()=>!document.getElementById('mdMask').hidden, 6000);
await sleep(200);
document.getElementById('mdOk').click();
await waitFor(()=>document.getElementById('mdMask').hidden, 10000);
await sleep(1800);
return {title, desc, okTxt, okCls, cancelled, gone: !rowByName('move-target')};
""")
print("     标题:", r.get("title"))
print("     说明:", (r.get("desc") or "").replace("\n", " | ")[:160])
print("     确认按钮:", r.get("okTxt"), "| class:", r.get("okCls"))
check("有二次确认弹窗", r.get("title") == "确认删除？", r.get("title"))
check("说明里列出待删项", "move-target" in (r.get("desc") or ""), r.get("desc"))
check("确认按钮为危险样式", "danger" in (r.get("okCls") or ""), r.get("okCls"))
check("点取消不会删除", r.get("cancelled") is True)
check("确认后已删除", r.get("gone") is True)
check("磁盘上确实删掉了", not os.path.exists(os.path.join(SANDBOX, "move-target")))
shot("03_删除后")

print("\n【10】右键菜单")
r = step("""
const tr = rowByName('readme.txt');
if(!tr) return {err:'找不到 readme.txt'};
tr.dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,cancelable:true,clientX:420,clientY:320}));
await sleep(400);
const menu = document.getElementById('ctxMenu');
const vis = !menu.hidden;
const items = [...menu.querySelectorAll('.ctxitem')].map(b=>({t:b.textContent.trim(), dis:b.disabled, hid:b.hidden}));
const rect = menu.getBoundingClientRect();
return {vis, items, x:Math.round(rect.left), y:Math.round(rect.top),
        sel: document.querySelectorAll('#fileBody tr.selected').length};
""")
print("     菜单项:", [(i["t"], "禁用" if i["dis"] else "可用") for i in (r.get("items") or [])])
check("右键弹出菜单", r.get("vis") is True)
check("右键自动选中该行", r.get("sel") == 1, r.get("sel"))
check("菜单在视口内", 0 <= r.get("x", -1) < 1440 and 0 <= r.get("y", -1) < 900, (r.get("x"), r.get("y")))
shot("04_右键菜单", {"x": 300, "y": 180, "width": 560, "height": 420, "scale": 2})

r = step("""
document.getElementById('ctxMask').dispatchEvent(new PointerEvent('pointerdown',{bubbles:true}));
await sleep(250);
return {hidden: document.getElementById('ctxMenu').hidden,
        maskHidden: document.getElementById('ctxMask').hidden};
""")
check("点空白关闭菜单", r.get("hidden") is True and r.get("maskHidden") is True)

print("\n【11】触屏模式适配")
send("Emulation.setEmulatedMedia", {"features": [{"name": "pointer", "value": "coarse"}]})
send("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5})
send("Page.navigate", {"url": URL + "/"})
time.sleep(4)
r = step("""
document.querySelector('[data-tab="transfer"]').click();
await sleep(1200);
await waitFor(()=>document.querySelectorAll('#fileBody tr').length>0, 25000);
const h = (id)=>{const e=document.getElementById(id); return e? Math.round(e.getBoundingClientRect().height):0;};
return {touchClass: document.body.classList.contains('touch-mode'),
        btnH: h('btnNewDir'), rowH: (()=>{const t=document.querySelector('#fileBody tr'); return t?Math.round(t.getBoundingClientRect().height):0;})(),
        barVis: !!document.getElementById('fsBar')};
""")
print("     ", r)
check("识别为触屏模式", r.get("touchClass") is True)
check("触屏下按钮更高(≥38px)", r.get("btnH", 0) >= 38, r.get("btnH"))
check("触屏下工具栏可见", r.get("barVis") is True)
shot("05_触屏模式")

send("Emulation.setEmulatedMedia", {"features": []})
send("Emulation.setTouchEmulationEnabled", {"enabled": False})

print("\n【12】受限目录提示")
r = step("""
// 直接用 UI 的方式无法跳到系统保护目录，这里验证"进入失败后是否有提示"
// 用磁盘树进入 C: 后逐层找 "System Volume Information"
const li = [...document.querySelectorAll('#diskTree li')].find(x=>x.textContent.indexOf('C:')>=0);
if(!li) return {err:'找不到 C: 磁盘'};
li.click(); await sleep(1500);
const has = !!rowByName('System Volume Information');
if (has) { await dbl('System Volume Information'); await sleep(1200); }
return {has, path: ($('#pathInfo').textContent||''),
        denyHidden: document.getElementById('denyBar').hidden,
        denyText: (document.getElementById('denyReason')||{}).textContent,
        emptyText: (document.getElementById('emptyHint')||{}).textContent,
        newDirDisabled: document.getElementById('btnNewDir').disabled};
""")
print("     ", r)
if r.get("has"):
    check("受限目录显示提示条", r.get("denyHidden") is False)
    check("提示里有原因文字", bool(r.get("denyText")))
    check("受限目录禁用新建", r.get("newDirDisabled") is True)
    shot("06_受限目录")
else:
    print("     （C 盘根未出现该系统目录，改用路径不存在的情况验证提示不误报）")
    check("正常目录不显示提示条", r.get("denyHidden") is True)

print("\n【13】空目录显示")
r = step("""
const li = [...document.querySelectorAll('#diskTree li')].find(x=>x.textContent.indexOf('C:')>=0);
li.click(); await sleep(1200);
for (const s of %s) { await dbl(s); await sleep(400); }
await dbl('empty-dir'); await sleep(1200);
return {path: ($('#pathInfo').textContent||''),
        rows: document.querySelectorAll('#fileBody tr').length,
        emptyShown: document.getElementById('emptyHint').style.display !== 'none',
        emptyText: document.getElementById('emptyHint').textContent};
""" % json.dumps(SANDBOX.split("\\")[1:], ensure_ascii=False))
print("     ", r)
check("空目录有提示文字", r.get("rows") == 0 and r.get("emptyShown") is True, r.get("emptyText"))

print()
print("=" * 70)
print("结果：通过 %d / 失败 %d" % (PASS, FAIL))
print("截图目录:", SHOT)
print("=" * 70)
ws.close()
sys.exit(0 if FAIL == 0 else 1)
