#!/usr/bin/env python3
"""
监控结果 → 飞书电子表格发布（编排层）。

把 select_top_per_tier 的结果写入一个飞书电子表格（每期一个新工作表），
并在群里发一张含「打开表格」按钮的卡片。

职责分工：
- 数据来源：rank_metrics.select_top_per_tier
- 表格读写：sif_feishu_sync.FeishuClient + overwrite_sheet_with_matrix
- 群消息：feishu_messenger.FeishuMessenger.send_card
- 本模块：state 管理 + 矩阵构造 + 发布编排 + 链接卡片
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import monitor_config as cfg
from feishu_messenger import _fmt_pct, _hr, _markdown_div
from sif_feishu_sync import overwrite_sheet_with_matrix

STATE_FILE = Path(__file__).resolve().parent / ".feishu_report_state.json"

REPORT_HEADER = [
    "档位", "序号", "关键词", "本期rank", "上期rank",
    "3期前rank", "WoW", "CAGR", "跨档跃迁", "是否新词",
]


# ============================================================================
# 工具
# ============================================================================

def granularity_label(granularity: str) -> str:
    return "周" if granularity == "week" else "月"


def _fmt_rank_int(value) -> str:
    """表格专用：None → ""（比卡片的 "-" 更干净），数字 → str(int)。"""
    if value is None:
        return ""
    try:
        return str(int(value))
    except (TypeError, ValueError):
        return ""


def _sheet_title_for(granularity: str, generated_at: datetime) -> str:
    if granularity == "month":
        return f"月报_{generated_at.strftime('%Y-%m')}"
    return f"周报_{generated_at.strftime('%Y-%m-%d')}"


def _report_url(spreadsheet_url: str, sheet_id: str) -> str:
    """把表格根 URL 拼上 ?sheet= 定位到指定工作表。"""
    if not spreadsheet_url:
        return f"https://feishu.cn/sheets/?sheet={sheet_id}"
    sep = "&" if "?" in spreadsheet_url else "?"
    return f"{spreadsheet_url}{sep}sheet={sheet_id}"


# ============================================================================
# state 管理（持久化 spreadsheet_token，避免每次推送都建新表）
# ============================================================================

def load_report_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_report_entry(granularity: str, spreadsheet_token: str, url: str) -> None:
    state = load_report_state()
    state[granularity] = {"spreadsheet_token": spreadsheet_token, "url": url}
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def get_or_create_report_spreadsheet(client, granularity: str, folder_token=None) -> dict:
    """state 有 token 直接复用；无则新建电子表格并记入 state。返回 {"spreadsheet_token", "url"}。"""
    entry = load_report_state().get(granularity)
    if entry and entry.get("spreadsheet_token"):
        return entry

    title = f"ABA 关键词排名监控 · {granularity_label(granularity)}报"
    info = client.create_spreadsheet(title=title, folder_token=folder_token)
    _save_report_entry(granularity, info["spreadsheet_token"], info["url"])
    return {"spreadsheet_token": info["spreadsheet_token"], "url": info["url"]}


# ============================================================================
# selection → 二维矩阵（纯函数，dry-run 复用）
# ============================================================================

def _metric_to_row(metric: dict, *, tier: str, idx: int) -> list:
    jump = metric.get("tier_jump")
    jump_str = f"{jump[0]}→{jump[1]}" if jump else ""
    return [
        tier,
        idx,
        metric.get("keyword", ""),
        _fmt_rank_int(metric.get("current_rank")),
        _fmt_rank_int(metric.get("prev_rank")),
        _fmt_rank_int(metric.get("rank_ago")),
        _fmt_pct(metric.get("wow")),
        _fmt_pct(metric.get("cagr")),
        jump_str,
        "是" if metric.get("is_new") else "否",
    ]


def selection_to_matrix(
    *,
    title: str,
    date_range: dict,
    selection: dict,
    summary: dict,
    generated_at: datetime,
) -> list[list]:
    """把分档筛选结果展开成二维数组：标题/元信息/表头/各档数据/新晋段。"""
    pad = len(REPORT_HEADER) - 1
    matrix: list[list] = []

    matrix.append([title] + [""] * pad)
    matrix.append(
        [f"数据范围：{date_range.get('start') or '-'} ~ {date_range.get('end') or '-'} · 共 {date_range.get('points', 0)} 期"]
        + [""] * pad
    )
    matrix.append(
        [f"入选 {summary.get('total_tier', 0)} 条 · 新晋 {summary.get('total_newcomers', 0)} 条 · 生成于 {generated_at.strftime('%Y-%m-%d %H:%M')}"]
        + [""] * pad
    )
    matrix.append([])  # 空行分隔
    matrix.append(list(REPORT_HEADER))

    # T1-T5 各档（段内序号从 1 计）
    for tier_label, _lo, _hi, _top_n in cfg.TIERS:
        for idx, metric in enumerate(selection["tiers"].get(tier_label, []), start=1):
            matrix.append(_metric_to_row(metric, tier=tier_label, idx=idx))

    # 新晋段（独立一段，序号重置）
    newcomers = selection.get("newcomers", [])
    if newcomers:
        matrix.append([])  # 段间空行
    for idx, metric in enumerate(newcomers, start=1):
        matrix.append(_metric_to_row(metric, tier="新晋", idx=idx))

    return matrix


# ============================================================================
# 发布：建 sheet → 写矩阵
# ============================================================================

def publish_report_to_sheet(
    client,
    spreadsheet_token: str,
    spreadsheet_url: str,
    *,
    selection: dict,
    summary: dict,
    date_range: dict,
    granularity: str,
    generated_at: datetime,
) -> dict:
    """新建工作表（插在最前）并写入结果矩阵。返回 {sheet_id, sheet_title, url, rows, cols}。"""
    title = f"📈 ABA 关键词排名监控 · {granularity_label(granularity)}报"
    sheet_title = _sheet_title_for(granularity, generated_at)

    sheet_info = client.create_sheet(spreadsheet_token, sheet_title, index=0)
    sheet_id = sheet_info["sheet_id"]

    matrix = selection_to_matrix(
        title=title,
        date_range=date_range,
        selection=selection,
        summary=summary,
        generated_at=generated_at,
    )
    rows, cols = overwrite_sheet_with_matrix(client, spreadsheet_token, sheet_id, matrix)

    return {
        "sheet_id": sheet_id,
        "sheet_title": sheet_title,
        "url": _report_url(spreadsheet_url, sheet_id),
        "rows": rows,
        "cols": cols,
    }


# ============================================================================
# 群链接卡片
# ============================================================================

def build_report_link_card(
    *,
    title: str,
    granularity: str,
    date_range: dict,
    summary: dict,
    report_url: str,
    generated_at: datetime,
) -> dict:
    """构造「监控结果已生成 + 打开表格按钮」的飞书卡片（JSON 1.0）。"""
    label = granularity_label(granularity)
    range_start = date_range.get("start") or "-"
    range_end = date_range.get("end") or "-"
    points = date_range.get("points", 0)

    elements = [
        _markdown_div(
            f"📅 数据范围：**{range_start} ~ {range_end}** · 共 {points} 期 · {label}粒度\n"
            f"📊 入选：档位 **{summary.get('total_tier', 0)}** 条 · 新晋 **{summary.get('total_newcomers', 0)}** 条\n"
            f"🗂 完整明细已写入飞书表格，点击下方按钮查看"
        ),
        _hr(),
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": f"📊 打开完整{label}报表格"},
                    "type": "primary",
                    "url": report_url,
                }
            ],
        },
        {
            "tag": "note",
            "elements": [
                {
                    "tag": "plain_text",
                    "content": f"数据来源: Sif ABA · 生成于 {generated_at.strftime('%Y-%m-%d %H:%M')}",
                }
            ],
        },
    ]

    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": title}, "template": "blue"},
        "elements": elements,
    }


# ============================================================================
# Funnel 接入飞书（在已有 spreadsheet 建新工作表，绕过 folder 权限）
# ============================================================================

def funnel_records_to_matrix(records):
    """funnel 记录列表 → 二维数组（表头 + 数据行），列顺序与 CSV 一致。

    飞书单元格不接受 bool 类型（code=90204），需把 bool 转「是/否」；
    flatten_record 已把 dict/list 转 JSON 串，其他非基础类型兜底转 str。
    """
    from sif_keyword_funnel import (  # 延迟 import 避免循环
        collect_columns,
        flatten_record,
    )

    columns = collect_columns(records)
    matrix = [list(columns)]
    for rec in records:
        flat = flatten_record(rec)
        row = []
        for c in columns:
            v = flat.get(c, "")
            if isinstance(v, bool):
                v = "是" if v else "否"
            elif not isinstance(v, (str, int, float, type(None))):
                v = str(v)
            row.append(v)
        matrix.append(row)
    return matrix


def _funnel_sheet_title(query_keywords, strategy, generated_at):
    kw_str = "_".join(query_keywords)[:30]
    return f"Funnel_{kw_str}_{strategy}_{generated_at.strftime('%Y-%m-%d_%H%M')}"


def _funnel_style_ranges(columns, record_count):
    """根据列顺序生成价格/比率数值列的 (range_str, formatter)（数据行跳过表头）。

    价格列→货币 $#,##0.00、比率列→百分比 0.00%；与 sif_keyword_funnel.PRICE_COLS / RATIO_COLS 对齐。
    返回 [(range_str, formatter)]，range_str 为 1-based A1 范围如 'D2:D55'。
    """
    from sif_feishu_sync import col_index_to_name
    from sif_keyword_funnel import PRICE_COLS, RATIO_COLS

    ranges = []
    start_row, end_row = 2, record_count + 1  # 数据行（表头在第 1 行）
    for c_idx, col in enumerate(columns):
        if col in PRICE_COLS:
            fmt = "$#,##0.00"
        elif col in RATIO_COLS:
            fmt = "0.00%"
        else:
            continue
        letter = col_index_to_name(c_idx)
        ranges.append((f"{letter}{start_row}:{letter}{end_row}", fmt))
    return ranges


def publish_funnel_to_sheet(
    client,
    spreadsheet_token,
    spreadsheet_url,
    *,
    records,
    query_keywords,
    strategy,
    generated_at,
):
    """在已有 spreadsheet 里新建工作表并写入 funnel 结果。返回 sheet 信息 dict。"""
    sheet_title = _funnel_sheet_title(query_keywords, strategy, generated_at)
    sheet_info = client.create_sheet(spreadsheet_token, sheet_title, index=0)
    sheet_id = sheet_info["sheet_id"]

    matrix = funnel_records_to_matrix(records)
    rows, cols = overwrite_sheet_with_matrix(client, spreadsheet_token, sheet_id, matrix)

    # 价格/比率数值列设单元格数字格式（appendStyle，数据已写入，失败仅告警不阻断）
    for range_str, fmt in _funnel_style_ranges(matrix[0], len(records)):
        try:
            client.set_range_formatter(spreadsheet_token, sheet_id, range_str, fmt)
        except Exception as exc:
            print(f"⚠️ 设格式失败 {range_str}（数据已写入，忽略）: {exc}")

    return {
        "sheet_id": sheet_id,
        "sheet_title": sheet_title,
        "url": _report_url(spreadsheet_url, sheet_id),
        "rows": rows,
        "cols": cols,
        "record_count": len(records),
    }


def build_funnel_link_card(
    *,
    query_keywords,
    strategy,
    record_count,
    summary,
    report_url,
    generated_at,
):
    """构造 funnel 结果链接卡片。summary 为 compute_weighted_metrics 返回（可传 {}）。"""
    kw_display = "、".join(query_keywords[:5]) + ("..." if len(query_keywords) > 5 else "")
    elements = [
        _markdown_div(
            f"🔍 关键词：**{kw_display}** · 策略 **{strategy}**\n"
            f"📊 共 **{record_count}** 条记录（含相关市场词）"
        )
    ]
    if summary:
        elements.append(
            _markdown_div(
                f"📈 加权综合指标（搜索量加权）\n"
                f"  · 总搜索量 **{summary.get('total_search_volume', 0):,}**\n"
                f"  · 综合点击转化率 **{summary.get('weighted_conversion', 0) * 100:.2f}%**\n"
                f"  · 综合建议竞价 **${summary.get('weighted_median_cpc', 0):.2f}～${summary.get('weighted_end_cpc', 0):.2f}**"
            )
        )
    elements.append(_hr())
    elements.append(
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "📊 打开 Funnel 结果表格"},
                    "type": "primary",
                    "url": report_url,
                }
            ],
        }
    )
    elements.append(
        {
            "tag": "note",
            "elements": [
                {
                    "tag": "plain_text",
                    "content": f"数据来源: Sif Funnel · 生成于 {generated_at.strftime('%Y-%m-%d %H:%M')}",
                }
            ],
        }
    )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "🔍 Sif 关键词 Funnel 查询结果"},
            "template": "green",
        },
        "elements": elements,
    }
