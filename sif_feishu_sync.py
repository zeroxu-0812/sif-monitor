#!/usr/bin/env python3
"""
从飞书读取关键词，批量抓取 Sif 周/月趋势，并将排名数据写回飞书表格。

默认写入格式：
1. 周趋势和月趋势分别写入不同工作表
2. A 列为关键词
3. B 列开始为日期列
4. 单元格值为对应日期的 `searchesRank`
"""

import argparse
import json
import sys
import time
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import requests

import auth
import feishu_config
import sif_config

SIF_TRENDS_URL = "https://new.sif.com/api/search/compare/estSearchesNumHistory"
FEISHU_TENANT_TOKEN_URL = (
    "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
)
FEISHU_SHEET_META_URL = "https://open.feishu.cn/open-apis/sheets/v3/spreadsheets/{token}"
FEISHU_SHEETS_QUERY_URL = (
    "https://open.feishu.cn/open-apis/sheets/v3/spreadsheets/{token}/sheets/query"
)
FEISHU_VALUES_URL = "https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{token}/values/{range}"
FEISHU_VALUES_UPDATE_URL = "https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{token}/values"
FEISHU_DIMENSION_RANGE_URL = (
    "https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{token}/dimension_range"
)
FEISHU_CREATE_SPREADSHEET_URL = "https://open.feishu.cn/open-apis/sheets/v3/spreadsheets"
FEISHU_BATCH_UPDATE_SHEETS_URL = (
    "https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{token}/sheets_batch_update"
)
FEISHU_STYLE_URL = (
    "https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{token}/style"
)
MAX_KEYWORDS_PER_BATCH = 10
DEFAULT_CHECKPOINT_FILE = Path("sif_sync_checkpoint.json")


def request_json(method, url, *, retries=5, retry_delay=1.5, timeout=30, **kwargs):
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            response = requests.request(method, url, timeout=timeout, **kwargs)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt == retries:
                break
            sleep_seconds = retry_delay * attempt
            print(f"请求失败，{sleep_seconds:.1f}s 后重试 ({attempt}/{retries})：{exc}")
            time.sleep(sleep_seconds)

    raise RuntimeError(f"请求失败，已重试 {retries} 次: {last_error}") from last_error


def load_checkpoint(checkpoint_file, keywords, batch_size):
    if not checkpoint_file.exists():
        return None

    try:
        payload = json.loads(checkpoint_file.read_text(encoding="utf-8"))
    except Exception:
        return None

    if payload.get("keywords") != keywords or payload.get("batch_size") != batch_size:
        return None

    return payload


def save_checkpoint(checkpoint_file, keywords, batch_size, next_batch_index, weekly, monthly):
    payload = {
        "keywords": keywords,
        "batch_size": batch_size,
        "next_batch_index": next_batch_index,
        "weekly": weekly,
        "monthly": monthly,
    }
    checkpoint_file.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def remove_checkpoint(checkpoint_file):
    if checkpoint_file.exists():
        checkpoint_file.unlink()


