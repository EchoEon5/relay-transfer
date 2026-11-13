#!/bin/bash

set -euo pipefail

APP_DIR="${RELAY_APP_DIR:-/opt/server}"
SERVICE_NAME="${RELAY_SERVICE_NAME:-relay}"
PY_BIN=""
PORT="8080"

red()   { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
cyan()  { printf '\033[36m%s\033[0m\n' "$*"; }
bold()  { printf '\033[1m%s\033[0m\n' "$*"; }
die()   { echo; red "  [X] $*"; echo; exit 1; }

UNATTENDED="${RELAY_UNATTENDED:-0}"

ask() {
    local prompt="$1" default="${2:-}" secret="${3:-}" envname="${4:-}"
    if [ "$UNATTENDED" = "1" ] && [ -n "$envname" ]; then
        local preset="${!envname:-}"
        [ -n "$preset" ] || preset="$default"
        printf '%s' "$preset"
        return
    fi
    local tip="$prompt"
    [ -n "$default" ] && tip="$tip [$default]"
    tip="$tip: "
    local val=""
    while true; do
        if [ "$secret" = "secret" ]; then
            printf '%s' "$tip" >&2
            read -rs val </dev/tty || die "读取输入失败"
            printf '\n' >&2
            val="${val//$'\r'/}"
            val="${val//$'\n'/}"
            val="${val//$'\t'/}"
        else
            printf '%s' "$tip" >&2
            read -r val </dev/tty || die "读取输入失败"
            val="${val#"${val%%[![:space:]]*}"}"
            val="${val%"${val##*[![:space:]]}"}"
        fi
        [ -z "$val" ] && val="$default"
        if [ -n "$val" ]; then
            printf '%s' "$val"
            return
        fi
        printf '  这一项必填。\n' >&2
    done
}

ask_yes() {
    local prompt="$1" default="${2:-y}"
    if [ "$UNATTENDED" = "1" ]; then
        [ "$default" = "y" ] && return 0 || return 1
    fi
    local hint="[Y/n]"; [ "$default" = "n" ] && hint="[y/N]"
    local val=""
    read -rp "$prompt $hint: " val </dev/tty || val=""
    val="${val//$'\r'/}"
    val="${val//$'\n'/}"
    val="${val:-$default}"
    case "$val" in y|Y|yes|YES) return 0;; *) return 1;; esac
}

clear 2>/dev/null || true
bold "============================================================"
bold "  文件传输中继 —— 一键部署"
bold "============================================================"
echo

[ "$(id -u)" -eq 0 ] || die "请用 root 运行：sudo bash install.sh"

echo "[1/7] 检查 Python"
for cand in python3.12 python3.11 python3.10 python3; do
    if command -v "$cand" >/dev/null 2>&1; then
        ver=$("$cand" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || echo "0.0")
        major=${ver%%.*}; minor=${ver##*.}
        if [ "$major" -eq 3 ] && [ "$minor" -ge 8 ]; then
            PY_BIN=$(command -v "$cand")
            green "  OK  $PY_BIN  (Python $ver)"
            break
        fi
    fi
done
[ -n "$PY_BIN" ] || die "找不到 Python 3.8+。请先安装：apt install -y python3  或  yum install -y python3"

echo
echo "[2/7] 配置"
mkdir -p "$APP_DIR"

SRC_ROOT=""
if [ -n "${RELAY_SRC:-}" ] && [ -f "${RELAY_SRC}/server/relay.py" ]; then
    SRC_ROOT="$(cd "$RELAY_SRC" && pwd)"
fi

if [ -z "$SRC_ROOT" ]; then
    SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || pwd)"
    for cand in "$SELF_DIR" "$SELF_DIR/.." "$SELF_DIR/../.." "$SELF_DIR/../../.." \
                "$(pwd)" "$(pwd)/.." "$(pwd)/../.."; do
        if [ -f "$cand/server/relay.py" ]; then
            SRC_ROOT="$(cd "$cand" && pwd)"
            break
        fi
    done
fi

