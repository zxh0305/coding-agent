#!/bin/bash
# 一键管理对外服务:后端 + Cloudflare 公网隧道
# 用法:
#   ./share.sh start   启动(已在跑的部分自动跳过,不会重复启动)
#   ./share.sh restart 只重启后端(保留隧道,公网网址不变)
#   ./share.sh stop    全部停止
#   ./share.sh status  看状态和当前公网网址
#   ./share.sh url     只打印公网网址
cd "$(dirname "$0")"

WEB_LOG=/tmp/agent_web.log
TUNNEL_LOG=/tmp/tunnel.log

get_url() {
  grep -o "https://[a-z0-9-]*\.trycloudflare\.com" "$TUNNEL_LOG" 2>/dev/null | tail -1
}

case "$1" in
  start)
    if pgrep -f "backend/app.py" > /dev/null; then
      echo "✔ 应用服务已在运行,跳过"
    else
      nohup python3 backend/app.py > "$WEB_LOG" 2>&1 &
      echo "✔ 应用服务已启动 (PID $!)"
    fi
    if pgrep -f "cloudflared tunnel" > /dev/null; then
      echo "✔ 隧道已在运行,跳过"
    else
      : > "$TUNNEL_LOG"   # 清掉旧内容,保证取到的是本次的网址
      nohup cloudflared tunnel --url http://localhost:8000 > "$TUNNEL_LOG" 2>&1 &
      echo "… 隧道启动中,等 Cloudflare 分配网址"
      for i in $(seq 1 15); do
        sleep 1
        grep -q "trycloudflare.com" "$TUNNEL_LOG" && break
      done
    fi
    URL=$(get_url)
    if [ -n "$URL" ]; then
      echo "🌐 公网地址: $URL"
    else
      echo "✗ 15 秒内没拿到网址,排查: tail -20 $TUNNEL_LOG"
    fi
    ;;
  restart)   # 只重启后端;隧道不动,公网网址保持不变
    pkill -f "backend/app.py" && echo "✔ 旧后端已停止" || echo "后端本来就没在跑"
    sleep 1
    nohup python3 backend/app.py > "$WEB_LOG" 2>&1 &
    echo "✔ 后端已重启 (PID $!)"
    URL=$(get_url)
    [ -n "$URL" ] && echo "🌐 公网地址不变: $URL"
    ;;
  stop)
    pkill -f "cloudflared tunnel" && echo "✔ 隧道已停止" || echo "隧道本来就没在跑"
    pkill -f "backend/app.py" && echo "✔ 应用服务已停止" || echo "应用服务本来就没在跑"
    ;;
  status)
    pgrep -fl "backend/app.py" > /dev/null && echo "✔ 应用服务在跑" || echo "✗ 应用服务没在跑"
    pgrep -fl "cloudflared tunnel" > /dev/null && echo "✔ 隧道在跑" || echo "✗ 隧道没在跑"
    URL=$(get_url)
    [ -n "$URL" ] && echo "🌐 公网地址: $URL"
    ;;
  url)
    get_url
    ;;
  *)
    echo "用法: $0 {start|stop|status|url}"
    ;;
esac
