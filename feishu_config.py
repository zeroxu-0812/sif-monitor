#!/usr/bin/env python3
"""
飞书配置

- 敏感凭据（FEISHU_APP_ID / FEISHU_APP_SECRET）从 .env 读取，缺失时启动期即报错
- 非敏感配置（sheet / token / 工作表标题）保留在此文件
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# 模块级加载 .env，使本模块可独立 import，避免依赖「先 import auth 才能读到 .env」
# 的脆弱顺序耦合（load_dotenv 幂等，不会覆盖已存在的环境变量）。
load_dotenv(Path(__file__).resolve().parent / ".env")


def _require_env(name: str) -> str:
    """读取必需的环境变量，缺失时给出明确错误指引。"""
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(
            f"缺少环境变量 {name}，请在项目根目录的 .env 中配置（参考 .env.example）"
        )
    return value


def _optional_env(name: str) -> str | None:
    """读取可选环境变量，缺失或为空时返回 None（不报错）。"""
    value = os.getenv(name, "").strip()
    return value or None


# ============================================================================
# 飞书 API 凭据（从 .env 读取）
# ============================================================================

FEISHU_APP_ID = _require_env("FEISHU_APP_ID")
FEISHU_APP_SECRET = _require_env("FEISHU_APP_SECRET")

# 监控结果表存放文件夹（可选，留空则建在应用根目录）
# 从飞书云文档文件夹 URL 末段取，如 https://xxx.feishu.cn/drive/folder/fldbcO1... 的 fldbcO1...
FEISHU_REPORT_FOLDER_TOKEN = _optional_env("FEISHU_REPORT_FOLDER_TOKEN")


# ============================================================================
# 关键词表格配置（读取关键词列表）
# ============================================================================

# 以下 token / sheet_id 为数据位置标识（非密码），默认值保留现有表，
# 可在 .env 中覆盖以适配换环境/换表，无需改动源码。
KEYWORDS_WIKI_TOKEN = _optional_env("KEYWORDS_WIKI_TOKEN") or "Dz9PwV7MWi1qgFkLQ2ycpX1ynHd"
KEYWORDS_SHEET_ID = _optional_env("KEYWORDS_SHEET_ID") or "f4763f"


# ============================================================================
# 趋势数据表格配置（写入趋势数据）
# ============================================================================

# 趋势表格的 Wiki Token（可以和关键词表格是同一个表格）
TRENDS_WIKI_TOKEN = _optional_env("TRENDS_WIKI_TOKEN") or "Dz9PwV7MWi1qgFkLQ2ycpX1ynHd"

# 周趋势数据写入的工作表 ID
TRENDS_SHEET_ID_WEEKLY = _optional_env("TRENDS_SHEET_ID_WEEKLY") or "csSwu0"

# 月趋势数据写入的工作表 ID
TRENDS_SHEET_ID_MONTHLY = _optional_env("TRENDS_SHEET_ID_MONTHLY") or "hzQkaQ"

# Funnel 结果输出到的 spreadsheet（wiki 链接；与趋势表共用同一个表，应用已有权限）
# 用于 sif_keyword_funnel.py --push-feishu：在该表里建新工作表存 funnel 结果
FUNNEL_SPREADSHEET_WIKI_URL = (
    _optional_env("FUNNEL_SPREADSHEET_WIKI_URL")
    or "https://nxx2pqvw7t.feishu.cn/wiki/Dz9PwV7MWi1qgFkLQ2ycpX1ynHd"
)


# ============================================================================
# 获取配置信息的方法
# ============================================================================

# 如何获取 Wiki Token：
# 1. 在飞书表格中，点击右上角「分享」→「复制链接」
# 2. 链接格式：https://...wiki/token/XXXXXXXXXX/...
# 3. XXXXXXXXXX 就是 Wiki Token

# 如何获取 Sheet ID：
# 方法 1：在飞书表格 URL 中查找
#         链接格式：.../sheet/XXXXXX/...
#         XXXXXX 就是 Sheet ID
#
# 方法 2：在飞书表格中，右键点击工作表标签 →「查看工作表信息」
#         可以看到工作表 ID

# ============================================================================
# 可选：使用工作表标题查找（如果不想填 Sheet ID，可以使用标题）
# ============================================================================

# 启用此选项后，可以通过工作表标题查找（需要在 sif_trends_fetcher.py 中修改代码）
USE_SHEET_TITLE = False

KEYWORDS_SHEET_TITLE = "关键词"        # 关键词工作表标题
TRENDS_SHEET_TITLE_WEEKLY = "周趋势"   # 周趋势工作表标题
TRENDS_SHEET_TITLE_MONTHLY = "月趋势"  # 月趋势工作表标题