class FeishuClient:
    """飞书表格最小客户端。"""

    def __init__(self, app_id, app_secret):
        self.app_id = app_id
        self.app_secret = app_secret
        self._tenant_access_token = None
        self._token_expires_at = 0.0

    def _request(self, method, url, **kwargs):
        payload = request_json(method, url, timeout=30, **kwargs)
        if payload.get("code") not in (0, None):
            raise RuntimeError(
                f"飞书接口失败: code={payload.get('code')} msg={payload.get('msg')}"
            )
        return payload

    def get_tenant_access_token(self):
        # 缓存 token 并记录过期时间（飞书 tenant_access_token 默认 2 小时），
        # 提前 5 分钟刷新，避免长批量任务中途 token 过期导致 401。
        if self._tenant_access_token and time.time() < self._token_expires_at:
            return self._tenant_access_token

        payload = {"app_id": self.app_id, "app_secret": self.app_secret}
        data = self._request("POST", FEISHU_TENANT_TOKEN_URL, json=payload)
        self._tenant_access_token = data["tenant_access_token"]
        expire = int(data.get("expire", 2 * 60 * 60))
        self._token_expires_at = time.time() + max(expire - 5 * 60, 60)
        return self._tenant_access_token

    def _headers(self):
        return {"Authorization": f"Bearer {self.get_tenant_access_token()}"}

    def get_spreadsheet_token(self, token_like):
        """
        尝试把配置里的 token 解析成 spreadsheet_token。
        如果它本身已经是 spreadsheet_token，也直接返回。
        """
        try:
            data = self._request(
                "GET",
                FEISHU_SHEET_META_URL.format(token=token_like),
                headers=self._headers(),
            )
            return data["data"]["spreadsheet"]["token"]
        except requests.RequestException:
            # 网络/HTTP 层错误不掩盖，直接抛出，避免静默返回错误 token
            raise
        except (RuntimeError, KeyError):
            # token 无效或响应结构不符（可能是 wiki token），按原样返回交给后续处理
            return token_like

    def read_range(self, spreadsheet_token, sheet_id, range_str):
        encoded_range = quote(f"{sheet_id}!{range_str}", safe="!:$")
        url = FEISHU_VALUES_URL.format(token=spreadsheet_token, range=encoded_range)
        data = self._request(
            "GET",
            url,
            headers=self._headers(),
            params={"valueRenderOption": "UnformattedValue"},
        )
        return data.get("data", {}).get("valueRange", {}).get("values", [])

    def write_range(self, spreadsheet_token, sheet_id, range_str, values):
        raw_range = f"{sheet_id}!{range_str}"
        url = FEISHU_VALUES_UPDATE_URL.format(token=spreadsheet_token)
        payload = {"valueRange": {"range": raw_range, "values": values}}
        self._request(
            "PUT",
            url,
            headers={**self._headers(), "Content-Type": "application/json"},
            json=payload,
        )

    def list_sheets(self, spreadsheet_token):
        data = self._request(
            "GET",
            FEISHU_SHEETS_QUERY_URL.format(token=spreadsheet_token),
            headers=self._headers(),
        )
        return data.get("data", {}).get("sheets", [])

    def get_sheet_info(self, spreadsheet_token, sheet_id):
        for sheet in self.list_sheets(spreadsheet_token):
            if sheet.get("sheet_id") == sheet_id:
                return sheet
        raise RuntimeError(f"找不到工作表: {sheet_id}")

    def add_dimension(self, spreadsheet_token, sheet_id, major_dimension, length):
        if length <= 0:
            return

        payload = {
            "dimension": {
                "sheetId": sheet_id,
                "majorDimension": major_dimension,
                "length": length,
            }
        }
        self._request(
            "POST",
            FEISHU_DIMENSION_RANGE_URL.format(token=spreadsheet_token),
            headers={**self._headers(), "Content-Type": "application/json"},
            json=payload,
        )

    def set_cell_styles(self, spreadsheet_token, sheet_id, style_ranges):
        """批量设置单元格数字格式（用于价格→货币、比率→百分比）。

        Args:
            style_ranges: list[dict]，每个 {
                "start_row", "end_row": 行半开区间（0 = 表头行），
                "start_col", "end_col": 列半开区间（0 = A 列），
                "type":   "CURRENCY" / "PERCENT" / ...,
                "pattern": "$#,##0.00" / "0.00%" / ...,
            }
        """
        if not style_ranges:
            return
        url = FEISHU_STYLE_URL.format(token=spreadsheet_token)
        data = [
            {
                "sheetId": sheet_id,
                "startRowIndex": r["start_row"],
                "endRowIndex": r["end_row"],
                "startColumnIndex": r["start_col"],
                "endColumnIndex": r["end_col"],
                "style": {
                    "numberFormat": {"type": r["type"], "pattern": r["pattern"]}
                },
            }
            for r in style_ranges
        ]
        self._request(
            "PUT",
            url,
            headers={**self._headers(), "Content-Type": "application/json"},
            json={"data": data},
        )

    def set_range_formatter(self, spreadsheet_token, sheet_id, range_str, formatter):
        """设置某范围的数字格式（货币 $#,##0.00 / 百分比 0.00% 等）。

        飞书设置样式的正确请求体是 appendStyle（PUT /style）：
          {"appendStyle": {"range": "<sheet>!A1:B2", "style": {"formatter": "..."}}}
        range_str 为不含 sheet_id 的范围，如 'G2:G55'。
        """
        url = FEISHU_STYLE_URL.format(token=spreadsheet_token)
        body = {
            "appendStyle": {
                "range": f"{sheet_id}!{range_str}",
                "style": {"formatter": formatter},
            }
        }
        self._request(
            "PUT",
            url,
            headers={**self._headers(), "Content-Type": "application/json"},
            json=body,
        )

    def create_spreadsheet(self, title, folder_token=None):
        """新建电子表格。folder_token 为 None 时建在应用根目录。

        返回 {"spreadsheet_token", "url", "title"}。
        """
        body = {"title": title}
        if folder_token:
            body["folder_token"] = folder_token
        data = self._request(
            "POST",
            FEISHU_CREATE_SPREADSHEET_URL,
            headers={**self._headers(), "Content-Type": "application/json"},
            json=body,
        )
        spreadsheet = data["data"]["spreadsheet"]
        return {
            "spreadsheet_token": spreadsheet["spreadsheet_token"],
            "url": spreadsheet.get("url", ""),
            "title": spreadsheet.get("title", title),
        }

    def create_sheet(self, spreadsheet_token, title, index=0):
        """在电子表格中新建工作表，index 默认 0（插在最前，让最新一期默认可见）。

        返回 {"sheet_id", "title", "index"}。
        """
        body = {
            "requests": [
                {"addSheet": {"properties": {"title": title, "index": index}}}
            ]
        }
        data = self._request(
            "POST",
            FEISHU_BATCH_UPDATE_SHEETS_URL.format(token=spreadsheet_token),
            headers={**self._headers(), "Content-Type": "application/json"},
            json=body,
        )
        props = data["data"]["replies"][0]["addSheet"]["properties"]
        return {
            "sheet_id": props["sheetId"],
            "title": props.get("title", title),
            "index": props.get("index", index),
        }


