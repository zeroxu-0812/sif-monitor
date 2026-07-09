#!/usr/bin/env python3
"""
Sif 关键词 cpc_median 周监控 → 飞书趋势表按周追加列 + 类目汇总卡片。

针对多类目关键词趋势表（如 ssFow6，结构：A 类目 / B 关键词 / C 翻译 / D ABA排名 /
E、F... 各周数据列）：

- 读 B 列关键词（全部类目），调 funnel 取 cpc_median
- 按 funnel period 在该表追加/覆盖对应周列（列头 period~period+6），货币格式
- 按 A 列类目分组，复用 compute_weighted_metrics 做加权汇总，生成飞书卡片
  （每类目：总搜索量 / 综合点击转化率 / 综合建议竞价 / 综合最高竞价），
  多类目同卡按序展示，形式与 funnel 卡片一致

手动验证：.venv/bin/python sif_weekly_cpc_monitor.py [--dry-run] [--push-card]
定时调度：schedule/com.abatrends.weekly-cpc.plist（每周一 10:00）
"""

import argparse
import sys
from collections import OrderedDict
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent
load_dotenv(PROJECT_DIR / ".env")

import feishu_config
import monitor_config as mcfg
import sif_keyword_funnel as skf
from feishu_messenger import FeishuMessenger, _hr, _markdown_div
from sif_feishu_sync import FeishuClient, col_index_to_name

# 监控目标工作表（多类目关键词趋势表）
WEEKLY_CPC_SHEET_ID = "ssFow6"
CATEGORY_COLUMN = 0   # A 列（0-based）
KEYWORD_COLUMN = 1    # B 列（0-based）


def read_sheet(client, spreadsheet_token, sheet_id):
    """读表，返回 (header, rows, col_count)。rows = [(row_1based, category, keyword)]。"""
    sheet_info = client.get_sheet_info(spreadsheet_token, sheet_id)
    grid = sheet_info.get("grid_properties", {})
    row_count = int(grid.get("row_count", 0))
    col_count = int(grid.get("column_count", 0))
    if row_count < 2 or col_count < 2:
        raise RuntimeError(f"表 {sheet_id} 行列异常: rows={row_count} cols={col_count}")

    last_col = col_index_to_name(col_count - 1)
    values = client.read_range(spreadsheet_token, sheet_id, f"A1:{last_col}{row_count}")
    if not values:
        raise RuntimeError(f"表 {sheet_id} 读不到数据")

    header = values[0]
    rows = []
    for row_idx, row in enumerate(values[1:], start=2):  # 数据从第 2 行起
        if not row or KEYWORD_COLUMN >= len(row):
            continue
        kw = row[KEYWORD_COLUMN]
        if kw is not None and str(kw).strip():
            cat = ""
            if len(row) > CATEGORY_COLUMN and row[CATEGORY_COLUMN] is not None:
                cat = str(row[CATEGORY_COLUMN]).strip()
            rows.append((row_idx, cat, str(kw).strip()))
    return header, rows, col_count


def week_header_from_period(period):
    """period（如 '2026-06-21'）→ 'YYYY-MM-DD~YYYY-MM-DD'（起始~起始+6天）。"""
    start = datetime.strptime(str(period)[:10], "%Y-%m-%d")
    end = start + timedelta(days=6)
    return f"{start.strftime('%Y-%m-%d')}~{end.strftime('%Y-%m-%d')}"


def build_cpc_median_map(records, requested_keywords, strategy="exact"):
    """keyword → cpc_median。仅精确匹配请求词，排除 API 顺带返回的相关市场词。"""
    bucket_key = skf.STRATEGY_MAP.get(strategy, "legacyForSales_exact")
    kw_set = set(requested_keywords)
    cpc_map = {}
    seen = set()
    for rec in records:
        kw = rec.get("keyword") or ""
        if kw not in kw_set or kw in seen:
            continue
        seen.add(kw)
        bucket = (rec.get("cpc") or {}).get(bucket_key) or []
        cpc_map[kw] = bucket[0].get("median") if bucket else None
    return cpc_map


