
"""给 /opt/server/config.json 补上 lifecycle 段（幂等）。"""
import json
import os
import sys

p = sys.argv[1] if len(sys.argv) > 1 else "/opt/server/config.json"
cfg = json.load(open(p, encoding="utf-8"))
if "lifecycle" in cfg:
    print("已存在 lifecycle 段，未改动：")
else:
    cfg["lifecycle"] = {
        "_说明": "桶内对象到期自动清理。基础设置，默认所有人一致；改完重跑 tools/setup_oss.py 生效",
        "_说明2": "enabled=false 关掉；days 保留天数；prefix 留空=整个桶",
        "enabled": True,
        "days": 7,
        "prefix": "",
        "abort_multipart": True,
    }

    order = ["_说明", "_说明2", "listen", "port", "username", "password",
             "device_key", "session_hours", "max_fail", "lock_seconds",
             "public_base", "lifecycle", "oss"]
    out = {k: cfg[k] for k in order if k in cfg}
    for k, v in cfg.items():
        if k not in out:
            out[k] = v
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.chmod(p, 0o600)
    print("已写入 lifecycle 段：")
print(json.dumps(cfg.get("lifecycle"), ensure_ascii=False, indent=2))
