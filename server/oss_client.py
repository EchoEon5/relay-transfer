
"""阿里云 OSS 最小客户端（纯标准库，无第三方依赖）

设计要点：
  * 控制面（init/complete/abort/list_parts）由 ECS 中继调用 —— 报文极小，几乎不耗流量
  * 数据面（分片上传 / 下载）由目标端和浏览器 **直连 OSS** —— 不经过 ECS
  * 预签名 URL 用 V1 签名本地计算，不产生任何 API 调用

签名算法（V1）：
  StringToSign = VERB \\n Content-MD5 \\n Content-Type \\n Date \\n CanonicalizedOSSHeaders \\n CanonicalizedResource
  预签名时 Date 换成 Expires，且 Content-MD5 / Content-Type 视为空
"""

import base64
import hashlib
import hmac
import time
import urllib.request
import urllib.error
from email.utils import formatdate
from urllib.parse import quote, urlencode
from xml.etree import ElementTree as ET

SUBRES_WHITELIST = {
    "uploads", "uploadId", "partNumber", "delete", "security-token",
    "lifecycle", "acl", "policy", "tagging", "cors", "logging",
    "response-content-type", "response-content-language", "response-expires",
    "response-cache-control", "response-content-disposition", "response-content-encoding",
}

class OSSError(Exception):
    pass

