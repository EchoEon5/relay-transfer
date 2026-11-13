
"""把 client/agent.ps1 打进 1 个 bat（单文件交付）。

用法：
  python build.py --relay http://203.0.113.10:8080 --key 你的设备密钥
  python build.py --relay ... --autostart 0 --out 传输助手.bat   # key 自动读 server/config.json
"""

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_TEMPLATE = os.path.join(HERE, "client", "bat_template.txt")
DEFAULT_PAYLOAD = os.path.join(HERE, "client", "agent.ps1")
DEFAULT_OUT = os.path.join(HERE, "dist", "传输助手.bat")

def read_device_key():
    """从 server/config.json 读 device_key，省得手抄出错"""
    for name in ("config.json", "config.example.json"):
        path = os.path.join(HERE, "server", name)
        if not os.path.isfile(path):
            continue
        try:
            import json
            with open(path, "r", encoding="utf-8-sig") as f:
                key = (json.load(f).get("device_key") or "").strip()
            if key and not key.startswith("换成"):
                return key
        except Exception:
            pass
    return ""

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--relay", required=True, help="中继地址，如 http://203.0.113.10:8080")
    ap.add_argument("--key", default="", help="设备密钥；省略则自动读 server/config.json 的 device_key")
    ap.add_argument("--autostart", default="1", choices=["0", "1"], help="1=登记当前用户开机自启")
    ap.add_argument("--transport", default="oss", choices=["oss", "auto", "ipv6"],
                    help="oss=仅 OSS（默认）；auto=IPv6 优先回落 OSS；ipv6=仅 IPv6")
    ap.add_argument("--template", default=DEFAULT_TEMPLATE)
    ap.add_argument("--payload", default=DEFAULT_PAYLOAD)
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()

    if not args.key:
        args.key = read_device_key()
        if not args.key:
            sys.exit(
                "未指定 --key，且读不到 server/config.json 的 device_key。\n"
                "  先做这两步之一：\n"
                "    1) 运行 python setup.py 生成配置（推荐）\n"
                "    2) 手动把 server/config.json 的 device_key 改成真实值\n"
                "  或者在命令行显式指定：--key 你的设备密钥"
            )

    with open(args.template, "r", encoding="utf-8-sig") as f:
        tpl = f.read()
    with open(args.payload, "r", encoding="utf-8-sig") as f:
        payload = f.read()

    if "#PSSTART" not in tpl:
        sys.exit("模板缺少 #PSSTART 标记")
    if "RELAY_URL=" not in tpl or "DEVICE_KEY=" not in tpl:
        sys.exit("模板缺少 RELAY_URL / DEVICE_KEY 占位符")

    head = tpl.replace("__RELAY_URL__", args.relay.rstrip("/"))
    head = head.replace("__DEVICE_KEY__", args.key)
    head = head.replace("__AUTOSTART__", args.autostart)
    head = head.replace("__TRANSPORT__", args.transport)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    data = head.replace("\r\n", "\n").replace("\n", "\r\n") + \
        payload.replace("\r\n", "\n").replace("\n", "\r\n")
    with open(args.out, "w", encoding="utf-8", newline="") as f:
        f.write(data)

    size = os.path.getsize(args.out)
    print("已生成: %s  (%.1f KB)" % (args.out, size / 1024.0))
    print("  RELAY_URL  = %s" % args.relay)
    print("  DEVICE_KEY = %s" % args.key)
    print("  AUTOSTART  = %s" % args.autostart)
    print("  TRANSPORT  = %s" % args.transport)

if __name__ == "__main__":
    main()
