
"""构建单文件自解压部署包 dist/relay-install.run

做法：把一段 shell 前导脚本 + 一行标记 + dist/relay-deploy.tar.gz 的原始字节
拼成一个文件。用户只需要上传这一个文件、运行一条命令。

    scp dist/relay-install.run root@服务器:/root/
    ssh root@服务器 "bash /root/relay-install.run"

payload 直接复用 pack_all.py 产出的 tar.gz，两边永远同步，不会出现
「.run 里的 relay.py 比 tar.gz 里的旧」这种问题。
"""
import os
import subprocess
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(BASE, "dist")
TARBALL = os.path.join(DIST, "relay-deploy.tar.gz")
OUT = os.path.join(DIST, "relay-install.run")

MARK = "__PAYLOAD_BELOW__"

PREAMBLE = r"""#!/bin/bash
# ============================================================
#   跨公网信息传输 —— 单文件一键部署（Linux 服务器）
#
#   用法：
#       sudo bash relay-install.run
#
#   这一个文件里已经包含了服务端全部程序文件。
#   运行后它会问你几个问题，然后自动完成：
#     · 复制程序到 /opt/server
#     · 生成配置文件     · 创建系统用户
#     · 安装并启动服务（开机自启）
#     · 真实读写阿里云 OSS 自检
#     · 放行本机防火墙端口
#     · 打印控制台地址、登录密码、设备密钥
#
#   重复运行是安全的：已有配置会问你是否保留。
# ============================================================

# 万一用 sh 执行，自动切到 bash
if [ -z "${BASH_VERSION:-}" ]; then
    exec bash "$0" "$@"
fi

set -euo pipefail

red()   { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
cyan()  { printf '\033[36m%s\033[0m\n' "$*"; }
bold()  { printf '\033[1m%s\033[0m\n' "$*"; }
die()   { echo; red "  [X] $*"; echo; exit 1; }

case "${1:-}" in
    -h|--help)
        sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
        exit 0
        ;;
esac

bold "============================================================"
bold "  跨公网信息传输 —— 单文件一键部署"
bold "============================================================"
echo

[ "$(id -u)" -eq 0 ] || die "请用 root 运行：sudo bash relay-install.run"
command -v tar >/dev/null 2>&1 || die "系统缺少 tar，请先安装：apt install -y tar"

SELF="$0"
[ -f "$SELF" ] || die "请直接把本文件当脚本运行，不要用管道输入"

# 定位内嵌数据起始行（标记行必须整行完全相等，故用 == 比较）
MARK="__PAYLOAD_BELOW__"
PAYLOAD_LINE=$(LC_ALL=C awk -v m="$MARK" '$0 == m { print NR + 1; exit }' "$SELF" 2>/dev/null || true)
[ -n "${PAYLOAD_LINE:-}" ] || die "安装包已损坏：找不到内嵌数据标记。请重新下载"

TMP="$(mktemp -d /tmp/relay-install.XXXXXX)" || die "无法创建临时目录"
cleanup() {
    rm -rf "$TMP"
}
trap cleanup EXIT INT TERM

cyan "  正在解出安装文件…"
tail -n +"$PAYLOAD_LINE" "$SELF" | tar -xzf - -C "$TMP" 2>/dev/null \
    || die "解压失败：安装包可能不完整，请重新下载"

[ -f "$TMP/relay-deploy/server/deploy/install.sh" ] || die "安装包内容异常：缺少 deploy/install.sh"

green "  解包完成"
echo
cyan "  下面开始正式部署。"
echo

bash "$TMP/relay-deploy/server/deploy/install.sh"

echo
bold "============================================================"
green "  全部完成"
bold "============================================================"
echo
echo "  服务器这边已经不需要再做任何操作了，本窗口可以关闭。"
echo
echo "  唯一还需要你自己确认的一件事："
echo "    如果浏览器打不开上面的地址，请到云厂商控制台"
echo "    （阿里云/腾讯云/AWS 等）的安全组里，放行对应的 TCP 端口。"
echo "    这一步在机器外部，脚本无法代劳。"
echo

# 必须在这里退出：下面紧跟的就是内嵌数据标记行，
# 不退出的话 bash 会把那一行当命令去执行，报 command not found。
exit 0
"""

def main():
    if not os.path.exists(TARBALL):
        print("找不到 %s，先运行：python pack_all.py" % TARBALL)
        return 1

    payload = open(TARBALL, "rb").read()
    if not payload:
        print("tar.gz 是空的")
        return 1

    pre = PREAMBLE.replace("\r\n", "\n").replace("\r", "\n")
    if not pre.endswith("\n"):
        pre += "\n"
    pre_bytes = pre.encode("utf-8")
    if pre_bytes.startswith(b"\xef\xbb\xbf"):
        print("前导脚本不能有 BOM")
        return 1

    data = pre_bytes + (MARK + "\n").encode("ascii") + payload
    with open(OUT, "wb") as f:
        f.write(data)

    print("已生成: %s" % OUT)
    print("  前导脚本 : %d 字节" % len(pre_bytes))
    print("  内嵌数据 : %d 字节（relay-deploy.tar.gz）" % len(payload))
    print("  合计     : %.1f KB" % (len(data) / 1024.0))
    print()
    print("  ── 使用方法 ──")
    print()
    print("    scp %s root@你的服务器:/root/" % os.path.basename(OUT))
    print("    ssh root@你的服务器 \"bash /root/%s\"" % os.path.basename(OUT))
    print()
    return 0

if __name__ == "__main__":
    sys.exit(main())
