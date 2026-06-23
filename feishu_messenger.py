#!/usr/bin/env python3
"""
飞书 IM 卡片消息推送。

- 复用 sif_feishu_sync.FeishuClient 的 tenant_access_token 获取能力
- 在此基础上增加 token 过期保护（提前 5 分钟刷新）
- 提供 send_card 与 build_monitor_card 两个核心能力
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Iterable

import requests

from sif_feishu_sync import FeishuClient, request_json
import monitor_config as cfg


FEISHU_SEND_MESSAGE_URL = "https://open.feishu.cn/open-apis/im/v1/messages"
FEISHU_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
TOKEN_SAFETY_MARGIN_SECONDS = 5 * 60   # 提前 5 分钟刷新
TOKEN_VALID_SECONDS = 2 * 60 * 60      # 飞书 tenant_access_token 默认 2 小时


class FeishuMessenger:
    """
    飞书应用消息发送器。

    与 FeishuClient 平级（独立维护 token），避免修改既有 FeishuClient 的行为。
    """

    def __init__(self, app_id: str, app_secret: str):
        self.app_id = app_id
        self.app_secret = app_secret
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    # ------------------------------------------------------------------
    # token 管理
    # ------------------------------------------------------------------

    def _refresh_token(self) -> None:
        payload = {"app_id": self.app_id, "app_secret": self.app_secret}
        data = request_json("POST", FEISHU_TOKEN_URL, json=payload, timeout=15)
        token = data.get("tenant_access_token")
        if not token:
            raise RuntimeError(
                f"飞书 tenant_access_token 获取失败: {data.get('msg') or data}"
            )
        expire = int(data.get("expire", TOKEN_VALID_SECONDS))
        self._token = token
        # expire 是飞书返回的剩余秒数；留出 safety margin
        self._token_expires_at = time.time() + max(expire - TOKEN_SAFETY_MARGIN_SECONDS, 60)

    def _get_token(self) -> str:
        if not self._token or time.time() >= self._token_expires_at:
            self._refresh_token()
        assert self._token is not None
        return self._token

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._get_token()}",
            "Content-Type": "application/json; charset=utf-8",
        }

    # ------------------------------------------------------------------
    # 发送
    # ------------------------------------------------------------------

    def send_card(self, chat_id: str, card: dict) -> dict:
        """
        向指定 chat_id 发送交互式卡片消息。

        参数:
            chat_id: 群聊 ID（如 oc_xxx）
            card: 卡片 JSON 对象（config / header / elements）

        返回飞书 API 响应 dict。
        """
        body = {
            "receive_id": chat_id,
            "msg_type": "interactive",
            "content": json.dumps(card, ensure_ascii=False),
        }
        resp = request_json(
            "POST",
            FEISHU_SEND_MESSAGE_URL,
            params={"receive_id_type": "chat_id"},
            headers=self._headers(),
            json=body,
            timeout=15,
        )
        if resp.get("code") not in (0, None):
            raise RuntimeError(
                f"飞书消息发送失败: code={resp.get('code')} msg={resp.get('msg')}"
            )
        return resp


# ============================================================================
# 卡片构造
# ============================================================================

def _fmt_pct(value: float | None) -> str:
    """把 0.123 格式化成 '+12.3%'；负值也带符号；None 返回 '-'。"""
    if value is None:
        return "-"
    sign = "+" if value >= 0 else ""
    return f"{sign}{value * 100:.1f}%"


def _fmt_rank(value: int | None) -> str:
    return "-" if value is None else f"{value:,}"


def _hr() -> dict:
    return {"tag": "hr"}


def _markdown_div(content: str) -> dict:
    return {"tag": "div", "text": {"tag": "lark_md", "content": content}}


def _tier_section(
    label: str,
    lo: int,
    hi: int,
    top_n: int,
    items: list[dict],
) -> list[dict]:
    """
    构造一个档位的 elements：标题 div + 列表 markdown + hr。
    返回 element 列表（已含 hr）。
    """
    emoji = cfg.TIER_EMOJI.get(label, "•")
    color = cfg.TIER_COLOR.get(label, "grey")
    header_line = (
        f"**{emoji} {label} ({lo:,}-{hi:,}) · Top {min(top_n, len(items))}**"
    )

    if not items:
        body = "_本期无符合条件的关键词_"
    else:
        lines = []
        for idx, metric in enumerate(items, start=1):
            lines.append(_format_keyword_line(idx, metric))
        body = "\n".join(lines)

    # 用 column_set + 两列 div 来给档位标题加底色，比纯 markdown 更显眼
    return [
        {
            "tag": "column_set",
            "flex_mode": "bisect",
            "background_style": "default",
            "columns": [
                {
                    "tag": "column",
                    "elements": [
                        {
                            "tag": "div",
                            "text": {
                                "tag": "lark_md",
                                "content": header_line,
                            },
                        }
                    ],
                    "width": "weighted",
                    "weight": 1,
                    "vertical_align": "top",
                    "background_style": color if color in ("default", "grey") else "default",
                }
            ],
        },
        _markdown_div(body),
        _hr(),
    ]


def _format_keyword_line(idx: int, metric: dict) -> str:
    """构造单条关键词的 markdown 行。"""
    keyword = metric.get("keyword", "")
    curr = metric.get("current_rank")
    prev = metric.get("prev_rank")
    cagr = metric.get("cagr")
    wow = metric.get("wow")
    jump = metric.get("tier_jump")

    # rank 变化：上期➡️本期（None 显示为 -）
    rank_segment = f"{_fmt_rank(prev)}➡️{_fmt_rank(curr)}"

    parts = [f"{idx}. **{keyword}**", rank_segment]

    if jump:
        prev_tier, curr_tier = jump
        # T 编号小 = 排名靠前 = 更好。
        # prev_tier 编号 > curr_tier 编号（如 T4→T1）= 排名提升
        prev_idx = int(prev_tier[1:])
        curr_idx = int(curr_tier[1:])
        if curr_idx < prev_idx:
            parts.append(f"⬆️{prev_tier}→{curr_tier}")
        else:
            parts.append(f"⬇️{prev_tier}→{curr_tier}")

    # CAGR 是主指标，放前面，加粗
    parts.append(f"CAGR **{_fmt_pct(cagr)}**")
    parts.append(f"WoW {_fmt_pct(wow)}")

    return " · ".join(parts)


def _newcomers_section(items: list[dict]) -> list[dict]:
    """新晋词分组。"""
    header = f"**🆕 新晋词 (≤ {cfg.NEW_KEYWORD_RANK_CAP:,}) · {len(items)}**"
    if not items:
        body = "_本期无新晋词_"
    else:
        lines = []
        for item in items:
            keyword = item.get("keyword", "")
            curr = item.get("current_rank")
            tier = item.get("tier") or "-"
            lines.append(f"- **{keyword}** · rank {_fmt_rank(curr)} · {tier}")
        body = "\n".join(lines)

    return [
        {
            "tag": "column_set",
            "flex_mode": "bisect",
            "background_style": "default",
            "columns": [
                {
                    "tag": "column",
                    "elements": [
                        {"tag": "div", "text": {"tag": "lark_md", "content": header}}
                    ],
                    "width": "weighted",
                    "weight": 1,
                    "vertical_align": "top",
                    "background_style": "grey",
                }
            ],
        },
        _markdown_div(body),
        _hr(),
    ]


def build_monitor_card(
    *,
    title: str,
    granularity: str,
    date_range: dict,
    selection: dict,
    summary: dict,
    generated_at: datetime | None = None,
) -> dict:
    """
    构造完整的飞书卡片 JSON。

    参数:
        title: 卡片标题
        granularity: 'week' 或 'month'
        date_range: {'start': str, 'end': str, 'points': int}
        selection: select_top_per_tier 的返回值
        summary: summarize_selection 的返回值
        generated_at: 生成时间（默认 now）

    返回可直接传给 send_card 的 dict。
    """
    generated_at = generated_at or datetime.now()
    granularity_label = "周" if granularity == "week" else "月"

    elements: list[dict] = []

    # 数据范围摘要
    range_start = date_range.get("start") or "-"
    range_end = date_range.get("end") or "-"
    points = date_range.get("points", 0)
    elements.append(
        _markdown_div(
            f"📅 数据范围：**{range_start} ~ {range_end}** · 共 {points} 期 · {granularity_label}粒度\n"
            f"📊 入选：档位 {summary.get('total_tier', 0)} 条 · 新晋 {summary.get('total_newcomers', 0)} 条"
        )
    )
    elements.append(_hr())

    # 各档位
    for label, lo, hi, top_n in cfg.TIERS:
        items = selection["tiers"].get(label, [])
        elements.extend(_tier_section(label, lo, hi, top_n, items))

    # 新晋词
    elements.extend(_newcomers_section(selection["newcomers"]))

    # 页脚 note
    elements.append(
        {
            "tag": "note",
            "elements": [
                {
                    "tag": "plain_text",
                    "content": (
                        f"数据来源: Sif ABA · 飞书表 · 生成于 {generated_at.strftime('%Y-%m-%d %H:%M')} · "
                        f"每周三 14:00 自动推送"
                    ),
                }
            ],
        }
    )

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": "blue",
        },
        "elements": elements,
    }
