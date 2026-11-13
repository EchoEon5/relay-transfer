
"""打包「服务器部署包」和「客户端配置生成器」。

用法：
  python pack_all.py

产出：
  dist/relay-deploy.tar.gz      服务器部署包（上传到 ECS 用）
  dist/配置生成器.zip            客户端配置生成器（发给其他人用）
  dist/传输助手.bat              你自己用的（已内置当前配置）
"""

import hashlib
import io
import json
import os
import sys
import tarfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "server")
CLIENT = os.path.join(HERE, "client")
DIST = os.path.join(HERE, "dist")

CLIENT_DOCS = [
    ("快速开始.md",     "01-快速开始.txt"),
    ("部署文档.md",     "02-服务器部署.txt"),
    ("使用教程.md",     "03-使用教程.txt"),
    ("常见问题.md",     "04-常见问题.txt"),
]

SERVER_FILES = [
    "server/relay.py",
    "server/oss_client.py",
    "server/config.example.json",
    "server/web/index.html",
    "server/web/login.html",
    "server/web/manifest.json",
    "server/web/favicon.ico",
    "server/web/favicon-256.png",
    "server/web/icon-192.png",
    "server/web/apple-touch-icon.png",
    "server/deploy/install.sh",
    "server/deploy/relay.service",
    "server/deploy/nginx-relay.conf",
    "tools/setup_oss.py",
    "setup.py",
    "build.py",
    "doctor.py",
    "pack_config.py",
]

