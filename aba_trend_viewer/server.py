#!/usr/bin/env python3
"""
Local ABA rank trend viewer.

This app intentionally does not depend on the Feishu sync module. It reuses
auth.py for Sif login/token handling, then exposes a small JSON API for the
static ECharts frontend.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

PROJECT_DIR = Path(__file__).resolve().parents[1]
APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
sys.path.insert(0, str(PROJECT_DIR))

import auth  # noqa: E402
import requests  # noqa: E402


SIF_TRENDS_URL = "https://new.sif.com/api/search/compare/estSearchesNumHistory"
MAX_KEYWORDS_PER_BATCH = 10
SUPPORTED_GRANULARITIES = ("week", "month")


def request_json(method: str, url: str, **kwargs) -> dict:
    response = requests.request(method, url, timeout=30, **kwargs)
    response.raise_for_status()
    data = response.json()
    if data.get("code") != 1:
        message = data.get("message") or data.get("msg") or "unknown error"
        raise RuntimeError(f"Sif API failed: code={data.get('code')} msg={message}")
    return data


def parse_keywords(value) -> list[str]:
    if isinstance(value, str):
        raw_items = value.replace("，", ",").replace("\n", ",").split(",")
    elif isinstance(value, list):
        raw_items = value
    else:
        raw_items = []

    keywords = []
    seen = set()
    for item in raw_items:
        keyword = str(item).strip()
        if keyword and keyword not in seen:
            keywords.append(keyword)
            seen.add(keyword)
    return keywords


def parse_date(value: str | None, granularity: str) -> datetime | None:
    if not value:
        return None
    fmt = "%Y-%m" if granularity == "month" and len(value) == 7 else "%Y-%m-%d"
    return datetime.strptime(value, fmt)


def date_sort_key(value: str) -> datetime:
    fmt = "%Y-%m" if len(value) == 7 else "%Y-%m-%d"
    return datetime.strptime(value, fmt)


def fetch_trends_batch(keywords: list[str], granularity: str) -> dict:
    token = auth.get_token()
    payload = {
        "keywords": keywords[:MAX_KEYWORDS_PER_BATCH],
        "granularity": granularity,
    }
    headers = {
        "accept": "application/json",
        "authorization": token,
        "content-type": "application/json",
    }
    data = request_json(
        "POST",
        SIF_TRENDS_URL,
        params=auth.build_sif_query_params(country="US"),
        headers=headers,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    )
    return data.get("data", {}).get("estSearchesNumHistory", {})


def fetch_trends(keywords: list[str], granularity: str, batch_size: int) -> dict:
    result = {}
    batch_size = max(1, min(batch_size, MAX_KEYWORDS_PER_BATCH))
    for start in range(0, len(keywords), batch_size):
        batch = keywords[start : start + batch_size]
        result.update(fetch_trends_batch(batch, granularity))
    return result


def normalize_trends(
    trends: dict,
    keyword_order: list[str],
    granularity: str,
    start_date: str | None,
    end_date: str | None,
) -> dict:
    start = parse_date(start_date, granularity)
    end = parse_date(end_date, granularity)
    dates = set()
    rows = {}

    for keyword in keyword_order:
        trend = trends.get(keyword, {})
        row = {}
        raw_dates = trend.get("date", [])
        ranks = trend.get("searchesRank", [])
        searches = trend.get("estSearchesNum", [])

        for index, raw_date in enumerate(raw_dates):
            date_value = str(raw_date)
            parsed = date_sort_key(date_value)
            if start and parsed < start:
                continue
            if end and parsed > end:
                continue
            dates.add(date_value)
            row[date_value] = {
                "rank": ranks[index] if index < len(ranks) else None,
                "searches": searches[index] if index < len(searches) else None,
            }
        rows[keyword] = row

    ordered_dates = sorted(dates, key=date_sort_key)
    series = []
    for keyword in keyword_order:
        values = []
        searches = []
        for date_value in ordered_dates:
            point = rows.get(keyword, {}).get(date_value, {})
            values.append(point.get("rank"))
            searches.append(point.get("searches"))
        series.append(
            {
                "keyword": keyword,
                "ranks": values,
                "searches": searches,
                "summary": summarize_keyword(keyword, ordered_dates, values),
            }
        )

    return {
        "dates": ordered_dates,
        "range": {
            "start": ordered_dates[0] if ordered_dates else None,
            "end": ordered_dates[-1] if ordered_dates else None,
            "points": len(ordered_dates),
        },
        "series": series,
    }


def summarize_keyword(keyword: str, dates: list[str], ranks: list[int | None]) -> dict:
    valid = [(date, rank) for date, rank in zip(dates, ranks) if isinstance(rank, int)]
    if not valid:
        return {
            "keyword": keyword,
            "current": None,
            "best": None,
            "worst": None,
            "delta": None,
        }

    first_rank = valid[0][1]
    current_date, current_rank = valid[-1]
    best_date, best_rank = min(valid, key=lambda item: item[1])
    worst_date, worst_rank = max(valid, key=lambda item: item[1])
    return {
        "keyword": keyword,
        "current": {"date": current_date, "rank": current_rank},
        "best": {"date": best_date, "rank": best_rank},
        "worst": {"date": worst_date, "rank": worst_rank},
        "delta": current_rank - first_rank,
    }


def build_response(payload: dict) -> dict:
    keywords = parse_keywords(payload.get("keywords", ""))
    if not keywords:
        raise ValueError("请输入至少 1 个关键词")
    if len(keywords) > 50:
        raise ValueError("一次最多查询 50 个关键词")

    requested = payload.get("granularity", "week")
    granularities = SUPPORTED_GRANULARITIES if requested == "all" else (requested,)
    for granularity in granularities:
        if granularity not in SUPPORTED_GRANULARITIES:
            raise ValueError("granularity must be week, month, or all")

    batch_size = int(payload.get("batchSize") or MAX_KEYWORDS_PER_BATCH)
    result = {}
    for granularity in granularities:
        trends = fetch_trends(keywords, granularity, batch_size)
        result[granularity] = normalize_trends(
            trends,
            keywords,
            granularity,
            payload.get("startDate"),
            payload.get("endDate"),
        )

    return {
        "keywords": keywords,
        "granularities": result,
        "source": {
            "ranking": "searchesRank",
            "note": "时间范围来自 Sif 接口实际返回数据；前端日期过滤只裁剪展示结果。",
        },
    }


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            self.send_json({"ok": True})
            return
        if parsed.path == "/":
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/api/trends":
            self.send_error(404)
            return

        try:
            length = int(self.headers.get("content-length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            self.send_json(build_response(payload))
        except Exception as exc:
            self.send_json({"error": str(exc)}, status=400)

    def send_json(self, payload: dict, status: int = 200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"ABA Trend Viewer: http://127.0.0.1:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
