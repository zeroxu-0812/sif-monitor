#!/usr/bin/env python3
"""
Sif 关键词 Funnel 数据查询（周维度）。

调用 https://www.sif.com/api/search/keywordFunnel/list 抓取关键词的：
- 搜索量 / 点击量 / 购买量
- 平均、最大、最小 CPC
- 点击转化率 / 购买转化率 / 点击共享 / 转化共享
- Top ASIN 列表

支持 exact / broad 两种广告匹配策略，输出 CSV 与多 sheet XLSX。

与 sif_feishu_sync.py 平级，不耦合飞书写表流程。
"""

import argparse
import csv
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import auth
import sif_config

PROJECT_DIR = Path(__file__).resolve().parent

FUNNEL_PATH = "/api/search/keywordFunnel/list"
DEFAULT_PAGE_SIZE = 100
DEFAULT_PAGE_NUM = 1

STRATEGY_MAP = {
    "exact": "legacyForSales_exact",
    "broad": "legacyForSales_broad",
}
ALL_STRATEGIES = ["exact", "broad"]


def fetch_keyword_funnel(
    keywords,
    strategy="exact",
    country="US",
    session=None,
    batch_size=None,
    delay=None,
):
    """
    批量抓取关键词 funnel 数据（最新一周）。

    Args:
        keywords: 关键词列表
        strategy: "exact" / "broad"（映射到 legacyForSales_exact / _broad）
        country: 国家，默认 US
        session: 已有 requests.Session；不传则用 auth.get_session()
        batch_size: 每批词数，默认 sif_config.DEFAULT_BATCH_SIZE
        delay: 批间延迟秒数，默认 sif_config.DEFAULT_DELAY

    Returns:
        list[dict]：每个关键词一条记录（接口返回原样 + 注入 keyword/strategy）
    """
    if not keywords:
        return []
    if strategy not in STRATEGY_MAP:
        raise ValueError(f"未知 strategy: {strategy}（支持: {list(STRATEGY_MAP)})")

    batch_size = batch_size or sif_config.DEFAULT_BATCH_SIZE
    delay = sif_config.DEFAULT_DELAY if delay is None else delay
    session = session or auth.get_session()

    api_strategy = STRATEGY_MAP[strategy]
    records = []
    total_batches = (len(keywords) + batch_size - 1) // batch_size

    for idx, start in enumerate(range(0, len(keywords), batch_size), 1):
        batch = keywords[start : start + batch_size]
        print(f"[{strategy}] 批次 {idx}/{total_batches}: {len(batch)} 词")

        payload = {
            "pageSize": DEFAULT_PAGE_SIZE,
            "pageNum": DEFAULT_PAGE_NUM,
            "sortBy": "",
            "customPrice": "",
            "customProfitRate": "",
            "desc": True,
            "strategy": api_strategy,
            "matchTypes": [],
            "keywords": batch,
        }
        resp = auth.api_post(
            session,
            FUNNEL_PATH,
            json_data=payload,
            params={"country": country},
            timeout=30,
        )
        data = resp.json()
        if data.get("code") != 1:
            raise RuntimeError(
                f"Sif funnel 接口失败: code={data.get('code')} "
                f"msg={data.get('message') or data.get('msg')}"
            )

        batch_records = data.get("data", {}).get("keywords", []) or []
        for rec in batch_records:
            rec["strategy"] = strategy
        records.extend(batch_records)

        if idx < total_batches and delay > 0:
            time.sleep(delay)

    return records


def flatten_record(rec, top_asin_count=3):
    """
    把单条记录拍平成一行 dict（含 topAsins 前 N 个 ASIN 拆列）。
    topAsins 之外的嵌套对象保留为 JSON 字符串。
    """
    flat = {}
    for key, value in rec.items():
        if key == "topAsins":
            flat["topAsins_json"] = json.dumps(value, ensure_ascii=False)
            for i, asin_rec in enumerate(value[:top_asin_count], 1):
                flat[f"topAsin{i}"] = asin_rec.get("asin", "")
                flat[f"topAsin{i}_price"] = asin_rec.get("price", "")
                flat[f"topAsin{i}_title"] = (asin_rec.get("title") or "")[:200]
        elif isinstance(value, (dict, list)):
            flat[key] = json.dumps(value, ensure_ascii=False)
        else:
            flat[key] = value
    return flat


