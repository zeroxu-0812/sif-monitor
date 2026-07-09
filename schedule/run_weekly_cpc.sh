#!/bin/bash
#
# Sif cpc_median 周监控启动脚本（launchd 调用入口）
#
# 每周一 10:00 触发：读 ssFow6 的 B 列关键词，查 funnel cpc_median，
# 按当周 period 追加/覆盖对应周列。
#
# 部署到新机器时：只需修改下面的 PROJECT_DIR 即可。
# 日志输出到 ~/Library/Logs/aba-trends/weekly-cpc.{out,err}.log
#

set -euo pipefail

# ============================================================================
# 部署配置（按实际路径修改）
# ============================================================================

PROJECT_DIR="/Users/haiyi/Desktop/project/about-sif-main"

# ============================================================================
# 以下内容通常无需修改
# ============================================================================

LOG_DIR="$HOME/Library/Logs/aba-trends"
mkdir -p "$LOG_DIR"

OUT_LOG="$LOG_DIR/weekly-cpc.out.log"
ERR_LOG="$LOG_DIR/weekly-cpc.err.log"

# launchd 默认 PATH 极简，这里显式覆盖
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export LANG=en_US.UTF-8
export LC_ALL=en_US.UTF-8

# 项目依赖装在 .venv 里，必须用 venv 的 python（不是系统 python3）
PYTHON="$PROJECT_DIR/.venv/bin/python"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] start weekly-cpc monitor python=$PYTHON" >> "$OUT_LOG"

cd "$PROJECT_DIR"

# 依赖检查
if [[ ! -f ".env" ]]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] FAIL: .env 不存在，请参考 .env.example 创建" >> "$ERR_LOG"
    exit 2
fi
if [[ ! -x "$PYTHON" ]]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] FAIL: 找不到 venv python: $PYTHON" >> "$ERR_LOG"
    exit 2
fi

# 跑监控（捕获异常退出码）
if ! "$PYTHON" "$PROJECT_DIR/sif_weekly_cpc_monitor.py" >> "$OUT_LOG" 2>> "$ERR_LOG"; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] sif_weekly_cpc_monitor.py exited non-zero" >> "$ERR_LOG"
    exit 1
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] done" >> "$OUT_LOG"
