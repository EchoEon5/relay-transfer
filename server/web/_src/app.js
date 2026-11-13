'use strict';

const $ = (s) => document.querySelector(s);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const fmtSize = (n) => {
  if (n === null || n === undefined || isNaN(n)) return '—';
  if (n < 1024) return n + ' B';
  const u = ['KB', 'MB', 'GB', 'TB', 'PB'];
  let v = n / 1024, i = 0;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return (v >= 100 ? v.toFixed(0) : v.toFixed(v >= 10 ? 1 : 2)) + ' ' + u[i];
};
const fmtTime = (sec) => {
  if (!sec) return '—';
  const d = new Date(sec * 1000);
  const p = (x) => String(x).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
};
const fmtEta = (sec) => {
  if (sec === null || sec === undefined || !isFinite(sec) || sec < 0) return '剩余 —';
  sec = Math.round(sec);
  if (sec < 60) return `剩余 ${sec} 秒`;
  if (sec < 3600) return `剩余 ${Math.floor(sec / 60)} 分 ${sec % 60} 秒`;
  return `剩余 ${Math.floor(sec / 3600)} 时 ${Math.floor((sec % 3600) / 60)} 分`;
};
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const ICON = {
  dir: '📁',
  disk: '💾',
};

function iconOf(entry) {
  if (entry.is_dir) return ICON.dir;
  const ext = (entry.name.split('.').pop() || '').toLowerCase();
  if (['zip', 'rar', '7z', 'gz', 'tar', 'iso'].includes(ext)) return '🗜️';
  if (['mp4', 'mkv', 'avi', 'mov', 'wmv', 'flv'].includes(ext)) return '🎬';
  if (['jpg', 'jpeg', 'png', 'gif', 'bmp', 'webp', 'psd'].includes(ext)) return '🖼️';
  if (['mp3', 'wav', 'flac', 'ape', 'm4a'].includes(ext)) return '🎵';
  if (['doc', 'docx', 'pdf', 'txt', 'md', 'xls', 'xlsx', 'ppt', 'pptx', 'csv'].includes(ext)) return '📄';
  if (['exe', 'msi', 'bat', 'cmd', 'ps1'].includes(ext)) return '⚙️';
  if (['py', 'js', 'ts', 'c', 'cpp', 'h', 'java', 'go', 'rs', 'json', 'xml', 'html', 'css'].includes(ext)) return '🧾';
  return '📄';
}

const S = {
  ver: 0,
  devices: [],
  device: null,
  path: '',
  parent: '',
  entries: [],
  loaded: false,
  loading: false,
  selection: new Map(),  
  dirSizes: new Map(),   
  pending: new Map(),    
  seen: new Set(),
  job: null,
  pushItems: [],
  links: [],
  filter: '',
};

async function apiPost(path, body) {
  const r = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
    credentials: 'same-origin',
  });
  if (r.status === 401) { location.href = '/login'; throw new Error('未登录'); }
  let j = null;
  try { j = await r.json(); } catch (e) { throw new Error('服务器响应异常 (HTTP ' + r.status + ')'); }
  if (!j || j.ok !== true) throw new Error((j && j.error) || '请求失败');
  return j;
}

async function apiGet(path) {
  const r = await fetch(path, { credentials: 'same-origin' });
  if (r.status === 401) { location.href = '/login'; throw new Error('未登录'); }
  let j = null;
  try { j = await r.json(); } catch (e) { throw new Error('服务器响应异常 (HTTP ' + r.status + ')'); }
  if (!j || j.ok !== true) throw new Error((j && j.error) || '请求失败');
  return j;
}

function cmd(action, args, timeoutMs) {
  if (!S.device) return Promise.reject(new Error('未选择设备'));
  return apiPost('/api/cmd', { device: S.device, action, args }).then((res) => {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        if (S.pending.delete(res.seq)) reject(new Error(action + ' 指令超时'));
      }, timeoutMs || 60000);
      S.pending.set(res.seq, { resolve, reject, action, timer });
    });
  });
}

function applySnapshot(snap) {
  S.ver = snap.ver;
  S.devices = snap.devices || [];
  if (!S.ossWarned && snap.oss === false) {
    S.ossWarned = true;
    console.warn('OSS 未配置：' + (snap.oss_error || ''));
  }

  if (!S.device || !S.devices.some((d) => d.id === S.device)) {
    const pick = S.devices.find((d) => d.online) || S.devices[0];
    S.device = pick ? pick.id : null;
    localStorage.setItem('relay.device', S.device || '');
  }

  const dev = S.devices.find((d) => d.id === S.device);

  if (dev) {
    const results = dev.results || {};
    Object.keys(results).forEach((k) => {
      const r = results[k];
      const seq = r.seq;
      if (S.seen.has(seq)) return;
      S.seen.add(seq);
      const p = S.pending.get(seq);
      if (!p) return;
      S.pending.delete(seq);
      clearTimeout(p.timer);
      if (r.ok) p.resolve(r.data || {});
      else p.reject(new Error(r.error || '目标端执行失败'));
    });
    if (S.seen.size > 400) S.seen = new Set([...S.seen].slice(-200));

    if (JSON.stringify(dev.job) !== JSON.stringify(S.job)) {
      S.job = dev.job;
      renderJob();
    }
    renderDevices(dev);
  } else {
    renderDevices(null);
  }
 
  syncHistory(snap.history || []);
  renderSelInfo();
}

async function loop() {
  while (true) {
    try {
      const snap = await apiPost('/api/events', { ver: S.ver, wait: STILL ? 0.3 : 25 });
      applySnapshot(snap);
      if (STILL) await sleep(120);
    } catch (e) {
      await sleep(2000);
    }
  }
}

function currentDev() {
  return S.devices.find((d) => d.id === S.device) || null;
}