def collect_columns(records):
    """收集所有出现过的字段名（保持出现顺序），保证 CSV 列稳定。"""
    cols = []
    seen = set()
    # 优先按业务相关性排序
    priority = [
        "keyword",
        "strategy",
        "period",
        "searchVolume",
        "clickVolume",
        "purchaseVolume",
        "avgKwPrice",
        "maxKwPrice",
        "minKwPrice",
        "searchClickRatio",
        "searchPurchaseRatio",
        "clickPurchaseRatio",
        "clickShared",
        "conversionShared",
        "translateKeyword",
        "source",
        "updateTime",
        "isExc",
        "isFixed",
        "groups",
    ]
    for k in priority:
        for rec in records:
            if k in rec:
                if k not in seen:
                    cols.append(k)
                    seen.add(k)
                break

    # 再补所有 record 出现过、但不在 priority 里的字段（除 topAsins 单独处理）
    for rec in records:
        for k in rec.keys():
            if k in ("topAsins",):
                continue
            if k not in seen:
                cols.append(k)
                seen.add(k)

    # 最后追加 topAsins 拆列
    for rec in records:
        top = rec.get("topAsins") or []
        for i in range(1, 4):
            for suffix in ["", "_price", "_title"]:
                col = f"topAsin{i}{suffix}"
                if col not in seen:
                    cols.append(col)
                    seen.add(col)
        if top:
            break
    cols.append("topAsins_json")
    return cols


def write_csv(path, records, columns):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for rec in records:
            writer.writerow(flatten_record(rec))


def write_xlsx(path, sheets):
    """sheets: {sheet_name: (records, columns)}"""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    wb.remove(wb.active)

    header_fill = PatternFill("solid", fgColor="305496")
    header_font = Font(bold=True, color="FFFFFF")
    center = Alignment(horizontal="center", vertical="center", wrap_text=True)

    for name, (records, columns) in sheets.items():
        ws = wb.create_sheet(name[:31])
        for c_idx, col in enumerate(columns, 1):
            cell = ws.cell(row=1, column=c_idx, value=col)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = center

        for r_idx, rec in enumerate(records, 2):
            flat = flatten_record(rec)
            for c_idx, col in enumerate(columns, 1):
                cell = ws.cell(row=r_idx, column=c_idx, value=flat.get(col, ""))
                cell.alignment = Alignment(horizontal="center", vertical="center")

        ws.freeze_panes = "B2"
        ws.column_dimensions["A"].width = 28
        for c in range(2, len(columns) + 1):
            ws.column_dimensions[get_column_letter(c)].width = 14

    wb.save(path)


def _extract_cpc(rec, cpc_match):
    """
    从记录里取出 cpc 桶（按匹配类型）。
    返回 (start, median, end, category_name)，缺失则为 None。
    """
    cpc = rec.get("cpc") or {}
    bucket = cpc.get(cpc_match) or []
    if not bucket:
        return None, None, None, None
    first = bucket[0] or {}
    return (
        first.get("start"),
        first.get("median"),
        first.get("end"),
        first.get("categoryName"),
    )


