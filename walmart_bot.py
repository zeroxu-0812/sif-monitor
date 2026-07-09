#!/usr/bin/env python3
"""
飞书群聊 @机器人 触发 Walmart 商品爬取（长连接服务）。

镜像 feishu_bot.py（funnel 机器人）的成功范式，互不冲突：
  群里 @机器人 + Walmart 意图（含触发词 + 英文商品名 + 可选排序/页数）
    例：「@机器人 帮我查 walmart mechanical keyboard 价格从低到高 2页」
  → 解析出 (商品名, 排序, 页数)
  → curl_cffi 爬取 Walmart 搜索结果
  → 在飞书电子表格建新工作表写入结果
  → 群里回复含「打开表格」按钮的卡片

长连接模式（lark-oapi ws），本地可跑，无需公网/内网穿透。

前置（飞书开放平台配置，否则收不到消息）：
  1. 应用 → 添加应用能力 → 勾选「机器人」
  2. 事件与回调 → 订阅方式「使用长连接接收事件」→ 添加 im.message.receive_v1
  3. 权限：im:message.group_at_msg:readonly（收@消息）、im:message:send_as_bot（发消息）
  4. 发布新版本 + 管理员审批 + 机器人加群
  5. 先本地跑本脚本建立长连接，再去平台保存订阅方式

运行：
  .venv/bin/python walmart_bot.py
  → 看到 "connected to wss://..." 即长连接成功

注意：若与 funnel 机器人共用同一个飞书应用，同一应用只能由一个 ws 进程接收事件。
建议为 Walmart 单独建一个应用（独立 app_id/secret），避免事件争抢。
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime
from urllib.parse import urlparse

import lark_oapi as lark
from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody

import auth  # noqa: F401  副作用：加载 .env
import walmart_config as cfg
from sif_feishu_sync import FeishuClient
from walmart_report import build_walmart_link_card, publish_walmart_to_sheet
from walmart_scraper import SORT_OPTIONS, scrape_walmart


# ============================================================================
# lark API client（发消息用，与 ws.Client 分离——ws.Client 只收不发）
# ============================================================================

_api_client = (
    lark.Client.builder()
    .app_id(cfg.FEISHU_APP_ID)
    .app_secret(cfg.FEISHU_APP_SECRET)
    .log_level(lark.LogLevel.INFO)
    .build()
)


# ============================================================================
# 回复工具
# ============================================================================

def _reply_text(message_id: str, text: str) -> None:
    req = (
        ReplyMessageRequest.builder()
        .message_id(message_id)
        .request_body(
            ReplyMessageRequestBody.builder()
            .msg_type("text")
            .content(json.dumps({"text": text}, ensure_ascii=False))
            .build()
        )
        .build()
    )
    resp = _api_client.im.v1.message.reply(req)
    if not resp.success():
        lark.logger.error(
            f"reply_text 失败: code={resp.code} msg={resp.msg} log_id={resp.get_log_id()}"
        )


def _reply_card(message_id: str, card: dict) -> None:
    req = (
        ReplyMessageRequest.builder()
        .message_id(message_id)
        .request_body(
            ReplyMessageRequestBody.builder()
            .msg_type("interactive")
            .content(json.dumps(card, ensure_ascii=False))
            .build()
        )
        .build()
    )
    resp = _api_client.im.v1.message.reply(req)
    if not resp.success():
        lark.logger.error(
            f"reply_card 失败: code={resp.code} msg={resp.msg} log_id={resp.get_log_id()}"
        )


# ============================================================================
# 智能解析：自然语言 → (is_at_bot, 商品名|None, 排序, 页数|None)
# ============================================================================

_WALMART_TRIGGERS = ("walmart", "沃尔玛", "瓦玛", "美亚")

# 排序短语 → URL 参数（顺序敏感：先匹配更长的词）
_SORT_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("price_low",  re.compile(r"(价格从低到高|价格低|从低到高|低价|price\s*low|low\s*to\s*high|price_low)", re.I)),
    ("price_high", re.compile(r"(价格从高到低|价格高|从高到低|高价|price\s*high|high\s*to\s*low|price_high)", re.I)),
    ("rating_high", re.compile(r"(评分最高|评分|好评|rating|highest\s*rating|rating_high)", re.I)),
    ("new",        re.compile(r"(最新|新品|newest|new)", re.I)),
]
_PAGES_RE = re.compile(r"(\d+)\s*(?:页|pages?|p)\b", re.I)
_AT_PLACEHOLDER_RE = re.compile(r"@_user_\d+")
_CHINESE_RE = re.compile(r"[一-鿿]+")


def _detect_sort(text: str) -> tuple[str, str]:
    """返回 (清洗后的 text, sort 值)。默认 best_match。"""
    for value, pat in _SORT_PATTERNS:
        if pat.search(text):
            text = pat.sub(" ", text)
            return text, value
    return text, "best_match"


def _token_from_url(url: str) -> str:
    """从飞书 wiki/sheet 链接末段取 token。"""
    if not url:
        return ""
    last = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    return last


def parse_walmart_intent(msg) -> tuple[bool, "str | None", str, "int | None"]:
    """解析消息。

    返回 (is_at_bot, 商品名 or None, sort, pages or None)。
    - is_at_bot：mentions 含 mentioned_type == 'app'
    - 商品名：英文产品词（Walmart 商品名为英文），未识别返回 None
    - sort：默认 best_match
    - pages：未指定返回 None（调用方用默认值）
    """
    mentions = msg.mentions or []
    is_at_bot = any(getattr(m, "mentioned_type", None) == "app" for m in mentions)

    if msg.message_type != "text":
        return is_at_bot, None, "best_match", None

    try:
        text = json.loads(msg.content).get("text", "")
    except (TypeError, ValueError, json.JSONDecodeError):
        return is_at_bot, None, "best_match", None

    clean = _AT_PLACEHOLDER_RE.sub("", text)
    lowered = clean.lower()

    # 必须含 Walmart 触发词，否则不是本机器人的活
    if not any(t in lowered for t in _WALMART_TRIGGERS):
        return is_at_bot, None, "best_match", None

    # 1) 页数
    pages = None
    m = _PAGES_RE.search(clean)
    if m:
        pages = int(m.group(1))
        clean = _PAGES_RE.sub(" ", clean)

    # 2) 排序
    clean, sort = _detect_sort(clean)

    # 3) 去触发词 → 去中文 → 取最长英文/数字/连字符 连续词组作为商品名
    for t in _WALMART_TRIGGERS:
        clean = re.sub(re.escape(t), " ", clean, flags=re.I)
    no_chinese = _CHINESE_RE.sub(" ", clean)
    matches = re.findall(r"[a-zA-Z][a-zA-Z0-9\s\-]*", no_chinese)
    candidates = [re.sub(r"\s+", " ", x).strip() for x in matches]
    candidates = [c for c in candidates if len(c) >= 2]
    product = max(candidates, key=len) if candidates else None

    return is_at_bot, product, sort, pages


# ============================================================================
# 业务：爬取 + 写飞书表
# ============================================================================

def run_walmart(query: str, sort: str, pages: int) -> dict:
    """爬取 Walmart 并写入飞书新工作表。返回 report dict（含 url/sheet_title/summary）。"""
    rows = scrape_walmart(
        query, pages=pages, sort=sort,
        on_progress=lambda msg: lark.logger.info(msg),
    )
    if not rows:
        raise RuntimeError(f"Walmart 未返回「{query}」的商品（可能被风控或无结果）")

    client = FeishuClient(cfg.FEISHU_APP_ID, cfg.FEISHU_APP_SECRET)

    # 解析输出表格 token
    token_like = cfg.WALMART_SPREADSHEET_TOKEN or _token_from_url(cfg.WALMART_SPREADSHEET_WIKI_URL)
    if token_like:
        spreadsheet_token = client.get_spreadsheet_token(token_like)
        spreadsheet_url = cfg.WALMART_SPREADSHEET_WIKI_URL
    else:
        # 未配置表格 → 新建一个电子表格
        info = client.create_spreadsheet(
            title="Walmart 商品爬取结果", folder_token=cfg.WALMART_REPORT_FOLDER_TOKEN
        )
        spreadsheet_token = info["spreadsheet_token"]
        spreadsheet_url = info["url"]

    return publish_walmart_to_sheet(
        client,
        spreadsheet_token,
        spreadsheet_url,
        rows=rows,
        query=query,
        sort=sort,
        generated_at=datetime.now(),
    )


# ============================================================================
# 事件回调
# ============================================================================

def _handle_message_receive(data) -> None:
    msg = data.event.message
    is_at_bot, product, sort, pages = parse_walmart_intent(msg)
    if not is_at_bot:
        return  # 没被 @，忽略

    if not product:
        _reply_text(
            msg.message_id,
            "👋 我是 Walmart 商品爬取机器人，可以帮你抓 Walmart 搜索结果并写入飞书表格。\n"
            "用法示例（@我 之后发）：\n"
            "  • 查 walmart mechanical keyboard\n"
            "  • walmart airpods pro 价格从低到高 2页\n"
            "  • 沃尔玛 wireless mouse 评分\n"
            "可选排序：价格从低到高 / 价格从高到低 / 评分 / 最新",
        )
        return

    # 页数兜底 + 上限
    pages = pages or cfg.WALMART_DEFAULT_PAGES
    pages = max(1, min(pages, cfg.WALMART_MAX_PAGES))
    sort_label = dict(SORT_OPTIONS).get(sort, sort)

    lark.logger.info(f"收到 Walmart 查询: product={product!r} sort={sort} pages={pages} chat={msg.chat_id}")
    # 立即 ack，避免飞书 3 秒超时重推
    _reply_text(
        msg.message_id,
        f"收到，正在爬取 Walmart「{product}」（{sort_label}，{pages} 页）并写入表格，请稍候...",
    )

    # 重活丢后台线程（爬取 + 建表耗时）
    def worker():
        try:
            report = run_walmart(product, sort, pages)
            card = build_walmart_link_card(
                query=product, sort=sort, report=report, generated_at=datetime.now()
            )
            _reply_card(msg.message_id, card)
            lark.logger.info(
                f"完成: product={product!r} count={report.get('count')} sheet={report['sheet_title']}"
            )
        except Exception as exc:  # noqa: BLE001
            lark.logger.exception("walmart worker 失败")
            _reply_text(msg.message_id, f"❌ 爬取「{product}」失败：{exc}")

    threading.Thread(target=worker, daemon=True).start()


# ============================================================================
# 长连接启动
# ============================================================================

def main() -> int:
    event_handler = (
        lark.EventDispatcherHandler.builder("", "")  # 长连接：两个空字符串
        .register_p2_im_message_receive_v1(_handle_message_receive)
        .build()
    )
    cli = lark.ws.Client(
        cfg.FEISHU_APP_ID,
        cfg.FEISHU_APP_SECRET,
        event_handler=event_handler,
        log_level=lark.LogLevel.INFO,
    )
    print("=" * 60)
    print("飞书 Walmart 爬取机器人启动（长连接模式）")
    print(f"应用: {cfg.FEISHU_APP_ID or '(未配置!)'}")
    print(f"输出表格: {cfg.WALMART_SPREADSHEET_WIKI_URL or '(未配置 → 每次新建)'}")
    print("等待群消息… 在群里 @机器人 发「查 walmart keyboard 价格从低到高 2页」")
    print("=" * 60)
    cli.start()  # 阻塞
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