function renderDevices(dev) {
  const sel = $('#devSelect');
  const opts = S.devices.map((d) => {
    const mark = d.online ? '● ' : '○ ';
    return `<option value="${esc(d.id)}"${d.id === S.device ? ' selected' : ''}>${mark}${esc(d.host || d.id)}</option>`;
  });
  const html = opts.length ? opts.join('') : '<option>（暂无设备）</option>';
  if (sel.innerHTML !== html) sel.innerHTML = html;

  const job = S.job || {};
  let cls = '', text = '待连接';
  if (dev && dev.online && job.state === 'running') { cls = 'warn'; text = '传输中'; }
  else if (dev && dev.online) { cls = 'ok'; text = '已连接'; }
  else if (dev) { cls = 'err'; text = '离线'; }
  $('#devState').className = 'pill ' + cls;
  $('#devState').textContent = text;

  $('#pDev').textContent = dev ? `${dev.host} / ${dev.user || '?'}` : '—';
  $('#pConn').textContent = dev ? (dev.online ? (dev.admin ? '已连接（管理员）' : '已连接（普通用户）') : '离线') : '—';
  $('#pConn').style.color = dev && dev.online ? 'var(--ok)' : 'var(--err)';

  renderDisks(dev);
  $('#btnPush').disabled = !(dev && dev.online) || S.selection.size === 0;
  $('#btnCancel').hidden = !(job.state === 'running');
}

function renderDisks(dev) {
  const ul = $('#diskTree');
  const disks = (dev && dev.disks) || [];
  if (!disks.length) {
    ul.innerHTML = '<li class="dim">（未获取到磁盘）</li>';
    return;
  }
  ul.innerHTML = disks.map((d) => {
    const active = S.path && d.path && S.path.toUpperCase().startsWith(d.path.toUpperCase()) ? ' active' : '';
    const label = (d.label ? d.label + ' ' : '') + d.name;
    const meta = d.free != null ? fmtSize(d.free) + ' 可用' : '';
    return `<li data-path="${esc(d.path)}" class="${active.trim()}">
      <span class="ico">${ICON.disk}</span><span title="${esc(label)}">${esc(label)}</span>
      <span class="meta">${esc(meta)}</span></li>`;
  }).join('');
  ul.querySelectorAll('li').forEach((li) => {
    li.onclick = () => navigate(li.dataset.path);
  });
}

async function navigate(path, keepSelection) {
  if (!S.device || !path) return;
  if (!(currentDev() && currentDev().online)) { toast('目标设备离线'); return; }
  S.loading = true;
  $('#emptyHint').textContent = '正在读取目录…';
  $('#emptyHint').style.display = 'block';
  try {
    const r = await cmd('list', { path }, 60000);
   
    if (!keepSelection && S.path !== (r.path || path)) {
      if (S.selection.size) toast('已切换目录，之前的勾选已清空');
      S.selection.clear();
      S.dirSizes.clear();
    }
    S.path = r.path || path;
    S.parent = r.parent || '';
    S.entries = r.entries || [];
    S.loaded = true;
    S.skipped = r.skipped || 0;
  } catch (e) {
    S.entries = [];
    S.loaded = true;
    toast('读取失败：' + e.message);
  } finally {
    S.loading = false;
    renderAll();
  }
}

function renderAll() {
  renderCrumbs();
  renderTable();
  renderSelInfo();
  renderDisks(currentDev());
}

function renderCrumbs() {
  const box = $('#crumbs');
  const p = S.path || '';
  if (!p) { box.innerHTML = '<span class="dim">请选择左侧磁盘或快捷入口</span>'; return; }
  const parts = p.split('\\').filter(Boolean);
  let acc = '';
  const html = [`<span class="seg" data-path="">此电脑</span>`];
  parts.forEach((part, i) => {
    acc += (i === 0 ? '' : '\\') + part;
    const isDrive = i === 0;
    const target = isDrive ? acc + '\\' : acc;
    html.push(`<span class="sep">›</span><span class="seg" data-path="${esc(target)}">${esc(part)}</span>`);
  });
  box.innerHTML = html.join('');
  box.querySelectorAll('.seg').forEach((el) => {
    el.onclick = () => {
      const t = el.dataset.path;
      if (t) navigate(t);
    };
  });
}

function visibleEntries() {
  const f = S.filter.trim().toLowerCase();
  if (!f) return S.entries;
  return S.entries.filter((e) => e.name.toLowerCase().includes(f));
}

function renderTable() {
  const tb = $('#fileBody');
  const list = visibleEntries();
  if (!S.loaded) { tb.innerHTML = ''; return; }
  if (!list.length) {
    tb.innerHTML = '';
    $('#emptyHint').style.display = 'block';
    $('#emptyHint').textContent = S.entries.length ? '没有匹配的项' : (S.path ? '此文件夹为空或无权访问' : '请选择左侧磁盘');
    return;
  }
  $('#emptyHint').style.display = 'none';
  tb.innerHTML = list.map((e) => {
    const full = e.path || joinPath(S.path, e.name);
    const sel = S.selection.has(full) ? ' class="selected"' : '';
    const type = e.is_dir ? '文件夹' : (e.name.split('.').pop().toUpperCase() || '文件');
    const sizeTxt = e.is_dir ? '—' : fmtSize(e.size);
    return `<tr data-path="${esc(full)}" data-dir="${e.is_dir ? 1 : 0}"${sel}>
      <td class="col-check"><input type="checkbox" ${S.selection.has(full) ? 'checked' : ''}></td>
      <td><span class="fname ${e.is_dir ? 'dir' : ''}"><span class="ico">${iconOf(e)}</span><span title="${esc(e.name)}">${esc(e.name)}</span></span></td>
      <td class="dim">${sizeTxt}</td>
      <td class="dim">${fmtTime(e.mtime)}</td>
      <td class="dim">${esc(type)}</td>
    </tr>`;
  }).join('');

  tb.querySelectorAll('tr').forEach((tr) => {
    const full = tr.dataset.path;
    const isDir = tr.dataset.dir === '1';
    const entry = list.find((e) => (e.path || joinPath(S.path, e.name)) === full);
    tr.__meta = { is_dir: isDir, label: entry ? entry.name : baseName(full) };
    tr.querySelector('input[type=checkbox]').onchange = (ev) => {
      ev.stopPropagation();
      toggleSelect(full, ev.target.checked, tr.__meta);
      paintRow(tr, full);
      syncCheckAll();
    };
    const nameCell = tr.querySelector('.fname');
    if (isDir) nameCell.ondblclick = () => navigate(full);
   
    tr.onclick = (ev) => {
      if (ev.target.tagName === 'INPUT') return;
      document.querySelectorAll('#fileBody tr.cur').forEach((x) => x.classList.remove('cur'));
      tr.classList.add('cur');
    };
  });

  syncCheckAll();
}