def compute_weighted_metrics(records, requested_keywords=None, cpc_match="legacyForSales_exact"):
    """
    按搜索量加权计算综合点击转化率 / 综合建议竞价 / 综合最高竞价。

    权重 w_i = searchVolume_i / Σ searchVolume
    综合指标 = Σ w_i × 指标_i

    Args:
        records: 接口返回的记录列表
        requested_keywords: 若提供，则只纳入这些关键词（区分大小写精确匹配），
                            避免被 API 顺带返回的相关市场词污染。
        cpc_match: CPC 桶匹配类型，默认 autoForSales_phrase。
                   可选：autoForSales_exact / autoForSales_phrase / autoForSales_broad
                         legacyForSales_exact / legacyForSales_phrase / legacyForSales_broad

    Returns:
        dict: {
            "items": [每词贡献详情],
            "total_search_volume": 总搜索量,
            "weighted_conversion": 综合点击转化率,
            "weighted_median_cpc": 综合建议竞价(median),
            "weighted_end_cpc": 综合最高竞价(end),
            "cpc_match": 使用的匹配类型,
            "period": 数据周期,
        }
    """
    keyword_set = set(requested_keywords) if requested_keywords else None

    items = []
    seen_keywords = set()
    for rec in records:
        kw = rec.get("keyword") or ""
        if keyword_set is not None and kw not in keyword_set:
            continue
        if kw in seen_keywords:
            continue
        seen_keywords.add(kw)
        sv = rec.get("searchVolume") or 0
        if sv <= 0:
            continue
        cpc_start, cpc_median, cpc_end, cpc_cat = _extract_cpc(rec, cpc_match)
        items.append({
            "keyword": kw,
            "searchVolume": sv,
            "clickPurchaseRatio": rec.get("clickPurchaseRatio") or 0,
            "cpc_category": cpc_cat or "",
            "cpc_start": cpc_start if cpc_start is not None else 0,
            "cpc_median": cpc_median if cpc_median is not None else 0,
            "cpc_end": cpc_end if cpc_end is not None else 0,
        })

    total_sv = sum(it["searchVolume"] for it in items)
    if total_sv <= 0:
        return None

    for it in items:
        it["weight"] = it["searchVolume"] / total_sv
        it["w_conv"] = it["weight"] * it["clickPurchaseRatio"]
        it["w_median_cpc"] = it["weight"] * it["cpc_median"]
        it["w_end_cpc"] = it["weight"] * it["cpc_end"]

    period = items[0].get("period") if items else None
    return {
        "items": items,
        "total_search_volume": total_sv,
        "weighted_conversion": sum(it["w_conv"] for it in items),
        "weighted_median_cpc": sum(it["w_median_cpc"] for it in items),
        "weighted_end_cpc": sum(it["w_end_cpc"] for it in items),
        "cpc_match": cpc_match,
        "period": period,
    }


def write_weighted_summary(path, summary, strategy):
    """输出综合指标明细表（每词一行 + 末尾合计行）。"""
    fieldnames = [
        "keyword",
        "searchVolume",
        "weight",
        "clickPurchaseRatio",
        "cpc_category",
        "cpc_start",
        "cpc_median",
        "cpc_end",
        "w_conv",
        "w_median_cpc",
        "w_end_cpc",
    ]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for it in summary["items"]:
            writer.writerow({
                "keyword": it["keyword"],
                "searchVolume": it["searchVolume"],
                "weight": f"{it['weight']:.6f}",
                "clickPurchaseRatio": it["clickPurchaseRatio"],
                "cpc_category": it["cpc_category"],
                "cpc_start": f"{it['cpc_start']:.2f}",
                "cpc_median": f"{it['cpc_median']:.2f}",
                "cpc_end": f"{it['cpc_end']:.2f}",
                "w_conv": f"{it['w_conv']:.6f}",
                "w_median_cpc": f"{it['w_median_cpc']:.4f}",
                "w_end_cpc": f"{it['w_end_cpc']:.4f}",
            })
        writer.writerow({
            "keyword": f"[合计 strategy={strategy}]",
            "searchVolume": summary["total_search_volume"],
            "weight": "1.000000",
            "clickPurchaseRatio": f"{summary['weighted_conversion']:.6f}",
            "cpc_category": "",
            "cpc_start": "",
            "cpc_median": f"{summary['weighted_median_cpc']:.2f}",
            "cpc_end": f"{summary['weighted_end_cpc']:.2f}",
            "w_conv": f"{summary['weighted_conversion']:.6f}",
            "w_median_cpc": f"{summary['weighted_median_cpc']:.4f}",
            "w_end_cpc": f"{summary['weighted_end_cpc']:.4f}",
        })


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Sif 关键词 Funnel 数据查询（周维度）")
    parser.add_argument("--keywords", help="逗号分隔的关键词列表")
    parser.add_argument("--keywords-file", help="关键词文件（每行一个，UTF-8）")
    parser.add_argument(
        "--strategy",
        choices=["exact", "broad", "both"],
        default="exact",
        help="广告匹配策略，both 表示同时跑 exact + broad",
    )
    parser.add_argument("--country", default="US")
    parser.add_argument(
        "--cpc-match",
        default="legacyForSales_exact",
        choices=[
            "autoForSales_exact",
            "autoForSales_phrase",
            "autoForSales_broad",
            "legacyForSales_exact",
            "legacyForSales_phrase",
            "legacyForSales_broad",
        ],
        help="CPC 桶匹配类型，默认 legacyForSales_exact（与请求 strategy 一致）",
    )
    parser.add_argument("--batch-size", type=int, default=sif_config.DEFAULT_BATCH_SIZE)
    parser.add_argument("--delay", type=float, default=sif_config.DEFAULT_DELAY)
    parser.add_argument("--out-dir", default=str(PROJECT_DIR), help="输出目录")
    parser.add_argument("--no-xlsx", action="store_true", help="不生成 xlsx，只导出 CSV")
    return parser.parse_args(argv)