if [ -n "$SRC_ROOT" ]; then
    cp -f "$SRC_ROOT/server/relay.py"       "$APP_DIR/"
    cp -f "$SRC_ROOT/server/oss_client.py"  "$APP_DIR/"
    mkdir -p "$APP_DIR/web" "$APP_DIR/deploy" "$APP_DIR/tools"
    cp -f "$SRC_ROOT/server/web/"*          "$APP_DIR/web/" 2>/dev/null || true
    cp -f "$SRC_ROOT/server/deploy/"*       "$APP_DIR/deploy/" 2>/dev/null || true
    if [ -f "$SRC_ROOT/tools/setup_oss.py" ]; then
        cp -f "$SRC_ROOT/tools/setup_oss.py" "$APP_DIR/tools/"
    elif [ -f "$SRC_ROOT/server/tools/setup_oss.py" ]; then
        cp -f "$SRC_ROOT/server/tools/setup_oss.py" "$APP_DIR/tools/"
    fi
    green "  OK  程序文件已复制到 $APP_DIR"
else
    [ -f "$APP_DIR/relay.py" ] || die "找不到 relay.py。请确认在解压后的 relay-deploy 目录里运行本脚本"
    cyan "  使用 $APP_DIR 里已有的程序文件"
fi

CONFIG="$APP_DIR/config.json"
if [ -f "$CONFIG" ]; then
    echo
    cyan "  检测到已存在的配置文件：$CONFIG"
    if ask_yes "  保留它（不重新配置）" y; then
        KEEP_CONFIG=1
    else
        KEEP_CONFIG=0
        cp -f "$CONFIG" "$CONFIG.bak.$(date +%s)"
        echo "  已备份旧配置"
    fi
else
    KEEP_CONFIG=0
fi

if [ "${KEEP_CONFIG:-0}" = "0" ]; then
    echo
    cyan "  下面几个问题填完就能用。带 * 的必填。"
    echo

    echo "  ── 服务器地址 ──"
    echo "  目标电脑和你的浏览器都要能访问这个地址。"
    while true; do
        PUBLIC_BASE=$(ask "  公网 IP 或域名 *" "" "" RELAY_PUBLIC_HOST)
        PUBLIC_BASE="${PUBLIC_BASE#http://}"; PUBLIC_BASE="${PUBLIC_BASE#https://}"
        PUBLIC_BASE="${PUBLIC_BASE%/}"
        [ -n "$PUBLIC_BASE" ] && break
    done
    if ! echo "$PUBLIC_BASE" | grep -qE ':[0-9]+$'; then
        PORT=$(ask "  端口" "8080" "" RELAY_PORT)
        PUBLIC_BASE="${PUBLIC_BASE}:${PORT}"
    else
        PORT=$(echo "$PUBLIC_BASE" | sed 's/.*:\([0-9]*\)$/\1/')
    fi
    PUBLIC_BASE="http://${PUBLIC_BASE}"
    green "  中继地址: $PUBLIC_BASE"

    echo
    echo "  ── 控制台登录密码 ──"
    while true; do
        PWD1=$(ask "  密码（至少12位）*" "" secret RELAY_PASSWORD)
        [ "${#PWD1}" -ge 12 ] || { echo "  太短了，至少 12 位。"; continue; }
        PWD2=$(ask "  再输一次 *" "" secret RELAY_PASSWORD)
        [ "$PWD1" = "$PWD2" ] || { echo "  两次不一致。"; continue; }
        break
    done
    PASSWORD="$PWD1"

    echo
    echo "  ── 设备密钥 ──"
    AUTO_KEY=$("$PY_BIN" -c 'import secrets;print(secrets.token_hex(16))')
    echo "  已自动生成 32 位随机串，直接回车即可。"
    DEVICE_KEY=$(ask "  设备密钥" "$AUTO_KEY" "" RELAY_DEVICE_KEY)

    echo
    echo "  ── 阿里云 OSS ──"
    echo "  在 https://oss.console.aliyun.com/ 创建 Bucket 后填这里。"
    echo "  Bucket 权限选「私有」，存储类型选「标准存储」。"
    echo
    while true; do
        OSS_ENDPOINT=$(ask "  Endpoint *" "oss-cn-hangzhou.aliyuncs.com" "" OSS_ENDPOINT)
        OSS_ENDPOINT="${OSS_ENDPOINT#https://}"; OSS_ENDPOINT="${OSS_ENDPOINT#http://}"
        OSS_ENDPOINT="${OSS_ENDPOINT%/}"
        echo "$OSS_ENDPOINT" | grep -qE '^[a-z0-9.-]+\.[a-z]+$' && break
        echo "  格式不对，应该像 oss-cn-hangzhou.aliyuncs.com"
    done
    while true; do
        OSS_BUCKET=$(ask "  Bucket 名 *" "" "" OSS_BUCKET)
        echo "$OSS_BUCKET" | grep -qE '^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$' && break
        echo "  只能用小写字母、数字、短横线，3-63 位。"
    done
    echo
    echo "  建议用 RAM 子账号的 AccessKey（不要用主账号）。"
    while true; do
        OSS_AK=$(ask "  AccessKeyId *" "" "" OSS_AK)
        [ -n "$OSS_AK" ] && break
    done
    while true; do
        OSS_SK=$(ask "  AccessKeySecret *" "" secret OSS_SK)
        [ -n "$OSS_SK" ] && break
    done

    RELAY_CONF_PORT="$PORT" \
    RELAY_CONF_PASSWORD="$PASSWORD" \
    RELAY_CONF_DEVICE_KEY="$DEVICE_KEY" \
    RELAY_CONF_PUBLIC_BASE="$PUBLIC_BASE" \
    RELAY_CONF_OSS_ENDPOINT="$OSS_ENDPOINT" \
    RELAY_CONF_OSS_BUCKET="$OSS_BUCKET" \
    RELAY_CONF_OSS_AK="$OSS_AK" \
    RELAY_CONF_OSS_SK="$OSS_SK" \
    "$PY_BIN" - "$CONFIG" <<'PYEOF'
