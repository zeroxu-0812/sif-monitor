#!/usr/bin/env python3
"""
Sif 关键词竞品数量查询（Number of competing products / 流量位竞品数量）。

调用 https://www.sif.com/api/search/relevanceScreen/importKeyword 抓取关键词的：
- globalKeywordNum：全局关键词数量基准（ABA 前 100 万名池上限）
- competingProductCount：流量位竞品 ASIN 数量（由 imgs 列表长度得出）
- imgs：流量位竞品 ASIN 列表（asin / img / title）
- estSearchesNumHistory：本期搜索量 / ABA 排名历史
- estSearchesNumHistoryPrev：去年同期历史
- translateKeyword / isFocus / isRemove 等

接口接受关键词文本（与 funnel 一致），无需 keywordsId。输出 CSV 与多 sheet XLSX，
风格与 sif_keyword_funnel.py 一致。

注意：该接口会消耗 Sif 积分（consumeIntegral），批量查询请留意积分余额。
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

AMOUNT_PATH = "/api/search/relevanceScreen/importKeyword"
DEFAULT_PAGE_SIZE = 100
DEFAULT_PAGE_NUM = 1
# 默认按本期搜索量降序、周维度（与 /amount 页面 getList 一致）
DEFAULT_TREND_TYPE = "week"
DEFAULT_SORT_BY = "estSearchesNum"


def fetch_keyword_amount(
    keywords,
    country="US",
    session=None,
    batch_size=None,
    delay=None,
):
    """
    批量抓取关键词竞品数量数据。

    Args:
        keywords: 关键词列表
        country: 国家，默认 US
        session: 已有 requests.Session；不传则用 auth.get_session()
        batch_size: 每批词数，默认 sif_config.DEFAULT_BATCH_SIZE
        delay: 批间延迟秒数，默认 sif_config.DEFAULT_DELAY

    Returns:
        list[dict]：每个关键词一条记录（接口返回原样 + 注入
        globalKeywordNum / snapId / competingProductCount）
    """
    if not keywords:
        return []

    batch_size = batch_size or sif_config.DEFAULT_BATCH_SIZE
    delay = sif_config.DEFAULT_DELAY if delay is None else delay
    session = session or auth.get_session()

    records = []
    total_batches = (len(keywords) + batch_size - 1) // batch_size

    for idx, start in enumerate(range(0, len(keywords), batch_size), 1):
        batch = keywords[start : start + batch_size]
        print(f"批次 {idx}/{total_batches}: {len(batch)} 词")

        payload = {
            "keywords": batch,
            "pageNum": DEFAULT_PAGE_NUM,
            "pageSize": DEFAULT_PAGE_SIZE,
            "desc": True,
            "trendType": DEFAULT_TREND_TYPE,
            "sortBy": DEFAULT_SORT_BY,
            "isExpand": False,
        }
        resp = auth.api_post(
            session,
            AMOUNT_PATH,
            json_data=payload,
            params={"country": country},
            timeout=30,
        )
        data = resp.json()
        if data.get("code") != 1:
            raise RuntimeError(
                f"Sif amount 接口失败: code={data.get('code')} "
                f"msg={data.get('message') or data.get('msg')}"
            )

        inner = data.get("data") or {}
        global_num = inner.get("globalKeywordNum")
        snap_id = inner.get("snapId")
        batch_records = inner.get("keywords") or []
        for rec in batch_records:
            rec["globalKeywordNum"] = global_num
            rec["snapId"] = snap_id
            rec["competingProductCount"] = len(rec.get("imgs") or [])
        records.extend(batch_records)

        consumed = data.get("consumeIntegral")
        if consumed:
            print(
                f"  本次消耗 {consumed} 积分，剩余 {data.get('balanceIntegral')} "
                f"(snapId={snap_id}, globalKeywordNum={global_num})"
            )

        if idx < total_batches and delay > 0:
            time.sleep(delay)

    return records


def _disp_width(s):
    """估算终端显示宽度（CJK 宽字符算 2）。"""
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in str(s))


def _pad(s, width):
    """按显示宽度右补空格，把单元格对齐到指定列宽。"""
    s = str(s)
    return s + " " * max(0, width - _disp_width(s))


def _latest_metrics(rec):
    """从 estSearchesNumHistory 取最新一期的 (搜索量, 排名)；缺失则为 (None, None)。"""
    hist = rec.get("estSearchesNumHistory") or {}
    sv_list = hist.get("estSearchesNum") or []
    rank_list = hist.get("searchesRank") or []
    sv = sv_list[-1] if sv_list else None
    rank = rank_list[-1] if rank_list else None
    return sv, rank


def print_amount_table(records):
    """把爬取结果以表格形式打印到终端。"""
    if not records:
        print("（无数据）")
        return
    cols = [
        ("关键词", 30),
        ("中文", 14),
        ("竞品数", 8),
        ("最新搜索量", 13),
        ("最新排名", 11),
        ("Top竞品ASIN", 14),
    ]
    header = "".join(_pad(name, w) for name, w in cols)
    print(header)
    print("-" * _disp_width(header))
    for rec in records:
        sv, rank = _latest_metrics(rec)
        imgs = rec.get("imgs") or []
        top = imgs[0].get("asin", "-") if imgs else "-"
        row = [
            (rec.get("keyword") or "-"),
            (rec.get("translateKeyword") or "-"),
            str(rec.get("competingProductCount") or 0),
            f"{sv:,}" if sv is not None else "-",
            f"{rank:,}" if rank is not None else "-",
            top,
        ]
        # 关键词/中文超长截断，避免撑乱表格
        row[0] = row[0][:28]
        row[1] = row[1][:12]
        print("".join(_pad(v, w) for v, (_, w) in zip(row, cols)))


def flatten_record(rec, top_img_count=3):
    """
    把单条记录拍平成一行 dict（含 imgs 前 N 个竞品 ASIN 拆列）。
    imgs 之外的嵌套对象（estSearchesNumHistory 等）保留为 JSON 字符串。
    """
    flat = {}
    for key, value in rec.items():
        if key == "imgs":
            flat["imgs_json"] = json.dumps(value, ensure_ascii=False)
            for i, img_rec in enumerate(value[:top_img_count], 1):
                flat[f"topAsin{i}"] = img_rec.get("asin", "")
                flat[f"topAsin{i}_title"] = (img_rec.get("title") or "")[:200]
        elif isinstance(value, (dict, list)):
            flat[key] = json.dumps(value, ensure_ascii=False)
        else:
            flat[key] = value
    return flat


def collect_columns(records):
    """收集所有出现过的字段名（保持出现顺序），保证 CSV 列稳定。"""
    cols = []
    seen = set()
    priority = [
        "keyword",
        "translateKeyword",
        "globalKeywordNum",
        "competingProductCount",
        "estSearchesNum",
        "searchesRank",
        "confirmRelevance",
        "isFocus",
        "isRemove",
        "snapId",
    ]
    for k in priority:
        for rec in records:
            if k in rec:
                if k not in seen:
                    cols.append(k)
                    seen.add(k)
                break

    # 补所有 record 出现过、但不在 priority 里的字段（imgs 单独处理）
    for rec in records:
        for k in rec.keys():
            if k in ("imgs",):
                continue
            if k not in seen:
                cols.append(k)
                seen.add(k)

    # 最后追加 imgs 拆列
    for rec in records:
        top = rec.get("imgs") or []
        for i in range(1, 4):
            for suffix in ["", "_title"]:
                col = f"topAsin{i}{suffix}"
                if col not in seen:
                    cols.append(col)
                    seen.add(col)
        if top:
            break
    cols.append("imgs_json")
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


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Sif 关键词竞品数量查询（流量位竞品数量）")
    parser.add_argument("--keywords", help="逗号分隔的关键词列表")
    parser.add_argument("--keywords-file", help="关键词文件（每行一个，UTF-8）")
    parser.add_argument("--country", default="US")
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

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    try:
        session = auth.get_session()
    except auth.SIFAuthError as exc:
        print(f"\n❌ SIF 鉴权失败: {exc}")
        print("   请检查 .env 中 SIF_PHONE / SIF_PASSWORD 是否填写正确")
        return 2

    try:
        records = fetch_keyword_amount(
            keywords,
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

    csv_path = out_dir / f"amount_{ts}.csv"
    columns = collect_columns(records)
    write_csv(csv_path, records, columns)
    print(f"写入 {len(records)} 条 → {csv_path}")

    # 终端表格预览
    print(f"\n=== 关键词竞品数量（共 {len(records)} 词，globalKeywordNum={records[0].get('globalKeywordNum') if records else '-'}）===")
    print_amount_table(records)
    print()

    if not args.no_xlsx and records:
        xlsx_path = out_dir / f"amount_{ts}.xlsx"
        write_xlsx(xlsx_path, {"amount": (records, columns)})
        print(f"Excel 工作簿: {xlsx_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
