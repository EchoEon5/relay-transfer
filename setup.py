
"""交互式配置向导：生成 server/config.json。

用法：
  python setup.py            # 在项目根目录运行
  python setup.py --force    # 覆盖已有 config.json

只依赖标准库。生成前会做基本校验（必填项、密钥长度、地址格式）。
"""

import argparse
import json
import os
import re
import secrets
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
EXAMPLE = os.path.join(HERE, "server", "config.example.json")
TARGET = os.path.join(HERE, "server", "config.json")

def ask(prompt, default="", required=True, secret=False):
    """读一行输入；空输入用默认值。"""
    tip = prompt
    if default:
        tip += " [%s]" % ("******" if secret else default)
    tip += ": "
    while True:
        try:
            val = input(tip).strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已取消。")
            sys.exit(1)
        if not val:
            val = default
        if val or not required:
            return val
        print("  这一项必填，不能为空。")

def ask_choice(prompt, options, default):
    """从若干选项里选一个。"""
    print(prompt)
    for i, (label, _) in enumerate(options, 1):
        mark = "*" if options[i - 1][1] == default else " "
        print("  %s %d) %s" % (mark, i, label))
    while True:
        raw = ask("选择", default="", required=False)
        if not raw:
            return default
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1][1]
        print("  请输入 1-%d 的数字。" % len(options))

def valid_endpoint(s):
    return bool(re.match(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$", s))

def valid_bucket(s):
    return bool(re.match(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$", s))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="覆盖已存在的 config.json")
    args = ap.parse_args()

    print("=" * 62)
    print("  跨公网信息传输 —— 配置向导")
    print("=" * 62)
    print()

    if os.path.exists(TARGET) and not args.force:
        print("已存在 %s" % TARGET)
        print("如需重新生成，请加 --force")
        sys.exit(1)

    print("【1/5】中继地址")
    print("  目标电脑和你的浏览器都要能访问这个地址。")
    print("  例：http://1.2.3.4:8080  或  https://relay.你的域名.com")
    print()
    while True:
        public_base = ask("中继地址", required=True).rstrip("/")
        if not re.match(r"^https?://[^\s/]+$", public_base):
            print("  格式不对，必须以 http:// 或 https:// 开头，且不能带路径。")
            continue
        if public_base.endswith(":80") or public_base.endswith(":443"):
            print("  提示：80/443 是默认端口，可以不写。")
        break

    print()
    print("【2/5】控制台登录密码")
    print("  这是你在浏览器里登录用的密码。")
    print()
    while True:
        pwd = ask("密码（至少 12 位）", required=True, secret=True)
        if len(pwd) < 12:
            print("  太短了，至少 12 位。")
            continue
        if pwd in ("换成你自己的强密码", "admin", "123456"):
            print("  别用这种。")
            continue
        again = ask("再输一次", required=True, secret=True)
        if again != pwd:
            print("  两次不一致。")
            continue
        break

    print()
    print("【3/5】设备密钥")
    print("  目标端的 bat 里会带这个值，用来证明“我是被授权的那台机器”。")
    print("  泄露的后果只是别人能冒充一台设备上线，读不到你的文件。")
    print()
    auto_key = secrets.token_hex(16)
    print("  已自动生成 32 位随机串。")
    key = ask("设备密钥", default=auto_key, required=True)

    print()
    print("【4/5】阿里云 OSS")
    print("  在 https://oss.console.aliyun.com/ 创建 Bucket 后填这里。")
    print()
    while True:
        endpoint = ask("Endpoint（例 oss-cn-hangzhou.aliyuncs.com）", required=True)
        endpoint = endpoint.replace("https://", "").replace("http://", "").strip("/")
        if not valid_endpoint(endpoint):
            print("  格式不对，应该是 xxx.aliyuncs.com 这种形式。")
            continue
        break
    while True:
        bucket = ask("Bucket 名", required=True)
        if not valid_bucket(bucket):
            print("  只能用小写字母、数字、短横线，3-63 位。")
            continue
        break
    print()
    print("  RAM 子账号的 AccessKey（不要用主账号的）。")
    while True:
        ak = ask("AccessKeyId（LTAI 开头）", required=True)
        if not ak.startswith("LTAI"):
            print("  阿里云的 AccessKeyId 通常以 LTAI 开头，确认一下？")
            if ask("仍然使用", default="n", required=False).lower() not in ("y", "yes"):
                continue
        break
    while True:
        sk = ask("AccessKeySecret", required=True, secret=True)
        if len(sk) < 20:
            print("  看起来太短，正常的 Secret 是 30 位左右。")
            if ask("仍然使用", default="n", required=False).lower() not in ("y", "yes"):
                continue
        break

    print()
    print("【5/5】传输参数（一般直接回车）")
    print()
    part_mb = ask("分片大小 MB（大文件建议 64）", default="64", required=False)
    try:
        part_size = int(part_mb) * 1024 * 1024
        if part_size < 100 * 1024 or part_size > 5 * 1024 * 1024 * 1024:
            raise ValueError
    except ValueError:
        print("  数值无效，用默认 64 MB。")
        part_size = 64 * 1024 * 1024
    hours = ask("下载链接有效期（小时）", default="24", required=False)
    try:
        download_expires = int(hours) * 3600
    except ValueError:
        download_expires = 86400

    with open(EXAMPLE, "r", encoding="utf-8-sig") as f:
        cfg = json.load(f)
    cfg["password"] = pwd
    cfg["device_key"] = key
    cfg["public_base"] = public_base
    cfg["oss"]["endpoint"] = endpoint
    cfg["oss"]["bucket"] = bucket
    cfg["oss"]["access_key_id"] = ak
    cfg["oss"]["access_key_secret"] = sk
    cfg["oss"]["part_size"] = part_size
    cfg["oss"]["download_expires"] = download_expires

    os.makedirs(os.path.dirname(TARGET), exist_ok=True)
    with open(TARGET, "w", encoding="utf-8", newline="\n") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")
    try:
        os.chmod(TARGET, 0o600)
    except Exception:
        pass

    print()
    print("=" * 62)
    print("  已写入 %s" % TARGET)
    print("=" * 62)
    print()
    print("  中继地址  : %s" % public_base)
    print("  登录密码  : （已保存）")
    print("  设备密钥  : %s" % key)
    print("  OSS       : %s / %s" % (bucket, endpoint))
    print()
    print("下一步：")
    print("  1) 启动中继：  python server/relay.py --config server/config.json")
    print("  2) 构建 bat  ：  python build.py --relay %s" % public_base)
    print("  3) 浏览器打开：  %s" % public_base)
    print()

if __name__ == "__main__":
    main()