function paintRow(tr, path) {
  const on = S.selection.has(path);
  tr.classList.toggle('selected', on);
  const box = tr.querySelector('input[type=checkbox]');
  if (box) box.checked = on;
}

function syncCheckAll() {
  const rows = [...document.querySelectorAll('#fileBody tr')];
  const all = rows.length > 0 && rows.every((tr) => S.selection.has(tr.dataset.path));
  $('#checkAll').checked = all;
}

function joinPath(dir, name) {
  if (!dir) return name;
  return dir.endsWith('\\') ? dir + name : dir + '\\' + name;
}
function baseName(p) {
  const i = p.lastIndexOf('\\');
  return i >= 0 ? p.slice(i + 1) : p;
}

function toggleSelect(path, on, meta) {
  if (on) {
    S.selection.set(path, meta || { is_dir: false, label: baseName(path) });
    if (meta && meta.is_dir) ensureDirSize(path);
  } else {
    S.selection.delete(path);
  }
  renderSelInfo();
  renderDevices(currentDev());
}

async function ensureDirSize(p) {
  if (S.dirSizes.has(p)) return;
  S.dirSizes.set(p, { loading: true });
  renderSelInfo();
  try {
    const r = await cmd('size', { paths: [p] }, 300000);
    S.dirSizes.set(p, { total: r.total || 0, files: r.files || 0, skipped: r.skipped || 0 });
  } catch (e) {
    S.dirSizes.set(p, { error: e.message });
  }
  renderSelInfo();
}

function selectionStats() {
  let files = 0, dirs = 0, bytes = 0, unknown = 0, loading = 0;
  for (const [p, m] of S.selection) {
    if (m.is_dir) {
      dirs++;
      const st = S.dirSizes.get(p);
      if (!st) { unknown++; loading++; }
      else if (st.loading) { unknown++; loading++; }
      else if (st.total != null) bytes += st.total;
      else unknown++;
    } else {
      files++;
      const e = S.entries.find((x) => x.path === p || joinPath(S.path, x.name) === p);
      if (e && e.size != null) bytes += e.size;
      else unknown++;
    }
  }
  return { files, dirs, bytes, unknown, loading, count: S.selection.size };
}

function renderSelInfo() {
  const s = selectionStats();
  const el = $('#selInfo');
  if (!s.count) {
    el.textContent = '未选择内容';
  } else {
    const parts = [`已选 ${s.count} 项`];
    if (s.dirs) parts.push(`(${s.dirs} 个文件夹)`);
    if (s.bytes > 0) parts.push(`· 合计 ${fmtSize(s.bytes)}`);
    if (s.unknown) parts.push(s.loading ? '· 大小计算中…' : `· ${s.unknown} 项大小未知`);
    el.textContent = parts.join(' ');
    el.style.color = 'var(--text)';
  }
  $('#pSel').textContent = s.count
    ? `${s.count} 项 / ${s.bytes > 0 ? fmtSize(s.bytes) : (s.unknown ? '计算中' : '0 B')}`
    : '—';
  $('#pathInfo').textContent = S.path || '';
  $('#btnPush').disabled = !(currentDev() && currentDev().online) || s.count === 0 ||
    (S.job && S.job.state === 'running');
}

async function doPush() {
 
  const items = [...S.selection.entries()].map(([p, m]) => {
    let size = null;
    if (m.is_dir) {
      const st = S.dirSizes.get(p);
      size = (st && !st.loading && st.total != null) ? st.total : null;
    } else {
      const e = S.entries.find((x) => x.path === p || joinPath(S.path, x.name) === p);
      size = e && e.size != null ? e.size : null;
    }
    return {
      path: p,
      is_dir: !!m.is_dir,
      label: m.label || baseName(p),
      size,
    };
  });
  if (!items.length) return;

  const known = items.reduce((a, x) => a + (x.size || 0), 0);
  const unknownCnt = items.filter((x) => x.size == null).length;
  const sizeTxt = unknownCnt ? `${fmtSize(known)} + ${unknownCnt} 项待计算` : fmtSize(known);
  const preview = items.slice(0, 5).map((x) => '  · ' + x.label + (x.is_dir ? '（文件夹）' : '')).join('\n');
  const more = items.length > 5 ? `\n  · …还有 ${items.length - 5} 项` : '';
  const msg = `即将传输 ${items.length} 项，合计 ${sizeTxt}\n\n${preview}${more}\n\n确定开始吗？`;
  if (!confirm(msg)) return;

  S.pushItems = items;
  S.links = [];
  const btn = $('#btnPush');
  btn.disabled = true;
  btn.textContent = '正在下发…';
  try {
    await cmd('push', { items }, 30000);
    toast('已下发传输任务');
  } catch (e) {
    toast('下发失败：' + e.message);
  } finally {
    btn.textContent = '开始传输';
    renderSelInfo();
  }
}