def resolve_target_column(header, target_header):
    """在列头里找 target_header：命中→(该列索引, '覆盖')；未命中→(末尾追加索引, '追加')。"""
    for idx, val in enumerate(header):
        if val is not None and str(val).strip() == target_header:
            return idx, "覆盖"
    last_used = -1
    for idx, val in enumerate(header):
        if val is not None and str(val).strip():
            last_used = idx
    return last_used + 1, "追加"


def write_week_column(client, spreadsheet_token, sheet_id, col_idx, target_header,
                      rows, cpc_map, col_count):
    """写目标列：行 1 列头，行 2.. 每个关键词的 cpc_median；数据区设货币格式。"""
    if col_idx >= col_count:
        client.add_dimension(spreadsheet_token, sheet_id, "COLUMNS", col_idx - col_count + 1)

    col_letter = col_index_to_name(col_idx)
    column_values = [[target_header]]
    for _row, _cat, kw in rows:
        median = cpc_map.get(kw)
        column_values.append([median if median is not None else ""])

    last_row = 1 + len(rows)
    range_str = f"{col_letter}1:{col_letter}{last_row}"
    client.write_range(spreadsheet_token, sheet_id, range_str, column_values)

    try:
        client.set_range_formatter(
            spreadsheet_token, sheet_id,
            f"{col_letter}2:{col_letter}{last_row}", "$#,##0.00",
        )
    except Exception as exc:
        print(f"⚠️ 设货币格式失败（数据已写入，忽略）: {exc}")
    return col_letter, last_row


def build_category_summaries(records, rows):
    """按 A 列类目分组，每组调 compute_weighted_metrics（搜索量加权）。

    返回 [(category, n_words, summary_or_None)]，类目顺序与表中出现顺序一致。
    """
    cat_groups = OrderedDict()
    for _r, cat, kw in rows:
        cat_groups.setdefault(cat, []).append(kw)
    summaries = []
    for cat, kws in cat_groups.items():
        summary = skf.compute_weighted_metrics(records, requested_keywords=kws)
        summaries.append((cat, len(kws), summary))
    return summaries


def build_weekly_cpc_card(*, period_header, category_summaries, sheet_url, generated_at):
    """构造按类目汇总的飞书卡片（模仿 funnel 卡片：多类目同卡按序展示）。"""
    elements = [
        _markdown_div(
            f"📅 数据周期：**{period_header}** · 共 **{len(category_summaries)}** 个类目"
        ),
        _hr(),
    ]
    for cat, n, s in category_summaries:
        if s:
            body = (
                f"🏷️ **{cat}**\n"
                f"📈 加权综合指标（搜索量加权）\n"
                f"  · 总搜索量 **{s['total_search_volume']:,}**\n"
                f"  · 综合点击转化率 **{s['weighted_conversion'] * 100:.2f}%**\n"
                f"  · 综合建议竞价 **${s['weighted_median_cpc']:.2f}～${s['weighted_end_cpc']:.2f}**"
            )
        else:
            body = f"🏷️ **{cat}**（{n} 词）\n_无有效搜索量数据_"
        elements.append(_markdown_div(body))
        elements.append(_hr())
    elements.append({
        "tag": "action",
        "actions": [{
            "tag": "button",
            "text": {"tag": "plain_text", "content": "📊 打开趋势表"},
            "type": "primary",
            "url": sheet_url,
        }],
    })
    elements.append({
        "tag": "note",
        "elements": [{
            "tag": "plain_text",
            "content": f"数据来源: Sif Funnel · 生成于 {generated_at.strftime('%Y-%m-%d %H:%M')}",
        }],
    })
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "关键词周报监控"},
            "template": "green",
        },
        "elements": elements,
    }


