#!/usr/bin/env python3
"""
Walmart 爬取结果 → 飞书电子表格发布 + 链接卡片（镜像 feishu_report）。

职责：
- 把 scrape_walmart 返回的 rows 写入飞书电子表格的一个新工作表；
- 构造一张含「打开表格」按钮 + 价格/评分汇总的飞书卡片。

复用既有组件（不重复造轮子）：
- sif_feishu_sync.FeishuClient / overwrite_sheet_with_matrix
- feishu_messenger._markdown_div / _hr
"""

from __future__ import annotations

from datetime import datetime

from feishu_messenger import _hr, _markdown_div
from sif_feishu_sync import overwrite_sheet_with_matrix
from walmart_scraper import FIELDS, SORT_OPTIONS

# 飞书表里的列头（中文，便于阅读）；顺序与 FIELDS 一致
HEADER_MAP = {
    "usItemId": "商品ID",
    "name": "商品名称",
    "brand": "品牌",
    "price": "价格",
    "wasPrice": "原价",
    "averageRating": "评分",
    "numberOfReviews": "评论数",
    "sellerName": "卖家",
    "availability": "库存",
    "isSponsored": "广告",
    "thumbnailUrl": "图片",
    "productUrl": "链接",
    "page": "页",
}


def _report_url(spreadsheet_url: str, sheet_id: str) -> str:
    """把表格根 URL 拼上 ?sheet= 定位到指定工作表。"""
    if not spreadsheet_url:
        return f"https://feishu.cn/sheets/?sheet={sheet_id}"
    sep = "&" if "?" in spreadsheet_url else "?"
    return f"{spreadsheet_url}{sep}sheet={sheet_id}"


def _to_cell(value):
    """飞书单元格兼容：bool→是/否，其他非基础类型→str。"""
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (str, int, float)) or value is None:
        return "" if value is None else value
    return str(value)


def walmart_rows_to_matrix(rows: list[dict]) -> list[list]:
    """rows → 二维数组（中文表头 + 数据行），列顺序与 FIELDS 一致。"""
    matrix = [[HEADER_MAP.get(k, k) for k in FIELDS]]
    for r in rows:
        matrix.append([_to_cell(r.get(k, "")) for k in FIELDS])
    return matrix


def _walmart_sheet_title(query: str, sort: str, generated_at: datetime) -> str:
    kw = query.replace(" ", "_")[:25]
    return f"Walmart_{kw}_{sort}_{generated_at.strftime('%Y-%m-%d_%H%M')}"


def summarize(rows: list[dict]) -> dict:
    """汇总：条数、价格区间/均价、评分均值、评论总数、有货数、广告数。"""
    prices: list[float] = []
    ratings: list[float] = []
    reviews: list[int] = []
    in_stock = 0
    sponsored = 0
    for r in rows:
        p = str(r.get("price", "")).replace("$", "").replace(",", "")
        try:
            prices.append(float(p))
        except ValueError:
            pass
        try:
            ratings.append(float(r["averageRating"]))
        except (TypeError, ValueError, KeyError):
            pass
        try:
            reviews.append(int(r["numberOfReviews"]))
        except (TypeError, ValueError, KeyError):
            pass
        if "stock" in str(r.get("availability", "")).lower():
            in_stock += 1
        if r.get("isSponsored"):
            sponsored += 1
    return {
        "count": len(rows),
        "price_min": min(prices) if prices else None,
        "price_max": max(prices) if prices else None,
        "price_avg": (sum(prices) / len(prices)) if prices else None,
        "rating_avg": (sum(ratings) / len(ratings)) if ratings else None,
        "reviews_total": sum(reviews),
        "in_stock": in_stock,
        "sponsored": sponsored,
        "with_price": len(prices),
        "with_rating": len(ratings),
    }


def _money(v) -> str:
    return "-" if v is None else f"${v:.2f}"


def publish_walmart_to_sheet(
    client,
    spreadsheet_token: str,
    spreadsheet_url: str,
    *,
    rows: list[dict],
    query: str,
    sort: str,
    generated_at: datetime,
) -> dict:
    """在已有 spreadsheet 里新建工作表（插在最前）并写入结果矩阵。

    返回 {sheet_id, sheet_title, url, rows, cols, count, summary}。
    """
    sheet_title = _walmart_sheet_title(query, sort, generated_at)
    sheet_info = client.create_sheet(spreadsheet_token, sheet_title, index=0)
    sheet_id = sheet_info["sheet_id"]

    matrix = walmart_rows_to_matrix(rows)
    written_rows, cols = overwrite_sheet_with_matrix(client, spreadsheet_token, sheet_id, matrix)

    return {
        "sheet_id": sheet_id,
        "sheet_title": sheet_title,
        "url": _report_url(spreadsheet_url, sheet_id),
        "rows": written_rows,
        "cols": cols,
        "count": len(rows),
        "summary": summarize(rows),
    }


def build_walmart_link_card(
    *,
    query: str,
    sort: str,
    report: dict,
    generated_at: datetime,
) -> dict:
    """构造「Walmart 爬取结果 + 打开表格按钮」的飞书卡片（JSON 1.0）。"""
    s = report.get("summary", {})
    sort_label = dict(SORT_OPTIONS).get(sort, sort)

    elements = [
        _markdown_div(
            f"🔍 关键词：**{query}** · 排序 **{sort_label}**\n"
            f"📊 共 **{report.get('count', 0)}** 条去重商品"
        ),
        _markdown_div(
            f"💰 价格区间：**{_money(s.get('price_min'))} ~ {_money(s.get('price_max'))}**"
            f" · 均价 **{_money(s.get('price_avg'))}**（{n_of(s,'with_price')} 条有价）\n"
            f"⭐ 评分均值 **{_rating(s.get('rating_avg'))}**（{n_of(s,'with_rating')} 条有评分）"
            f" · 评论共 **{s.get('reviews_total', 0):,}**\n"
            f"📦 有货 **{s.get('in_stock', 0)}** · 广告 **{s.get('sponsored', 0)}**"
        ),
        _hr(),
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "📊 打开 Walmart 结果表格"},
                    "type": "primary",
                    "url": report.get("url", ""),
                }
            ],
        },
        {
            "tag": "note",
            "elements": [
                {
                    "tag": "plain_text",
                    "content": f"数据来源: Walmart · 生成于 {generated_at.strftime('%Y-%m-%d %H:%M')}",
                }
            ],
        },
    ]
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"🛒 Walmart 商品爬取结果 · {query}"},
            "template": "green",
        },
        "elements": elements,
    }


def n_of(summary: dict, key: str) -> int:
    return int(summary.get(key, 0) or 0)


def _rating(v) -> str:
    return "-" if v is None else f"{v:.2f}"
