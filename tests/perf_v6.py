
"""C1 性能探针：用 curl 测 IPv6 文件服务的真实吞吐和并发。

Invoke-WebRequest 在 PowerShell 里太慢，测不出服务端上限，所以用 curl.exe。
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time

AGENT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "client", "agent.ps1")

PS_SETUP = r"""
$ErrorActionPreference = 'Stop'
. '%s'
$tmp = Join-Path $env:TEMP 'v6perf'
New-Item -ItemType Directory -Path $tmp -Force | Out-Null
$big = Join-Path $tmp 'big.bin'
if (-not (Test-Path $big) -or (Get-Item $big).Length -ne 104857600) {
    $fs = [IO.File]::Create($big)
    $chunk = New-Object byte[] 1048576
    (New-Object Random 7).NextBytes($chunk)
    for ($i = 0; $i -lt 100; $i++) { $fs.Write($chunk, 0, $chunk.Length) }
    $fs.Close()
}
$res = Start-V6Serve -Files @(@{ id = 'big'; token = 'tok'; path = $big; name = 'big.bin' }) -Port 0
if (-not $res.ok) { Write-Output ('FAIL ' + $res.error); exit 1 }
Write-Output ('PORT ' + $res.port)
Write-Output ('ADDRS ' + (@($res.addrs) -join ','))
# 保持进程活着，等外部测完
Write-Output 'READY'
$null = Read-Host
Stop-V6Serve
""" % AGENT.replace("'", "''")

def main():
    print("启动服务…")
    p = subprocess.Popen(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", PS_SETUP],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
    port = None
    for line in p.stdout:
        line = line.strip()
        if line.startswith("PORT "):
            port = int(line.split()[1])
            print("  端口:", port)
        elif line.startswith("ADDRS "):
            print("  报告地址:", line[6:] or "(本机无公网 IPv6，符合预期)")
        elif line == "READY":
            break
        elif line.startswith("FAIL"):
            print("  启动失败:", line)
            p.kill()
            return 1

    if not port:
        print("没拿到端口")
        p.kill()
        return 1

    base = "http://[::1]:%d/f/tok/big" % port
    curl = "curl.exe"

    def run(label, url, extra=None, outfile=None):
        args = [curl, "-s", "-o", outfile or "NUL", "-w", "%{http_code} %{size_download} %{speed_download}"]
        if extra:
            args += extra
        args.append(url)
        t0 = time.time()
        r = subprocess.run(args, capture_output=True, text=True)
        dt = time.time() - t0
        return r.stdout.strip(), dt, r.stderr.strip()

    print("\n[单连接 100 MB]")
    out, dt, err = run("single", base)
    if err:
        print("  curl 错误:", err)
    parts = out.split()
    if len(parts) == 3:
        code, size, speed = parts[0], int(parts[1]), float(parts[2])
        print("  状态 %s  下载 %d 字节  %.1f MB/s  墙钟 %.2fs"
              % (code, size, speed / 1048576, dt))
    else:
        print("  输出:", out)

    print("\n[8 路并发，各取 25 MB Range]")
    results = []
    lock = threading.Lock()

    def worker(i):
        rng = "0-26214399"
        out, dt, err = run("w%d" % i, base, ["-r", rng])
        with lock:
            results.append((i, out, dt, err))

    t0 = time.time()
    ths = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    wall = time.time() - t0

    total = 0
    bad = 0
    for i, out, dt, err in sorted(results):
        parts = out.split()
        if len(parts) == 3 and parts[0] == "206":
            total += int(parts[1])
        else:
            bad += 1
            print("  worker %d 异常: %r %r" % (i, out, err))
    print("  合计下载 %.1f MB，墙钟 %.2fs，聚合 %.1f MB/s，异常 %d 路"
          % (total / 1048576.0, wall, total / 1048576.0 / wall, bad))

    print("\n[416 越界 Range]")
    out, dt, err = run("416", base, ["-r", "999999999-1000000000"])
    print("  curl 返回:", out, err)

    print("\n[错误令牌 403]")
    out, dt, err = run("403", base.replace("/f/tok/", "/f/bad/"))
    print("  curl 返回:", out, err)

    print("\n[ping]")
    out, dt, err = run("ping", "http://[::1]:%d/ping" % port)
    print("  curl 返回:", out, err)

    try:
        p.stdin.write("\n")
        p.stdin.flush()
    except Exception:
        pass
    time.sleep(0.5)
    try:
        p.kill()
    except Exception:
        pass
    return 0

if __name__ == "__main__":
    sys.exit(main())
