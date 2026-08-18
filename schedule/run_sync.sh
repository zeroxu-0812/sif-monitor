#!/bin/bash
#
# Sif → 飞书 排名数据同步启动脚本（launchd 调用入口，每周三 09:00）
#
# 从飞书关键词表读词 → 批量抓 Sif 周/月趋势 → 写回飞书趋势表。
# 日志输出到 ~/Library/Logs/aba-trends/sync.{out,err}.log
#

set -euo pipefail

PROJECT_DIR="/Users/haiyi/Desktop/project/about-sif-main"

LOG_DIR="$HOME/Library/Logs/aba-trends"
mkdir -p "$LOG_DIR"
OUT_LOG="$LOG_DIR/sync.out.log"
ERR_LOG="$LOG_DIR/sync.err.log"

# launchd 默认 PATH 极简，显式覆盖
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export LANG=en_US.UTF-8
export LC_ALL=en_US.UTF-8

# 选择 python：优先项目 .venv（依赖齐全），其次 homebrew，再系统
if [[ -x "$PROJECT_DIR/.venv/bin/python" ]]; then
    PYTHON="$PROJECT_DIR/.venv/bin/python"
elif [[ -x "/opt/homebrew/bin/python3" ]]; then
    PYTHON="/opt/homebrew/bin/python3"
elif [[ -x "/usr/local/bin/python3" ]]; then
    PYTHON="/usr/local/bin/python3"
else
    PYTHON="$(command -v python3)"
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] start sync python=$PYTHON" >> "$OUT_LOG"

cd "$PROJECT_DIR"

if [[ ! -f ".env" ]]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] FAIL: .env 不存在，请参考 .env.example 创建" >> "$ERR_LOG"
    exit 2
fi

if ! "$PYTHON" "$PROJECT_DIR/sif_feishu_sync.py" >> "$OUT_LOG" 2>> "$ERR_LOG"; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] sif_feishu_sync.py exited non-zero" >> "$ERR_LOG"
    exit 1
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] done" >> "$OUT_LOG"
