
r"""本地假 OSS：只实现 multipart 上传 / 下载所需的那几个接口，用于联调。

    python fake_oss.py --port 9000 --dir .\_fakeoss

支持：
    POST   /{bucket}/{key}?uploads                  -> 返回 UploadId
    PUT    /{bucket}/{key}?partNumber=N&uploadId=U  -> 存分片，返回 ETag
    GET    /{bucket}/{key}?uploadId=U               -> ListParts（断点续传用）
    POST   /{bucket}/{key}?uploadId=U               -> CompleteMultipartUpload
    DELETE /{bucket}/{key}?uploadId=U               -> Abort
    PUT    /{bucket}/{key}                          -> 单次上传
    GET    /{bucket}/{key}                          -> 下载（支持 Range）
    HEAD   /{bucket}/{key}                          -> 元信息

仅用于本机联调，不做鉴权、不做并发控制。
"""

import argparse
import hashlib
import json
import os
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote
from xml.etree import ElementTree as ET

ROOT = os.path.abspath("_fakeoss")
THROTTLE_MBPS = 0.0
LOCK = threading.Lock()
UPLOADS = {}

def part_path(upload_id, n):
    d = os.path.join(ROOT, "_parts", upload_id)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "%05d" % n)

def obj_path(key):
    safe = key.replace("/", os.sep)
    p = os.path.join(ROOT, "objects", safe)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p

