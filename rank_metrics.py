#!/usr/bin/env python3
"""
ABA 排名指标纯计算层。

本模块不依赖任何外部 IO（飞书 / Sif / 文件系统），所有函数只接收与返回
Python 原生数据结构，便于单测与复用。

核心约定：
- rank 数字越小排名越靠前
- "改善率" / CAGR 取正值代表排名变好（rank 数字变小）
"""

from __future__ import annotations

from collections.abc import Iterable

# ============================================================================
# 档位判定
# ============================================================================

def tier_of(rank: int | None, tiers_config: list[tuple[str, int, int, int]]) -> str | None:
    """
    返回 rank 所在档位标签 (如 'T1')；rank 为 None 或落在所有档位之外返回 None。
    """
    if rank is None or rank <= 0:
        return None
    for label, lo, hi, _ in tiers_config:
        if lo <= rank <= hi:
            return label
    return None


# ============================================================================
# 改善率与 CAGR
# ============================================================================

def compute_improvement(prev: int | None, curr: int | None) -> float | None:
    """
    改善率 = (prev - curr) / prev。
    - 正值：排名变好（curr < prev）
    - 负值：排名变差
    - 任一参数为 None 或 prev <= 0：返回 None
    """
    if prev is None or curr is None or prev <= 0 or curr <= 0:
        return None
    return (prev - curr) / prev


def compute_cagr(
    rank_ago: int | None,
    curr: int | None,
    periods: int = 3,
) -> float | None:
    """
    三周/三月复合增长率（几何平均）。

    CAGR = (rank_ago / curr)^(1/periods) - 1
    - rank_ago > curr（排名变好）→ 正值
    - 任一为 None 或 <= 0 → None
    - periods 必须 > 0
    """
    if periods <= 0:
        return None
    if rank_ago is None or curr is None or rank_ago <= 0 or curr <= 0:
        return None
    return (rank_ago / curr) ** (1.0 / periods) - 1.0


# ============================================================================
# 单关键词指标计算
# ============================================================================