function renderJob() {
  const j = S.job || {};
  const state = j.state || 'idle';
  const total = j.total || 0;
  const done = j.done || 0;
  const pct = total > 0 ? Math.min(100, (done / total) * 100) : (state === 'done' ? 100 : 0);

  $('#jobPhase').textContent = ({
    idle: '空闲', running: j.phase || '传输中', done: '已完成', error: '传输失败', cancelled: '已取消',
  })[state] || state;
  $('#jobPct').textContent = pct.toFixed(1) + '%';
  $('#jobBar').style.width = pct + '%';
  $('#jobBar').style.background = state === 'error'
    ? 'linear-gradient(90deg,#b4322b,#ff8b84)'
    : 'linear-gradient(90deg,#0f6cbd,#4cc2ff)';
  $('#jobSpeed').textContent = state === 'running' ? fmtSize(j.speed || 0) + '/s' : '—';
  $('#jobEta').textContent = state === 'running' ? fmtEta(j.eta) : '剩余 —';
  $('#jobBytes').textContent = total ? `${fmtSize(done)} / ${fmtSize(total)}` : (j.message || '—');
  $('#jobSkipped').textContent = j.skipped ? `跳过 ${j.skipped} 项` : '';

  const running = state === 'running';
  $('#btnCancel').hidden = !running;
  if (running) $('#btnPush').disabled = true;
}

function syncHistory(hist) {
  const seen = new Set(S.links.map((l) => l.key));
  let added = 0;
  hist.forEach((h) => {
    if (!h || !h.key || seen.has(h.key)) return;
    S.links.push({
      key: h.key,
      size: h.size,
      kind: h.kind,
      name: h.name,
      at: h.at,
      device: h.device,
      url: '',
      expires: 0,
    });
    seen.add(h.key);
    added++;
  });
  if (added) {
   
    const order = new Map(hist.map((h, i) => [h.key, i]));
    S.links.sort((a, b) => (order.has(a.key) ? order.get(a.key) : 9999) - (order.has(b.key) ? order.get(b.key) : 9999));
    renderLinks();
    S.links.forEach((l) => { if (!l.url && !l.error) refreshLink(l); });
  }
 
  const alive = new Set(hist.map((h) => h.key));
  const before = S.links.length;
  S.links = S.links.filter((l) => alive.has(l.key) || l.pending);
  if (S.links.length !== before) renderLinks();
}

async function refreshLink(l) {
  try {
    const r = await apiPost('/api/link', { key: l.key, filename: l.name });
    l.url = r.url;
    l.expires = r.expires_at;
    l.error = '';
    renderLinks();
  } catch (e) {
    l.error = e.message;
    renderLinks();
  }
}

function renderLinks() {
  const box = $('#linkList');
  if (!S.links.length) {
    box.innerHTML = '<div class="empty small">传输完成后，这里会保留所有历史文件，不会因为新的传输而消失</div>';
    return;
  }
  box.innerHTML = S.links.map((l, i) => {
    const exp = l.expires ? new Date(l.expires * 1000).toLocaleString('zh-CN', { hour12: false }) : '';
    const when = l.at ? new Date(l.at * 1000).toLocaleString('zh-CN', { hour12: false }) : '';
    return `<div class="link-item">
      <div class="nm" title="${esc(l.name)}">${esc(l.name)}</div>
      <div class="sz">${fmtSize(l.size)}${l.kind === 'zip' ? ' · 打包 ZIP' : ''}${when ? ' · ' + when : ''}${exp ? ' · 有效期至 ' + exp : ''}</div>
      ${l.error ? `<div class="sz" style="color:var(--err)">${esc(l.error)}</div>` : ''}
      <div class="acts">
        <button class="btn primary" data-i="${i}" data-act="copy">复制链接</button>
        <button class="btn" data-i="${i}" data-act="open">下载</button>
        <button class="btn danger-ghost" data-i="${i}" data-act="del" title="从 OSS 删除，省存储费">删除</button>
      </div>
    </div>`;
  }).join('');
  box.querySelectorAll('button').forEach((b) => {
    b.onclick = async () => {
      const l = S.links[+b.dataset.i];
      if (b.dataset.act === 'del') {
        if (!confirm('确定从 OSS 删除「' + l.name + '」？删除后链接立即失效，不可恢复。')) return;
        try {
          await apiPost('/api/link', { key: l.key, op: 'delete' });
          S.links = S.links.filter((x) => x.key !== l.key);
          renderLinks();
          toast('已删除');
        } catch (e) { toast(e.message); }
        return;
      }
      if (!l.url) { await refreshLink(l); }
      if (b.dataset.act === 'copy') {
        try { await navigator.clipboard.writeText(l.url); toast('链接已复制'); }
        catch (e) { prompt('复制下面的链接：', l.url); }
      } else if (l.url) {
        window.open(l.url, '_blank');
      }
    };
  });
}

let toastTimer = null;
function toast(msg) {
  let el = $('#toast');
  if (!el) {
    el = document.createElement('div');
    el.id = 'toast';
    el.style.cssText = 'position:fixed;left:50%;bottom:34px;transform:translateX(-50%);' +
      'background:rgba(32,32,32,.92);color:#fff;padding:9px 16px;border-radius:8px;font-size:12.5px;' +
      'z-index:99;box-shadow:0 8px 24px rgba(0,0,0,.3);transition:opacity .25s;opacity:0;pointer-events:none;';
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.style.opacity = '1';
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.style.opacity = '0'; }, 2600);
}

