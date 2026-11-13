
"""把 Generator.cs 编译成单文件 EXE，模板嵌入资源。

用法：
  python build_exe.py

产出：
  dist/信息传输助手生成器.exe
"""

import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(HERE, "dist")
CLIENT = os.path.join(HERE, "client")

CSC_CANDIDATES = [
    r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe",
    r"C:\Windows\Microsoft.NET\Framework\v4.0.30319\csc.exe",
    r"C:\Windows\Microsoft.NET\Framework64\v3.5\csc.exe",
]

REFERENCE_DIRS = [
    r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319",
    r"C:\Windows\Microsoft.NET\Framework\v4.0.30319",
]

def find_csc():
    for p in CSC_CANDIDATES:
        if os.path.isfile(p):
            return p

    for name in ("csc.exe", "csc"):
        found = shutil.which(name)
        if found:
            return found
    return None

def build_template():
    """把 bat_template.txt 的头部 + agent.ps1 载荷拼成模板（占位符保持原样）。

    注意：#PSSTART 在模板里出现两次——一次在 cmd 的 PowerShell 命令行字符串中
    （作为 [Array]::IndexOf 的查找参数），一次是真正独立成行的分隔符。
    必须只按「独立成行」的那一处切分。
    """
    with open(os.path.join(CLIENT, "bat_template.txt"), "r", encoding="utf-8-sig") as f:
        tpl = f.read()
    with open(os.path.join(CLIENT, "agent.ps1"), "r", encoding="utf-8-sig") as f:
        payload = f.read()

    lines = tpl.replace("\r\n", "\n").split("\n")
    marker = None
    for i, line in enumerate(lines):
        if line.strip() == "#PSSTART":
            marker = i
            break
    if marker is None:
        sys.exit("模板缺少独立成行的 #PSSTART 分隔符")

    head = "\n".join(lines[:marker + 1])
    out = head + "\n" + payload
    return out

def main():
    csc = find_csc()
    if not csc:
        sys.exit("找不到 csc.exe。需要安装 .NET Framework 4.x")

    print("=" * 60)
    print("  编译生成器 EXE")
    print("=" * 60)
    print("\n编译器: %s" % csc)

    tpl = build_template()
    tmpdir = tempfile.mkdtemp(prefix="relaygen_")
    tpl_path = os.path.join(tmpdir, "template.dat")
    with open(tpl_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(tpl)
    print("模板: %d 字节" % len(tpl.encode("utf-8")))

    os.makedirs(DIST, exist_ok=True)
    out_exe = os.path.join(DIST, "信息传输助手生成器.exe")
    if os.path.exists(out_exe):
        os.remove(out_exe)

    refs = []
    for d in REFERENCE_DIRS:
        if os.path.isdir(d):
            for dll in ("System.dll", "System.Drawing.dll", "System.Windows.Forms.dll",
                        "System.Core.dll"):
                p = os.path.join(d, dll)
                if os.path.isfile(p):
                    refs.append("/reference:" + p)
            break

    cmd = [
        csc,
        "/target:winexe",
        "/platform:anycpu",
        "/optimize+",
        "/nologo",
        "/out:" + out_exe,
        "/resource:%s,%s" % (tpl_path, "template.dat"),
    ] + refs + [
        "/win32icon:" + ensure_icon(tmpdir) if ensure_icon(tmpdir) else "",
        os.path.join(CLIENT, "Generator.cs"),
    ]
    cmd = [c for c in cmd if c]

    print("\n编译中…")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print("编译失败:")
        print(r.stdout)
        print(r.stderr)
        sys.exit(1)

    if r.stdout.strip():
        print(r.stdout.strip())

    if not os.path.isfile(out_exe):
        sys.exit("编译声称成功，但没生成文件")

    size = os.path.getsize(out_exe)
    print("\n完成: %s (%.1f KB)" % (out_exe, size / 1024.0))

    with open(out_exe, "rb") as f:
        blob = f.read()
    for marker in (b"__RELAY_URL__", b"__DEVICE_KEY__", b"#PSSTART"):
        if marker not in blob:
            print("  [警告] EXE 里找不到标记 %s" % marker.decode())
        else:
            print("  [OK] 含标记 %s" % marker.decode())

    shutil.rmtree(tmpdir, ignore_errors=True)
    return 0

def ensure_icon(tmpdir):
    """没有 ico 就返回 None"""
    p = os.path.join(CLIENT, "app.ico")
    return p if os.path.isfile(p) else None

if __name__ == "__main__":
    sys.exit(main())