CORS_HEADERS = (
    ("Access-Control-Allow-Origin", "*"),
    ("Access-Control-Allow-Methods", "GET, PUT, HEAD, POST, DELETE, OPTIONS"),
    ("Access-Control-Allow-Headers", "*"),
    ("Access-Control-Expose-Headers", "ETag, Content-Length, Content-Range, Accept-Ranges"),
    ("Access-Control-Max-Age", "3600"),
)

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def end_headers(self):

        for k, v in CORS_HEADERS:
            self.send_header(k, v)
        super().end_headers()

    def do_OPTIONS(self):

        self._trace("options")
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _trace(self, tag):
        if os.environ.get("FAKEOSS_VERBOSE"):
            print(tag, self.command, self.path, flush=True)

    def _xml(self, body, status=200):
        raw = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/xml")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _err(self, code, msg, status=400):
        self._xml("<Error><Code>%s</Code><Message>%s</Message></Error>" % (code, msg), status)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _parse(self):
        u = urlparse(self.path)
        key = unquote(u.path.lstrip("/"))
        if "/" in key:
            bucket, key = key.split("/", 1)
        else:
            bucket, key = key, ""
        return bucket, key, parse_qs(u.query, keep_blank_values=True)

    def do_POST(self):
        bucket, key, q = self._parse()
        body = self._body()
        if "uploads" in q:
            self._trace("init")
            uid = uuid.uuid4().hex
            with LOCK:
                UPLOADS[uid] = {"key": key, "parts": {}}
            return self._xml("<InitiateMultipartUploadResult><Bucket>%s</Bucket><Key>%s</Key>"
                             "<UploadId>%s</UploadId></InitiateMultipartUploadResult>" % (bucket, key, uid))
        if "uploadId" in q:
            uid = q["uploadId"][0]
            with LOCK:
                if uid not in UPLOADS:
                    return self._err("NoSuchUpload", uid, 404)
                parts = UPLOADS[uid]["parts"]
                try:
                    root = ET.fromstring(body or b"<x/>")
                    order = []
                    for p in root.findall("Part"):
                        n = int(p.findtext("PartNumber") or 0)
                        if n in parts:
                            order.append(n)
                except ET.ParseError:
                    order = sorted(parts)
                if not key:
                    key = UPLOADS[uid]["key"]
                self._trace("complete")
                dst = obj_path(key)
                h = hashlib.md5()
                with open(dst, "wb") as out:
                    for n in order:
                        with open(parts[n], "rb") as f:
                            while True:
                                chunk = f.read(1 << 20)
                                if not chunk:
                                    break
                                out.write(chunk)
                                h.update(chunk)
                UPLOADS.pop(uid, None)
            etag = h.hexdigest()
            return self._xml("<CompleteMultipartUploadResult><Location>local</Location>"
                             "<Bucket>%s</Bucket><Key>%s</Key><ETag>\"%s\"</ETag>"
                             "</CompleteMultipartUploadResult>" % (bucket, key, etag))
        return self._err("NotImplemented", "no such op", 400)

    def do_PUT(self):
        bucket, key, q = self._parse()
        body = self._body()
        if "partNumber" in q and "uploadId" in q:
            self._trace("part")
            uid = q["uploadId"][0]
            n = int(q["partNumber"][0])
            if THROTTLE_MBPS > 0 and body:
                time.sleep(len(body) / (THROTTLE_MBPS * 1024 * 1024))
            with LOCK:
                if uid not in UPLOADS:
                    return self._err("NoSuchUpload", uid, 404)
                p = part_path(uid, n)
                with open(p, "wb") as f:
                    f.write(body)
                UPLOADS[uid]["parts"][n] = p
            etag = hashlib.md5(body).hexdigest()
            self.send_response(200)
            self.send_header("ETag", '"%s"' % etag)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        dst = obj_path(key)
        with open(dst, "wb") as f:
            f.write(body)
        etag = hashlib.md5(body).hexdigest()
        self.send_response(200)
        self.send_header("ETag", '"%s"' % etag)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        bucket, key, q = self._parse()
        if "uploadId" in q and "partNumber" not in q:
            uid = q["uploadId"][0]
            with LOCK:
                if uid not in UPLOADS:
                    return self._err("NoSuchUpload", uid, 404)
                parts = UPLOADS[uid]["parts"]
                items = []
                for n in sorted(parts):
                    size = os.path.getsize(parts[n])
                    items.append("<Part><PartNumber>%d</PartNumber><Size>%d</Size>"
                                 "<ETag>\"x\"</ETag></Part>" % (n, size))
            return self._xml("<ListPartsResult>%s</ListPartsResult>" % "".join(items))

        p = obj_path(key)
        if not os.path.isfile(p):
            return self._err("NoSuchKey", key, 404)
        size = os.path.getsize(p)
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                if m.group(1):
                    start = int(m.group(1))
                if m.group(2):
                    end = int(m.group(2))
                end = min(end, size - 1)
        length = max(0, end - start + 1)
        self.send_response(206 if rng else 200)

        self.send_header("Content-Type", (q.get("response-content-type") or ["application/octet-stream"])[0])
        if q.get("response-content-disposition"):
            self.send_header("Content-Disposition", q["response-content-disposition"][0])
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if rng:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        with open(p, "rb") as f:
            f.seek(start)
            left = length
            while left > 0:
                chunk = f.read(min(1 << 20, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    def do_HEAD(self):
        bucket, key, q = self._parse()
        p = obj_path(key)
        if not os.path.isfile(p):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        size = os.path.getsize(p)
        self.send_response(200)
        self.send_header("Content-Length", str(size))
        self.send_header("ETag", '"%s"' % hashlib.md5(open(p, "rb").read()[:0]).hexdigest())
        self.end_headers()

    def do_DELETE(self):
        bucket, key, q = self._parse()
        if "uploadId" in q:
            with LOCK:
                UPLOADS.pop(q["uploadId"][0], None)
        elif key:
            self._trace("delete")
            p = obj_path(key)
            if os.path.isfile(p):
                os.remove(p)
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

def main():
    global ROOT, THROTTLE_MBPS
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--dir", default="_fakeoss")
    ap.add_argument("--throttle-mbps", type=float, default=0.0)
    a = ap.parse_args()
    THROTTLE_MBPS = a.throttle_mbps
    ROOT = os.path.abspath(a.dir)
    os.makedirs(ROOT, exist_ok=True)
    print("fake-oss on http://127.0.0.1:%d  dir=%s" % (a.port, ROOT), flush=True)
    ThreadingHTTPServer(("127.0.0.1", a.port), Handler).serve_forever()

if __name__ == "__main__":
    main()