import json, os, sys

def env(name, default=""):
    v = os.environ.get(name)
    return default if v is None else v

def clean(name, keep_space=True):
    """去掉首尾空白与控制字符。

    终端/SSH 客户端可能把 CR、LF 带进输入，曾导致密码变成 "\\n真实密码"，
    而浏览器密码框输入不了换行，用户输对也登不上。
    keep_space=True 时保留中间的空格（密码内空格合法），只剥首尾的空白与控制符。
    """
    v = env(name)
    v = v.replace("\r", "").replace("\n", "").replace("\t", "").replace("\0", "")
    return v.strip() if keep_space else v

cfg = {
  "_说明": "本文件由 install.sh 生成。改完需 systemctl restart relay",
  "listen": "0.0.0.0",
  "port": int(env("RELAY_CONF_PORT", "8080")),
  "username": "admin",
  "password": clean("RELAY_CONF_PASSWORD"),
  "device_key": clean("RELAY_CONF_DEVICE_KEY"),
  "session_hours": 12,
  "max_fail": 5,
  "lock_seconds": 900,
  "public_base": clean("RELAY_CONF_PUBLIC_BASE"),
  "lifecycle": {
    "_说明": "桶内对象到期自动清理。基础设置，默认所有人一致，需要时改这里",
    "enabled": True,
    "days": 7,
    "prefix": "",
    "abort_multipart": True
  },
  "oss": {
    "endpoint": clean("RELAY_CONF_OSS_ENDPOINT"),
    "bucket": clean("RELAY_CONF_OSS_BUCKET"),
    "access_key_id": clean("RELAY_CONF_OSS_AK"),
    "access_key_secret": clean("RELAY_CONF_OSS_SK"),
    "scheme": "https",
    "host_override": "",
    "part_size": 8388608,
    "download_expires": 86400
  }
}
with open(sys.argv[1], "w", encoding="utf-8", newline="\n") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
    f.write("\n")
PYEOF
    chmod 600 "$CONFIG"
    green "  OK  已生成 $CONFIG"

    if ! RELAY_CONF_PATH="$CONFIG" \
         RELAY_CONF_EXPECT_PWD="$PASSWORD" \
         RELAY_CONF_EXPECT_KEY="$DEVICE_KEY" \
         "$PY_BIN" - <<'PYEOF'
import json, os, sys

cfg = json.load(open(os.environ["RELAY_CONF_PATH"], encoding="utf-8"))
bad = []
for label, expect, got in (
    ("密码", os.environ.get("RELAY_CONF_EXPECT_PWD", ""), cfg.get("password", "")),
    ("设备密钥", os.environ.get("RELAY_CONF_EXPECT_KEY", ""), cfg.get("device_key", "")),
):
    if expect == got:
        continue
    bad.append(label)
    print("  [警告] %s 保存后不一致！" % label)
    print("         输入 %d 位，存储 %d 位" % (len(expect), len(got)))
    for i in range(max(len(expect), len(got))):
        a = expect[i] if i < len(expect) else None
        b = got[i] if i < len(got) else None
        if a != b:
            print("         第 %d 位：输入 %r  存储 %r" % (i, a, b))
            break
