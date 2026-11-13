
"""给 OSS 桶设置生命周期规则（到期自动删除 + 清理未完成分片）。

这个脚本是**可复用**的：任何部署本项目的服务器，只要 config.json 里
填好了 OSS，跑一下就会得到和其他人完全相同的设置。

用法（在服务器上，读 /opt/server/config.json）：

    python3 tools/setup_oss.py --stats                 # 只看桶里现状，不改任何东西
    python3 tools/setup_oss.py --show                  # 看当前生效的生命周期规则
    python3 tools/setup_oss.py --apply                 # 应用默认规则（7 天）
    python3 tools/setup_oss.py --apply --days 30       # 自定义天数
    python3 tools/setup_oss.py --apply --prefix trans/ # 只对某个前缀生效
    python3 tools/setup_oss.py --remove                # 删掉生命周期规则

默认规则（与项目文档一致）：
    1) 桶内全部对象 7 天后自动删除
    2) 未完成的分片上传 7 天后清理

注意：这是**永久删除**，不是转归档、不可恢复。桶请专用于本工具。
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

for _cand in (os.path.join(ROOT, "server"), ROOT, os.path.dirname(HERE)):
    if os.path.isfile(os.path.join(_cand, "oss_client.py")):
        sys.path.insert(0, _cand)
        break

from oss_client import OSSClient, OSSError

DEFAULT_DAYS = 7
DEFAULT_RULE_PREFIX = ""

def load_oss(cfg_path):
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    o = cfg.get("oss") or {}
    ak = os.environ.get("OSS_AK") or o.get("access_key_id") or ""
    sk = os.environ.get("OSS_SK") or o.get("access_key_secret") or ""
    bucket = os.environ.get("OSS_BUCKET") or o.get("bucket") or ""
    endpoint = o.get("endpoint") or ""
    if not (ak and sk and bucket and endpoint):
        raise SystemExit("config.json 里 OSS 配置不完整（需要 endpoint/bucket/ak/sk）")
    cli = OSSClient(endpoint, bucket, ak, sk,
                    scheme=o.get("scheme") or "https",
                    host_override=o.get("host_override") or None)
    return cfg, cli

def lifecycle_cfg(cfg):
    """从 config.json 的 lifecycle 段读默认值；缺失则用内置默认。

    这是「基础设置」：所有人装出来都一样，但都能在自己的 config.json 里改。
    """
    lc = cfg.get("lifecycle") or {}
    return {
        "enabled": bool(lc.get("enabled", True)),
        "days": int(lc.get("days", DEFAULT_DAYS)),
        "prefix": str(lc.get("prefix", DEFAULT_RULE_PREFIX) or ""),
        "abort_multipart": bool(lc.get("abort_multipart", True)),
    }

def build_xml(days, prefix, abort_days):
    """生成 PutBucketLifecycle 的 XML。

    注意：OSS 不允许两条规则用同一个前缀（Found same prefix in different rules），
    所以「到期删除」和「清理未完成分片」必须写进**同一条** Rule。
    """
    p = (prefix or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    body = ("  <Rule>\n"
            "    <ID>relay-retention</ID>\n"
            "    <Prefix>%s</Prefix>\n"
            "    <Status>Enabled</Status>\n"
            "    <Expiration><Days>%d</Days></Expiration>\n" % (p, days))
    if abort_days:
        body += ("    <AbortMultipartUpload><Days>%d</Days></AbortMultipartUpload>\n"
                 % abort_days)
    body += "  </Rule>\n"
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            "<LifecycleConfiguration>\n%s</LifecycleConfiguration>\n"
            % body).encode("utf-8")

def show(cli):
    try:
        raw = cli._request("GET", "", subres={"lifecycle": ""})
    except OSSError as e:
        if "NoSuchLifecycle" in str(e) or "404" in str(e):
            print("当前没有生命周期规则。")
            return
        raise
    print("当前生效的生命周期规则：")
    print(raw.decode("utf-8", "replace"))

def cors_origins(cfg):
    """浏览器直传 OSS 需要的跨域来源。

    默认从 public_base 推出来（不留空、不写死），另外永远放行本机调试地址。
    config.json 里可以显式写 "cors": {"origins": [...]} 覆盖。
    """
    c = cfg.get("cors") or {}
    if c.get("enabled") is False:
        return []
    listed = [str(x).strip() for x in (c.get("origins") or []) if str(x).strip()]
    if listed:
        return listed

    out = []
    base = str(cfg.get("public_base") or "").strip()
    if base:

        parts = base.split("://", 1)
        if len(parts) == 2:
            host = parts[1].split("/", 1)[0]
            out.append("%s://%s" % (parts[0], host))
    out += ["http://localhost:*", "http://127.0.0.1:*"]

    seen = set()
    uniq = []
    for o in out:
        if o not in seen:
            seen.add(o)
            uniq.append(o)
    return uniq

def build_cors_xml(origins):
    esc = lambda s: s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    ors = "".join("    <AllowedOrigin>%s</AllowedOrigin>\n" % esc(o) for o in origins)
    methods = "".join("    <AllowedMethod>%s</AllowedMethod>\n" % m
                      for m in ("PUT", "GET", "HEAD", "POST"))
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            "<CORSConfiguration>\n"
            "  <CORSRule>\n"
            "%s"
            "%s"
            "    <AllowedHeader>*</AllowedHeader>\n"
            "    <ExposeHeader>ETag</ExposeHeader>\n"
            "    <MaxAgeSeconds>3600</MaxAgeSeconds>\n"
            "  </CORSRule>\n"
            "</CORSConfiguration>\n" % (ors, methods)).encode("utf-8")

def show_cors(cli):
    try:
        raw = cli._request("GET", "", subres={"cors": ""})
    except OSSError as e:
        if "NoSuchCORS" in str(e) or "404" in str(e):
            print("当前没有跨域规则。")
            return
        raise
    print(raw.decode("utf-8", "replace"))

def list_objects(cli, prefix="", limit=50000):
    """列出桶内对象。返回 [(key, last_modified, size), ...]

    自己拼 URL：max-keys / marker / prefix 是普通查询参数，
    不参与 V1 签名，所以不能借 OSSClient._request（它会按子资源白名单过滤）。
    """
    import re
    from email.utils import formatdate

    out = []
    marker = ""
    while len(out) < limit:
        q = ["max-keys=1000"]
        if prefix:
            q.append("prefix=" + urllib.parse.quote(prefix, safe=""))
        if marker:
            q.append("marker=" + urllib.parse.quote(marker, safe=""))
        url = cli._object_url("") + "?" + "&".join(q)

        date = formatdate(timeval=None, localtime=False, usegmt=True)
        sts = "\n".join(["GET", "", "", date, cli._canonical_resource("")])
        headers = {"Date": date,
                   "Authorization": "OSS %s:%s" % (cli.ak, cli._sign(sts))}
        if getattr(cli, "token", None):
            headers["x-oss-security-token"] = cli.token

        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            detail = e.read()[:1500].decode("utf-8", "replace")
            raise OSSError("列举对象失败 HTTP %s: %s" % (e.code, detail)) from None
        except urllib.error.URLError as e:
            raise OSSError("OSS 连接失败: %s" % e.reason) from None

        for m in re.finditer(r"<Contents>(.*?)</Contents>", raw, re.S):
            blk = m.group(1)
            k = re.search(r"<Key>(.*?)</Key>", blk, re.S)
            lm = re.search(r"<LastModified>(.*?)</LastModified>", blk, re.S)
            sz = re.search(r"<Size>(\d+)</Size>", blk, re.S)
            if k:
                out.append((k.group(1),
                            lm.group(1) if lm else "",
                            int(sz.group(1)) if sz else 0))

        nxt = re.search(r"<NextMarker>(.*?)</NextMarker>", raw, re.S)
        if "<IsTruncated>true</IsTruncated>" not in raw or not nxt:
            break
        marker = nxt.group(1)
    return out

def stats(cli, days):
    """统计对象数量/总量/前缀分布/是否已超期。"""
    import time
    keys = list_objects(cli)

    total_bytes = sum(k[2] for k in keys)
    pref = {}
    for k, _, sz in keys:
        p = k.split("/")[0] + "/" if "/" in k else "(无前缀)"
        d = pref.setdefault(p, [0, 0])
        d[0] += 1
        d[1] += sz

    print("桶内对象数：%d" % len(keys))
    print("占用空间  ：%.2f MB" % (total_bytes / 1048576.0))
    if pref:
        print("前缀分布  ：")
        for p, (c, s) in sorted(pref.items(), key=lambda kv: -kv[1][1])[:10]:
            print("    %-28s %6d 个  %10.2f MB" % (p, c, s / 1048576.0))

    if not keys:
        print("（桶是空的，设了规则也不会误删任何东西）")
        return

    now = time.time()
    old = 0
    old_bytes = 0
    for k, lm, sz in keys:
        try:
            ts = time.mktime(time.strptime(lm.replace("Z", "").replace("T", " "),
                                           "%Y-%m-%d %H:%M:%S"))
            ts -= 8 * 3600
        except Exception:
            continue
        if (now - ts) > days * 86400:
            old += 1
            old_bytes += sz
    print("")
    if old:
        print("⚠️  按 %d 天规则，已有 %d 个对象（%.2f MB）超期，"
              "规则生效后会被删除。" % (days, old, old_bytes / 1048576.0))
    else:
        print("✅ 目前没有超过 %d 天的对象，设了规则也不会立刻删掉任何东西。" % days)

def default_config():
    """找一个能用的 config.json。

    两种常见布局都要支持：
      仓库里   <项目>/tools/setup_oss.py  → <项目>/server/config.json
      装好后   /opt/server/tools/setup_oss.py → /opt/server/config.json
    找不到就返回仓库布局那个（让报错信息指向预期的位置）。
    """
    cands = (
        os.path.join(ROOT, "server", "config.json"),
        os.path.join(ROOT, "config.json"),
        os.path.join(HERE, "config.json"),
        "/opt/server/config.json",
    )
    for p in cands:
        if os.path.isfile(p):
            return p
    return cands[0]

def main():
    ap = argparse.ArgumentParser(description="设置 OSS 生命周期规则")
    ap.add_argument("--config", default=default_config())
    ap.add_argument("--days", type=int, default=None,
                    help="覆盖 config.json 里的 lifecycle.days")
    ap.add_argument("--prefix", default=None,
                    help="覆盖 config.json 里的 lifecycle.prefix；留空=整个桶")
    ap.add_argument("--no-abort", action="store_true", help="不清理未完成分片")
    ap.add_argument("--stats", action="store_true", help="只看桶里现状")
    ap.add_argument("--show", action="store_true", help="看当前规则")
    ap.add_argument("--apply", action="store_true", help="应用规则")
    ap.add_argument("--remove", action="store_true", help="删除规则")
    a = ap.parse_args()

    if not (a.stats or a.show or a.apply or a.remove):
        ap.print_help()
        return 0

    cfg, cli = load_oss(a.config)
    lc = lifecycle_cfg(cfg)
    days = a.days if a.days is not None else lc["days"]
    prefix = a.prefix if a.prefix is not None else lc["prefix"]
    abort_mpu = lc["abort_multipart"] and not a.no_abort

    print("桶：%s    端点：%s" % (cli.bucket, cli.endpoint))
    print("")

    try:
        if a.show:
            show(cli)
        if a.stats:
            stats(cli, days)
        if a.remove:
            cli._request("DELETE", "", subres={"lifecycle": ""})
            print("已删除生命周期规则。")
        if a.apply:
            if not lc["enabled"] and a.days is None:
                print("config.json 里 lifecycle.enabled = false，跳过。")
                return 0
            if days < 1 or days > 3650:
                raise SystemExit("天数必须在 1~3650 之间")
            xml = build_xml(days, prefix, days if abort_mpu else None)
            cli._request("PUT", "", subres={"lifecycle": ""}, body=xml,
                         content_type="application/xml", timeout=30)
            print("已应用生命周期规则：")
            print("  作用范围：%s" % (prefix or "整个桶"))
            print("  对象到期：%d 天后自动删除（永久，不可恢复）" % days)
            if abort_mpu:
                print("  未完成分片：%d 天后清理" % days)
            else:
                print("  未完成分片：不清理")
            print("  （以上来自 config.json 的 lifecycle 段，改完重跑本脚本即可）")
            print("")
            show(cli)

            origins = cors_origins(cfg)
            if origins:
                cli._request("PUT", "", subres={"cors": ""},
                             body=build_cors_xml(origins),
                             content_type="application/xml", timeout=30)
                print("已应用跨域规则（浏览器直传用）：")
                for o in origins:
                    print("  允许来源：%s" % o)
                print("")
                show_cors(cli)
            else:
                print("跨域规则：已关闭（cors.enabled = false），浏览器直传将不可用")
    except OSSError as e:
        print("失败：%s" % e)
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())
