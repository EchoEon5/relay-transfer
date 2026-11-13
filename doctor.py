
"""部署自检：检查配置、OSS 连通性、权限、目录。

用法：
  python doctor.py                      # 检查 server/config.json
  python doctor.py --config 别的路径

退出码 0 = 全部通过，1 = 有失败项。
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "server"))

OK, BAD, WARN = "  [OK]  ", "  [失败]", "  [注意]"
results = []

def check(name, fn):
    try:
        msg = fn()
        if msg is True or msg is None:
            results.append((True, name, ""))
            print("%s %s" % (OK, name))
        else:
            results.append((True, name, str(msg)))
            print("%s %s —— %s" % (OK, name, msg))
    except Exception as e:
        results.append((False, name, str(e)))
        print("%s %s —— %s" % (BAD, name, e))

def warn(name, msg):
    results.append((True, name, msg))
    print("%s %s —— %s" % (WARN, name, msg))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(HERE, "server", "config.json"))
    args = ap.parse_args()

    print()
    print("=" * 62)
    print("  部署自检")
    print("=" * 62)
    print()

    cfg = {}

    def load():
        if not os.path.isfile(args.config):
            raise RuntimeError("找不到 %s（先运行 python setup.py）" % args.config)
        with open(args.config, "r", encoding="utf-8-sig") as f:
            cfg.update(json.load(f))
        return args.config

    check("配置文件存在", load)

    if not cfg:
        sys.exit(1)

    print()
    print("  -- 基本配置 --")

    def chk_pwd():
        p = cfg.get("password") or ""
        if not p:
            raise RuntimeError("password 为空")
        if p.startswith("换成") or p in ("admin", "123456", "password"):
            raise RuntimeError("还是占位符/弱密码，必须改")
        if len(p) < 12:
            raise RuntimeError("只有 %d 位，建议至少 12 位" % len(p))
        return "%d 位" % len(p)

    check("登录密码", chk_pwd)

    def chk_key():
        k = cfg.get("device_key") or ""
        if not k:
            raise RuntimeError("device_key 为空")
        if k.startswith("换成"):
            raise RuntimeError("还是占位符，必须改")
        if len(k) < 16:
            raise RuntimeError("只有 %d 位，建议 32 位随机串" % len(k))
        return "%d 位" % len(k)

    check("设备密钥", chk_key)

    def chk_base():
        b = cfg.get("public_base") or ""
        if not b:
            raise RuntimeError("public_base 为空")
        if "127.0.0.1" in b or "localhost" in b:
            raise RuntimeError("指向本机，目标电脑连不上；改成 ECS 公网 IP 或域名")
        if not (b.startswith("http://") or b.startswith("https://")):
            raise RuntimeError("必须以 http:// 或 https:// 开头")
        return b

    check("中继对外地址", chk_base)

    print()
    print("  -- OSS --")

    o = cfg.get("oss") or {}

    def chk_oss_fields():
        miss = [k for k in ("endpoint", "bucket", "access_key_id", "access_key_secret")
                if not (o.get(k) or "").strip()]
        if miss:
            raise RuntimeError("缺: %s" % ", ".join(miss))
        b = (o.get("bucket") or "").strip()
        if b.startswith("你的"):
            raise RuntimeError("bucket 还是占位符")
        if not b.isascii():
            raise RuntimeError("bucket 名含非 ASCII 字符（中文？），只能用小写字母/数字/短横线")
        if not (b[0].isalnum() and b[-1].isalnum()):
            raise RuntimeError("bucket 名必须以字母或数字开头和结尾")
        if not (3 <= len(b) <= 63):
            raise RuntimeError("bucket 名长度应为 3-63，当前 %d" % len(b))
        return "%s / %s" % (b, o["endpoint"])

    oss_ok = True
    try:
        msg = chk_oss_fields()
        results.append((True, "OSS 四项已填", msg))
        print("%s %s —— %s" % (OK, "OSS 四项已填", msg))
    except Exception as e:
        oss_ok = False
        results.append((False, "OSS 四项已填", str(e)))
        print("%s %s —— %s" % (BAD, "OSS 四项已填", e))
        print("%s 跳过后续 OSS 检查（先把上面填对）" % WARN)

    client = None

    def make_client():
        nonlocal client
        from oss_client import OSSClient
        client = OSSClient(
            o["endpoint"], o["bucket"], o["access_key_id"], o["access_key_secret"],
            o.get("sts_token"), scheme=o.get("scheme") or "https",
            host_override=o.get("host_override") or None,
        )
        return "endpoint 可解析"

    if oss_ok:
        check("创建 OSS 客户端", make_client)

    def friendly(err):
        """把 OSS 的英文错误翻译成能照着做的中文提示"""
        s = str(err)
        if "must be addressed using the specified endpoint" in s:
            import re as _re
            m = _re.search(r"<Endpoint>([^<]+)</Endpoint>", s)
            want = m.group(1) if m else "（见下方原文）"
            return ("endpoint 写错了：这个 bucket 实际在 %s。\n"
                    "           把 config.json 里的 oss.endpoint 改成 %s 再跑一次。" % (want, want))
        if "InvalidAccessKeyId" in s or "AccessKeyId" in s and "not found" in s:
            return "AccessKeyId 不存在，检查是否抄错、或这个 AK 已被禁用。"
        if "SignatureDoesNotMatch" in s:
            return "AccessKeySecret 不对（签名对不上），检查是否抄错或多了空格。"
        if "AccessDenied" in s:
            return "AK 没有这个桶的权限：给 RAM 子账号加上 oss:PutObject / oss:GetObject 等策略。"
        if "NoSuchBucket" in s:
            return "这个 bucket 不存在：检查桶名，以及 bucket 和 endpoint 是否属于同一地域。"
        if "SecondLevelDomainForbidden" in s:
            return "endpoint 用了二级域名，请改用 oss-cn-xxx.aliyuncs.com 这种区域 endpoint。"
        if "连接失败" in s or "timed out" in s.lower():
            return "连不上 OSS：检查这台机器的出网、DNS，以及 443 端口是否被防火墙拦。"
        return s

    if client is not None:
        def chk_write():
            import uuid as _uuid
            key = "_doctor/%s.txt" % _uuid.uuid4().hex
            try:
                client.put(key, b"doctor", content_type="text/plain")
                client.delete(key)
            except Exception as e:
                raise RuntimeError(friendly(e))
            return "上传+删除成功（AK 有写权限）"

        check("写入权限", chk_write)

        def chk_sign():
            url = client.presign_get("_doctor/probe.bin", expires=600, filename="测试.bin")
            if "Signature=" not in url:
                raise RuntimeError("预签名 URL 里没有 Signature")
            return "签名生成正常"

        check("预签名下载链接", chk_sign)

        def chk_multipart():
            key = "_doctor/multipart-probe.bin"
            try:
                up = client.initiate_multipart(key)
                if not up:
                    raise RuntimeError("initiate 没返回 uploadId")
                try:
                    client.abort_multipart(key, up)
                except Exception:
                    pass
            except Exception as e:
                raise RuntimeError(friendly(e))
            return "分片上传接口可用（已清理）"

        check("分片上传权限", chk_multipart)

    print()
    print("  -- 运行环境 --")

    def chk_py():
        v = sys.version_info
        if v < (3, 8):
            raise RuntimeError("Python %d.%d 太旧，需要 3.8+" % (v[0], v[1]))
        return "Python %d.%d.%d" % (v[0], v[1], v[2])

    check("Python 版本", chk_py)

    def chk_web():
        d = os.path.join(HERE, "server", "web")
        need = ["index.html", "login.html", "app.js", "style.css"]
        miss = [n for n in need if not os.path.isfile(os.path.join(d, n))]
        if miss:
            raise RuntimeError("缺文件: %s" % ", ".join(miss))
        return "控制台页面齐全"

    check("Web 资源", chk_web)

    def chk_state():
        base = cfg.get("state_dir") or "state"
        if not os.path.isabs(base):
            base = os.path.join(HERE, "server", base)
        try:
            os.makedirs(base, exist_ok=True)
            probe = os.path.join(base, ".write-probe")
            with open(probe, "w") as f:
                f.write("x")
            os.remove(probe)
        except Exception as e:
            raise RuntimeError("状态目录不可写: %s" % e)
        return base

    check("状态目录可写", chk_state)

    def chk_port():
        import socket
        port = int(cfg.get("port") or 8080)
        s = socket.socket()
        try:
            s.bind(("0.0.0.0", port))
            return "端口 %d 空闲" % port
        except OSError:
            raise RuntimeError("端口 %d 已被占用" % port)
        finally:
            s.close()

    check("监听端口", chk_port)

    print()
    print("  -- 提醒 --")
    if os.environ.get("RELAY_DEV_OPEN") == "1":
        warn("开发模式", "RELAY_DEV_OPEN=1 已开启，本机免登录 —— 生产环境务必关掉")
    if (cfg.get("public_base") or "").startswith("http://") and ":8080" in (cfg.get("public_base") or ""):
        warn("明文 HTTP", "公司网络常只放行 443；连不上就改用域名 + HTTPS")

    bad = [r for r in results if not r[0]]
    print()
    print("=" * 62)
    if bad:
        print("  有 %d 项没通过：" % len(bad))
        for _, name, msg in bad:
            print("    - %s: %s" % (name, msg))
        print()
        sys.exit(1)
    print("  全部通过。可以启动中继了：")
    try:
        shown = os.path.relpath(args.config)
    except ValueError:
        shown = args.config
    print("    python server/relay.py --config %s" % shown)
    print()
    sys.exit(0)

if __name__ == "__main__":
    main()
