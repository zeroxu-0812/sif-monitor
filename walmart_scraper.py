#!/usr/bin/env python3
"""
Walmart 商品搜索爬虫 —— 静默库版（供飞书机器人 import 调用）。

与 midsummer/curl-cfii-scraper/scraper.py 同源，去掉了 CLI / 交互 / print，
只保留可 import 的核心：

    from walmart_scraper import scrape_walmart
    rows = scrape_walmart("mechanical keyboard", pages=2, sort="price_low")

核心逻辑：
  1. curl_cffi.requests.get(..., impersonate="chrome") 模拟 Chrome TLS 指纹绕过反爬。
  2. Walmart 全量数据在 <script id="__NEXT_DATA__"> 的 JSON 里，路径：
        props.pageProps.initialData.searchResult.itemStacks[].items[]
  3. 按关键词搜索 + 翻页 + 按 usItemId 全局去重。
  4. 可选 on_progress 回调，供调用方（如飞书机器人）透传"正在爬第 N 页"等状态。

依赖：curl_cffi、beautifulsoup4（已加入机器人 venv）。
"""

from __future__ import annotations

import json
import random
import time
from typing import Callable

from bs4 import BeautifulSoup
from curl_cffi import requests

BASE_URL = "https://www.walmart.com/search"
HOME_URL = "https://www.walmart.com"
IMPERSONATE = "chrome"          # curl_cffi 指纹类型
EXTRA_HEADERS = {"accept-language": "en-US,en;q=0.9"}

# CSV / 表格列与顺序
FIELDS = [
    "usItemId", "name", "brand", "price", "wasPrice",
    "averageRating", "numberOfReviews", "sellerName", "availability",
    "isSponsored", "thumbnailUrl", "productUrl", "page",
]

# Walmart 搜索支持的排序方式：(URL 参数值, 中文说明)
SORT_OPTIONS = [
    ("best_match",  "综合排序 (Best Match)"),
    ("price_low",   "价格从低到高"),
    ("price_high",  "价格从高到低"),
    ("rating_high", "评分最高"),
    ("new",         "最新上架"),
]
DEFAULT_SORT = "best_match"
SORT_VALUES = {v for v, _ in SORT_OPTIONS}


def fetch_page(keyword: str, page: int, sort: str = DEFAULT_SORT,
               timeout: int = 30, retries: int = 3):
    """请求单页搜索结果，失败/被风控时指数退避重试。返回 Response。"""
    params = {"q": keyword, "page": page}
    if sort and sort != DEFAULT_SORT:
        params["sort"] = sort
    last_err = "unknown"
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(BASE_URL, params=params, impersonate=IMPERSONATE,
                                headers=EXTRA_HEADERS, timeout=timeout)
            if resp.status_code == 200 and "__NEXT_DATA__" in resp.text:
                return resp
            last_err = f"HTTP {resp.status_code} 或缺少 __NEXT_DATA__"
        except Exception as e:  # 网络异常等
            last_err = repr(e)
        if attempt < retries:
            time.sleep(2 * attempt + random.random())
    raise RuntimeError(f"第 {page} 页抓取失败（{keyword!r}）：{last_err}")


def _next_data(html: str) -> dict:
    """从 HTML 中解析出 __NEXT_DATA__ JSON。"""
    soup = BeautifulSoup(html, "html.parser")
    nx = soup.find("script", id="__NEXT_DATA__")
    raw = nx.string if nx else None
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def normalize_item(it: dict, page: int) -> dict:
    """把单个原始 item 映射成扁平行（字段同 FIELDS）。"""
    price = it.get("priceInfo") or {}
    avail = it.get("availabilityStatusV2") or {}
    img = it.get("imageInfo") or {}
    canon = it.get("canonicalUrl") or ""
    url = HOME_URL + canon if canon.startswith("/") else canon
    return {
        "usItemId": it.get("usItemId") or "",
        "name": (it.get("name") or "").strip(),
        "brand": it.get("brand") or "",
        "price": price.get("linePrice") or price.get("itemPrice") or "",
        "wasPrice": price.get("wasPrice") or "",
        "averageRating": it.get("averageRating") or "",
        "numberOfReviews": it.get("numberOfReviews") or "",
        "sellerName": it.get("sellerName") or "",
        "availability": avail.get("display") or "",
        "isSponsored": bool(it.get("sponsoredProduct")),
        "thumbnailUrl": img.get("thumbnailUrl") or "",
        "productUrl": url,
        "page": page,
    }


def extract_items(html: str, page: int) -> list[dict]:
    """从一页 HTML 中提取并归一化所有商品。"""
    data = _next_data(html)
    search_result = (data.get("props", {})
                         .get("pageProps", {})
                         .get("initialData", {})
                         .get("searchResult", {}))
    stacks = search_result.get("itemStacks", []) or []
    items = []
    for stack in stacks:
        for it in stack.get("items", []) or []:
            items.append(normalize_item(it, page))
    return items


def _crawl(keyword: str, max_pages: int, delay_min: float, delay_max: float,
           sort: str, seen: set[str],
           on_progress: Callable[[str], None] | None = None) -> list[dict]:
    """按关键词爬取最多 max_pages 页，按 usItemId 去重（逐条增量）。"""
    results: list[dict] = []
    for page in range(1, max_pages + 1):
        try:
            resp = fetch_page(keyword, page, sort=sort)
        except RuntimeError as e:
            if on_progress:
                on_progress(f"第 {page} 页抓取失败，终止：{e}")
            break

        items = extract_items(resp.text, page)
        new_items = []
        for it in items:
            uid = it["usItemId"]
            if uid and uid not in seen:
                seen.add(uid)
                new_items.append(it)
        results.extend(new_items)

        if on_progress:
            on_progress(f"{keyword} 第 {page}/{max_pages} 页：新增 {len(new_items)} 条，累计 {len(results)} 条")

        if not items:
            break
        if page > 1 and not new_items:
            break
        if page < max_pages:
            time.sleep(random.uniform(delay_min, delay_max))
    return results


def scrape_walmart(
    query: "str | list[str]",
    pages: int = 3,
    sort: str = DEFAULT_SORT,
    delay_min: float = 1.5,
    delay_max: float = 3.0,
    on_progress: Callable[[str], None] | None = None,
) -> list[dict]:
    """自定义爬取接口：传入搜索栏关键词，返回与 FIELDS 同格式的商品列表（静默）。

    参数:
        query:        搜索关键词，str 或 list[str]（多关键词按 usItemId 全局去重合并）。
        pages:        每个关键词的最大翻页数。
        sort:         排序方式，见 SORT_OPTIONS。
        delay_min/max: 每页请求之间的随机延时秒数（礼貌限速）。
        on_progress:  可选进度回调 fn(str)，调用方据此透传状态（如发飞书消息）。
    返回:
        list[dict] —— 每个元素是一行商品，字段同 FIELDS。
    """
    queries = [query] if isinstance(query, str) else list(query)
    if sort not in SORT_VALUES:
        raise ValueError(f"非法 sort={sort!r}，可选：{sorted(SORT_VALUES)}")
    seen: set[str] = set()
    rows: list[dict] = []
    for kw in queries:
        if on_progress:
            on_progress(f"开始爬取：{kw}（{dict(SORT_OPTIONS).get(sort, sort)}，{pages} 页）")
        rows.extend(_crawl(kw, pages, delay_min, delay_max, sort, seen, on_progress))
    return rows