def _coerce_rank(value) -> int | None:
    """把飞书表单元格（可能是 ''/None/int/float/str）规范化为 int 或 None。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if value != value or value in (float("inf"), float("-inf")):  # noqa: PLR0124  NaN/Inf 检测
            return None
        if value <= 0:
            return None
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = float(text)
    except ValueError:
        return None
    if parsed <= 0:
        return None
    return int(parsed)


def _last_n_valid(
    ranks: list[int | None],
    n: int,
) -> list[int | None]:
    """
    从 ranks 末尾向前取 n 个位置（保留 None），用于找"上期"、"三期前"等。
    返回长度总是 n（不足补 None），顺序与原 ranks 一致（旧→新）。
    """
    if n <= 0:
        return []
    tail = ranks[-n:]
    padding = [None] * (n - len(tail))
    return padding + tail


def compute_keyword_metrics(
    keyword: str,
    ranks: list[int | None],
    tiers_config: list[tuple[str, int, int, int]],
    cagr_periods: int = 3,
) -> dict:
    """
    计算单个关键词的全部指标。

    参数:
        keyword: 关键词文本
        ranks: 按时间顺序（旧→新）的 rank 序列，可能含 None
        tiers_config: 来自 monitor_config.TIERS
        cagr_periods: CAGR 周期数（默认 3）

    说明:
        is_new 只表示"首次出现"（历史无数据），与 rank 大小无关。
        是否进入新晋分组（rank ≤ NEW_KEYWORD_RANK_CAP）由
        select_top_per_tier 负责。

    返回字段:
        keyword, current_rank, tier, prev_rank, rank_ago,
        wow, cagr, is_new, tier_jump
    """
    normalized = [_coerce_rank(r) for r in ranks]

    # 取尾部几个关键节点
    tail_window = _last_n_valid(normalized, cagr_periods + 1)
    curr_rank = tail_window[-1] if tail_window else None
    prev_rank = tail_window[-2] if len(tail_window) >= 2 else None
    rank_ago = tail_window[0] if len(tail_window) >= cagr_periods + 1 else None

    wow = compute_improvement(prev_rank, curr_rank)
    cagr = compute_cagr(rank_ago, curr_rank, periods=cagr_periods)
    tier = tier_of(curr_rank, tiers_config)

    # 新词：当前有 rank 且历史上完全没有任何有效数据
    history = normalized[:-1] if normalized else []
    is_new = curr_rank is not None and not any(r is not None for r in history)

    # 跨档跃迁：上期档位 ≠ 本期档位，且都在 T1-T5 范围内
    prev_tier = tier_of(prev_rank, tiers_config)
    tier_jump = None
    if tier is not None and prev_tier is not None and tier != prev_tier:
        tier_jump = (prev_tier, tier)

    return {
        "keyword": keyword,
        "current_rank": curr_rank,
        "tier": tier,
        "prev_rank": prev_rank,
        "rank_ago": rank_ago,
        "wow": wow,
        "cagr": cagr,
        "is_new": is_new,
        "tier_jump": tier_jump,
    }


# ============================================================================
# 分档筛选
# ============================================================================

def _cagr_sort_key(metric: dict) -> tuple:
    """
    CAGR 降序排序键。None 排末尾。
    排序维度：
      1. cagr 是否存在（有的优先）
      2. cagr 数值（大的优先）
      3. 当前 rank（小的优先，即排名更靠前的优先）
    """
    cagr = metric.get("cagr")
    curr = metric.get("current_rank")
    # 用 -cagr 实现降序；用 curr 实现升序。cagr 可能为 None（缺失），显式转 float 规避类型歧义
    cagr_val: float = float(cagr) if cagr is not None else 0.0
    return (
        0 if cagr is not None else 1,            # 有 CAGR 的排前
        -cagr_val,                               # CAGR 大的排前
        curr if isinstance(curr, int) else 10**9,  # 当前 rank 小的排前
        metric.get("keyword", ""),               # 关键词字典序稳定排序
    )


def select_top_per_tier(
    metrics: Iterable[dict],
    tiers_config: list[tuple[str, int, int, int]],
    new_keyword_cap: int,
) -> dict:
    """
    按档位分组，每档取 Top N；新词单独收集到 'newcomers'。

    入选规则：
      - 当前 rank 必须非空且 ≤ MAX_RANK（即落在 T1-T5）
      - 新词（is_new=True）不参与档位 Top N，进 newcomers

    返回:
        {
            "tiers": {"T1": [...], "T2": [...], ...},  # 每档最多 N 条
            "newcomers": [...],
        }
    """
    tier_buckets: dict[str, list[dict]] = {label: [] for label, *_ in tiers_config}
    newcomers: list[dict] = []

    for metric in metrics:
        if metric.get("is_new"):
            # 新词：只要 rank ≤ cap 就进 newcomers
            curr = metric.get("current_rank")
            if isinstance(curr, int) and curr <= new_keyword_cap:
                newcomers.append(metric)
            continue

        tier = metric.get("tier")
        if tier is None:
            continue  # rank 缺失或 > MAX_RANK，跳过
        if tier not in tier_buckets:
            continue
        tier_buckets[tier].append(metric)

    # 每档排序 + 取 Top N
    result_tiers: dict[str, list[dict]] = {}
    for label, _lo, _hi, top_n in tiers_config:
        bucket = sorted(tier_buckets[label], key=_cagr_sort_key)
        result_tiers[label] = bucket[:top_n]

    # 新词按当前 rank 升序（更靠前的排前面）
    newcomers.sort(key=lambda m: (m.get("current_rank") or 10**9, m.get("keyword", "")))

    return {"tiers": result_tiers, "newcomers": newcomers}


# ============================================================================
# 聚合辅助
# ============================================================================

def summarize_selection(selection: dict) -> dict:
    """生成简短统计摘要，用于卡片头部。"""
    tier_counts = {label: len(items) for label, items in selection["tiers"].items()}
    return {
        "tier_counts": tier_counts,
        "total_tier": sum(tier_counts.values()),
        "total_newcomers": len(selection["newcomers"]),
    }