class OSSClient:
    def __init__(self, endpoint, bucket, access_key_id, access_key_secret, sts_token=None,
                 scheme="https", host_override=None):
        if not endpoint or not bucket:
            raise ValueError("OSS endpoint / bucket 未配置")
        self.endpoint = endpoint.strip()
        self.bucket = bucket.strip()
        self.ak = access_key_id.strip()
        self.sk = access_key_secret.strip()
        self.token = (sts_token or "").strip() or None
        self.scheme = (scheme or "https").strip()

        self.host = (host_override or "").strip() or ("%s.%s" % (self.bucket, self.endpoint))
        self.base_url = "%s://%s" % (self.scheme, self.host)
        self.path_style = bool((host_override or "").strip())

    def _canonical_headers(self):
        """CanonicalizedOSSHeaders：x-oss-* 头要按字典序参与签名（STS token 也走这里）"""
        if not self.token:
            return ""
        return "x-oss-security-token:%s\n" % self.token

    def _canonical_resource(self, key, subres=None):
        res = "/%s/%s" % (self.bucket, key)
        if subres:
            items = [(k, v) for k, v in subres.items() if k in SUBRES_WHITELIST]
            if items:
                items.sort(key=lambda kv: kv[0])
                res += "?" + "&".join(
                    k if v is None or v == "" else "%s=%s" % (k, v) for k, v in items
                )
        return res

    def _sign(self, string_to_sign):
        digest = hmac.new(self.sk.encode("utf-8"), string_to_sign.encode("utf-8"),
                          hashlib.sha1).digest()
        return base64.b64encode(digest).decode("ascii")

    def _object_url(self, key):
        """虚拟主机风格 <bucket>.endpoint/key；指定 host_override 时退化为路径风格 endpoint/bucket/key"""
        qk = quote(key.lstrip("/"), safe="/~")
        if self.path_style:
            return "%s/%s/%s" % (self.base_url, quote(self.bucket, safe=""), qk)
        return "%s/%s" % (self.base_url, qk)

    def _request(self, verb, key, subres=None, body=None, content_type="", timeout=30):
        url = self._object_url(key)
        if subres:
            pairs = [(k, v) for k, v in subres.items() if k in SUBRES_WHITELIST]
            pairs.sort(key=lambda kv: kv[0])
            url += "?" + "&".join("%s=%s" % (k, quote(str(v), safe="")) for k, v in pairs)

        date = formatdate(timeval=None, localtime=False, usegmt=True)
        sts = "\n".join([verb, "", content_type, date,
                         self._canonical_headers() + self._canonical_resource(key, subres)])
        headers = {
            "Date": date,
            "Authorization": "OSS %s:%s" % (self.ak, self._sign(sts)),
        }
        if content_type:
            headers["Content-Type"] = content_type
        if self.token:
            headers["x-oss-security-token"] = self.token

        req = urllib.request.Request(url, data=body, headers=headers, method=verb)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            detail = e.read()[:2000].decode("utf-8", "replace")
            raise OSSError("OSS %s %s -> HTTP %s: %s" % (verb, key, e.code, detail)) from None
        except urllib.error.URLError as e:
            raise OSSError("OSS 连接失败: %s" % e.reason) from None

    def put(self, key, body, content_type="application/octet-stream"):
        """整块上传小对象（自检、写标记文件用）"""
        if isinstance(body, str):
            body = body.encode("utf-8")
        return self._request("PUT", key, body=body, content_type=content_type)

    def delete(self, key):
        """删除对象（省存储费）；不存在也当成功"""
        try:
            self._request("DELETE", key)
        except OSSError as e:
            if "HTTP 404" in str(e):
                return True
            raise
        return True

    def presign(self, verb, key, expires=3600, subres=None, content_type=""):
        """生成带有效期的签名 URL。subres 参与签名，可含 response-content-disposition。"""
        exp = int(time.time()) + int(expires)
        sts = "\n".join([verb, "", content_type, str(exp),
                         self._canonical_headers() + self._canonical_resource(key, subres)])
        query = []
        if subres:
            for k in sorted(subres):
                if k in SUBRES_WHITELIST:
                    query.append((k, subres[k]))
        query.append(("OSSAccessKeyId", self.ak))
        query.append(("Expires", str(exp)))
        query.append(("Signature", self._sign(sts)))
        if self.token:
            query.append(("security-token", self.token))
        qs = "&".join("%s=%s" % (quote(str(k), safe=""), quote(str(v), safe="")) for k, v in query)
        return "%s?%s" % (self._object_url(key), qs)

    def presign_part(self, key, upload_id, part_number, expires=7200):
        """分片上传：第 N 片的 PUT 直链（目标端用它直传 OSS）"""
        return self.presign("PUT", key,
                            expires=expires,
                            subres={"partNumber": str(part_number), "uploadId": upload_id},
                            content_type="application/octet-stream")

    def presign_get(self, key, expires=86400, filename=None):
        """下载直链；filename 走 response-content-disposition，让浏览器/IDM 存成原名

        注意：不要加 response-content-type。阿里云 OSS 会返回
        InvalidRequest: Can not override response header on content-type
        """
        subres = {}
        if filename:
            subres["response-content-disposition"] = content_disposition(filename)
        return self.presign("GET", key, expires=expires, subres=subres)

    def initiate_multipart(self, key, content_type="application/octet-stream"):
        raw = self._request("POST", key, subres={"uploads": ""}, body=b"", content_type=content_type)
        root = ET.fromstring(raw)
        upload_id = root.findtext("UploadId")
        if not upload_id:
            raise OSSError("InitiateMultipartUpload 未返回 UploadId: %r" % raw[:400])
        return upload_id

    def complete_multipart(self, key, upload_id, parts):
        """parts: [{'PartNumber': n, 'ETag': '"xxx"'}, ...]（ETag 带双引号原样传入）"""
        xml = ["<CompleteMultipartUpload>"]
        for p in sorted(parts, key=lambda x: int(x["PartNumber"])):
            etag = str(p["ETag"])
            if not etag.startswith('"'):
                etag = '"%s"' % etag
            xml.append("<Part><PartNumber>%d</PartNumber><ETag>%s</ETag></Part>"
                       % (int(p["PartNumber"]), etag.replace("&", "&amp;")))
        xml.append("</CompleteMultipartUpload>")
        body = "".join(xml).encode("utf-8")
        raw = self._request("POST", key, subres={"uploadId": upload_id},
                            body=body, content_type="application/xml", timeout=120)
        if b"<Error>" in raw:
            raise OSSError("CompleteMultipartUpload 失败: %r" % raw[:400])
        return raw

    def list_parts(self, key, upload_id):
        """断点续传用：已上传的分片清单"""
        raw = self._request("GET", key, subres={"uploadId": upload_id})
        root = ET.fromstring(raw)
        out = []
        for part in root.findall("Part"):
            out.append({
                "n": int(part.findtext("PartNumber") or 0),
                "size": int(part.findtext("Size") or 0),
                "etag": (part.findtext("ETag") or "").strip('"'),
            })
        return out

    def abort_multipart(self, key, upload_id):
        try:
            self._request("DELETE", key, subres={"uploadId": upload_id})
        except OSSError:
            pass

    def head(self, key):
        """返回 (size, etag)；不存在返回 None"""
        try:
            url = self._object_url(key)
            date = formatdate(timeval=None, localtime=False, usegmt=True)
            sts = "\n".join(["HEAD", "", "", date, self._canonical_resource(key)])
            req = urllib.request.Request(url, headers={
                "Date": date,
                "Authorization": "OSS %s:%s" % (self.ak, self._sign(sts)),
            }, method="HEAD")
            with urllib.request.urlopen(req, timeout=20) as resp:
                return int(resp.headers.get("Content-Length") or 0), (resp.headers.get("ETag") or "")
        except Exception:
            return None

def content_disposition(filename):
    """Content-Disposition 头（纯 ASCII，中文用 RFC5987 filename*）"""
    base = filename.replace("\\", "/").rsplit("/", 1)[-1]
    safe = "".join(c for c in base if 32 <= ord(c) < 127 and c not in '"\\').strip()

    if not safe.strip(".") or safe.startswith("."):
        safe = "download" + (_ext(base) or "")
    try:
        base.encode("ascii")
        return 'attachment; filename="%s"' % safe
    except UnicodeEncodeError:
        return 'attachment; filename="%s"; filename*=UTF-8\'\'%s' % (safe, quote(base, safe=""))

def _ext(name):
    dot = name.rfind(".")
    return name[dot:] if 0 < dot < len(name) - 1 else ""