SERVER_README = """信息传输中继 —— 服务器部署包
============================================================

这个包用于在一台全新的 Linux 服务器上部署中继服务。

【先看这个】
  本包只装服务器，不含使用文档。文档在 Windows 上看的，
  放在「配置生成器.zip」里，共 4 份：
      01-快速开始.txt     从零到能传，约 20 分钟，含阿里云逐步操作
      02-服务器部署.txt   本包的详细部署说明（本文是精简版）
      03-使用教程.txt     日常使用、费用、安全
      04-常见问题.txt     出错怎么办
  手边只有这个 tar.gz 的话，照下面做也能装完。

一、准备工作
------------

1. 一台 Linux 服务器（Ubuntu 20.04+ / Debian 11+ / CentOS 8+ / Alibaba Cloud Linux）
   最低配置：1 核 1 GB 内存即可（中继不转发文件内容，只传指令）

2. 一个阿里云 OSS Bucket
   控制台：https://oss.console.aliyun.com/
   点「创建 Bucket」，填：
       Bucket 名称    自己起，全阿里云唯一，只能小写字母数字和短横线
       地域           选离你服务器近的，例如华东1（杭州）
       存储类型       标准存储
       读写权限       私有        ← 必须，不能选公共读
       冗余类型       本地冗余 LRS
   创建完记下两件事：Bucket 名、地域对应的 Endpoint
   例：华东1（杭州）的 Endpoint 是 oss-cn-hangzhou.aliyuncs.com

3. 一个 RAM 子账号的 AccessKey（不要用主账号）
   控制台：https://ram.console.aliyun.com/users
   a. 点「创建用户」，只勾「OpenAPI 调用访问」，不要勾控制台登录
   b. 创建完立刻会显示 AccessKey ID 和 AccessKey Secret
      Secret 只显示这一次，关掉就再也看不到，务必当场复制保存
   c. 给这个用户授权：点「添加权限」，选「自定义策略」，
      策略内容用本文件最后一节的 JSON（先把 BUCKET名 换成你的）
   d. 授权后生效要等约 1 分钟

4. 云服务器安全组放行你选的端口（默认 8080）
   控制台 → 实例 → 安全组 → 配置规则 → 入方向 → 手动添加：
       端口范围   8080/8080
       授权对象   0.0.0.0/0   （或只填你自己的 IP，更安全）
   不放行的话，服务器上能跑，但浏览器打不开。

二、部署（3 条命令）
--------------------

把 relay-deploy.tar.gz 上传到服务器（用 scp / Xftp / 宝塔都行），然后：

    tar xzf relay-deploy.tar.gz
    cd relay-deploy
    sudo bash server/deploy/install.sh

脚本会依次问你 7 个问题，带 * 的必填：

    公网 IP 或域名 *     填你服务器的公网 IP，例：121.0.0.1
                         有域名且已解析就填域名，例：relay.example.com
    端口                 直接回车＝8080
    密码（至少12位）*     控制台的登录密码，自己定，记住它
    再输一次 *           同上，防打错
    设备密钥             直接回车＝自动生成32位随机串（推荐）
    Endpoint *           OSS 地域地址，直接回车＝华东1杭州
                         oss-cn-hangzhou.aliyuncs.com
    Bucket 名 *          第一步创建的 Bucket 名字
    AccessKeyId *        RAM 子账号的 AccessKey ID
    AccessKeySecret *    RAM 子账号的 AccessKey Secret（输入时屏幕不显示）

脚本然后自动完成这些事，不用你手动做：
    · 检查 Python 版本（要求 3.8+）
    · 复制程序到 /opt/server
    · 写配置文件并设为 600 权限（里面有 AK，不能让别人读到）
    · 创建 relay 系统用户（不给登录 shell）
    · 装成 systemd 服务并设开机自启
    · 配 OSS 生命周期规则（7 天自动清理）和 CORS
    · 真连一次 OSS 做自检，配置错了当场报出来
    · 配置防火墙放行端口

全程约 1 分钟。如果某一步报错，脚本会停下并打印原因。

三、验证
--------

安装脚本最后会打印：

    控制台地址 : http://你的IP:8080
    登录密码   : 你填的密码
    设备密钥   : 你填的或自动生成的32位串

三样都记下来，后面生成传输助手要用到「地址」和「设备密钥」。

用浏览器打开控制台地址，输入密码能登录就成功了。

打不开的排查顺序：
    1. 服务器上跑 curl -s http://127.0.0.1:8080/ 有返回吗？
       有 → 服务正常，问题在安全组／防火墙，看第三步第 4 条
       没有 → 服务没起来，跑 systemctl status relay 看原因
    2. 安全组入方向放行 8080 了吗
    3. 云厂商的服务器还要确认「防火墙」没拦（阿里云只有安全组）

如果自检报 OSS 错误，通常是这三种：
    AccessDenied     AK 权限不够，检查 RAM 策略里的 BUCKET名 换对了没
    NoSuchBucket     Bucket 名写错，或 Endpoint 和桶不在同一地域
    InvalidAccessKeyId  AK 复制错了，注意别带空格

四、给目标电脑生成传输助手
--------------------------

在 Windows 电脑上用「配置生成器」：

    1. 解压 配置生成器.zip
    2. 记事本打开 配置.txt，填：
           服务器地址=你的IP
           设备密钥=上面打印的设备密钥
    3. 双击 生成传输助手.bat
    4. 把生成的 传输助手.bat 发到目标电脑

五、常用命令
------------

    systemctl status relay              查看状态
    systemctl restart relay             重启
    journalctl -u relay -n 50 --no-pager   看日志
    vi /opt/server/config.json          改配置（改完要重启）

六、RAM 权限策略（推荐）
-----------------------

在 https://ram.console.aliyun.com/policies 创建自定义策略，
脚本编辑模式粘贴下面内容（把 3 处 BUCKET名 换成你的）：

{
  "Version": "1",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "oss:PutObject",
        "oss:GetObject",
        "oss:DeleteObject",
        "oss:AbortMultipartUpload",
        "oss:ListParts",
        "oss:ListObjects"
      ],
      "Resource": [
        "acs:oss:*:*:BUCKET名",
        "acs:oss:*:*:BUCKET名/*"
      ]
    },
    {
      "Effect": "Allow",
      "Action": [
        "oss:PutBucketLifecycle",
        "oss:GetBucketLifecycle",
        "oss:DeleteBucketLifecycle",
        "oss:PutBucketCORS",
        "oss:GetBucketCORS"
      ],
      "Resource": [
        "acs:oss:*:*:BUCKET名"
      ]
    }
  ]
}

注意第二个 Statement：这 5 条是「桶级」权限，只作用于桶本身
（Resource 结尾不带 /*），install.sh 用它自动配置生命周期和跨域规则。
少了它们，自检的最后一步会报 PutBucketLifecycle 拒绝，
安装脚本会提示「生命周期规则应用失败」——不影响传文件，
但对象就不会 7 天自动清理了，得你自己去控制台点。

七、费用说明
------------

    上传到 OSS   免费
    从 OSS 下载  约 0.5 元/GB
    OSS 存储     约 0.12 元/GB/月
    ECS          你自己的机器

OSS 的生命周期规则（自动清理）和跨域规则（CORS）由 install.sh 自动配好，
不用你手动去控制台设置：

    保留天数      7 天（到期自动删除对象，未完成的分片同样 7 天后清理）
    跨域 CORS     自动按你的控制台地址写入

想改保留天数，编辑 /opt/server/config.json 的 lifecycle 段（days 字段），然后：

    sudo -u relay python3 /opt/server/tools/setup_oss.py --apply

注意是「删除」不是「归档」，到期文件不可恢复。
想长期保留就把 enabled 设成 false，代价是存储费会一直涨。

另外在费用中心设置「余额预警」（比如 10 元），
欠费会导致所有 OSS 操作返回 UserDisable。
"""

