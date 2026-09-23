#!/bin/sh
# accio2api 保活守护 —— 与服务同目录，60 秒巡检一次，掉线自动重启。
#
# 什么场景需要它：
#   容器/PaaS 环境里，如果服务进程与 shell 同属一个进程组，
#   shell 一退出就可能被连带清理。用 setsid 脱离进程组的守护进程拉
#   服务，可避免这类「莫名掉线」。systemd 用户可直接用 systemd 单元，无需本脚本。
#
# 用法：
#   sh watchdog.sh start     # 启动守护（幂等）
#   sh watchdog.sh stop      # 停止守护与服务
#   sh watchdog.sh status    # 查看状态
#   sh watchdog.sh log       # 实时日志
#
# 配置：目录自动定位为脚本所在目录，端口默认 8808（可用 PORT 环境变量覆盖）

DIR=$(cd "$(dirname "$0")" && pwd)
PORT=${PORT:-8808}
LOG=$DIR/service.log
WLOG=$DIR/watchdog.log
PIDFILE=$DIR/.watchdog.pid
BIND=${BIND:-127.0.0.1}

alive() {
  curl -sS -m 4 "http://$BIND:$PORT/health" >/dev/null 2>&1
}

case "$1" in
  start)
    if [ -f "$PIDFILE" ] && kill -0 "$(cat $PIDFILE)" 2>/dev/null; then
      echo "守护已在运行 (pid $(cat $PIDFILE))"; exit 0
    fi
    echo "启动守护…"
    setsid sh -c "
      DIR='$DIR'; PORT='$PORT'; BIND='$BIND'
      LOG=\$DIR/service.log; WLOG=\$DIR/watchdog.log
      while true; do
        if ! curl -sS -m 4 \"http://\$BIND:\$PORT/health\" >/dev/null 2>&1; then
          echo \"[\$(date +%H:%M:%S)] 服务离线，重启\" >> \$WLOG
          ( cd \$DIR && set -a && . \$DIR/.env 2>/dev/null && set +a && \
            setsid python3 -m uvicorn app.main:app \
              --host \$BIND --port \$PORT >> \$LOG 2>&1 < /dev/null & )
          sleep 12
        fi
        sleep 60
      done
    " > /dev/null 2>&1 < /dev/null &
    echo $! > "$PIDFILE"
    sleep 15
    if alive; then echo "✅ 服务已就绪 :$PORT"; else echo "⏳ 守护已起，服务启动中…"; fi
    ;;
  stop)
    [ -f "$PIDFILE" ] && kill "$(cat $PIDFILE)" 2>/dev/null && rm -f "$PIDFILE"
    pkill -f "uvicorn app.main:app --host $BIND --port $PORT" 2>/dev/null
    echo "已停止"
    ;;
  status)
    if alive; then echo "✅ 服务在线 :$PORT"; else echo "❌ 服务离线"; fi
    [ -f "$PIDFILE" ] && echo "守护 pid: $(cat $PIDFILE)"
    echo "--- 最近日志 ---"; tail -6 "$WLOG" 2>/dev/null
    ;;
  log) tail -f "$LOG" ;;
  *) echo "用法: sh watchdog.sh {start|stop|status|log}" ;;
esac
