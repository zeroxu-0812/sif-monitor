#!/usr/bin/env python3
"""
飞书配置

- 敏感凭据（FEISHU_APP_ID / FEISHU_APP_SECRET）从 .env 读取，缺失时启动期即报错
- 非敏感配置（sheet / token / 工作表标题）保留在此文件
"""

import os


def _require_env(name: str) -> str:
    """读取必需的环境变量，缺失时给出明确错误指引。"""
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(
            f"缺少环境变量 {name}，请在项目根目录的 .env 中配置（参考 .env.example）"
        )
    return value


# ============================================================================
# 飞书 API 凭据（从 .env 读取）
# ============================================================================

FEISHU_APP_ID = _require_env("FEISHU_APP_ID")
FEISHU_APP_SECRET = _require_env("FEISHU_APP_SECRET")


# ============================================================================
# 关键词表格配置（读取关键词列表）
# ============================================================================

KEYWORDS_WIKI_TOKEN = "Dz9PwV7MWi1qgFkLQ2ycpX1ynHd"     # ← 填写关键词表格的 Wiki Token
KEYWORDS_SHEET_ID = "f4763f"       # ← 填写关键词所在工作表的 Sheet ID


# ============================================================================
# 趋势数据表格配置（写入趋势数据）
# ============================================================================

# 趋势表格的 Wiki Token（可以和关键词表格是同一个表格）
TRENDS_WIKI_TOKEN = "Dz9PwV7MWi1qgFkLQ2ycpX1ynHd"

# 周趋势数据写入的工作表 ID
TRENDS_SHEET_ID_WEEKLY = "csSwu0"

# 月趋势数据写入的工作表 ID
TRENDS_SHEET_ID_MONTHLY = "hzQkaQ"


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