def read(path):
    with open(path, "rb") as f:
        return f.read()

def build_server_package():
    out = os.path.join(DIST, "relay-deploy.tar.gz")
    missing = []
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for rel in SERVER_FILES:
            full = os.path.join(HERE, rel)
            if not os.path.isfile(full):
                missing.append(rel)
                continue

            arc = os.path.join("relay-deploy", rel).replace("\\", "/")
            info = t.gettarinfo(full, arcname=arc)
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            if rel.endswith(".sh"):
                info.mode = 0o755
            elif rel.endswith(".py"):
                info.mode = 0o755
            else:
                info.mode = 0o644
            with open(full, "rb") as f:
                t.addfile(info, f)

        data = SERVER_README.encode("utf-8")
        info = tarfile.TarInfo("relay-deploy/README.txt")
        info.size = len(data)
        info.mode = 0o644
        info.uid = info.gid = 0
        info.uname = info.gname = "root"
        t.addfile(info, io.BytesIO(data))

    with open(out, "wb") as f:
        f.write(buf.getvalue())
    if missing:
        print("  [警告] 缺少文件: %s" % ", ".join(missing))
    return out

def _inline(s):
    """清掉行内 Markdown 标记：**粗体** `代码` [文字](链接) __下划线__"""
    import re
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)
    s = s.replace("**", "").replace("__", "").replace("`", "")
    s = s.replace("~~", "")
    return s.strip()

def md_to_txt(md, width=76):
    """把 Markdown 粗略转成「记事本直接看」的纯文本。

    目标不是完美排版，而是：去掉 # * ` | 这类符号后仍然好读。
    代码块原样保留（缩进 4 空格），表格转成对齐的竖线文本。
    """
    out = []
    in_code = False
    in_table = False

    for raw in md.splitlines():
        line = raw.rstrip()

        if line.strip().startswith("```"):
            if not in_code:
                in_code = True
                out.append("")
            else:
                in_code = False
                out.append("")
            continue
        if in_code:
            out.append("    " + line)
            continue

        s = line.strip()

        if s.startswith("|") and set(s) <= set("|-: "):
            in_table = True
            continue

        if s.startswith("#"):
            lvl = len(s) - len(s.lstrip("#"))
            txt = _inline(s[lvl:].strip())
            if lvl == 1:
                out.append("")
                out.append("=" * width)
                out.append(txt)
                out.append("=" * width)
            elif lvl == 2:
                out.append("")
                out.append(txt)
                out.append("-" * min(width, max(8, len(txt) * 2)))
            else:
                out.append("")
                out.append("· " + txt)
            in_table = False
            continue

        if s.startswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            cells = [_inline(c) for c in cells]
            out.append("  " + " | ".join(cells))
            in_table = True
            continue
        if in_table and not s:
            in_table = False
            out.append("")
            continue

        if s.startswith(("- ", "* ")):
            out.append("  · " + _inline(s[2:]))
            continue
        if len(s) > 2 and s[0].isdigit() and s[1:3] in (". ", ") "):
            out.append("  " + _inline(s))
            continue

        if s.startswith(">"):
            out.append("  " + _inline(s.lstrip("> ")))
            continue

        t = _inline(s)
        if t.startswith("---") and set(t) <= set("-"):
            out.append("-" * width)
            continue
        out.append(t)

    res = []
    for ln in out:
        if ln.strip() == "" and res and res[-1].strip() == "":
            continue
        res.append(ln)
    while res and res[0].strip() == "":
        res.pop(0)
    while res and res[-1].strip() == "":
        res.pop()
    return "\r\n".join(res) + "\r\n"

