#!/usr/bin/env python3
"""
Walmart 爬虫 → 飞书机器人 配置（镜像 feishu_config，互不冲突）。

凭据策略：
- 若 Walmart 用独立应用，在 .env 配 WALMART_FEISHU_APP_ID / WALMART_FEISHU_APP_SECRET。
- 否则回退到共享的 FEISHU_APP_ID / FEISHU_APP_SECRET（与现有 funnel 机器人同应用）。
- 两者都缺时启动期即报错。

输出表格：WALMART_SPREADSHEET_WIKI_URL（飞书电子表格的 wiki 链接），
机器人会把每次爬取结果写进该表格的一个新工作表，并在群里回带链接的卡片。
"""

import os


def _require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(
            f"缺少环境变量 {name}，请在项目根目录的 .env 中配置（参考 .env.example）"
        )
    return value


def _optional_env(name: str) -> str | None:
    value = os.getenv(name, "").strip()
    return value or None


def _first_env(*names: str) -> str | None:
    """按优先级返回第一个非空环境变量。"""
    for name in names:
        v = os.getenv(name, "").strip()
        if v:
            return v
    return None


# ============================================================================
# 飞书应用凭据（从 .env 读取；Walmart 专用 → 回退到共享）
# ============================================================================

FEISHU_APP_ID = _first_env("WALMART_FEISHU_APP_ID", "FEISHU_APP_ID") or ""
FEISHU_APP_SECRET = _first_env("WALMART_FEISHU_APP_SECRET", "FEISHU_APP_SECRET") or ""

if not FEISHU_APP_ID or not FEISHU_APP_SECRET:
    # 启动期不强制中断（便于先 import 做静态检查），运行时再用到的模块会校验。
    # 这里只打印一次警告。
    import sys
    print("⚠️ Walmart 飞书凭据未配置：请在 .env 设置 WALMART_FEISHU_APP_ID/SECRET "
          "或 FEISHU_APP_ID/SECRET", file=sys.stderr)


# ============================================================================
# 输出电子表格配置（机器人把爬取结果写到这里；每次一个新工作表）
# ============================================================================

# 飞书电子表格的 wiki 链接，如 https://xxx.feishu.cn/wiki/xxxxxxxx
# 留空则每次新建电子表格（建在应用根目录或下面的 folder）。
WALMART_SPREADSHEET_WIKI_URL = _optional_env("WALMART_SPREADSHEET_WIKI_URL") or ""

# 可选：表格 wiki token（若不填，FeishuClient 会用上面的 URL 自动解析）
WALMART_SPREADSHEET_TOKEN = _optional_env("WALMART_SPREADSHEET_TOKEN")

# 可选：新建电子表格时存放的文件夹 token（从飞书文件夹 URL 末段取）
WALMART_REPORT_FOLDER_TOKEN = _optional_env("WALMART_REPORT_FOLDER_TOKEN")


# ============================================================================
# 爬取默认参数
# ============================================================================

WALMART_DEFAULT_PAGES = int(os.getenv("WALMART_DEFAULT_PAGES", "2"))
WALMART_MAX_PAGES = int(os.getenv("WALMART_MAX_PAGES", "5"))
WALMART_DEFAULT_SORT = os.getenv("WALMART_DEFAULT_SORT", "best_match")


# ============================================================================
# 获取配置信息的方法（与 feishu_config 同款说明）
# ============================================================================

# 如何获取 Wiki Token / Sheet ID：
# 1. 在飞书表格中，点击右上角「分享」→「复制链接」
# 2. 链接格式：https://...wiki/XXXXXXXXXX  或  .../sheets/XXXXXXXXXX
# 3. XXXXXXXXXX 即 token；填到 WALMART_SPREADSHEET_WIKI_URL 即可，token 可不填。
