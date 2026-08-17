#!/usr/bin/env python3
"""
飞书群聊 @机器人 触发 Funnel 查询（长连接服务）。

工作流：
  群里 @机器人 + 自然语言（含 funnel 意图 + 英文产品名）
    例：「@机器人 帮我查一下 magnesium cream 的 funnel」
  → 智能解析英文产品名（magnesium cream）
  → 执行 funnel 查询（exact 策略）
  → 在飞书 spreadsheet 建新工作表（名字含产品名）
  → 群里回复链接卡片

长连接模式（lark-oapi ws），本地可跑，无需公网/内网穿透。

前置（飞书开放平台配置，否则收不到消息）：
  1. 应用 → 添加应用能力 → 勾选「机器人」
  2. 事件与回调 → 订阅方式「使用长连接接收事件」→ 添加 im.message.receive_v1
  3. 权限：im:message.group_at_msg:readonly（收@消息）、im:message:send_as_bot（发消息）
  4. 发布新版本 + 管理员审批 + 机器人加群
  5. 先本地跑本脚本建立长连接，再去平台保存订阅方式

运行：
  .venv/bin/python feishu_bot.py
  → 看到 "connected to wss://..." 即长连接成功
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)

import auth
import feishu_config
from feishu_report import publish_funnel_to_sheet
from sif_feishu_sync import FeishuClient
from sif_keyword_funnel import fetch_keyword_funnel

# ============================================================================
# lark API client（发消息用，与 ws.Client 分离——ws.Client 只收不发）
# ============================================================================

_api_client = (
    lark.Client.builder()
    .app_id(feishu_config.FEISHU_APP_ID)
    .app_secret(feishu_config.FEISHU_APP_SECRET)
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


def _reply_card(message_id: str, product: str, sheet_url: str, sheet_title: str) -> None:
    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"🔍 {product} Funnel 查询结果"},
            "template": "green",
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        f"产品：**{product}**\n"
                        f"工作表：{sheet_title}\n"
                        f"点击下方按钮查看完整 funnel 数据（含相关市场词）"
                    ),
                },
            },
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "📊 打开 Funnel 工作表"},
                        "type": "primary",
                        "url": sheet_url,
                    }
                ],
            },
        ],
    }
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
# 智能解析：自然语言 → (is_at_bot, 英文产品名)
# ============================================================================

_FUNNEL_KEYWORDS = ("funnel", "漏斗")


def parse_intent(msg) -> tuple[bool, str | None]:
    """从消息解析：(是否@机器人, 英文产品名 or None)。

    规则（利用「产品名是英文」）：
    - @机器人：mentions 含 mentioned_type == 'app'
    - funnel 意图：文本含 funnel / 漏斗
    - 产品名：去 @占位符 → 去中文/标点 → 去 funnel 关键词 → 取最长英文连续词组
    """
    mentions = msg.mentions or []
    is_at_bot = any(getattr(m, "mentioned_type", None) == "app" for m in mentions)

    if msg.message_type != "text":
        return is_at_bot, None

    try:
        text = json.loads(msg.content).get("text", "")
    except (TypeError, ValueError, json.JSONDecodeError):
        return is_at_bot, None

    # 1. 去 @占位符（@_user_1 等）
    clean = re.sub(r"@_user_\d+", "", text)

    # 2. funnel 意图判断
    if not any(kw in clean.lower() for kw in _FUNNEL_KEYWORDS):
        return is_at_bot, None

    # 3. 去中文（产品名是英文）
    no_chinese = re.sub(r"[一-鿿]+", " ", clean)
    # 4. 去 funnel / 漏斗 关键词
    no_funnel = re.sub(r"funnel|漏斗", " ", no_chinese, flags=re.IGNORECASE)
    # 5. 提取产品名（字母开头，含字母/数字/空格/连字符——产品名常含数字如 d3/k2/b12）
    matches = re.findall(r"[a-zA-Z][a-zA-Z0-9\s\-]*", no_funnel)
    candidates = [re.sub(r"\s+", " ", m).strip() for m in matches]
    candidates = [c for c in candidates if len(c) >= 2]
    if not candidates:
        return is_at_bot, None

    # 取最长的候选作为产品名
    product = max(candidates, key=len)
    return is_at_bot, product


# ============================================================================
# 业务：对单个产品跑 funnel 查询 + 建工作表
# ============================================================================

def run_funnel_for_product(product: str) -> dict:
    """查询 product 的 funnel（exact）并写入飞书新工作表。返回 report dict。"""
    session = auth.get_session()
    records = fetch_keyword_funnel(
        [product],
        strategy="exact",
        country="US",
        session=session,
    )
    if not records:
        raise RuntimeError(f"Sif 未返回「{product}」的 funnel 数据")

    client = FeishuClient(feishu_config.FEISHU_APP_ID, feishu_config.FEISHU_APP_SECRET)
    spreadsheet_token = client.get_spreadsheet_token(feishu_config.TRENDS_WIKI_TOKEN)
    spreadsheet_url = feishu_config.FUNNEL_SPREADSHEET_WIKI_URL

    return publish_funnel_to_sheet(
        client,
        spreadsheet_token,
        spreadsheet_url,
        records=records,
        query_keywords=[product],
        strategy="exact",
        generated_at=datetime.now(),
    )


# ============================================================================
# 事件回调
# ============================================================================

def _handle_message_receive(data) -> None:
    msg = data.event.message
    is_at_bot, product = parse_intent(msg)
    if not is_at_bot:
        return  # 没被 @，忽略（不干扰群里普通消息）

    if not product:
        # 被 @ 了但不是 funnel 查询 → 友好提示用法
        _reply_text(
            msg.message_id,
            "👋 我是 Funnel 查询机器人，专门查关键词的 funnel 数据（搜索量 / CPC / 转化率 / Top ASIN）。\n"
            "用法示例：\n"
            "  • 帮我查一下 magnesium cream 的 funnel\n"
            "  • 查询 vitamin d3 k2 funnel\n"
            "  • 看看 airpods pro 的漏斗",
        )
        return

    lark.logger.info(f"收到 funnel 查询: product={product!r} chat={msg.chat_id}")
    # 立即 ack，避免飞书 3 秒超时重推
    _reply_text(msg.message_id, f"收到，正在查询「{product}」funnel 并建表，请稍候...")

    # 重活丢后台线程（funnel 查询 + 建表耗时）
    def worker():
        try:
            report = run_funnel_for_product(product)
            _reply_card(
                msg.message_id,
                product=product,
                sheet_url=report["url"],
                sheet_title=report["sheet_title"],
            )
            lark.logger.info(f"完成: product={product!r} sheet={report['sheet_title']}")
        except Exception as exc:
            lark.logger.exception("funnel worker 失败")
            _reply_text(msg.message_id, f"❌ 查询「{product}」失败：{exc}")

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
        feishu_config.FEISHU_APP_ID,
        feishu_config.FEISHU_APP_SECRET,
        event_handler=event_handler,
        log_level=lark.LogLevel.INFO,
    )
    print("=" * 60)
    print("飞书 Funnel 查询机器人启动（长连接模式）")
    print(f"应用: {feishu_config.FEISHU_APP_ID}")
    print("等待群消息… 在群里 @机器人 发「帮我查一下 XX 的 funnel」")
    print("=" * 60)
    cli.start()  # 阻塞
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
