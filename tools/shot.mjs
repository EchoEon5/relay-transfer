
const [, , url, out, expr, waitArg, wArg, hArg] = process.argv;
const PORT = process.env.CDP_PORT || 9333;
const waitMs = Number(waitArg || 3500);
const W = Number(wArg || 1500);
const H = Number(hArg || 940);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function j(path, method = 'GET') {
  const r = await fetch(`http://127.0.0.1:${PORT}${path}`, { method });
  const txt = await r.text();
  try { return JSON.parse(txt); } catch { return { raw: txt }; }
}

async function findPage() {
  for (let i = 0; i < 60; i++) {
    try {
      const list = await j('/json/list');
      if (Array.isArray(list)) {
        const p = list.find((t) => t.type === 'page');
        if (p) return p;
      }
    } catch {  }
    await sleep(200);
  }
  throw new Error('未找到可调试的页面');
}

const page = await findPage();
const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });

let seq = 0;
const waiting = new Map();
ws.onmessage = (ev) => {
  const m = JSON.parse(ev.data);
  if (m.id && waiting.has(m.id)) { waiting.get(m.id)(m); waiting.delete(m.id); }
};
const send = (method, params = {}) => new Promise((res) => {
  const id = ++seq;
  waiting.set(id, res);
  ws.send(JSON.stringify({ id, method, params }));
});

await send('Page.enable');
await send('Runtime.enable');
await send('Emulation.setDeviceMetricsOverride', {
  width: W, height: H, deviceScaleFactor: 1, mobile: false,
});
await send('Page.navigate', { url });
await sleep(waitMs);

if (expr) {
  const r = await send('Runtime.evaluate', {
    expression: expr, awaitPromise: true, returnByValue: true,
  });
  const val = r.result && r.result.result && r.result.result.value;
  if (val !== undefined) console.log('eval:', JSON.stringify(val).slice(0, 2000));
  if (r.result && r.result.exceptionDetails) {
    console.log('eval error:', JSON.stringify(r.result.exceptionDetails).slice(0, 800));
  }
  await sleep(1500);
}

const shot = await send('Page.captureScreenshot', { format: 'png' });
if (!shot.result || !shot.result.data) throw new Error('截图失败: ' + JSON.stringify(shot).slice(0, 400));
const { writeFileSync } = await import('node:fs');
writeFileSync(out, Buffer.from(shot.result.data, 'base64'));
console.log('saved', out);
ws.close();
process.exit(0);