def col_index_to_name(index):
    """0 -> A, 25 -> Z, 26 -> AA"""
    result = []
    while True:
        index, remainder = divmod(index, 26)
        result.append(chr(65 + remainder))
        if index == 0:
            break
        index -= 1
    return "".join(reversed(result))


def normalize_keywords(values, keyword_column=0, skip_header=True):
    keywords = []
    start_index = 1 if skip_header else 0

    for row in values[start_index:]:
        if keyword_column >= len(row):
            continue
        keyword = str(row[keyword_column]).strip()
        if keyword and keyword not in keywords:
            keywords.append(keyword)

    return keywords


def fetch_trends_batch(keywords, granularity, _force_login=False):
    if not keywords:
        return {}

    token = auth.get_token(force_login=_force_login)
    payload = {"keywords": keywords[:MAX_KEYWORDS_PER_BATCH], "granularity": granularity}
    params = auth.build_sif_query_params(country="US")
    headers = {
        "accept": "application/json",
        "authorization": token,
        "content-type": "application/json",
    }

    data = request_json(
        "POST",
        SIF_TRENDS_URL,
        params=params,
        headers=headers,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        timeout=30,
    )

    if data.get("code") != 1:
        # code=-7 = 鉴权失效（token 中途过期）。强制重登拿新 token 重试一次，
        # 对齐 auth.api_post 的 _needs_auth_retry 自愈模式。fetch_trends_batch 走
        # new.sif.com 绕过了 api_post，原本缺这层重试，长批量跑到中途会崩。
        if data.get("code") == -7 and not _force_login:
            print("  token 失效(code=-7)，强制重登后重试本批")
            return fetch_trends_batch(keywords, granularity, _force_login=True)
        raise RuntimeError(
            f"Sif 趋势接口失败: code={data.get('code')} msg={data.get('message') or data.get('msg')}"
        )

    return data.get("data", {}).get("estSearchesNumHistory", {})


def fetch_all_trends(
    keywords,
    batch_size=10,
    delay=0.5,
    checkpoint_file=DEFAULT_CHECKPOINT_FILE,
):
    weekly = {}
    monthly = {}
    start_batch_index = 0

    checkpoint = load_checkpoint(checkpoint_file, keywords, batch_size)
    if checkpoint:
        weekly = checkpoint.get("weekly", {})
        monthly = checkpoint.get("monthly", {})
        start_batch_index = checkpoint.get("next_batch_index", 0)
        print(f"检测到 checkpoint，从第 {start_batch_index + 1} 批继续")

    total_batches = (len(keywords) + batch_size - 1) // batch_size

    for zero_based_index, start in enumerate(range(0, len(keywords), batch_size)):
        if zero_based_index < start_batch_index:
            continue

        batch_index = zero_based_index + 1
        batch = keywords[start : start + batch_size]
        print(f"抓取批次 {batch_index}/{total_batches}，关键词数 {len(batch)}")

        weekly.update(fetch_trends_batch(batch, "week"))
        monthly.update(fetch_trends_batch(batch, "month"))
        save_checkpoint(
            checkpoint_file,
            keywords,
            batch_size,
            zero_based_index + 1,
            weekly,
            monthly,
        )

        print(f"  当前累计: 周 {len(weekly)}，月 {len(monthly)}")
        if batch_index < total_batches and delay > 0:
            time.sleep(delay)

    return weekly, monthly


