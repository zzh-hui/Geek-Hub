#!/bin/sh
# ============================================================
# Geek-Hub 手机服务器 · 一键启动脚本（iSH / Alpine）
#
# 用法：
#   sh start.sh /var/www/iso            # 分享 /var/www/iso 目录
#   sh start.sh /var/www/iso myname     # 指定固定子域名（myname.serveo.net）
#
# 说明：
#   - 会在 tmux 里开两个窗口：web（文件服务）+ tun（隧道）
#   - 隧道断线自动重连，不会因为网络切换而彻底挂掉
#   - 日志：/tmp/gh-web.log、/tmp/gh-tun.log
# ============================================================

DIR="${1:-.}"
SUB="${2:-}"
PORT="${PORT:-8080}"
AUTH="${AUTH:-}"   # 例如 AUTH=user:pass sh start.sh /var/www/iso
SERVE_DIR="$(cd "$DIR" 2>/dev/null && pwd)" || { echo "目录不存在：$DIR"; exit 1; }

# 依赖检查
command -v python3 >/dev/null 2>&1 || { echo "缺少 python3：apk add python3"; exit 1; }
command -v ssh >/dev/null 2>&1 || { echo "缺少 ssh：apk add openssh-client"; exit 1; }

command -v tmux >/dev/null 2>&1 || { echo "缺少 tmux：apk add tmux"; exit 1; }

# 端口占用检查
if command -v netstat >/dev/null 2>&1 && netstat -lnt 2>/dev/null | grep -q ":$PORT "; then
    echo "端口 $PORT 已被占用，请先停掉旧进程：killall python3"
    exit 1
fi

SESSION="gh"
tmux kill-session -t "$SESSION" 2>/dev/null

# ---------- 窗口 1：文件服务 ----------
tmux new-session -d -s "$SESSION" -n web \
    "cd '$SERVE_DIR' && python3 /root/serve.py -p $PORT -d '$SERVE_DIR' ${AUTH:+-u $AUTH} 2>&1 | tee /tmp/gh-web.log"

# ---------- 窗口 2：SSH 隧道（自动重连） ----------
if [ -n "$SUB" ]; then
    # serveo：支持自定义子域名，地址稳定为 https://<sub>.serveo.net
    TUN="ssh -o StrictHostKeyChecking=no -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
-R $SUB:80:localhost:$PORT serveo.net"
else
    # localhost.run：随机地址，无需注册
    TUN="ssh -o StrictHostKeyChecking=no -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
-R 80:localhost:$PORT nokey@localhost.run"
fi

tmux new-window -t "$SESSION" -n tun \
    "while true; do echo '--- 隧道连接中 ---'; $TUN 2>&1 | tee -a /tmp/gh-tun.log; echo '--- 断开，5 秒后重连 ---'; sleep 5; done"

tmux select-window -t "$SESSION:web"

echo ""
echo "================ 已启动 ================"
echo " 本地目录：$SERVE_DIR"
echo " 本地端口：http://127.0.0.1:$PORT/"
echo ""
echo " 看文件服务日志：tmux attach -t gh    （Ctrl+B 再按 0/1 切窗口，Ctrl+B 再按 D 退出）"
echo " 取公网地址：    tail -20 /tmp/gh-tun.log"
[ -n "$SUB" ] && echo " 固定地址：      https://$SUB.serveo.net"
echo "========================================"
echo ""
sleep 6
echo "--- 隧道日志 ---"
tail -15 /tmp/gh-tun.log 2>/dev/null