if bad:
    print()
    print("  这通常是终端把控制字符带进了输入。请重跑安装脚本，")
    print("  或手工改 %s 后 systemctl restart relay" % os.environ["RELAY_CONF_PATH"])
    sys.exit(1)
PYEOF
    then
        red "  配置校验失败（见上），请修正后重试"
    else
        green "  OK  密码与设备密钥校验一致"
    fi
fi

_relay_cfg_read() {
    RELAY_CONF_PATH="$CONFIG" "$PY_BIN" -c '
import json, os, sys
cfg = json.load(open(os.environ["RELAY_CONF_PATH"], encoding="utf-8"))
v = cfg
for k in sys.argv[1:]:
    v = v[k]
sys.stdout.write(str(v))
' "$@"
}
PASSWORD=$(_relay_cfg_read password)
DEVICE_KEY=$(_relay_cfg_read device_key)
PUBLIC_BASE=$(_relay_cfg_read public_base)
OSS_BUCKET=$(_relay_cfg_read oss bucket)

echo
echo "[3/7] 创建系统用户"
if id relay >/dev/null 2>&1; then
    green "  OK  用户 relay 已存在"
else
    useradd --system --no-create-home --shell /usr/sbin/nologin relay 2>/dev/null \
        || useradd --system --no-create-home --shell /sbin/nologin relay
    green "  OK  已创建用户 relay"
fi
chown -R relay:relay "$APP_DIR"
chmod 600 "$CONFIG"

echo
echo "[4/7] 安装服务"
cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<SVCEOF
[Unit]
Description=File Relay (跨公网大文件传输中继)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=relay
Group=relay
WorkingDirectory=${APP_DIR}
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONIOENCODING=utf-8
ExecStart=${PY_BIN} ${APP_DIR}/relay.py --config ${APP_DIR}/config.json
Restart=always
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=${APP_DIR}

[Install]
WantedBy=multi-user.target
SVCEOF
systemctl daemon-reload
systemctl enable "$SERVICE_NAME" >/dev/null 2>&1
systemctl restart "$SERVICE_NAME"
sleep 2
green "  OK  服务已安装并启动"

echo
echo "[5/7] 现场自检"
SELFTEST="$APP_DIR/_selftest.py"
cat > "$SELFTEST" <<'PYEOF'
import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from oss_client import OSSClient, OSSError

cfg = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"), encoding="utf-8"))
o = cfg["oss"]
cli = OSSClient(o["endpoint"], o["bucket"], o["access_key_id"], o["access_key_secret"],
                scheme=o.get("scheme", "https"), host_override=o.get("host_override") or None)

fails = []
warns = []

def code_of(e):
    msg = str(e)
    return msg.split("<Code>")[1].split("</Code>")[0] if "<Code>" in msg else msg[:90]

def step(name, fn):
    try:
        fn()
        print("  OK    " + name)
    except Exception as e:
        c = code_of(e)
        print("  FAIL  " + name + " -> " + c)
        fails.append(name + ": " + c)

def opt(name, fn, why):
    """非必需项：失败只警告，不判整体失败。"""
    try:
        fn()
        print("  OK    " + name)
    except Exception as e:
        c = code_of(e)
        print("  WARN  " + name + " -> " + c)
        print("        " + why)
        warns.append(name + ": " + c)

print("  连接目标: %s://%s.%s" % (o.get("scheme", "https"), o["bucket"], o["endpoint"]))
print()

data = b"selftest-" + os.urandom(8)
step("写 OSS", lambda: cli.put("_selftest/probe.bin", data))
step("读 OSS", lambda: cli.head("_selftest/probe.bin"))
step("预签名", lambda: cli.presign_get("_selftest/probe.bin", expires=60))
opt("删 OSS", lambda: cli.delete("_selftest/probe.bin"),
    "不影响传输。只是控制台「删除」按钮用不了；"
    "取消上传的残留分片靠 7 天生命周期自动清理。\n"
    "        想启用就给 RAM 策略补 oss:DeleteObject。")