def collect_all_dates(trends_data):
    ordered_dates = OrderedDict()
    for entry in trends_data.values():
        for date_value in entry.get("date", []):
            ordered_dates.setdefault(str(date_value), None)
    return list(ordered_dates.keys())


def build_rank_matrix(trends_data, keyword_order):
    dates = collect_all_dates(trends_data)
    rows = [["关键词", *dates]]

    for keyword in keyword_order:
        trend = trends_data.get(keyword, {})
        rank_by_date = {}
        for idx, date_value in enumerate(trend.get("date", [])):
            ranks = trend.get("searchesRank", [])
            rank_by_date[str(date_value)] = ranks[idx] if idx < len(ranks) else ""

        row = [keyword]
        for date_value in dates:
            row.append(rank_by_date.get(date_value, ""))
        rows.append(row)

    return rows


def filter_trends_by_start_date(trends_data, start_date_str, date_format):
    if not start_date_str:
        return trends_data

    start_date = datetime.strptime(start_date_str, date_format)
    filtered = {}

    for keyword, trend in trends_data.items():
        dates = trend.get("date", [])
        searches = trend.get("estSearchesNum", [])
        ranks = trend.get("searchesRank", [])

        kept_dates = []
        kept_searches = []
        kept_ranks = []

        for idx, date_value in enumerate(dates):
            try:
                parsed_date = datetime.strptime(str(date_value), date_format)
            except (ValueError, TypeError):
                # 日期格式异常时跳过，避免单个脏数据拖垮整批同步
                continue
            if parsed_date < start_date:
                continue

            kept_dates.append(date_value)
            kept_searches.append(searches[idx] if idx < len(searches) else "")
            kept_ranks.append(ranks[idx] if idx < len(ranks) else "")

        filtered[keyword] = {
            "date": kept_dates,
            "estSearchesNum": kept_searches,
            "searchesRank": kept_ranks,
        }

    return filtered


def pad_matrix(values, min_rows, min_cols):
    padded = [list(row) + [""] * (max(min_cols, len(row)) - len(row)) for row in values]
    width = max([min_cols, *(len(row) for row in padded)] if padded else [min_cols])

    for row in padded:
        if len(row) < width:
            row.extend([""] * (width - len(row)))

    while len(padded) < min_rows:
        padded.append([""] * width)

    return padded


def build_probe_range(matrix, row_buffer=20, col_buffer=6):
    rows = max(len(matrix) + row_buffer, 100)
    cols = max(max((len(row) for row in matrix), default=0) + col_buffer, 26)
    last_col = col_index_to_name(cols - 1)
    return f"A1:{last_col}{rows}"


def overwrite_sheet_with_matrix(client, spreadsheet_token, sheet_id, matrix):
    sheet_info = client.get_sheet_info(spreadsheet_token, sheet_id)
    grid = sheet_info.get("grid_properties", {})
    current_rows = int(grid.get("row_count", 0))
    current_cols = int(grid.get("column_count", 0))

    probe_range = build_probe_range(matrix)

    try:
        existing_values = client.read_range(spreadsheet_token, sheet_id, probe_range)
    except RuntimeError as exc:
        if "90221" in str(exc):
            existing_values = []
        else:
            raise

    old_rows = len(existing_values)
    old_cols = max((len(row) for row in existing_values), default=0)
    new_rows = len(matrix)
    new_cols = max((len(row) for row in matrix), default=0)

    rows = max(old_rows, new_rows)
    cols = max(old_cols, new_cols)

    client.add_dimension(spreadsheet_token, sheet_id, "ROWS", rows - current_rows)
    client.add_dimension(spreadsheet_token, sheet_id, "COLUMNS", cols - current_cols)

    final_matrix = pad_matrix(matrix, rows, cols)

    row_chunk_size = 200
    col_chunk_size = 30

    for start_col in range(0, cols, col_chunk_size):
        end_col = min(start_col + col_chunk_size, cols)
        col_start_name = col_index_to_name(start_col)
        col_end_name = col_index_to_name(end_col - 1)

        for start_row in range(0, rows, row_chunk_size):
            chunk = [
                row[start_col:end_col]
                for row in final_matrix[start_row : start_row + row_chunk_size]
            ]
            row_start = start_row + 1
            row_end = start_row + len(chunk)
            range_str = f"{col_start_name}{row_start}:{col_end_name}{row_end}"
            client.write_range(spreadsheet_token, sheet_id, range_str, chunk)

    return rows, cols


