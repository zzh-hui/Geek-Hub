#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Geek-Hub 云盘服务 · 单文件版
- 公开只读：任何人都能浏览目录、下载文件（支持 Range 断点续传）
- 管理员写入：凭正确账号密码才能上传/删除（凭据从文件或环境变量读取，不硬编码）
- 兼容 Python 3.7+

用法：
  python3 serve.py -p 8099 -d files
  ADMIN_FILE=/root/.gh_admin python3 serve.py -p 8099 -d files
  ADMIN_AUTH=user:pass python3 serve.py -p 8099 -d files
"""

import argparse
import base64
import hmac
import html
import json
import mimetypes
import os
import re
import shutil
import sys
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CHUNK = 64 * 1024
RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")
UPLOAD_PREFIX = "/__upload/"
DELETE_PREFIX = "/__delete/"
MKDIR = "/__mkdir"
RENAME = "/__rename"
WHOAMI = "/__whoami"

MAX_NAME = 120

ADMIN_USER = ""
ADMIN_PASS = ""
ROOT = "."

# ---------- 防爆破 ----------
# 每 IP 累计失败次数（成功登录才清零）：
#   满 10 次 → 锁 5 分钟；满 15 次 → 锁 30 分钟；
#   满 20 次 → 锁 1 天；此后每多错 1 次加 1 天
FAIL_DELAY = 2  # 每次失败强制等待秒数
FAIL_LOCK = threading.Lock()
AUTH_STATE = {}  # ip -> {"fails": int, "lock_until": float}


def _lock_duration(fails):
    if fails >= 20:
        return 86400 * (1 + fails - 20)
    if fails >= 15:
        return 1800
    if fails >= 10:
        return 300
    return 0


def clean_name(raw):
    """校验用户输入的文件/文件夹名，返回安全名字或 None"""
    if not isinstance(raw, str):
        return None
    n = raw.strip().strip("\x00")
    n = n.replace("\n", " ").replace("\r", " ")
    if not n or n in (".", ".."):
        return None
    if "/" in n or "\\" in n:
        return None
    if len(n) > MAX_NAME:
        return None
    return n


def human(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%d B" % n) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0


CSS = """
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;padding:24px 16px;background:radial-gradient(circle at top right,#1e1b4b,#0f172a);
color:#f8fafc;font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;min-height:100vh}
h1{font-size:1.3rem;margin:0 0 14px;display:flex;align-items:center;gap:8px}
h1::before{content:"";width:5px;height:22px;background:#38bdf8;border-radius:3px}
.wrap{max-width:820px;margin:0 auto}
table{width:100%;border-collapse:collapse;background:rgba(30,41,59,.72);border:1px solid rgba(255,255,255,.1);
border-radius:14px;overflow:hidden;font-size:.92rem}
th,td{text-align:left;padding:12px 14px;border-bottom:1px solid rgba(255,255,255,.07)}
th{font-size:.78rem;color:#94a3b8;font-weight:600;letter-spacing:.5px}
tr:last-child td{border-bottom:none}
td:nth-child(2),td:nth-child(3),th:nth-child(2),th:nth-child(3){white-space:nowrap;color:#94a3b8;font-size:.82rem}
a{color:#38bdf8;text-decoration:none}a:hover{text-decoration:underline}
.foot{margin-top:18px;font-size:.75rem;color:#64748b;text-align:center}
.admin{margin:0 0 14px;padding:12px 14px;border:1px dashed rgba(255,255,255,.16);border-radius:12px;
background:rgba(30,41,59,.45);font-size:.86rem;display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.admin input{background:#0f172a;border:1px solid rgba(255,255,255,.16);color:#f8fafc;border-radius:8px;
padding:8px 10px;font-size:.85rem;min-width:0}
.admin input#u{width:110px}.admin input#p{width:130px}
.admin button{background:#38bdf8;color:#0f172a;border:none;border-radius:8px;padding:8px 14px;
font-weight:700;font-size:.85rem;cursor:pointer}
.admin button.ghost{background:transparent;color:#94a3b8;border:1px solid rgba(255,255,255,.16);font-weight:500}
.admin .msg{color:#94a3b8;font-size:.8rem}
.admin .msg.ok{color:#4ade80}.admin .msg.err{color:#f87171}
.admin input.fake,.admin button.fakebtn{opacity:.5;pointer-events:none;filter:grayscale(.7);cursor:not-allowed}
.pick{display:inline-block;background:#38bdf8;color:#0f172a;font-weight:700;border-radius:8px;
padding:8px 14px;cursor:pointer;font-size:.85rem}
/* iOS Safari 不允许点 display:none 的文件框弹出选择器，改用视觉隐藏 */
.pick input{position:absolute;width:1px;height:1px;opacity:0;overflow:hidden;clip:rect(0 0 0 0)}
td.op{width:104px;text-align:right;white-space:nowrap}
.del{background:transparent;border:none;color:#f87171;cursor:pointer;font-size:.8rem;padding:4px 6px}
.ren{background:transparent;border:none;color:#38bdf8;cursor:pointer;font-size:.8rem;padding:4px 6px}
.renin{width:100%;min-width:110px;background:#0f172a;border:1px solid #38bdf8;color:#f8fafc;
border-radius:6px;padding:4px 8px;font-size:.9rem}
.renbox{display:flex;gap:4px;align-items:center}
.renbox button{background:transparent;border:none;cursor:pointer;font-size:.9rem;padding:2px 5px;
line-height:1;border-radius:5px}
.renbox .rok{color:#4ade80}.renbox .rno{color:#94a3b8}
.renbox button:hover{background:rgba(255,255,255,.08)}
.admin input#mkname{width:180px}
#mkRow{display:inline-flex;gap:6px;align-items:center;flex-wrap:wrap}
.legal{margin-top:16px;font-size:.76rem;color:#64748b;text-align:left}
.legal section{margin-top:8px;border:1px solid rgba(255,255,255,.08);border-radius:10px;
background:rgba(30,41,59,.35);padding:11px 13px 13px}
.legal h3{margin:0 0 7px;font-size:.79rem;font-weight:600;color:#94a3b8;
display:flex;align-items:center;gap:7px}
.legal h3::before{content:"";width:3px;height:12px;border-radius:2px;background:#38bdf8;
flex:0 0 auto}
.legal .body{line-height:1.8}
.legal .body p{margin:0 0 8px}
.legal .body p:last-child{margin-bottom:0}
.legal .body b{color:#94a3b8;font-weight:600}
.legal a{color:#38bdf8;text-decoration:none}
.legal a:hover{text-decoration:underline}
body:not(.is-admin) td.op,body:not(.is-admin) th.op{display:none}
"""

JS = """
(function(){
  var $ = function(id){ return document.getElementById(id); };
  var KEY = "gh_admin_auth";
  var body = document.body;

  function auth(){ try { return sessionStorage.getItem(KEY) || ""; } catch(e){ return ""; } }
  function setAuth(v){ try { v ? sessionStorage.setItem(KEY, v) : sessionStorage.removeItem(KEY); } catch(e){} }

  function msg(t, cls){ var m=$("msg"); m.textContent=t; m.className="msg"+(cls?" "+cls:""); }

  // ---- 锁定 UI：把输入框和按钮整体替换成假的静态元素 ----
  var origLoginHTML = null, lockTimer = null;
  function renderLock(remain){
    var row = $("loginRow");
    if(origLoginHTML === null) origLoginHTML = row.innerHTML;
    if(lockTimer) clearInterval(lockTimer);
    var left = Math.max(1, parseInt(remain,10) || 1);
    function paint(){
      var mm = Math.floor(left/60), ss = left%60;
      var t = mm>0 ? ("锁定 "+mm+" 分 "+ss+" 秒") : ("锁定 "+ss+" 秒");
      row.innerHTML =
        '<input class="fake" value="已锁定" readonly disabled>' +
        '<input class="fake" value="请稍后再试" readonly disabled>' +
        '<button class="fakebtn" disabled>'+t+'</button>';
    }
    paint();
    lockTimer = setInterval(function(){
      left--;
      if(left<=0){
        clearInterval(lockTimer); lockTimer=null;
        row.innerHTML = origLoginHTML; origLoginHTML = null;
        bindLogin();
        msg("锁定已解除，可以再试","ok");
      } else paint();
    },1000);
  }

  function check(){
    var a = auth();
    if(!a){ return; }
    fetch("/__whoami", {headers:{"Authorization":a}}).then(function(r){
      if(r.ok){ body.classList.add("is-admin"); $("loginRow").hidden=true; $("upRow").hidden=false; }
      else if(r.status===429){ r.json().then(function(j){ renderLock(j.remain||300); }); }
      else { setAuth(""); }
    }).catch(function(){});
  }

  function bindLogin(){
    $("login").addEventListener("click", function(){
      var u=$("u").value.trim(), p=$("p").value;
      if(!u||!p){ msg("请填写用户名和密码","err"); return; }
      var a = "Basic " + btoa(unescape(encodeURIComponent(u+":"+p)));
      msg("正在验证…");
      fetch("/__whoami", {headers:{"Authorization":a}}).then(function(r){
        if(r.ok){
          setAuth(a); body.classList.add("is-admin");
          $("loginRow").hidden=true; $("upRow").hidden=false;
          msg("已登录，可以上传了","ok");
        } else if(r.status===429){
          r.json().then(function(j){ renderLock(j.remain||300); msg("失败次数过多，已被锁定","err"); });
        } else { msg("用户名或密码不对","err"); }
      }).catch(function(){ msg("网络错误","err"); });
    });
  }

  $("logout").addEventListener("click", function(){
    setAuth(""); location.reload();
  });

  $("f").addEventListener("change", function(){
    var files = this.files; if(!files.length) return;
    var i = 0;

    // 分片大小随文件体积放大：小文件切细，大文件切粗，减少请求往返开销
    function pickChunk(size){
      if(size >= 2147483648) return 8 * 1024 * 1024;   // ≥2GB   → 8MB
      if(size >= 536870912)  return 4 * 1024 * 1024;   // ≥512MB → 4MB
      if(size >= 67108864)   return 2 * 1024 * 1024;   // ≥64MB  → 2MB
      if(size >= 8388608)    return 1024 * 1024;       // ≥8MB   → 1MB
      if(size >= 1048576)    return 512 * 1024;        // ≥1MB   → 512KB
      return 256 * 1024;                               // 小文件 → 256KB
    }

    function fmtPct(a,b){ return Math.min(100, Math.round(a*100/b)) + "%"; }

    // 上传单个文件：先问服务端已收到多少字节，再按块 PUT，失败自动重试
    function uploadOne(f, done){
      var UPS = pickChunk(f.size);
      var offset = 0, tries = 0;

      function probe(cb){
        var xhr = new XMLHttpRequest();
        xhr.open("GET", "/__upload/" + encodeURIComponent(f.name) +
                 "?dir=" + encodeURIComponent(DIR) + "&total=" + f.size +
                 "&_=" + Date.now(), true);
        xhr.setRequestHeader("Authorization", auth());
        xhr.timeout = 20000;
        xhr.onload = function(){
          if(xhr.status === 200){
            try {
              var j = JSON.parse(xhr.responseText);
              cb(typeof j.size === "number" ? j.size : 0, !!j.done);
            } catch(e){ cb(0, false); }
          } else { cb(0, false); }
        };
        xhr.onerror = function(){ cb(0, false); };
        xhr.ontimeout = function(){ cb(0, false); };
        xhr.send();
      }

      function sendChunk(){
        var end = Math.min(offset + UPS, f.size);
        var blob = f.slice(offset, end);
        var xhr = new XMLHttpRequest();
        var url = "/__upload/" + encodeURIComponent(f.name) +
                  "?offset=" + offset + "&total=" + f.size +
                  "&dir=" + encodeURIComponent(DIR);
        xhr.open("PUT", url, true);
        xhr.setRequestHeader("Authorization", auth());
        xhr.timeout = Math.max(120000, Math.round(UPS / 1048576) * 40000); // 每 MB 留 40 秒
        xhr.onload = function(){
          if(xhr.status === 200){
            tries = 0;
            try {
              var j = JSON.parse(xhr.responseText);
              offset = (typeof j.size === "number") ? j.size : end;
            } catch(e){ offset = end; }
            if(j && j.done || offset >= f.size){
              msg("✓ " + f.name, "ok");
              done();
            } else {
              msg("上传中 " + f.name + " " + fmtPct(offset, f.size));
              sendChunk();
            }
          } else if(xhr.status === 409){
            // 服务端进度和本地不一致，按服务端实际大小续传
            tries = 0;
            try { offset = JSON.parse(xhr.responseText).size || 0; } catch(e){ offset = 0; }
            msg("续传中 " + f.name + " " + fmtPct(offset, f.size));
            sendChunk();
          } else if(xhr.status === 401){
            setAuth(""); msg("登录已过期，请重新登录", "err");
          } else {
            retry("失败(" + xhr.status + ")");
          }
        };
        xhr.onerror = function(){ retry("网络中断"); };
        xhr.ontimeout = function(){ retry("超时"); };
        xhr.send(blob);
      }

      function retry(why){
        if(tries++ < 8){
          msg(f.name + " 第" + tries + "次重试（" + why + "），从 " + fmtPct(offset, f.size) + " 继续…");
          setTimeout(sendChunk, 800);
        } else {
          msg("✗ " + f.name + " 重试次数用完，已传 " + fmtPct(offset, f.size) + "，重新选择该文件即可续传", "err");
        }
      }

      probe(function(start, already){
        if(already){ msg("✓ " + f.name + "（服务器上已有同大小文件，跳过）", "ok"); done(); return; }
        offset = start;
        if(offset > 0) msg("发现未传完的分片，从 " + fmtPct(offset, f.size) + " 继续：" + f.name);
        sendChunk();
      });
    }

    (function next(){
      if(i >= files.length){ msg("全部上传完成 ✓","ok"); setTimeout(function(){location.reload();},700); return; }
      uploadOne(files[i++], next);
    })();
  });

  document.addEventListener("click", function(e){
    var b = e.target.closest(".del"); if(!b) return;
    var name = b.getAttribute("data-name");
    if(!confirm("确定删除 " + name + " ?")) return;
    fetch("/__delete/" + encodeURIComponent(name) + "?dir=" + encodeURIComponent(DIR), {method:"DELETE", headers:{"Authorization":auth()}})
      .then(function(r){
        if(r.ok){ b.closest("tr").remove(); return; }
        return r.json().then(function(j){ alert("删除失败：" + (j.error || r.status)); }).catch(function(){ alert("删除失败"); });
      })
      .catch(function(){ alert("网络错误"); });
  });

  // ---- 新建文件夹 ----
  function postJSON(url, obj){
    return fetch(url, {
      method: "POST",
      headers: {"Authorization": auth(), "Content-Type": "application/json"},
      body: JSON.stringify(obj)
    }).then(function(r){ return r.json().then(function(j){ return {ok:r.ok, j:j}; }); });
  }

  $("mkbtn").addEventListener("click", function(){
    $("mkRow").hidden = false;
    $("mkname").value = "";
    $("mkname").focus();
    msg("输入文件夹名后点创建");
  });
  $("mkno").addEventListener("click", function(){
    $("mkRow").hidden = true; $("mkname").value = "";
  });

  function createDir(){
    var v = $("mkname").value.trim();
    if(!v){ msg("请输入文件夹名", "err"); return; }
    msg("创建中…");
    postJSON("/__mkdir", {dir: DIR, name: v}).then(function(x){
      if(x.ok){ msg("已创建文件夹 " + x.j.name, "ok"); setTimeout(function(){location.reload();}, 400); }
      else { msg("创建失败：" + (x.j.error || "未知错误"), "err"); }
    }).catch(function(){ msg("网络错误", "err"); });
  }
  $("mkok").addEventListener("click", createDir);
  $("mkname").addEventListener("keydown", function(ev){
    if(ev.key === "Enter"){ ev.preventDefault(); createDir(); }
    if(ev.key === "Escape"){ $("mkRow").hidden = true; }
  });

  // ---- 改名（文件和文件夹都支持）----
  var ESCBOX = document.createElement("div");
  function esc(s){ ESCBOX.textContent = s; return ESCBOX.innerHTML; }
  var editing = null;   // 当前正在编辑的那一格，避免多个输入框叠着

  document.addEventListener("click", function(e){
    var b = e.target.closest(".ren"); if(!b) return;
    e.preventDefault();
    var tr = b.closest("tr");
    var cell = tr.querySelector("td:first-child");
    if(!cell || cell.querySelector("input")) return;
    if(editing){ editing.cancel(); }

    var oldName = b.getAttribute("data-name");
    var oldHTML = cell.innerHTML;
    var oldLink = cell.querySelector("a");
    var wasDir = oldLink ? /\/$/.test(oldLink.getAttribute("href") || "") : false;

    cell.textContent = "";
    var box = document.createElement("div");
    box.className = "renbox";
    var inp = document.createElement("input");
    inp.className = "renin";
    inp.value = oldName;
    var okb = document.createElement("button");
    okb.type = "button"; okb.className = "rok"; okb.textContent = "✓"; okb.title = "保存";
    var nob = document.createElement("button");
    nob.type = "button"; nob.className = "rno"; nob.textContent = "✗"; nob.title = "取消";
    box.appendChild(inp); box.appendChild(okb); box.appendChild(nob);
    cell.appendChild(box);

    var done = false;
    function finish(){ done = true; if(editing && editing.cell === cell) editing = null; }
    function cancel(){ if(done) return; finish(); cell.innerHTML = oldHTML; }
    function commit(){
      if(done) return;
      var v = inp.value.trim();
      if(!v || v === oldName){ cancel(); return; }   // 没改动就还原，不刷新
      finish();
      msg("改名中…");
      postJSON("/__rename", {dir: DIR, name: oldName, to: v}).then(function(x){
        if(x.ok){
          var nm = x.j.name || v;
          var href = encodeURIComponent(nm) + (wasDir ? "/" : "");
          cell.innerHTML = '<a href="' + href + '">' + esc(nm) + (wasDir ? "/" : "") + '</a>';
          Array.prototype.forEach.call(tr.querySelectorAll("button[data-name]"), function(btn){
            btn.setAttribute("data-name", nm);
          });
          msg("已改名为 " + nm, "ok");
        } else {
          cell.innerHTML = oldHTML;
          msg("改名失败：" + (x.j.error || "未知错误"), "err");
        }
      }).catch(function(){ cell.innerHTML = oldHTML; msg("网络错误", "err"); });
    }
    editing = {cell: cell, cancel: cancel};

    // 稍等一拍再聚焦：移动端刚插入就 focus 容易立刻失焦
    setTimeout(function(){ try { inp.focus(); inp.select(); } catch(err){} }, 60);

    okb.addEventListener("click", function(ev){ ev.preventDefault(); commit(); });
    nob.addEventListener("click", function(ev){ ev.preventDefault(); cancel(); });
    inp.addEventListener("keydown", function(ev){
      if(ev.key === "Enter"){ ev.preventDefault(); commit(); }
      else if(ev.key === "Escape"){ ev.preventDefault(); cancel(); }
    });
    // 失焦不还原：值改了就顺手保存，没改就保持编辑状态，用户可再点回来继续输入
    inp.addEventListener("blur", function(){
      setTimeout(function(){
        if(done) return;
        if(inp.value.trim() && inp.value.trim() !== oldName) commit();
      }, 220);
    });
  });

  bindLogin();
  check();
})();
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "GeekHubCloud/2.0"
    protocol_version = "HTTP/1.1"

    # ---------- 基础 ----------
    def log_message(self, fmt, *args):
        sys.stdout.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))
        sys.stdout.flush()

    def _client_ip(self):
        # 经 Cloudflare 隧道进来时，直连地址都是 127.0.0.1，优先取真实访客 IP
        for h in ("CF-Connecting-IP", "X-Real-IP", "X-Forwarded-For"):
            v = self.headers.get(h)
            if v:
                return v.split(",")[0].strip()
        return self.client_address[0]

    def _lock_remain(self, ip):
        with FAIL_LOCK:
            st = AUTH_STATE.get(ip)
            if not st:
                return None
            left = st["lock_until"] - time.time()
            return int(left) + 1 if left > 0 else None

    def _record_fail(self, ip):
        with FAIL_LOCK:
            st = AUTH_STATE.setdefault(ip, {"fails": 0, "lock_until": 0})
            st["fails"] += 1
            dur = _lock_duration(st["fails"])
            if dur:
                st["lock_until"] = time.time() + dur
            return dur

    def _check_admin(self):
        """返回 (ok, locked)：locked 非 None 表示该 IP 处于锁定中（值为剩余秒数）"""
        if not ADMIN_USER:
            return False, None
        head = self.headers.get("Authorization", "")
        ip = self._client_ip()

        remain = self._lock_remain(ip)
        if remain is not None:
            return False, remain

        if not head.startswith("Basic "):
            return False, None  # 匿名访问不算爆破尝试

        try:
            raw = base64.b64decode(head[6:]).decode("utf-8", "replace")
        except Exception:
            raw = None
        want = ("%s:%s" % (ADMIN_USER, ADMIN_PASS)).encode("utf-8")
        if raw is not None and hmac.compare_digest(raw.encode("utf-8", "replace"), want):
            with FAIL_LOCK:
                AUTH_STATE.pop(ip, None)  # 成功登录：清零
            return True, None

        self._record_fail(ip)
        time.sleep(FAIL_DELAY)  # 每次失败强制等 2 秒
        return False, None

    def _json(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _safe_path(self):
        path = urllib.parse.unquote(urllib.parse.urlparse(self.path).path)
        path = os.path.normpath(path).lstrip("/")
        full = os.path.abspath(os.path.join(ROOT, path))
        if not (full == os.path.abspath(ROOT) or full.startswith(os.path.abspath(ROOT) + os.sep)):
            return None
        return full

    def _resolve(self, rel):
        """把「相对 ROOT 的目录路径」解析成绝对路径，越界返回 None"""
        if not isinstance(rel, str):
            return None
        rel = rel.strip().strip("/")
        root = os.path.abspath(ROOT)
        if rel in ("", "."):
            return root
        full = os.path.abspath(os.path.join(root, rel))
        if full == root or full.startswith(root + os.sep):
            return full
        return None

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

    # ---------- 目录列表 ----------
    def _listing(self, full):
        try:
            entries = sorted(os.scandir(full), key=lambda e: (not e.is_dir(), e.name.lower()))
        except OSError as e:
            self._error(500, "无法读取目录：%s" % e)
            return

        rows = []
        if os.path.abspath(full) != os.path.abspath(ROOT):
            rows.append('<tr><td><a href="../">⬆️ 返回上级</a></td><td>—</td><td>—</td><td class="op"></td></tr>')

        for e in entries:
            # 上传中的临时分片与元数据不对外展示
            if e.name.endswith(".part") or e.name.endswith(".part.total"):
                continue
            try:
                st = e.stat()
            except OSError:
                continue
            name = e.name + ("/" if e.is_dir() else "")
            href = urllib.parse.quote(e.name) + ("/" if e.is_dir() else "")
            size = "目录" if e.is_dir() else human(st.st_size)
            mtime = time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))
            ename = html.escape(e.name, quote=True)
            op = ('<td class="op">'
                  '<button class="ren" data-name="%s" title="改名">改名</button>'
                  '<button class="del" data-name="%s" title="删除">删除</button>'
                  '</td>' % (ename, ename))
            rows.append('<tr><td><a href="%s">%s</a></td><td>%s</td><td>%s</td>%s</tr>'
                        % (href, html.escape(name), size, mtime, op))

        rel = os.path.relpath(full, ROOT)
        title = "Geek-Hub 云盘" if rel == "." else "/" + rel.replace(os.sep, "/")

        head = ('<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1">'
                '<meta name="robots" content="noindex,nofollow">'
                '<title>' + html.escape(title) + '</title><style>' + CSS + '</style></head><body><div class="wrap">'
                '<h1>' + html.escape(title) + '</h1>'
                '<div class="admin">'
                '<span id="loginRow"><input id="u" placeholder="用户名" autocomplete="username">'
                '<input id="p" type="password" placeholder="密码" autocomplete="current-password">'
                '<button id="login">登录</button></span>'
                '<span id="upRow" hidden><label class="pick">选择文件<input type="file" id="f" multiple></label>'
                '<button class="ghost" id="mkbtn">新建文件夹</button>'
                '<button class="ghost" id="logout">退出</button>'
                '<span id="mkRow" hidden><input id="mkname" placeholder="文件夹名（可含中文）">'
                '<button id="mkok">创建</button><button class="ghost" id="mkno">取消</button></span></span>'
                '<span class="msg" id="msg">管理员登录后可上传</span>'
                '</div>'
                '<table><thead><tr><th>名称</th><th>大小</th><th>修改时间</th><th class="op">操作</th></tr></thead><tbody>')

        tail = ('</tbody></table>'
                '<div class="legal">'
                '<section><h3>免责声明</h3><div class="body">'
                '<p>本站为个人非商业性质的技术分享站点，所有文件均来源于网络公开渠道或站长自制，'
                '仅供个人学习、研究与怀旧交流，<b>不得用于任何商业用途</b>。</p>'
                '<p>本站不对文件的安全性、完整性与可用性作任何担保。第三方软件、系统镜像及其附带工具'
                '可能存在争议，是否下载与使用由访客自行判断，由此产生的一切后果由使用者自行承担。</p>'
                '<p>若本站内容侵犯了您的合法权益，请邮件联系 '
                '<a href="mailto:ineedhelpofweb@163.com">ineedhelpofweb@163.com</a>，'
                '核实后将于第一时间删除相关内容。</p>'
                '</div></section>'
                '<section><h3>原创声明</h3><div class="body">'
                '<p>本站的页面设计、前端代码与原创文字（除特别标注外）均为站长原创，著作权归站长所有，'
                '受《中华人民共和国著作权法》保护，<b>严禁非法倒卖、洗稿搬运</b>；'
                '转载请注明出处并保留原文链接。</p>'
                '<p>本站收录的第三方软件、系统镜像、商标及相关素材，其著作权与商标权均归各自权利人所有，'
                '本站仅作存档、说明与分享，不构成任何授权关系。</p>'
                '</div></section>'
                '</div>'
                '<p class="foot">Geek-Hub · 云盘 · ' + time.strftime("%Y-%m-%d %H:%M") + '</p>'
                '</div><script>var DIR = ' + json.dumps("" if rel == "." else rel.replace(os.sep, "/"))
                + ';</script><script>' + JS + '</script></body></html>')

        data = (head + "\n".join(rows) + tail).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    # ---------- 下载 ----------
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
                    elif e:
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

    # ---------- GET / HEAD ----------
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path

        if path == WHOAMI:
            ok, locked = self._check_admin()
            if ok:
                self._json(200, {"ok": True, "user": ADMIN_USER})
            elif locked is not None:
                self._json(429, {"ok": False, "locked": True, "remain": locked})
            else:
                self._json(401, {"ok": False})
            return

        if path.startswith(UPLOAD_PREFIX):
            self._upload_probe(path)
            return

        full = self._safe_path()
        if full is None:
            self._error(403, "路径不合法")
            return

        if os.path.isdir(full):
            if not path.endswith("/"):
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

    # ---------- POST：新建文件夹 / 改名（管理员） ----------
    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path not in (MKDIR, RENAME):
            self._json(404, {"ok": False, "error": "unknown endpoint"})
            return

        ok, locked = self._check_admin()
        if not ok:
            if locked is not None:
                self._json(429, {"ok": False, "error": "locked", "remain": locked})
            else:
                self._json(401, {"ok": False, "error": "auth required"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 8192:
            self._json(400, {"ok": False, "error": "bad body"})
            return
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8", "replace") or "{}")
        except Exception:
            self._json(400, {"ok": False, "error": "bad json"})
            return

        base = self._resolve(data.get("dir", ""))
        if base is None or not os.path.isdir(base):
            self._json(400, {"ok": False, "error": "目录不存在"})
            return

        name = clean_name(data.get("name"))
        if name is None:
            self._json(400, {"ok": False, "error": "名字不合法"})
            return

        if path == MKDIR:
            target = os.path.join(base, name)
            if os.path.exists(target):
                self._json(409, {"ok": False, "error": "同名文件或文件夹已存在"})
                return
            try:
                os.mkdir(target)
            except OSError as e:
                self._json(500, {"ok": False, "error": str(e)})
                return
            self.log_message("MKDIR %s/%s", data.get("dir", ""), name)
            self._json(200, {"ok": True, "name": name})
            return

        # RENAME
        new = clean_name(data.get("to"))
        if new is None:
            self._json(400, {"ok": False, "error": "新名字不合法"})
            return
        src = os.path.join(base, name)
        dst = os.path.join(base, new)
        if not os.path.exists(src):
            self._json(404, {"ok": False, "error": "原文件不存在"})
            return
        if os.path.exists(dst):
            self._json(409, {"ok": False, "error": "新名字已被占用"})
            return
        try:
            os.rename(src, dst)
        except OSError as e:
            self._json(500, {"ok": False, "error": str(e)})
            return
        self.log_message("RENAME %s/%s -> %s", data.get("dir", ""), name, new)
        self._json(200, {"ok": True, "name": new})

    # ---------- PUT 上传（管理员） ----------
    # ---------- 上传进度探测（管理员） ----------
    def _upload_probe(self, path):
        """GET /__upload/<name>?dir=<rel>&total=<size>
        返回服务端已落盘的字节数，供前端刷新后从断点继续。
        """
        ok, locked = self._check_admin()
        if not ok:
            if locked is not None:
                self._json(429, {"ok": False, "error": "locked", "remain": locked})
            else:
                self._json(401, {"ok": False, "error": "auth required"})
            return

        name = os.path.basename(urllib.parse.unquote(path[len(UPLOAD_PREFIX):]))
        if not name or name in (".", "..") or "/" in name or "\\" in name:
            self._json(400, {"ok": False, "error": "bad name"})
            return

        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        try:
            total = int(qs.get("total", ["0"])[0])
        except ValueError:
            total = 0

        base = self._resolve(qs.get("dir", [""])[0])
        if base is None or not os.path.isdir(base):
            self._json(400, {"ok": False, "error": "目录不存在"})
            return
        dest = os.path.join(base, name)
        part = dest + ".part"
        meta = part + ".total"

        # 目标文件已存在且大小一致：直接视为已完成，避免重复上传
        if os.path.isfile(dest) and total > 0 and os.path.getsize(dest) == total:
            self._json(200, {"ok": True, "size": total, "total": total, "done": True})
            return

        if not os.path.exists(part):
            self._json(200, {"ok": True, "size": 0, "total": total, "done": False})
            return

        # 同名但不同大小的新文件：旧分片作废
        old_total = 0
        try:
            with open(meta, "r", encoding="utf-8") as f:
                old_total = int(f.read().strip() or "0")
        except (OSError, ValueError):
            old_total = 0
        if total and old_total and old_total != total:
            for p in (part, meta):
                try:
                    os.remove(p)
                except OSError:
                    pass
            self._json(200, {"ok": True, "size": 0, "total": total, "done": False})
            return

        size = os.path.getsize(part)
        if total and size >= total:
            # 分片已满但没改名（异常中断在收尾），让客户端从 0 重来
            for p in (part, meta):
                try:
                    os.remove(p)
                except OSError:
                    pass
            size = 0
        self._json(200, {"ok": True, "size": size, "total": total, "done": False})

    def do_PUT(self):
        path = urllib.parse.unquote(urllib.parse.urlparse(self.path).path)
        if not path.startswith(UPLOAD_PREFIX):
            self._json(404, {"ok": False, "error": "use /__upload/<name>"})
            return
        ok, locked = self._check_admin()
        if not ok:
            if locked is not None:
                self._json(429, {"ok": False, "error": "locked", "remain": locked})
            else:
                self._json(401, {"ok": False, "error": "auth required"})
            return

        name = os.path.basename(path[len(UPLOAD_PREFIX):])
        if not name or name in (".", "..") or "/" in name or "\\" in name:
            self._json(400, {"ok": False, "error": "bad name"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0:
            self._json(400, {"ok": False, "error": "empty body"})
            return

        # ---- 分块断点续传协议 ----
        # PUT /__upload/<name>?offset=N[&total=M]
        # offset=0 时重建 .part；之后按 offset 追加；带 total 且写满时自动改名
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        try:
            offset = int(qs.get("offset", ["0"])[0])
        except ValueError:
            offset = 0
        total = None
        if "total" in qs:
            try:
                total = int(qs["total"][0])
            except ValueError:
                total = None
        if offset < 0:
            self._json(400, {"ok": False, "error": "bad offset"})
            return

        base = self._resolve(qs.get("dir", [""])[0])
        if base is None or not os.path.isdir(base):
            self._json(400, {"ok": False, "error": "目录不存在"})
            return
        dest = os.path.join(base, name)
        part = dest + ".part"
        meta = part + ".total"

        def _safe_send(code, obj):
            try:
                self._json(code, obj)
            except (BrokenPipeError, ConnectionResetError):
                pass

        if offset == 0:
            with open(part, "wb") as f:
                pass
            if total is not None:
                try:
                    with open(meta, "w", encoding="utf-8") as mf:
                        mf.write(str(total))
                except OSError:
                    pass
        else:
            if not os.path.exists(part):
                self._json(409, {"ok": False, "error": "no part, restart", "size": 0})
                return
            cur = os.path.getsize(part)
            if cur != offset:
                # 服务端进度与客户端不一致，让客户端按服务端实际大小续传
                self._json(409, {"ok": False, "error": "offset mismatch", "size": cur})
                return

        got = 0
        try:
            with open(part, "ab") as f:
                while got < length:
                    chunk = self.rfile.read(min(CHUNK, length - got))
                    if not chunk:
                        break
                    f.write(chunk)
                    f.flush()
                    got += len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            self.log_message("UPLOAD %s chunk aborted at +%d (client disconnected)", name, got)
            return
        except OSError as e:
            _safe_send(500, {"ok": False, "error": str(e)})
            return

        size = offset + got
        if got != length:
            # 本块不完整：回滚到块起点，客户端重试同一 offset
            try:
                with open(part, "r+b") as f:
                    f.truncate(offset)
            except OSError:
                pass
            self.log_message("UPLOAD %s chunk incomplete at +%d/%d", name, got, length)
            _safe_send(400, {"ok": False, "error": "incomplete", "size": offset})
            return

        if total is not None and size >= total:
            if size != total:
                # 超出预期总长，文件异常，作废重来
                for p in (part, meta):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
                _safe_send(400, {"ok": False, "error": "size mismatch, restart"})
                return
            try:
                shutil.move(part, dest)
            except OSError as e:
                _safe_send(500, {"ok": False, "error": str(e)})
                return
            try:
                os.remove(meta)
            except OSError:
                pass
            self.log_message("UPLOAD %s complete (%d bytes)", name, size)
            _safe_send(200, {"ok": True, "name": name, "size": size, "done": True})
        else:
            _safe_send(200, {"ok": True, "size": size, "done": False})

    # ---------- DELETE（管理员） ----------
    def do_DELETE(self):
        path = urllib.parse.unquote(urllib.parse.urlparse(self.path).path)
        if not path.startswith(DELETE_PREFIX):
            self._json(404, {"ok": False, "error": "use /__delete/<name>"})
            return
        ok, locked = self._check_admin()
        if not ok:
            if locked is not None:
                self._json(429, {"ok": False, "error": "locked", "remain": locked})
            else:
                self._json(401, {"ok": False, "error": "auth required"})
            return

        name = os.path.basename(path[len(DELETE_PREFIX):])
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        base = self._resolve(qs.get("dir", [""])[0])
        if base is None or not os.path.isdir(base):
            self._json(400, {"ok": False, "error": "目录不存在"})
            return
        target = os.path.join(base, name)

        if os.path.isfile(target):
            try:
                os.remove(target)
            except OSError as e:
                self._json(500, {"ok": False, "error": str(e)})
                return
            # 顺带清掉可能残留的未完成分片
            for p in (target + ".part", target + ".part.total"):
                try:
                    os.remove(p)
                except OSError:
                    pass
            self.log_message("DELETE %s", name)
            self._json(200, {"ok": True})
        elif os.path.isdir(target):
            try:
                os.rmdir(target)  # 只能删空文件夹
            except OSError:
                self._json(409, {"ok": False, "error": "文件夹非空，请先清空"})
                return
            self.log_message("RMDIR %s", name)
            self._json(200, {"ok": True})
        else:
            self._json(404, {"ok": False, "error": "not found"})


def load_admin(args):
    global ADMIN_USER, ADMIN_PASS
    raw = ""
    if args.admin:
        raw = args.admin
    elif os.environ.get("ADMIN_AUTH"):
        raw = os.environ["ADMIN_AUTH"]
    else:
        path = os.environ.get("ADMIN_FILE", "/root/.gh_admin")
        if os.path.isfile(path):
            with open(path) as f:
                raw = f.read().strip()
    if raw and ":" in raw:
        ADMIN_USER, ADMIN_PASS = raw.split(":", 1)


def main():
    global ROOT
    ap = argparse.ArgumentParser(description="Geek-Hub 云盘服务")
    ap.add_argument("-p", "--port", type=int, default=8080)
    ap.add_argument("-d", "--dir", default=".")
    ap.add_argument("-b", "--bind", default="0.0.0.0")
    ap.add_argument("-u", "--auth", help="全站访问密码 user:pass（不推荐，会挡住下载）")
    ap.add_argument("--admin", help="管理员账号 user:pass，用于上传/删除")
    args = ap.parse_args()

    ROOT = os.path.abspath(args.dir)
    if not os.path.isdir(ROOT):
        print("目录不存在：%s" % ROOT)
        sys.exit(1)
    load_admin(args)

    srv = ThreadingHTTPServer((args.bind, args.port), Handler)
    srv.daemon_threads = True
    print("Geek-Hub 云盘已启动")
    print("  目录：%s" % ROOT)
    print("  地址：http://%s:%d/" % (args.bind, args.port))
    print("  读权限：公开")
    print("  写权限：%s" % ("管理员（%s）" % ADMIN_USER if ADMIN_USER else "未配置（禁用上传）"))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
