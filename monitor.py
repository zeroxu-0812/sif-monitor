#!/usr/bin/env python3
"""
ABA 排名上升关键词监控主入口。

流程：
  1. 从飞书表读取周/月度排名矩阵（复用 FeishuClient）
  2. 计算每个关键词的指标（rank_metrics）
  3. 分档筛选 Top N + 新晋词
  4. 构造飞书卡片并推送（feishu_messenger）

CLI:
  python monitor.py                          # 默认 weekly，真实推送
  python monitor.py --granularity month      # 月度
  python monitor.py --dry-run                # 打印卡片 JSON，不推送
  python monitor.py --limit T1               # 只跑指定档位（调试用）
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

# auth 在导入时会 load_dotenv（feishu_config 现也自带 load_dotenv，可独立 import）
import auth  # noqa: F401  - 副作用：加载 .env
import feishu_config
import monitor_config as cfg
from feishu_messenger import FeishuMessenger, build_monitor_card, send_failure_alert
from rank_metrics import (
    compute_keyword_metrics,
    select_top_per_tier,
    summarize_selection,
)
from sif_feishu_sync import FeishuClient

PROJECT_DIR = Path(__file__).resolve().parent


# ============================================================================
# 飞书表读取
# ============================================================================

def _read_rank_matrix(
    client: FeishuClient,
    spreadsheet_token: str,
    sheet_id: str,
) -> tuple[list[str], list[tuple[str, list]]]:
    """
    读取飞书表，返回 (dates, rows)。

    先查 sheet 实际行列数，构造精确范围，避免 A1:ZZ5000 超出飞书 10MB 限制。

    - dates: 表头日期字符串列表（去掉第一列"关键词"列）
    - rows: [(keyword, [rank_or_none, ...]), ...]

    表结构（来自 sif_feishu_sync.build_rank_matrix）：
      第一行: ["关键词", date1, date2, ...]
      后续行: [keyword, rank1, rank2, ...]
    """
    sheet_info = client.get_sheet_info(spreadsheet_token, sheet_id)
    grid = sheet_info.get("grid_properties", {})
    row_count = int(grid.get("row_count", 0))
    col_count = int(grid.get("column_count", 0))
    if row_count == 0 or col_count == 0:
        return [], []

    # 借用 sif_feishu_sync 的列名转换；预留若干空列避免边界漏数据
    from sif_feishu_sync import col_index_to_name
    last_col_letter = col_index_to_name(max(col_count, 2) - 1)
    read_range = f"A1:{last_col_letter}{row_count}"

    values = client.read_range(spreadsheet_token, sheet_id, read_range)
    if not values:
        return [], []

    header = values[0]
    if not header:
        return [], []

    # 飞书可能返回尾部多余的 None 列（grid 大于实际有效列），需要裁掉
    # 表头第 0 列是"关键词"，其余是日期；日期为 None/空 表示无效列
    effective_cols = 1  # 至少保留"关键词"列
    for cell in header[1:]:
        if cell is None:
            break
        text = str(cell).strip()
        if not text or text.lower() == "none":
            break
        effective_cols += 1

    header = header[:effective_cols]
    dates = [str(cell) for cell in header[1:]]

    rows: list[tuple[str, list]] = []
    for raw_row in values[1:]:
        if not raw_row:
            continue
        # 同样裁到有效列数
        raw_row = raw_row[:effective_cols]
        keyword = str(raw_row[0]).strip() if raw_row else ""
        if not keyword or keyword.lower() == "none":
            continue
        ranks = list(raw_row[1:])
        # 补齐长度，避免越界
        if len(ranks) < len(dates):
            ranks.extend([None] * (len(dates) - len(ranks)))
        rows.append((keyword, ranks))

    return dates, rows


def _load_active_keywords(client: FeishuClient, spreadsheet_token: str) -> set[str]:
    """读取关键词表第一列，返回现存关键词集合（小写）。

    monitor 只读趋势表；用户在关键词表删/改词后，趋势表里被删的词不会自动
    消失。用此集合过滤趋势表残留词，使"删词即生效"。趋势表历史数据保留不动。
    """
    sheet_id = feishu_config.KEYWORDS_SHEET_ID
    info = client.get_sheet_info(spreadsheet_token, sheet_id)
    row_count = int(info.get("grid_properties", {}).get("row_count", 0))
    if row_count == 0:
        return set()
    values = client.read_range(spreadsheet_token, sheet_id, f"A1:A{row_count}")
    keywords: set[str] = set()
    for row in values[1:]:  # 跳过表头
        if row and row[0] is not None:
            text = str(row[0]).strip().lower()
            if text:
                keywords.add(text)
    return keywords


# ============================================================================
# 数据范围计算
# ============================================================================

def _compute_date_range(dates: list[str]) -> dict:
    if not dates:
        return {"start": None, "end": None, "points": 0}
    return {
        "start": dates[0],
        "end": dates[-1],
        "points": len(dates),
    }


# ============================================================================
# 主流程
# ============================================================================

def run(
    *,
    granularity: str,
    dry_run: bool = False,
    limit_tier: str | None = None,
) -> dict:
    """
    执行监控流程。返回推送结果（dry_run 时为 {"dry_run": True, "card": ...}）。
    """
    if granularity == "week":
        sheet_id = feishu_config.TRENDS_SHEET_ID_WEEKLY
    elif granularity == "month":
        sheet_id = feishu_config.TRENDS_SHEET_ID_MONTHLY
    else:
        raise ValueError(f"granularity 只能是 week 或 month，收到: {granularity}")

    tiers_config = cfg.TIERS
    if limit_tier:
        tiers_config = [t for t in cfg.TIERS if t[0] == limit_tier]
        if not tiers_config:
            raise ValueError(
                f"--limit {limit_tier} 无效，可选: {[t[0] for t in cfg.TIERS]}"
            )

    print(f"[1/4] 读取飞书表: sheet_id={sheet_id} granularity={granularity}")
    client = FeishuClient(feishu_config.FEISHU_APP_ID, feishu_config.FEISHU_APP_SECRET)
    spreadsheet_token = client.get_spreadsheet_token(feishu_config.TRENDS_WIKI_TOKEN)
    dates, raw_rows = _read_rank_matrix(client, spreadsheet_token, sheet_id)
    # 用关键词表过滤：只保留关键词表里现存的词（趋势表里被删词的残留会被剔除）
    active_keywords = _load_active_keywords(client, spreadsheet_token)
    raw_rows = [
        (kw, ranks) for kw, ranks in raw_rows
        if kw.strip().lower() in active_keywords
    ]
    print(f"      读取到 {len(raw_rows)} 个关键词（关键词表现存 {len(active_keywords)} 个，已过滤残留），{len(dates)} 期数据")

    print("[2/4] 计算指标")
    metrics = [
        compute_keyword_metrics(
            keyword=keyword,
            ranks=ranks,
            tiers_config=cfg.TIERS,  # 档位判定用全量配置，与 limit 无关
            cagr_periods=cfg.CAGR_PERIODS,
        )
        for keyword, ranks in raw_rows
    ]

    print("[3/4] 分档筛选")
    selection = select_top_per_tier(metrics, tiers_config, cfg.NEW_KEYWORD_RANK_CAP)
    summary = summarize_selection(selection)
    for label, lo, hi, _ in tiers_config:
        count = len(selection["tiers"].get(label, []))
        print(f"      {label} ({lo:,}-{hi:,}): {count} 条")
    print(f"      新晋词: {summary['total_newcomers']} 条")

    print("[4/4] 构造卡片")
    date_range = _compute_date_range(dates)
    title = f"📈 ABA 关键词排名监控 · {granularity_label(granularity)}报"
    card = build_monitor_card(
        title=title,
        granularity=granularity,
        date_range=date_range,
        selection=selection,
        summary=summary,
        generated_at=datetime.now(),
    )

    if dry_run:
        print("\n=== DRY RUN: 卡片 JSON ===")
        print(json.dumps(card, ensure_ascii=False, indent=2))
        return {"dry_run": True, "card": card}

    print(f"推送到 chat_id={cfg.CHAT_ID}")
    messenger = FeishuMessenger(feishu_config.FEISHU_APP_ID, feishu_config.FEISHU_APP_SECRET)
    resp = messenger.send_card(cfg.CHAT_ID, card)
    print(f"✅ 推送成功: code={resp.get('code')} msg={resp.get('msg')}")
    return {"dry_run": False, "response": resp}


def granularity_label(granularity: str) -> str:
    return "周" if granularity == "week" else "月"


# ============================================================================
# CLI
# ============================================================================

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ABA 排名上升关键词监控 + 飞书推送")
    parser.add_argument(
        "--granularity",
        choices=("week", "month"),
        default=cfg.default_granularity(),
        help="粒度，默认 week",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印卡片 JSON，不实际推送",
    )
    parser.add_argument(
        "--limit",
        choices=[t[0] for t in cfg.TIERS],
        help="只跑指定档位（调试用，过滤显示的档位，不影响指标计算）",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        run(
            granularity=args.granularity,
            dry_run=args.dry_run,
            limit_tier=args.limit,
        )
        return 0
    except ValueError as exc:
        print(f"\n❌ 参数错误: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"\n❌ 执行失败: {exc}", file=sys.stderr)
        send_failure_alert(cfg.CHAT_ID, "monitor.py", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