function bind() {
  $('#btnUp').onclick = () => { if (S.parent) navigate(S.parent); };
  $('#btnHome').onclick = () => {
    const dev = currentDev();
    const firstDisk = dev && dev.disks && dev.disks[0];
    if (firstDisk) navigate(firstDisk.path);
  };
  $('#btnRefresh').onclick = () => { if (S.path) navigate(S.path); };
  $('#filter').oninput = (e) => { S.filter = e.target.value; renderTable(); };
  $('#checkAll').onchange = (e) => {
    visibleEntries().forEach((x) => {
      const full = x.path || joinPath(S.path, x.name);
      if (e.target.checked) {
        S.selection.set(full, { is_dir: x.is_dir, label: x.name });
        if (x.is_dir) ensureDirSize(full);
      } else {
        S.selection.delete(full);
      }
    });
    document.querySelectorAll('#fileBody tr').forEach((tr) => paintRow(tr, tr.dataset.path));
    syncCheckAll();
    renderSelInfo();
    renderDevices(currentDev());
  };
  $('#devSelect').onchange = (e) => {
    S.device = e.target.value;
    localStorage.setItem('relay.device', S.device);
   
    S.job = null; S.entries = []; S.loaded = false; S.path = '';
    S.selection.clear();
    S.dirSizes.clear();
    renderAll();
    renderDevices(currentDev());
    const dev = currentDev();
    if (dev && dev.disks && dev.disks[0]) navigate(dev.disks[0].path);
  };
  $('#btnPush').onclick = doPush;

  $('#btnCancel').onclick = async () => {
    const btn = $('#btnCancel');
    if (S.cancelling) return;
    S.cancelling = true;
    btn.disabled = true;
    btn.textContent = '正在取消传输…';
    setCancelNote('正在通知目标电脑停止上传，请稍候…', 'warn');
    try {
      await cmd('cancel', {}, 20000);
      setCancelNote('已取消。已上传的部分会清理，设备仍保持连接。', 'ok');
      toast('已取消传输');
    } catch (e) {
      setCancelNote('取消失败：' + e.message, 'err');
      toast('取消失败：' + e.message);
    } finally {
      S.cancelling = false;
      btn.disabled = false;
      btn.textContent = '取消传输';
     
      setTimeout(() => {
        const j = S.job || {};
        if (j.state !== 'running') {
          $('#btnCancel').hidden = true;
          renderDevices(currentDev());
        }
      }, 1500);
    }
  };

  $('#btnDisconnect').onclick = async () => {
    const dev = currentDev();
    if (!dev) return;
    if (!confirm(`确定让「${dev.host}」断开连接？\n\n目标电脑上的传输助手会停止运行，需要它重新双击 bat 才能再连上。\n已经传完的文件不受影响，仍然可以下载。`)) return;
    try {
      await apiPost('/api/disconnect', { device: dev.id });
      toast('已断开 ' + dev.host);
      setCancelNote('已断开连接。目标电脑需要重新双击传输助手才能上线。', 'ok');
    } catch (e) {
      toast('断开失败：' + e.message);
    }
  };

  $('#btnClearHist').onclick = async () => {
    if (!S.links.length) { toast('列表已经是空的'); return; }
    if (!confirm('清空这个下载列表？\n\n注意：只是把记录从列表里去掉，OSS 上的文件不会被删除，仍然占用存储空间。\n如果想省钱，请逐个点「删除」。')) return;
    try {
      await apiPost('/api/history', { op: 'clear' });
      S.links = [];
      renderLinks();
      toast('列表已清空');
    } catch (e) { toast(e.message); }
  };

  $('#btnLogout').onclick = async () => {
    try { await apiPost('/api/logout', {}); } catch (e) {  }
    location.href = '/login';
  };
  $('#quickTree').querySelectorAll('li').forEach((li) => {
    li.onclick = () => {
      const dev = currentDev();
      const key = li.dataset.path.replace('::', '');
      const real = dev && dev.roots && dev.roots[key];
      if (real) navigate(real);
      else toast('该路径不可用（目标端未上报）');
    };
  });

  document.querySelectorAll('.tab').forEach((b) => {
    b.onclick = () => switchTab(b.dataset.tab);
  });
 
  const btnSys = $('#btnSysRefresh');
  if (btnSys) btnSys.onclick = () => loadSysInfo();
 
  const btnAcct = $('#btnSaveAcct');
  if (btnAcct) btnAcct.onclick = saveAccount;
}

function setCancelNote(text, kind) {
  const el = $('#cancelNote');
  if (!el) return;
  if (!text) { el.hidden = true; el.textContent = ''; return; }
  el.hidden = false;
  el.textContent = text;
  el.className = 'cancel-note ' + (kind || '');
}

let SYS_TIMER = null;

function switchTab(name) {
  ['status', 'transfer', 'account'].forEach((t) => {
    const map = { transfer: '#pageTransfer', status: '#pageStatus', account: '#pageAccount' };
    const el = $(map[t]);
    if (el) el.hidden = (t !== name);
  });
  document.querySelectorAll('.tab').forEach((b) => {
    b.classList.toggle('active', b.dataset.tab === name);
  });
  localStorage.setItem('relay.tab', name);

  if (name === 'status') {
    loadSysInfo();
   
    if (SYS_TIMER) clearInterval(SYS_TIMER);
    SYS_TIMER = setInterval(() => {
      if (!$('#pageStatus').hidden) loadSysInfo();
    }, 3000);
  } else {
    if (SYS_TIMER) { clearInterval(SYS_TIMER); SYS_TIMER = null; }
  }
  if (name === 'account') loadAccount();
}

