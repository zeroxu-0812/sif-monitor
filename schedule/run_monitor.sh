#!/bin/bash
#
# ABA 周报监控启动脚本（launchd 调用入口）
#
# 部署到新机器时：只需修改下面的 PROJECT_DIR 即可。
# 日志输出到 ~/Library/Logs/aba-trends/monitor.{out,err}.log
#

set -euo pipefail

# ============================================================================
# 部署配置（按实际路径修改）
# ============================================================================

PROJECT_DIR="/Users/ethan/ABA_Trends"
GRANULARITY="${1:-week}"

# ============================================================================
# 以下内容通常无需修改
# ============================================================================

LOG_DIR="$HOME/Library/Logs/aba-trends"
mkdir -p "$LOG_DIR"

OUT_LOG="$LOG_DIR/monitor.out.log"
ERR_LOG="$LOG_DIR/monitor.err.log"

# launchd 默认 PATH 极简，这里显式覆盖
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export LANG=en_US.UTF-8
export LC_ALL=en_US.UTF-8

# 选择 python3：优先 homebrew，其次系统
if [[ -x "/opt/homebrew/bin/python3" ]]; then
    PYTHON="/opt/homebrew/bin/python3"
elif [[ -x "/usr/local/bin/python3" ]]; then
    PYTHON="/usr/local/bin/python3"
else
    PYTHON="$(command -v python3)"
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] start monitor: granularity=$GRANULARITY python=$PYTHON" >> "$OUT_LOG"

cd "$PROJECT_DIR"

# 依赖检查
if [[ ! -f ".env" ]]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] FAIL: .env 不存在，请参考 .env.example 创建" >> "$ERR_LOG"
    exit 2
fi

# 跑监控（捕获异常退出码）
if ! "$PYTHON" "$PROJECT_DIR/monitor.py" --granularity "$GRANULARITY" >> "$OUT_LOG" 2>> "$ERR_LOG"; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] monitor.py exited non-zero" >> "$ERR_LOG"
    exit 1
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] done" >> "$OUT_LOG"