print()
if fails:
    print("  自检未通过，常见原因：")
    print("    - AccessKey 填错或被禁用")
    print("    - Bucket 名写错，或不在这个 Endpoint 对应的地域")
    print("    - RAM 子账号没有该 Bucket 的权限")
    print("    - 账号欠费（错误码 UserDisable）")
    print("    上面「连接目标」那一行，对照你的桶名和地域再看一遍。")
    sys.exit(1)

if warns:
    print("  自检通过（核心功能可用，%d 项附加功能不可用，见上面 WARN）" % len(warns))
else:
    print("  自检全部通过")
PYEOF
chown relay:relay "$SELFTEST"
if sudo -u relay "$PY_BIN" "$SELFTEST" 2>&1; then
    rm -f "$SELFTEST"
    green "  OK  OSS 连通性通过"
    OSS_OK=1
else
    rm -f "$SELFTEST"
    red "  自检失败"
    OSS_OK=0
fi

if [ -f "$APP_DIR/tools/setup_oss.py" ] && [ "$OSS_OK" = "1" ]; then
    echo
    echo "  配置桶生命周期（到期自动清理）"
    if sudo -u relay "$PY_BIN" "$APP_DIR/tools/setup_oss.py" \
            --config "$CONFIG" --apply 2>&1 | sed 's/^/  /'; then
        green "  OK  生命周期规则已应用（可在 config.json 的 lifecycle 段调整）"
    else
        red "  生命周期规则应用失败（不影响传输，可稍后手动执行："
        red "    sudo -u relay $PY_BIN $APP_DIR/tools/setup_oss.py --apply）"
    fi
fi

echo
echo "[6/7] 服务状态"
if systemctl is-active --quiet "$SERVICE_NAME"; then
    green "  OK  服务运行中"
    if command -v curl >/dev/null 2>&1; then
        code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:${PORT}/" 2>/dev/null || echo "000")
        if [ "$code" = "200" ] || [ "$code" = "302" ]; then
            green "  OK  本地网页响应 HTTP $code"
        else
            red "  本地网页响应异常: HTTP $code"
        fi
    fi
else
    red "  服务未启动，看日志：journalctl -u $SERVICE_NAME -n 30 --no-pager"
fi

echo
echo "[7/7] 防火墙"
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
    ufw allow "${PORT}/tcp" >/dev/null 2>&1 && green "  OK  已放行 $PORT 端口 (ufw)"
elif command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
    firewall-cmd --permanent --add-port="${PORT}/tcp" >/dev/null 2>&1
    firewall-cmd --reload >/dev/null 2>&1
    green "  OK  已放行 $PORT 端口 (firewalld)"
else
    cyan "  未检测到启用的本机防火墙，跳过"
fi

echo
bold "============================================================"
if [ "${OSS_OK:-0}" = "1" ]; then
    green "  部署完成"
else
    red   "  部署完成，但 OSS 自检没通过（见上面）"
fi
bold "============================================================"
echo
echo "  控制台地址 : $PUBLIC_BASE"
echo "  登录账号   : admin"
echo "  登录密码   : $PASSWORD"
echo "  设备密钥   : $DEVICE_KEY"
echo "  OSS Bucket : $OSS_BUCKET"
echo
echo "  ── 下一步 ──"
echo
echo "  1) 浏览器打开 $PUBLIC_BASE 确认能登录"
echo
echo "  2) 生成给目标电脑的传输助手："
echo "     在你自己的 Windows 电脑上，用「配置生成器」填写："
echo "         服务器地址=$PUBLIC_BASE"
echo "         设备密钥=$DEVICE_KEY"
echo
echo "  ── 常用命令 ──"
echo
echo "    查看状态 : systemctl status $SERVICE_NAME"
echo "    重启     : systemctl restart $SERVICE_NAME"
echo "    看日志   : journalctl -u $SERVICE_NAME -n 50 --no-pager"
echo "    改配置   : vi $CONFIG && systemctl restart $SERVICE_NAME"
echo
echo "  ── 云服务器安全组 ──"
echo
echo "  记得在云厂商控制台的安全组里放行 TCP $PORT 端口，"
echo "  否则外部访问不进来。"
echo