def card_to_markdown(card):
    """把卡片 JSON 渲染成终端可读文本（预览用，模拟飞书卡片显示）。"""
    out = []
    title = card.get("header", {}).get("title", {}).get("content", "")
    out.append(f"【卡片标题】{title}  (template={card['header'].get('template')})")
    out.append("=" * 52)
    for el in card.get("elements", []):
        tag = el.get("tag")
        if tag == "div":
            out.append(el.get("text", {}).get("content", ""))
        elif tag == "hr":
            out.append("-" * 52)
        elif tag == "action":
            btns = [a.get("text", {}).get("content", "") for a in el.get("actions", [])]
            out.append(f"[按钮] {', '.join(btns)}")
        elif tag == "note":
            notes = [e.get("content", "") for e in el.get("elements", [])]
            out.append(f"  ({' · '.join(notes)})")
        out.append("")
    return "\n".join(out)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Sif cpc_median 周监控 → 追加周列 + 类目汇总卡片")
    p.add_argument("--sheet-id", default=WEEKLY_CPC_SHEET_ID, help="目标工作表 ID")
    p.add_argument("--strategy", default="exact", choices=["exact", "broad"])
    p.add_argument("--country", default="US")
    p.add_argument("--dry-run", action="store_true", help="只查不写表，但仍生成卡片预览")
    p.add_argument("--push-card", action="store_true", help="把类目汇总卡片推送到飞书群")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    client = FeishuClient(feishu_config.FEISHU_APP_ID, feishu_config.FEISHU_APP_SECRET)
    spreadsheet_token = client.get_spreadsheet_token(feishu_config.TRENDS_WIKI_TOKEN)

    # 1. 读表
    header, rows, col_count = read_sheet(client, spreadsheet_token, args.sheet_id)
    print(f"[1/5] 读表 {args.sheet_id}: {len(rows)} 个关键词, {col_count} 列")
    all_kw = [kw for _r, _c, kw in rows]

    # 2. 查 funnel
    records = skf.fetch_keyword_funnel(all_kw, strategy=args.strategy, country=args.country)
    period = next((r.get("period") for r in records if r.get("period")), None)
    if not period:
        print("⚠️ funnel 未返回 period，无可写入的周数据")
        return 1
    cpc_map = build_cpc_median_map(records, all_kw, args.strategy)
    target_header = week_header_from_period(period)
    hit = sum(1 for v in cpc_map.values() if v is not None)
    print(f"[2/5] funnel period={period} → 列头 {target_header} · cpc 命中 {hit}/{len(all_kw)}")

    # 3. 写表（dry-run 跳过）
    col_idx, action = resolve_target_column(header, target_header)
    print(f"[3/5] 目标列: {col_index_to_name(col_idx)} 列（{action}）")
    if args.dry_run:
        print("      [dry-run] 不写表")
    else:
        col_letter, last_row = write_week_column(
            client, spreadsheet_token, args.sheet_id, col_idx, target_header,
            rows, cpc_map, col_count,
        )
        print(f"      写入 {args.sheet_id}!{col_letter}1:{col_letter}{last_row}（{action}）✅")

    # 4. 类目汇总卡片（终端预览）
    category_summaries = build_category_summaries(records, rows)
    sheet_url = f"{feishu_config.FUNNEL_SPREADSHEET_WIKI_URL}?sheet={args.sheet_id}"
    card = build_weekly_cpc_card(
        period_header=target_header,
        category_summaries=category_summaries,
        sheet_url=sheet_url,
        generated_at=datetime.now(),
    )
    print("\n[4/5] 类目汇总卡片预览（这就是将推送到飞书的内容）:")
    print(card_to_markdown(card))

    # 5. 推送飞书（可选）
    if args.push_card:
        messenger = FeishuMessenger(feishu_config.FEISHU_APP_ID, feishu_config.FEISHU_APP_SECRET)
        resp = messenger.send_card(mcfg.CHAT_ID, card)
        print(f"\n[5/5] ✅ 卡片已推送: code={resp.get('code')} msg={resp.get('msg')}")
    else:
        print("\n[5/5] 未推送（加 --push-card 才发飞书）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
