
"""把 index.html + style.css + app.js + preview.js 合并成单个 index.html。

用途：在平板上改 UI 时，只需编辑一个文件。

用法：
    python merge_web.py

输入（都在 server/web/）：
    index.html    页面骨架（含 <link> 和 <script src> 引用）
    style.css     样式
    app.js        逻辑
    preview.js    本地预览假数据

输出：
    server/web/index.html      合并后的单文件（覆盖原文件）
    server/web/index.src.html  原始骨架备份（下次合并时读这个）

说明：
    第一次运行时会把当前的 index.html 备份成 index.src.html，
    之后每次都从 index.src.html 读取骨架，所以可以反复合并。
"""

import os
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "server", "web")

SRC = os.path.join(WEB, "index.src.html")
OUT = os.path.join(WEB, "index.html")
CSS = os.path.join(WEB, "_src", "style.css")
JS = os.path.join(WEB, "_src", "app.js")
PREVIEW = os.path.join(WEB, "_src", "preview.js")

def read(path):
    with open(path, "r", encoding="utf-8-sig") as f:
        return f.read()

def write(path, text):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)

def main():

    if not os.path.isfile(SRC):
        if not os.path.isfile(OUT):
            sys.exit("找不到 index.html")

        cur = read(OUT)
        if '<style>' in cur or '/* ==== 内联样式' in cur:
            sys.exit("index.html 已经是合并版，但缺少 index.src.html 骨架。\n"
                     "请从 git 或备份恢复原始的 index.html。")
        shutil.copy2(OUT, SRC)
        print("已备份骨架: index.src.html")

    html = read(SRC)
    css = read(CSS)
    js = read(JS)
    prev = read(PREVIEW)

    for name, text, tag in (("app.js", js, "</script"), ("preview.js", prev, "</script")):
        if tag in text.lower():
            sys.exit("%s 里含有 %s，直接内联会截断脚本。请先处理。" % (name, tag))
    if "</style" in css.lower():
        sys.exit("style.css 里含有 </style，直接内联会截断样式。")

    link_re = re.compile(r'<link[^>]*href="[^"]*style\.css"[^>]*>', re.I)
    if not link_re.search(html):
        sys.exit("index.src.html 里找不到 style.css 的 <link> 标签")

    style_block = (
        "<style>\n"
        "/* ===================================================================\n"
        "   样式区（原 style.css）\n"
        "   改外观就在这一大段里找对应的选择器\n"
        "   =================================================================== */\n"
        + css.rstrip() + "\n"
        "</style>"
    )
    html = link_re.sub(lambda m: style_block, html, count=1)

    app_re = re.compile(r'<script[^>]*src="app\.js"[^>]*>\s*</script>', re.I)
    if not app_re.search(html):
        sys.exit("index.src.html 里找不到 app.js 的 <script> 标签")

    js_block = (
        "<script>\n"
        "/* ===================================================================\n"
        "   逻辑区（原 app.js）—— 不要改，改了功能会坏\n"
        "   =================================================================== */\n"
        + js.rstrip() + "\n"
        "</script>"
    )
    html = app_re.sub(lambda m: js_block, html, count=1)

    loader_res = [
        re.compile(r"<script>\s*if \(location\.protocol === 'file:'\)\s*\{.*?\}\s*</script>",
                   re.I | re.S),
        re.compile(r"<script>\s*if \(location\.protocol !== 'http:'[^<]*?\{.*?\}\s*</script>",
                   re.I | re.S),
    ]
    for lr in loader_res:
        if lr.search(html):
            html = lr.sub("", html, count=1)
            break

    prev_block = (
        "<script>\n"
        "/* ===================================================================\n"
        "   预览数据区（原 preview.js）—— 必须在逻辑区之前\n"
        "   只在本地打开本文件时生效，线上 http/https 直接跳过\n"
        "   =================================================================== */\n"
        + prev.rstrip() + "\n"
        "</script>"
    )

    if js_block in html:
        html = html.replace(js_block, prev_block + "\n" + js_block, 1)
    else:

        html = html.replace("</body>", prev_block + "\n</body>", 1)

    html = html.replace(
        "<title>信息传输控制台</title>",
        "<title>信息传输控制台</title>\n<!-- 单文件版：CSS 与 JS 已内联，改样式搜「样式区」 -->",
        1)

    write(OUT, html)

    size = os.path.getsize(OUT)
    lines = html.count("\n") + 1
    print("\n合并完成")
    print("  输出: server/web/index.html")
    print("  大小: %.1f KB" % (size / 1024.0))
    print("  行数: %d" % lines)
    print("\n  原 style.css : %.1f KB → 内联" % (len(css.encode()) / 1024.0))
    print("  原 app.js    : %.1f KB → 内联" % (len(js.encode()) / 1024.0))
    print("  原 preview.js: %.1f KB → 内联" % (len(prev.encode()) / 1024.0))
    print("\n  骨架已备份到 index.src.html（下次合并会读它）")
    print("  改样式：搜「样式区」")
    print("  改结构：搜「传输面板」「服务器信息」等中文注释")

if __name__ == "__main__":
    main()