def load_keywords(args):
    if args.keywords:
        items = [k.strip() for k in args.keywords.split(",") if k.strip()]
    elif args.keywords_file:
        text = Path(args.keywords_file).read_text(encoding="utf-8")
        items = [line.strip() for line in text.splitlines() if line.strip()]
    else:
        raise SystemExit("必须提供 --keywords 或 --keywords-file")
    # 去重保序
    seen = set()
    out = []
    for k in items:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def main(argv=None):
    args = parse_args(argv)
    keywords = load_keywords(args)
    print(f"待查关键词 {len(keywords)} 个")

    strategies = ALL_STRATEGIES if args.strategy == "both" else [args.strategy]
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    try:
        session = auth.get_session()
    except auth.SIFAuthError as exc:
        print(f"\n❌ SIF 鉴权失败: {exc}")
        print("   请检查 .env 中 SIF_PHONE / SIF_PASSWORD 是否填写正确")
        return 2

    all_records_by_strategy = {}
    for strat in strategies:
        try:
            records = fetch_keyword_funnel(
                keywords,
                strategy=strat,
                country=args.country,
                session=session,
                batch_size=args.batch_size,
                delay=args.delay,
            )
        except auth.SIFAuthError as exc:
            print(f"\n❌ SIF 鉴权失败: {exc}")
            return 2
        except RuntimeError as exc:
            print(f"\n❌ 接口调用失败: {exc}")
            return 1

        all_records_by_strategy[strat] = records
        csv_path = out_dir / f"funnel_{strat}_{ts}.csv"
        columns = collect_columns(records)
        write_csv(csv_path, records, columns)
        print(f"[{strat}] 写入 {len(records)} 条 → {csv_path}")

        # 终端预览
        print(f"\n=== [{strat}] 最新一期预览（weekDate={records[0].get('period') if records else 'N/A'}）===")
        print(f"{'关键词':<32}{'搜索量':<10}{'CPC均值':<10}{'购买转化':<10}{'TopASIN':<14}")
        print("-" * 76)
        for rec in records:
            top_asins = rec.get("topAsins") or []
            top = top_asins[0].get("asin", "") if top_asins else ""
            print(
                f"{(rec.get('keyword') or ''):<32}"
                f"{(rec.get('searchVolume') or 0):<10}"
                f"{(rec.get('avgKwPrice') or 0):<10}"
                f"{(rec.get('clickPurchaseRatio') or 0):<10}"
                f"{top:<14}"
            )
        print()

        # 综合指标（搜索量加权）
        summary = compute_weighted_metrics(
            records, requested_keywords=keywords, cpc_match=args.cpc_match
        )
        if summary:
            print(f"--- [{strat}] 综合指标（搜索量加权，CPC 桶={args.cpc_match}）---")
            print(f"  有效词数: {len(summary['items'])} / {len(keywords)}")
            print(f"  总搜索量: {summary['total_search_volume']:,}")
            print(f"  综合点击转化率: {summary['weighted_conversion']*100:.2f}%")
            print(f"  综合建议竞价(median): ${summary['weighted_median_cpc']:.2f}")
            print(f"  综合最高竞价(end):    ${summary['weighted_end_cpc']:.2f}")
            summary_csv = out_dir / f"funnel_summary_{strat}_{ts}.csv"
            write_weighted_summary(summary_csv, summary, strat)
            print(f"  明细表: {summary_csv}")
            print()

    if not args.no_xlsx and all_records_by_strategy:
        xlsx_path = out_dir / f"funnel_{ts}.xlsx"
        sheets = {
            strat: (recs, collect_columns(recs))
            for strat, recs in all_records_by_strategy.items()
        }
        write_xlsx(xlsx_path, sheets)
        print(f"Excel 工作簿: {xlsx_path}（含 sheet: {', '.join(sheets.keys())}）")

    return 0


if __name__ == "__main__":
    sys.exit(main())