def build_client_package():
    """生成 EXE 生成器 + 网页版配置生成器 + 使用文档，一起打包成 zip"""
    import pack_config
    pack_config.main()

    exe_ok = False
    try:
        import build_exe
        build_exe.main()
        exe_ok = True
    except SystemExit:
        exe_ok = os.path.isfile(os.path.join(DIST, "信息传输助手生成器.exe"))
    except Exception as e:
        print("  [警告] EXE 编译失败: %s" % e)

    src = os.path.join(DIST, "配置生成器")
    exe_path = os.path.join(DIST, "信息传输助手生成器.exe")
    if exe_ok and os.path.isfile(exe_path):

        tmp_tpl = os.path.join(src, "template.dat")
        if os.path.exists(tmp_tpl):
            os.remove(tmp_tpl)

    out = os.path.join(DIST, "配置生成器.zip")
    if os.path.exists(out):
        os.remove(out)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:

        if exe_ok and os.path.isfile(exe_path):
            z.write(exe_path, "信息传输助手生成器.exe")
        for name in sorted(os.listdir(src)):
            full = os.path.join(src, name)
            if os.path.isfile(full):
                z.write(full, name)

        for md_name, out_name in CLIENT_DOCS:
            md_path = os.path.join(HERE, md_name)
            if not os.path.isfile(md_path):
                print("    [警告] 缺少文档 %s" % md_name)
                continue
            with open(md_path, encoding="utf-8") as f:
                txt = md_to_txt(f.read())

            z.writestr(out_name, "\ufeff" + txt)
    return out

def verify(path, label):
    """列出包内容并做基本校验"""
    print("\n  %s: %.1f KB" % (label, os.path.getsize(path) / 1024.0))
    if path.endswith(".tar.gz"):
        with tarfile.open(path) as t:
            for m in t.getmembers():
                if m.isfile():
                    print("    %-46s %8d" % (m.name, m.size))
    else:
        with zipfile.ZipFile(path) as z:
            for n in z.namelist():
                print("    %-46s %8d" % (n, z.getinfo(n).file_size))

def check_consistency():
    """确认包内文件和本地一致（防止又打包了旧版本）"""
    print("\n  一致性检查:")
    tar_path = os.path.join(DIST, "relay-deploy.tar.gz")
    bad = []
    with tarfile.open(tar_path) as t:
        for rel in SERVER_FILES:
            full = os.path.join(HERE, rel)
            if not os.path.isfile(full):
                continue
            arc = "relay-deploy/" + rel.replace("\\", "/")
            try:
                packed = t.extractfile(arc).read()
            except KeyError:
                bad.append(rel + " (包内缺失)")
                continue
            if hashlib.md5(packed).hexdigest() != hashlib.md5(read(full)).hexdigest():
                bad.append(rel + " (内容不一致)")
    if bad:
        for b in bad:
            print("    [X] %s" % b)
        return False
    print("    [OK] 全部 %d 个文件与本地一致" % len(SERVER_FILES))
    return True

