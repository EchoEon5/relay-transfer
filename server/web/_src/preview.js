
(function () {
  'use strict';

  const isHttp = location.protocol === 'http:' || location.protocol === 'https:';
  const forcePreview = new URLSearchParams(location.search).has('preview');
  if (isHttp && !forcePreview) return;

  const NOW = Math.floor(Date.now() / 1000);

  const DEV = {
    id: 'preview-device',
    host: 'PC-DEMO',
    user: '演示用户',
    admin: true,
    ip: '192.0.2.23',
    online: true,
    connected_at: NOW - 3725,
    last_seen: NOW - 3,
    agent: '2.1.0',
    disks: [
      { name: 'C:', path: 'C:\\', total: 511101108224, free: 178956970240 },
      { name: 'D:', path: 'D:\\', total: 1000204886016, free: 623456789012 },
    ],
  };

  const TREE = {
    'C:\\': [
      { name: 'Users', is_dir: true, size: 0, mtime: NOW - 86400, type: 'folder' },
      { name: 'Program Files', is_dir: true, size: 0, mtime: NOW - 172800, type: 'folder' },
      { name: 'Windows', is_dir: true, size: 0, mtime: NOW - 259200, type: 'folder' },
      { name: 'pagefile.sys', is_dir: false, size: 8589934592, mtime: NOW - 3600, type: 'file' },
    ],
    'C:\\Users': [
      { name: '演示用户', is_dir: true, size: 0, mtime: NOW - 7200, type: 'folder' },
      { name: 'Public', is_dir: true, size: 0, mtime: NOW - 86400, type: 'folder' },
    ],
    'C:\\Users\\演示用户': [
      { name: 'Desktop', is_dir: true, size: 0, mtime: NOW - 1800, type: 'folder' },
      { name: 'Downloads', is_dir: true, size: 0, mtime: NOW - 900, type: 'folder' },
      { name: 'Documents', is_dir: true, size: 0, mtime: NOW - 5400, type: 'folder' },
      { name: 'Pictures', is_dir: true, size: 0, mtime: NOW - 43200, type: 'folder' },
    ],
    'C:\\Users\\演示用户\\Downloads': [
      { name: '项目素材.zip', is_dir: false, size: 2147483648, mtime: NOW - 600, type: 'file' },
      { name: '设计稿.psd', is_dir: false, size: 524288000, mtime: NOW - 1200, type: 'file' },
      { name: '会议录像.mp4', is_dir: false, size: 4724464025, mtime: NOW - 3600, type: 'file' },
      { name: 'backup', is_dir: true, size: 0, mtime: NOW - 86400, type: 'folder' },
      { name: '安装包.exe', is_dir: false, size: 104857600, mtime: NOW - 7200, type: 'file' },
      { name: '说明文档.docx', is_dir: false, size: 245760, mtime: NOW - 10800, type: 'file' },
      { name: 'config.json', is_dir: false, size: 2048, mtime: NOW - 14400, type: 'file' },
    ],
    'C:\\Users\\演示用户\\Downloads\\backup': [
      { name: '数据库.sql', is_dir: false, size: 838860800, mtime: NOW - 90000, type: 'file' },
      { name: '照片', is_dir: true, size: 0, mtime: NOW - 95000, type: 'folder' },
    ],
    'C:\\Users\\演示用户\\Desktop': [
      { name: '待办事项.txt', is_dir: false, size: 4096, mtime: NOW - 300, type: 'file' },
      { name: '方案.pptx', is_dir: false, size: 15728640, mtime: NOW - 2400, type: 'file' },
    ],
    'C:\\Users\\演示用户\\Documents': [
      { name: '合同.pdf', is_dir: false, size: 3145728, mtime: NOW - 4800, type: 'file' },
      { name: '报表.xlsx', is_dir: false, size: 1048576, mtime: NOW - 9600, type: 'file' },
    ],
    'C:\\Users\\演示用户\\Pictures': [
      { name: '截图_001.png', is_dir: false, size: 2097152, mtime: NOW - 43200, type: 'file' },
    ],
    'D:\\': [
      { name: '项目', is_dir: true, size: 0, mtime: NOW - 14400, type: 'folder' },
      { name: '影视', is_dir: true, size: 0, mtime: NOW - 28800, type: 'folder' },
      { name: 'data.tar.gz', is_dir: false, size: 16106127360, mtime: NOW - 7200, type: 'file' },
    ],
    'D:\\项目': [
      { name: '源码', is_dir: true, size: 0, mtime: NOW - 3600, type: 'folder' },
      { name: 'README.md', is_dir: false, size: 8192, mtime: NOW - 3600, type: 'file' },
    ],
    'D:\\影视': [
      { name: '教程合集.mp4', is_dir: false, size: 8589934592, mtime: NOW - 20000, type: 'file' },
    ],
  };

  const QUICK = {
    '::userprofile': 'C:\\Users\\演示用户',
    '::desktop': 'C:\\Users\\演示用户\\Desktop',
    '::downloads': 'C:\\Users\\演示用户\\Downloads',
    '::documents': 'C:\\Users\\演示用户\\Documents',
  };

  let HISTORY = [
    { key: 'k1', name: '项目素材.zip', size: 2147483648, at: NOW - 900, kind: 'file' },
    { key: 'k2', name: '设计稿.psd', size: 524288000, at: NOW - 3600, kind: 'file' },
    { key: 'k3', name: '会议录像.mp4', size: 4724464025, at: NOW - 10800, kind: 'file' },
  ];

  let JOB = {
    state: 'running',
    phase: '正在上传',
    done: 1288490188,
    total: 2147483648,
    speed: 6291456,
    eta: 137,
    skipped: 0,
  };

  const TREND = { at: [], cpu: [], mem: [], net_rx: [], net_tx: [] };
  for (let i = 0; i < 40; i++) {
    TREND.at.push(NOW - (40 - i) * 3);
    TREND.cpu.push(+(8 + Math.random() * 14).toFixed(1));
    TREND.mem.push(+(34 + Math.random() * 4).toFixed(1));
    TREND.net_rx.push(Math.floor(Math.random() * 120000));
    TREND.net_tx.push(Math.floor(Math.random() * 80000));
  }

  let SEQ = 1;
  const RESULTS = {};

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const clone = (o) => JSON.parse(JSON.stringify(o));

  function apiState() {
    const dev = clone(DEV);
    dev.results = clone(RESULTS);
    return { ok: true, ver: Date.now(), devices: [dev], history: clone(HISTORY), job: clone(JOB) };
  }

  function apiSysinfo() {
    const now = Math.floor(Date.now() / 1000);
    TREND.at.push(now);
    TREND.cpu.push(+(8 + Math.random() * 14).toFixed(1));
    TREND.mem.push(+(34 + Math.random() * 4).toFixed(1));
    TREND.net_rx.push(Math.floor(Math.random() * 120000));
    TREND.net_tx.push(Math.floor(Math.random() * 80000));
    for (const k of Object.keys(TREND)) TREND[k] = TREND[k].slice(-60);

    return {
      ok: true,
      server: {
        hostname: 'relay-preview', local_ip: '198.51.100.184',
        public_base: 'http://203.0.113.10:8080', listen: '0.0.0.0', port: 8080,
        uptime: 86400 * 3 + 7200, started_at: now - (86400 * 3 + 7200),
        pid: 12345, rss: 31.2 * 1024 * 1024, threads: 4, python: '3.14.4',
      },
      os: { name: 'Ubuntu', version: '26.04.1 LTS', kernel: '7.0.0-38-generic', arch: 'x86_64' },
      cpu: {
        model: 'Intel(R) Xeon(R) Platinum', cores: 2,
        load1: '0.21', load5: '0.23', load15: '0.2',
        usage_pct: TREND.cpu[TREND.cpu.length - 1],
      },
      memory: {
        total: 3.54 * 1024 * 1024 * 1024, used: 1.22 * 1024 * 1024 * 1024,
        available: 2.32 * 1024 * 1024 * 1024,
        usage_pct: TREND.mem[TREND.mem.length - 1],
        swap_total: 0, swap_used: 0,
      },
      disks: [{ mount: '/', total: 41.9 * 1024 * 1024 * 1024, used: 16.9 * 1024 * 1024 * 1024 }],
      oss: {
        configured: true, live: true, endpoint: 'oss-cn-hangzhou.aliyuncs.com',
        bucket: 'your-bucket', scheme: 'https', part_size: 64 * 1024 * 1024,
        download_expires: 86400, ak_masked: 'LTAI5t6R…xXxX', sk_masked: '已配置（30 位）',
      },
      devices_online: 1, devices_total: 1,
      history_count: HISTORY.length,
      history_bytes: HISTORY.reduce((a, b) => a + (b.size || 0), 0),
      sessions: 1, state_dir: '/opt/server/state', config_path: '/opt/server/config.json',
      account: { username: 'admin', device_key: '00000000000000000000000000000000' },
      trend: clone(TREND),
    };
  }

  function apiCmd(body) {
    const a = body.action;
    const args = body.args || {};
    const seq = SEQ++;

    function reply(result) {
      RESULTS[seq] = { seq: seq, ok: true, data: result };
      return { ok: true, seq: seq };
    }

    if (a === 'list') {
      const p = QUICK[args.path] || args.path || 'C:\\';
      const parent = p.replace(/[^\\]+\\?$/, '') || '';
      return reply({ path: p, parent: parent, entries: clone(TREE[p] || []), disks: clone(DEV.disks) });
    }
    if (a === 'drives') return reply({ disks: clone(DEV.disks) });
    if (a === 'push') {
      JOB = { state: 'running', phase: '正在上传', done: 0, total: 52428800, speed: 5242880, eta: 10, skipped: 0 };
      let n = 0;
      const t = setInterval(() => {
        n++;
        JOB.done = Math.min(JOB.total, JOB.done + 3145728);
        JOB.eta = Math.max(0, 10 - n);
        if (JOB.done >= JOB.total) {
          JOB.state = 'done';
          JOB.phase = '已完成';
          HISTORY.unshift({
            key: 'knew' + Date.now(), name: '新传输的文件.bin',
            size: JOB.total, at: Math.floor(Date.now() / 1000), kind: 'file',
          });
          clearInterval(t);
        }
      }, 800);
      return reply({ accepted: true });
    }
    if (a === 'cancel') { JOB.state = 'canceled'; JOB.phase = '已取消'; return reply({ canceled: true }); }
    if (a === 'disconnect') return reply({ disconnected: true });
    return reply({});
  }

  const ROUTES = [
    [/\/api\/events$/, apiState],
    [/\/api\/state$/, apiState],
    [/\/api\/sysinfo$/, apiSysinfo],
    [/\/api\/cmd$/, apiCmd],
    [/\/api\/link$/, (b) => {
      if (b.op === 'delete') { HISTORY = HISTORY.filter((h) => h.key !== b.key); return { ok: true }; }
      return { ok: true, url: '#' };
    }],
    [/\/api\/history$/, (b) => {
      if (b.op === 'clear') { HISTORY = []; return { ok: true }; }
      return { ok: true };
    }],
    [/\/api\/disconnect$/, () => ({ ok: true })],
    [/\/api\/account$/, () => ({ ok: true, username: 'admin' })],
    [/\/api\/logout$/, () => ({ ok: true })],
  ];

  const realFetch = window.fetch.bind(window);

  window.fetch = function (input, init) {
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    const path = url.replace(/^https?:\/\/[^/]+/, '').replace(/^.*\/api\//, '/api/');

    let body = {};
    try { if (init && init.body) body = JSON.parse(init.body); } catch (e) { }

    if (/\/api\/login/.test(path)) {
      return sleep(100).then(() => mkRes({ ok: true, username: 'admin' }));
    }

    for (const [re, fn] of ROUTES) {
      if (re.test(path)) {
        return sleep(120).then(() => mkRes(fn(body)));
      }
    }
    return realFetch(input, init);
  };

  function mkRes(data) {
    return {
      ok: true, status: 200,
      json: () => Promise.resolve(data),
      text: () => Promise.resolve(JSON.stringify(data)),
    };
  }

  setInterval(() => {
    if (JOB.state === 'running') JOB.done = Math.min(JOB.total, JOB.done + 1048576);
  }, 1500);

  console.log('%c[预览模式] 正在使用模拟数据', 'color:#0f6cbd;font-weight:bold;font-size:13px');
  console.log('  所有数据都是假的，仅供调试样式');
  console.log('  通过 http:// 访问服务器时不会加载这个文件');
})();