const fmtDur = (sec) => {
  sec = Math.max(0, Math.floor(sec || 0));
  const d = Math.floor(sec / 86400);
  const h = Math.floor((sec % 86400) / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  if (d) return `${d} 天 ${h} 时 ${m} 分`;
  if (h) return `${h} 时 ${m} 分 ${s} 秒`;
  if (m) return `${m} 分 ${s} 秒`;
  return `${s} 秒`;
};

function sysRow(k, v, cls) {
  return `<div class="sys-row"><span class="k">${esc(k)}</span><span class="v ${cls || ''}">${v}</span></div>`;
}

function sysCard(title, rows, wide) {
  return `<div class="sys-card${wide ? ' wide' : ''}">
    <div class="sys-card-head">${esc(title)}</div>
    <div class="sys-card-body">${rows.join('')}</div>
  </div>`;
}

function pctBar(pct, warnAt) {
  const v = Math.max(0, Math.min(100, Number(pct) || 0));
  let cls = '';
  if (v >= (warnAt || 85)) cls = 'bad';
  else if (v >= (warnAt || 85) - 20) cls = 'warn';
  return `<div class="mini-bar"><div class="mini-fill ${cls}" style="width:${v}%"></div></div>`;
}

async function loadSysInfo() {
  const box = $('#sysBody');
  if (!box) return;
  try {
    const d = await apiGet('/api/sysinfo');
    renderSysInfo(d);
    const t = new Date();
    const p = (x) => String(x).padStart(2, '0');
    $('#sysUpdated').textContent = `更新于 ${p(t.getHours())}:${p(t.getMinutes())}:${p(t.getSeconds())}`;
  } catch (e) {
    box.innerHTML = `<div class="empty">读取失败：${esc(e.message)}</div>`;
  }
}

function renderSysInfo(d) {
  const box = $('#sysBody');
  if (!box || !d) return;

  const sv = d.server || {}, osd = d.os || {}, cpu = d.cpu || {};
  const mem = d.memory || {}, oss = d.oss || {};
  const tr = d.trend || { at: [], cpu: [], mem: [], net_rx: [], net_tx: [] };

  const cpuPct = Number(cpu.usage_pct) || 0;
  const memPct = Number(mem.usage_pct) || 0;
  const diskMain = (d.disks || [])[0] || {};
  const diskPct = diskMain.total ? Math.round(diskMain.used / diskMain.total * 100) : 0;

  const donuts = `<div class="donut-row">
    ${svgDonut(cpuPct, 'var(--accent)', 'CPU 使用率', (cpu.cores || 0) + ' 核')}
    ${svgDonut(memPct, '#7c5cff', '内存使用率', fmtSize(mem.total))}
    ${svgDonut(diskPct, '#00a3a3', '磁盘使用率', fmtSize(diskMain.total))}
    ${svgDonut(
      d.devices_total ? Math.round((d.devices_online / d.devices_total) * 100) : 0,
      'var(--ok)', '设备在线率', `${d.devices_online}/${d.devices_total}`)}
  </div>`;

  const cpuMemChart = svgLineChart([
    { name: 'CPU', color: '#0f6cbd', data: tr.cpu || [] },
    { name: '内存', color: '#7c5cff', data: tr.mem || [] },
  ], { width: 520, height: 140 });

  const hasNet = (tr.net_rx || []).some((v) => v > 0) || (tr.net_tx || []).some((v) => v > 0);
  const netChart = hasNet
    ? svgLineChart([
        { name: '接收', color: '#0f7b3f', data: tr.net_rx || [] },
        { name: '发送', color: '#a35b00', data: tr.net_tx || [] },
      ], { width: 520, height: 140, fmt: 'bytes' })
    : '<div class="chart-empty">该平台不提供网卡统计（Windows 目标端无此数据）</div>';

  const loadChart = svgSparkbars(
    (tr.cpu || []).slice(-30), '#0f6cbd');

  const diskBars = svgBarChart((d.disks || []).map((k) => ({
    label: k.mount,
    value: k.used,
    text: `${fmtSize(k.used)} / ${fmtSize(k.total)}`,
    color: (k.used / k.total) > 0.9 ? 'var(--err)' : '#00a3a3',
  })));

  const serverRows = [
    sysRow('主机名', esc(sv.hostname || '—')),
    sysRow('内网 IP', esc(sv.local_ip || '—')),
    sysRow('访问地址', `<a href="${esc(sv.public_base || '')}" target="_blank">${esc(sv.public_base || '—')}</a>`),
    sysRow('监听', esc(`${sv.listen || '0.0.0.0'}:${sv.port || ''}`)),
    sysRow('运行时长', esc(fmtDur(sv.uptime))),
    sysRow('启动时间', esc(fmtTime(sv.started_at))),
    sysRow('进程 PID', esc(sv.pid || '—')),
    sysRow('内存占用', esc(fmtSize(sv.rss))),
    sysRow('线程数', esc(sv.threads || '—')),
    sysRow('Python', esc(sv.python || '—')),
  ];

  const osRows = [
    sysRow('系统', esc(osd.name || '—')),
    sysRow('版本号', esc(osd.version || '—')),
    sysRow('内核', esc(osd.kernel || '—')),
    sysRow('架构', esc(osd.arch || '—')),
  ];

  const cpuRows = [
    sysRow('型号', esc(cpu.model || '—')),
    sysRow('核心数', esc(cpu.cores || '—')),
    sysRow('负载 1 分钟', esc(cpu.load1)),
    sysRow('负载 5 分钟', esc(cpu.load5)),
    sysRow('负载 15 分钟', esc(cpu.load15)),
    sysRow('使用率', esc(cpu.usage_pct) + '%'),
  ];

  const memRows = [
    sysRow('总计', esc(fmtSize(mem.total))),
    sysRow('已用', esc(fmtSize(mem.used))),
    sysRow('可用', esc(fmtSize(mem.available))),
    sysRow('使用率', esc(mem.usage_pct) + '%'),
  ];
  if (mem.swap_total) {
    memRows.push(sysRow('Swap', `${esc(fmtSize(mem.swap_used))} / ${esc(fmtSize(mem.swap_total))}`));
  }

  const ossRows = [
    sysRow('状态', oss.configured
      ? (oss.live ? '<span class="ok-txt">● 已连接</span>' : `<span class="err-txt">● 异常 ${esc(oss.error || '')}</span>`)
      : '<span class="err-txt">● 未配置</span>'),
    sysRow('Endpoint', esc(oss.endpoint || '—')),
    sysRow('Bucket', esc(oss.bucket || '—')),
    sysRow('协议', esc(oss.scheme || 'https')),
    sysRow('分片大小', esc(fmtSize(oss.part_size))),
    sysRow('下载有效期', esc(fmtDur(oss.download_expires))),
    sysRow('AccessKey', `<span class="mono">${esc(oss.ak_masked || '—')}</span>`),
    sysRow('Secret', esc(oss.sk_masked || '—')),
  ];

  const statRows = [
    sysRow('在线设备', `${esc(d.devices_online)} / ${esc(d.devices_total)}`),
    sysRow('可下载文件', esc(d.history_count) + ' 个'),
    sysRow('占用存储', esc(fmtSize(d.history_bytes))),
    sysRow('活跃会话', esc(d.sessions)),
    sysRow('状态目录', `<span class="mono tiny">${esc(d.state_dir || '—')}</span>`),
    sysRow('配置文件', `<span class="mono tiny">${esc(d.config_path || '—')}</span>`),
  ];

  const devs = d.devices || [];
  const devCards = devs.length
    ? devs.map((x) => {
        const rows = [
          sysRow('状态', x.online ? '<span class="ok-txt">● 在线</span>' : '<span class="err-txt">● 离线</span>'),
          sysRow('计算机', esc(x.host || x.id)),
          sysRow('用户', esc(x.user || '—') + (x.admin ? ' <span class="tag-admin">管理员</span>' : '')),
          sysRow('IP', esc(x.ip || '—')),
          sysRow('连接时间', esc(fmtTime(x.connected_at))),
          sysRow('最后心跳', esc(fmtTime(x.last_seen))),
          sysRow('客户端', esc(x.agent || '—')),
        ];
        (x.disks || []).forEach((k) => {
          const pct = k.total ? Math.round(((k.total - k.free) / k.total) * 100) : 0;
          rows.push(sysRow(k.name || k.path,
            `${esc(fmtSize(k.free))} 可用 ${pctBar(pct, 90)}`));
        });
        return sysCard(x.host || x.id, rows, true);
      }).join('')
    : '<div class="sys-card wide"><div class="sys-card-body"><div class="empty small">还没有设备连接过</div></div></div>';

  box.innerHTML = `
    <div class="sys-grid chart-grid">
      ${chartCard('资源使用率', donuts)}
    </div>
    <div class="sys-grid chart-grid">
      ${chartCard('CPU / 内存趋势', cpuMemChart, '最近 3 分钟')}
      ${chartCard('网络吞吐', netChart, '最近 3 分钟')}
    </div>
    <div class="sys-grid chart-grid">
      ${chartCard('CPU 负载分布', loadChart, '最近 30 个采样点')}
      ${chartCard('磁盘占用', diskBars)}
    </div>
    <div class="sys-sec-title">详细参数</div>
    <div class="sys-grid">
      ${sysCard('服务器', serverRows)}
      ${sysCard('操作系统', osRows)}
      ${sysCard('处理器', cpuRows)}
      ${sysCard('内存', memRows)}
      ${sysCard('对象存储 OSS', ossRows)}
      ${sysCard('统计', statRows)}
    </div>
    <div class="sys-sec-title">已连接设备（${devs.filter((x) => x.online).length} 在线 / 共 ${devs.length}）</div>
    <div class="sys-grid">${devCards}</div>
  `;
}

function svgLineChart(series, opts) {
  const o = opts || {};
  const W = o.width || 460;
  const H = o.height || 130;
  const PL = 42, PR = 8, PT = 10, PB = 18;
  const iw = W - PL - PR;
  const ih = H - PT - PB;
  const n = Math.max(...series.map((s) => s.data.length), 0);
  if (!n) return '<div class="chart-empty">等待数据…</div>';

  const maxV = Math.max(1, ...series.flatMap((s) => s.data));
  const niceMax = niceCeil(maxV);

  const X = (i) => PL + (n <= 1 ? iw : (i / (n - 1)) * iw);
  const Y = (v) => PT + ih - (Math.min(v, niceMax) / niceMax) * ih;

  let grid = '';
  for (let g = 0; g <= 4; g++) {
    const y = PT + (ih / 4) * g;
    const val = niceMax * (1 - g / 4);
    grid += `<line x1="${PL}" y1="${y.toFixed(1)}" x2="${W - PR}" y2="${y.toFixed(1)}" class="grid-line"/>`;
    grid += `<text x="${PL - 6}" y="${(y + 3.5).toFixed(1)}" class="axis-txt" text-anchor="end">${fmtAxis(val, o.fmt)}</text>`;
  }

  let paths = '';
  series.forEach((s) => {
    if (!s.data.length) return;
    const pts = s.data.map((v, i) => `${X(i).toFixed(1)},${Y(v).toFixed(1)}`).join(' ');
    if (o.area !== false && series.length === 1) {
      const areaPts = `${PL},${PT + ih} ${pts} ${X(s.data.length - 1).toFixed(1)},${PT + ih}`;
      paths += `<polygon points="${areaPts}" fill="${s.color}" opacity="0.14"/>`;
    }
    paths += `<polyline points="${pts}" fill="none" stroke="${s.color}" stroke-width="2"
      stroke-linejoin="round" stroke-linecap="round"/>`;
   
    const li = s.data.length - 1;
    paths += `<circle cx="${X(li).toFixed(1)}" cy="${Y(s.data[li]).toFixed(1)}" r="3" fill="${s.color}"/>`;
  });

  return `<svg viewBox="0 0 ${W} ${H}" class="chart-svg" preserveAspectRatio="none">
    ${grid}${paths}
  </svg>`;
}

function niceCeil(v) {
  if (v <= 0) return 1;
  const mag = Math.pow(10, Math.floor(Math.log10(v)));
  const norm = v / mag;
  let nice;
  if (norm <= 1) nice = 1;
  else if (norm <= 2) nice = 2;
  else if (norm <= 5) nice = 5;
  else nice = 10;
  return nice * mag;
}

function fmtAxis(v, kind) {
  if (kind === 'bytes') {
    if (v >= 1048576) return (v / 1048576).toFixed(1) + 'M';
    if (v >= 1024) return (v / 1024).toFixed(0) + 'K';
    return v.toFixed(0);
  }
  return v >= 100 ? v.toFixed(0) : v.toFixed(v >= 10 ? 0 : 1);
}

function svgDonut(pct, color, label, sub, invert) {
  const v = Math.max(0, Math.min(100, Number(pct) || 0));
  const R = 42, C = 2 * Math.PI * R;
  const dash = (v / 100) * C;
  let col = color || 'var(--accent)';
 
  if (!invert) {
    if (v >= 85) col = 'var(--err)';
    else if (v >= 65) col = 'var(--warn)';
  }
  return `<div class="donut">
    <svg viewBox="0 0 110 110" class="donut-svg">
      <circle cx="55" cy="55" r="${R}" fill="none" stroke="var(--line)" stroke-width="11"/>
      <circle cx="55" cy="55" r="${R}" fill="none" stroke="${col}" stroke-width="11"
        stroke-dasharray="${dash.toFixed(1)} ${C.toFixed(1)}"
        stroke-linecap="round" transform="rotate(-90 55 55)"/>
      <text x="55" y="52" text-anchor="middle" class="donut-val">${v.toFixed(0)}%</text>
      <text x="55" y="70" text-anchor="middle" class="donut-sub">${esc(sub || '')}</text>
    </svg>
    <div class="donut-label">${esc(label)}</div>
  </div>`;
}

function svgBarChart(items, opts) {
  const o = opts || {};
  if (!items.length) return '<div class="chart-empty">无数据</div>';
  const max = Math.max(...items.map((x) => x.value), 1);
  return `<div class="barchart">${items.map((x) => {
    const w = Math.max(1, (x.value / max) * 100);
    return `<div class="bar-row">
      <span class="bar-label" title="${esc(x.label)}">${esc(x.label)}</span>
      <span class="bar-track"><span class="bar-fill" style="width:${w.toFixed(1)}%;background:${x.color || 'var(--accent)'}"></span></span>
      <span class="bar-text">${esc(x.text)}</span>
    </div>`;
  }).join('')}</div>`;
}

function svgSparkbars(data, color) {
  if (!data || !data.length) return '<div class="chart-empty">等待数据…</div>';
  const W = 300, H = 56, n = data.length;
  const max = Math.max(1, ...data);
  const bw = Math.max(1.5, (W - 2) / n - 1.2);
  let bars = '';
  data.forEach((v, i) => {
    const h = Math.max(1.5, (v / max) * (H - 4));
    const x = 1 + i * ((W - 2) / n);
    bars += `<rect x="${x.toFixed(1)}" y="${(H - h).toFixed(1)}" width="${bw.toFixed(1)}"
      height="${h.toFixed(1)}" fill="${color || 'var(--accent)'}" rx="1" opacity="0.85"/>`;
  });
  return `<svg viewBox="0 0 ${W} ${H}" class="chart-svg sparkbars" preserveAspectRatio="none">${bars}</svg>`;
}

function chartCard(title, body, sub) {
  return `<div class="sys-card chart-card">
    <div class="sys-card-head">${esc(title)}${sub ? `<span class="chart-sub">${esc(sub)}</span>` : ''}</div>
    <div class="chart-body">${body}</div>
  </div>`;
}

async function loadAccount() {
  try {
    const d = await apiGet('/api/sysinfo');
    $('#acctCurUser').textContent = (d.account && d.account.username) || '—';
    $('#acctDevKey').textContent = (d.account && d.account.device_key) || '—';
  } catch (e) {
    $('#acctCurUser').textContent = '读取失败';
  }
}

async function saveAccount() {
  const msg = $('#acctMsg');
  const user = $('#acctUser').value.trim();
  const pwd = $('#acctPwd').value;
  const pwd2 = $('#acctPwd2').value;
  const old = $('#acctOld').value;

  const show = (t, k) => { msg.textContent = t; msg.className = 'acct-msg ' + (k || ''); };

  if (!user && !pwd) return show('用户名和新密码都空着，没有要改的内容', 'err');
  if (!old) return show('请填写当前密码', 'err');
  if (pwd && pwd.length < 12) return show('新密码至少 12 位', 'err');
  if (pwd && pwd !== pwd2) return show('两次输入的新密码不一致', 'err');
  if (user && !/^[0-9A-Za-z_.@-]{3,32}$/.test(user)) return show('用户名只能 3-32 位字母数字下划线', 'err');

  const btn = $('#btnSaveAcct');
  btn.disabled = true;
  const oldTxt = btn.textContent;
  btn.textContent = '正在保存…';
  try {
    const r = await apiPost('/api/account', {
      username: user || '', password: pwd || '', old_password: old,
    });
    if (r.relogin) {
      show('修改成功，密码已变更，正在跳转到登录页…', 'ok');
      setTimeout(() => { location.href = '/login'; }, 1600);
      return;
    }
    show('修改成功', 'ok');
    $('#acctUser').value = ''; $('#acctPwd').value = ''; $('#acctPwd2').value = ''; $('#acctOld').value = '';
    loadAccount();
  } catch (e) {
    show(e.message, 'err');
  } finally {
    btn.disabled = false;
    btn.textContent = oldTxt;
  }
}

const STILL = new URLSearchParams(location.search).has('still'); 

(async function init() {
  bind();
 
  const qs = new URLSearchParams(location.search);
  const urlTab = qs.get('tab');
  const savedTab = urlTab || localStorage.getItem('relay.tab') || 'status';
  switchTab(['status', 'transfer', 'account'].includes(savedTab) ? savedTab : 'status');
  try {
    const snap = await apiPost('/api/events', { ver: 0, wait: 0.2 });
    applySnapshot(snap);
  } catch (e) {
    toast(e.message);
  }
 
  loop();
  renderAll();
 
  const dev = currentDev();
  if (dev && dev.disks && dev.disks[0] && !S.loaded) {
    try { await navigate(dev.disks[0].path); } catch (e) {  }
  }
})();
