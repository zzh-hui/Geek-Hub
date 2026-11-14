#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Geek-Hub 手机服务器 · 单文件版
- 支持 Range 断点续传（大文件下载/视频拖动）
- 支持目录浏览、中文文件名
- 支持简单 Basic Auth（可选）
- 兼容 Python 3.7+（iSH 的 Python 3.9 可用）
用法：
  python3 serve.py                    # 端口 8080，目录 ./
  python3 serve.py -p 8080 -d /var/www/iso
  python3 serve.py -d /var/www/iso -u user:pass    # 开启账号密码
"""

import argparse
import base64
import html
import mimetypes
import os
import re
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CHUNK = 64 * 1024
RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")

AUTH_USER = None
AUTH_PASS = None
ROOT = "."


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%.1f %s" % (n, unit)) if unit != "B" else ("%d B" % n)
        n /= 1024.0


class Handler(BaseHTTPRequestHandler):
    server_version = "GeekHubPhone/1.0"
    protocol_version = "HTTP/1.1"

    # ---------- 工具 ----------
    def log_message(self, fmt, *args):
        sys.stdout.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))
        sys.stdout.flush()

    def _auth_ok(self):
        if not AUTH_USER:
            return True
        head = self.headers.get("Authorization", "")
        if not head.startswith("Basic "):
            return False
        try:
            raw = base64.b64decode(head[6:]).decode("utf-8", "replace")
        except Exception:
            return False
        return raw == "%s:%s" % (AUTH_USER, AUTH_PASS)

    def _need_auth(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Geek-Hub"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _safe_path(self):
        """把 URL 路径映射到磁盘路径，阻止 ../ 越权"""
        path = urllib.parse.unquote(urllib.parse.urlparse(self.path).path)
        path = os.path.normpath(path).lstrip("/")
        full = os.path.abspath(os.path.join(ROOT, path))
        if not (full == os.path.abspath(ROOT) or full.startswith(os.path.abspath(ROOT) + os.sep)):
            return None
        return full

    # ---------- 目录列表 ----------
    def _listing(self, full):
        try:
            entries = sorted(
                os.scandir(full),
                key=lambda e: (not e.is_dir(), e.name.lower()),
            )
        except OSError as e:
            self._error(500, "无法读取目录：%s" % e)
            return

        rows = []
        if os.path.abspath(full) != os.path.abspath(ROOT):
            rows.append('<tr><td><a href="../">⬆️ 返回上级</a></td><td>—</td><td>—</td></tr>')

        for e in entries:
            try:
                st = e.stat()
            except OSError:
                continue
            name = e.name + ("/" if e.is_dir() else "")
            href = urllib.parse.quote(e.name) + ("/" if e.is_dir() else "")
            size = "目录" if e.is_dir() else human(st.st_size)
            mtime = time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))
            rows.append(
                '<tr><td><a href="%s">%s</a></td><td>%s</td><td>%s</td></tr>'
                % (href, html.escape(name), size, mtime)
            )

        rel = os.path.relpath(full, ROOT)
        title = "Geek-Hub 文件站" if rel == "." else "/" + rel.replace(os.sep, "/")
        body = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>%s</title>
<style>
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;padding:24px 16px;background:radial-gradient(circle at top right,#1e1b4b,#0f172a);color:#f8fafc;
font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;min-height:100vh}
h1{font-size:1.3rem;margin:0 0 18px;display:flex;align-items:center;gap:8px}
h1::before{content:"";width:5px;height:22px;background:#38bdf8;border-radius:3px}
.wrap{max-width:820px;margin:0 auto}
table{width:100%%;border-collapse:collapse;background:rgba(30,41,59,.72);border:1px solid rgba(255,255,255,.1);
border-radius:14px;overflow:hidden;font-size:.92rem}
th,td{text-align:left;padding:12px 14px;border-bottom:1px solid rgba(255,255,255,.07)}
th{font-size:.78rem;color:#94a3b8;font-weight:600;letter-spacing:.5px}
tr:last-child td{border-bottom:none}
td:nth-child(2),td:nth-child(3),th:nth-child(2),th:nth-child(3){white-space:nowrap;color:#94a3b8;font-size:.82rem}
a{color:#38bdf8;text-decoration:none}a:hover{text-decoration:underline}
.foot{margin-top:18px;font-size:.75rem;color:#64748b;text-align:center}
</style></head><body><div class="wrap">
<h1>%s</h1>
<table><thead><tr><th>名称</th><th>大小</th><th>修改时间</th></tr></thead><tbody>
%s
</tbody></table>
<p class="foot">Geek-Hub · 手机文件服务 · %s</p>
</div></body></html>""" % (html.escape(title), html.escape(title), "\n".join(rows), time.strftime("%Y-%m-%d %H:%M"))

        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, code, msg):
        data = ("<!DOCTYPE html><meta charset='utf-8'><title>%d</title>"
                "<body style='background:#0f172a;color:#f8fafc;font-family:sans-serif;"
                "display:flex;align-items:center;justify-content:center;height:100vh;margin:0'>"
                "<div style='text-align:center'><h1 style='font-size:4rem;margin:0;color:#38bdf8'>%d</h1>"
                "<p style='color:#94a3b8'>%s</p><a href='/' style='color:#38bdf8'>返回根目录</a></div>"
                "</body>" % (code, code, html.escape(msg))).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    # ---------- 下载（含 Range） ----------
    def _send_file(self, full):
        try:
            f = open(full, "rb")
        except OSError as e:
            self._error(404, "文件打不开：%s" % e)
            return

        with f:
            size = os.fstat(f.fileno()).st_size
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
                ctype += "; charset=utf-8"
            start, end = 0, size - 1
            partial = False

            rng = self.headers.get("Range")
            if rng:
                m = RANGE_RE.match(rng.strip())
                if m:
                    s, e = m.group(1), m.group(2)
                    if s:
                        start = int(s)
                        end = int(e) if e else size - 1
                    elif e:  # bytes=-500 表示最后 500 字节
                        start = max(0, size - int(e))
                        end = size - 1
                    if start > end or start >= size:
                        self.send_response(416)
                        self.send_header("Content-Range", "bytes */%d" % size)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    partial = True

            length = end - start + 1
            self.send_response(206 if partial else 200)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            if partial:
                self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
            self.end_headers()

            if self.command == "HEAD":
                return

            f.seek(start)
            remain = length
            try:
                while remain > 0:
                    chunk = f.read(min(CHUNK, remain))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remain -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

    # ---------- 路由 ----------
    def do_GET(self):
        if not self._auth_ok():
            self._need_auth()
            return

        full = self._safe_path()
        if full is None:
            self._error(403, "路径不合法")
            return

        if os.path.isdir(full):
            if not self.path.endswith("/"):
                self.send_response(301)
                self.send_header("Location", self.path + "/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            index = os.path.join(full, "index.html")
            if os.path.isfile(index):
                self._send_file(index)
            else:
                self._listing(full)
            return

        if os.path.isfile(full):
            self._send_file(full)
        else:
            self._error(404, "找不到这个文件或目录")

    def do_HEAD(self):
        self.do_GET()


def main():
    global AUTH_USER, AUTH_PASS, ROOT
    ap = argparse.ArgumentParser(description="Geek-Hub 手机文件服务器")
    ap.add_argument("-p", "--port", type=int, default=8080)
    ap.add_argument("-d", "--dir", default=".")
    ap.add_argument("-b", "--bind", default="0.0.0.0")
    ap.add_argument("-u", "--auth", help="账号密码，格式 user:pass")
    args = ap.parse_args()

    ROOT = os.path.abspath(args.dir)
    if not os.path.isdir(ROOT):
        print("目录不存在：%s" % ROOT)
        sys.exit(1)
    if args.auth:
        if ":" not in args.auth:
            print("--auth 格式应为 user:pass")
            sys.exit(1)
        AUTH_USER, AUTH_PASS = args.auth.split(":", 1)

    srv = ThreadingHTTPServer((args.bind, args.port), Handler)
    srv.daemon_threads = True
    print("Geek-Hub 文件服务已启动")
    print("  目录：%s" % ROOT)
    print("  地址：http://%s:%d/" % (args.bind, args.port))
    print("  鉴权：%s" % ("开启（%s）" % AUTH_USER if AUTH_USER else "关闭"))
    print("支持 Range 断点续传 · Ctrl+C 停止")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
