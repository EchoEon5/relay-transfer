
"""把需要 UTF-8 BOM 的脚本补回 BOM。

agent.ps1 / *.ps1 在 Windows PowerShell 5.1 下如果没有 BOM，
会被按系统 ANSI 编码（中文系统是 GBK）读取，中文全部变成乱码并导致语法错误。

用法：
    python tools/fix_bom.py            # 修默认列表
    python tools/fix_bom.py a.ps1 b.ps1
"""
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULTS = [
    os.path.join(HERE, "client", "agent.ps1"),
    os.path.join(HERE, "tests", "test_v6serve.ps1"),
    os.path.join(HERE, "tests", "test_fsops.ps1"),
    os.path.join(HERE, "tests", "test_zip.ps1"),
    os.path.join(HERE, "tests", "test_gui.ps1"),
]

BOM = b"\xef\xbb\xbf"

def fix(path):
    if not os.path.isfile(path):
        return None
    with open(path, "rb") as f:
        raw = f.read()
    has = raw.startswith(BOM)
    if has:
        return "已OK"
    with open(path, "wb") as f:
        f.write(BOM + raw)
    return "补上"

def main(argv):
    targets = argv[1:] or DEFAULTS
    for p in targets:
        r = fix(p)
        if r is None:
            print("  跳过（不存在）: %s" % p)
        else:
            print("  %-4s %s" % (r, os.path.relpath(p, HERE)))
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv))
