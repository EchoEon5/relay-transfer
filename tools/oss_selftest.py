
"""OSS 真实读写验证：put -> head -> presign GET -> 实际下载校验 -> delete

在 ECS 上跑，用生产配置。验证的是真实链路，不是 mock。
"""
import hashlib
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, "/opt/server")
from oss_client import OSSClient

CFG = json.load(open("/opt/server/config.json"))
oss_cfg = CFG["oss"]

cli = OSSClient(
    endpoint=oss_cfg["endpoint"],
    bucket=oss_cfg["bucket"],
    access_key_id=oss_cfg["access_key_id"],
    access_key_secret=oss_cfg["access_key_secret"],
)

STAMP = time.strftime("%Y%m%d-%H%M%S")
KEY = "selftest/%s.txt" % STAMP
PAYLOAD = ("信息传输系统自检 " + STAMP + "\n").encode("utf-8") * 200
WANT = hashlib.sha256(PAYLOAD).hexdigest()

print("bucket   :", oss_cfg["bucket"])
print("endpoint :", oss_cfg["endpoint"])
print("key      :", KEY)
print("size     :", len(PAYLOAD), "bytes")
print("sha256   :", WANT[:24], "...")
print()

ok = True

t0 = time.time()
try:
    etag = cli.put(KEY, PAYLOAD, content_type="text/plain")
    print("[1/5] PUT          OK   %d ms   etag=%s" % ((time.time() - t0) * 1000, str(etag)[:16]))
except Exception as e:
    print("[1/5] PUT          FAIL", repr(e)[:200])
    ok = False

if ok:
    try:
        size, tag = cli.head(KEY)
        match = size == len(PAYLOAD)
        print("[2/5] HEAD         %s  size=%d (%s)" % ("OK" if match else "SIZE MISMATCH", size, "匹配" if match else "不匹配"))
        ok = ok and match
    except Exception as e:
        print("[2/5] HEAD         FAIL", repr(e)[:200])
        ok = False

url = None
if ok:
    try:
        url = cli.presign_get(KEY, expires=300)
        print("[3/5] PRESIGN GET  OK   %s..." % url[:80])
    except Exception as e:
        print("[3/5] PRESIGN GET  FAIL", repr(e)[:200])
        ok = False

if ok and url:
    try:
        t0 = time.time()
        got = urllib.request.urlopen(url, timeout=20).read()
        h = hashlib.sha256(got).hexdigest()
        match = h == WANT
        print("[4/5] DOWNLOAD     %s  %d bytes  %d ms  sha256=%s"
              % ("OK" if match else "HASH MISMATCH",
                 len(got), (time.time() - t0) * 1000, "匹配" if match else "不匹配"))
        ok = ok and match
    except Exception as e:
        print("[4/5] DOWNLOAD     FAIL", repr(e)[:200])
        ok = False

try:
    cli.delete(KEY)
    print("[5/5] DELETE       OK")
except Exception as e:
    print("[5/5] DELETE       FAIL", repr(e)[:200])

print()
print("结果:", "全部通过 ✓" if ok else "有失败项 ✗")
sys.exit(0 if ok else 1)