def check_from_stripped():
    """产物必须来自「去除注释」，绝不能是带注释的开发版。

    这里只做当前目录（去除注释）内的核对：
    tar.gz 内每个文件要与本目录同路径文件逐字节相同；模板里不能出现注释行。
    跨目录的完整校验交给仓库根目录的 verify_sync.py。
    """
    print("\n  来源检查（产物必须来自脱注释版）:")
    ok = True

    tar_path = os.path.join(DIST, "relay-deploy.tar.gz")
    if os.path.isfile(tar_path):
        bad = 0
        total = 0
        with tarfile.open(tar_path) as t:
            for m in t.getmembers():
                if not m.isfile():
                    continue
                rel = m.name.replace("relay-deploy/", "").replace("/", os.sep)
                full = os.path.join(HERE, rel)
                if not os.path.isfile(full):
                    continue
                total += 1
                if hashlib.md5(t.extractfile(m).read()).hexdigest() != \
                        hashlib.md5(read(full)).hexdigest():
                    bad += 1
                    print("    [X] %s 与本地脱注释版不一致" % rel)
        if bad:
            ok = False
        else:
            print("    [OK] tar.gz 内 %d 个文件均来自脱注释版" % total)

    tpl_disk = os.path.join(DIST, "配置生成器", "模板.dat")
    agent = os.path.join(HERE, "client", "agent.ps1")
    if os.path.isfile(tpl_disk) and os.path.isfile(agent):
        with open(agent, encoding="utf-8") as f:
            body = f.read()

        leftover = [ln for ln in body.splitlines()
                    if ln.lstrip().startswith("#") and not ln.startswith("#!")]
        with open(tpl_disk, encoding="utf-8", errors="replace") as f:
            tpl = f.read()
        leaked = [ln for ln in leftover if ln.strip() and ln.strip() in tpl]
        if leaked:
            ok = False
            print("    [X] 源 agent.ps1 仍有 %d 行注释，模板里带进了 %d 行"
                  % (len(leftover), len(leaked)))
        else:
            print("    [OK] 模板.dat 不含注释（源 agent.ps1 残留注释行 %d）"
                  % len(leftover))

    return ok

def rebuild_helper_bat():
    """用 server/config.json 的 public_base / device_key 重新生成 dist/传输助手.bat。

    以前这一步是手工做的，结果改了模板后 dist 里还是旧 bat（少了 TRANSPORT 行）。
    """
    import json
    import subprocess

    cfg_path = os.path.join(HERE, "server", "config.json")
    relay, key, transport = None, None, "oss"
    if os.path.isfile(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            relay = (cfg.get("public_base") or "").rstrip("/") or None
            key = cfg.get("device_key") or None
            transport = cfg.get("transport") or "oss"
        except Exception as e:
            print("  [警告] 读 config.json 失败: %s" % e)

    if not relay or not key:
        print("  [跳过] server/config.json 缺少 public_base 或 device_key")
        print("         手工生成: python build.py --relay <地址> --key <密钥>")
        return

    out = os.path.join(DIST, "传输助手.bat")
    cmd = [sys.executable, os.path.join(HERE, "build.py"),
           "--relay", relay, "--key", key,
           "--transport", transport, "--autostart", "1", "--out", out]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0 or not os.path.isfile(out):
        print("  [警告] 生成失败: %s" % (r.stderr or r.stdout))
        return
    print("    %-30s %7.1f KB  (relay=%s, transport=%s)"
          % ("传输助手.bat", os.path.getsize(out) / 1024.0, relay, transport))

def main():
    os.makedirs(DIST, exist_ok=True)

    print("=" * 60)
    print("  打包")
    print("=" * 60)

    print("\n[1/2] 服务器部署包")
    sp = build_server_package()
    verify(sp, "relay-deploy.tar.gz")

    print("\n[2/2] 客户端配置生成器")
    cp = build_client_package()
    verify(cp, "配置生成器.zip")

    print("\n[补] 重新生成 dist/传输助手.bat（避免打包出旧版本）")
    rebuild_helper_bat()

    ok = check_consistency()
    ok = check_from_stripped() and ok

    print("\n" + "=" * 60)
    if ok:
        print("  完成")
    else:
        print("  完成，但有一致性问题（见上）")
    print("=" * 60)
    print("\n  dist/relay-deploy.tar.gz    → 上传到新服务器")
    print("  dist/配置生成器.zip          → 发给其他人")
    print("  dist/传输助手.bat            → 你自己用\n")
    return 0 if ok else 1

if __name__ == "__main__":
    sys.exit(main())