def read_keywords_from_feishu(client, spreadsheet_token, sheet_id, keyword_column=0):
    values = client.read_range(spreadsheet_token, sheet_id, "A1:Z5000")
    keywords = normalize_keywords(values, keyword_column=keyword_column, skip_header=True)
    print(f"从飞书读取到 {len(keywords)} 个关键词")
    return keywords


def sync_trends_to_feishu(
    client,
    trends_wiki_token,
    weekly_sheet_id,
    monthly_sheet_id,
    keywords,
    batch_size,
    delay,
    checkpoint_file,
):
    weekly_trends, monthly_trends = fetch_all_trends(
        keywords,
        batch_size=batch_size,
        delay=delay,
        checkpoint_file=checkpoint_file,
    )
    weekly_trends = filter_trends_by_start_date(weekly_trends, "2025-01-01", "%Y-%m-%d")
    monthly_trends = filter_trends_by_start_date(monthly_trends, "2024-01", "%Y-%m")

    spreadsheet_token = client.get_spreadsheet_token(trends_wiki_token)

    weekly_matrix = build_rank_matrix(weekly_trends, keywords)
    monthly_matrix = build_rank_matrix(monthly_trends, keywords)

    weekly_rows, weekly_cols = overwrite_sheet_with_matrix(
        client, spreadsheet_token, weekly_sheet_id, weekly_matrix
    )
    print(f"周趋势写入完成: {weekly_rows} 行 x {weekly_cols} 列")

    monthly_rows, monthly_cols = overwrite_sheet_with_matrix(
        client, spreadsheet_token, monthly_sheet_id, monthly_matrix
    )
    print(f"月趋势写入完成: {monthly_rows} 行 x {monthly_cols} 列")
    remove_checkpoint(checkpoint_file)


def main():
    parser = argparse.ArgumentParser(description="从飞书读取关键词并回写 Sif 趋势排名")
    parser.add_argument("--batch-size", type=int, default=sif_config.DEFAULT_BATCH_SIZE)
    parser.add_argument("--delay", type=float, default=sif_config.DEFAULT_DELAY)
    parser.add_argument("--keyword-column", type=int, default=0, help="关键词列索引，默认第 1 列")
    parser.add_argument("--keyword-sheet-id", default=feishu_config.KEYWORDS_SHEET_ID)
    parser.add_argument("--keyword-token", default=feishu_config.KEYWORDS_WIKI_TOKEN)
    parser.add_argument("--trends-token", default=feishu_config.TRENDS_WIKI_TOKEN)
    parser.add_argument("--weekly-sheet-id", default=feishu_config.TRENDS_SHEET_ID_WEEKLY)
    parser.add_argument("--monthly-sheet-id", default=feishu_config.TRENDS_SHEET_ID_MONTHLY)
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 个关键词，0 表示全部")
    parser.add_argument(
        "--checkpoint-file",
        default=str(DEFAULT_CHECKPOINT_FILE),
        help="批量抓取 checkpoint 文件，用于失败后续跑",
    )
    args = parser.parse_args()

    if args.batch_size < 1 or args.batch_size > MAX_KEYWORDS_PER_BATCH:
        raise ValueError(f"--batch-size 必须在 1 到 {MAX_KEYWORDS_PER_BATCH} 之间")

    try:
        client = FeishuClient(feishu_config.FEISHU_APP_ID, feishu_config.FEISHU_APP_SECRET)

        keyword_spreadsheet_token = client.get_spreadsheet_token(args.keyword_token)
        keywords = read_keywords_from_feishu(
            client,
            keyword_spreadsheet_token,
            args.keyword_sheet_id,
            keyword_column=args.keyword_column,
        )

        if args.limit > 0:
            keywords = keywords[: args.limit]
            print(f"测试模式启用，只处理前 {len(keywords)} 个关键词")

        if not keywords:
            raise RuntimeError("没有读取到可抓取的关键词")

        sync_trends_to_feishu(
            client,
            args.trends_token,
            args.weekly_sheet_id,
            args.monthly_sheet_id,
            keywords,
            batch_size=args.batch_size,
            delay=args.delay,
            checkpoint_file=Path(args.checkpoint_file),
        )
        print("全部完成")
    except auth.SIFAuthError as exc:
        print(f"\n❌ SIF 鉴权失败: {exc}")
        print("   请检查 .env 中 SIF_PHONE / SIF_PASSWORD 是否填写正确")
        print("   也可以删除 .sif_cookie_cache 和 .sif_token_cache 强制重新登录")
        sys.exit(2)


if __name__ == "__main__":
    main()
