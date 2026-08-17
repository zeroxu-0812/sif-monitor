"""rank_metrics 纯计算层的单元测试。

不依赖飞书/Sif/文件系统，也不 import monitor_config（避免 .env 副作用），
档位配置在测试内自建，便于覆盖边界与截断逻辑。
"""

from __future__ import annotations

import pytest

from rank_metrics import (
    compute_cagr,
    compute_improvement,
    compute_keyword_metrics,
    select_top_per_tier,
    summarize_selection,
    tier_of,
)


# 与 monitor_config.TIERS 同结构，但 top_n 改小以便测截断
TIERS = [
    ("T1", 1, 3000, 2),
    ("T2", 3001, 5000, 2),
    ("T3", 5001, 10000, 1),
    ("T4", 10001, 20000, 1),
    ("T5", 20001, 30000, 1),
]
NEW_KEYWORD_CAP = 15000


# ============================================================================
# tier_of
# ============================================================================

@pytest.mark.parametrize(
    ("rank", "expected"),
    [
        (1, "T1"),
        (3000, "T1"),
        (3001, "T2"),
        (5000, "T2"),
        (5001, "T3"),
        (10000, "T3"),
        (10001, "T4"),
        (20000, "T4"),
        (20001, "T5"),
        (30000, "T5"),
        (30001, None),  # 超出上限
        (None, None),
        (0, None),
        (-5, None),
    ],
)
def test_tier_of_boundaries(rank, expected):
    assert tier_of(rank, TIERS) == expected


# ============================================================================
# compute_improvement
# ============================================================================

def test_improvement_positive_when_rank_smaller():
    # rank 从 100 前进到 50 → +50%
    assert compute_improvement(100, 50) == pytest.approx(0.5)


def test_improvement_negative_when_rank_larger():
    assert compute_improvement(100, 150) == pytest.approx(-0.5)


@pytest.mark.parametrize(
    ("prev", "curr"),
    [(None, 50), (100, None), (0, 50), (-1, 50), (None, None)],
)
def test_improvement_invalid_inputs_return_none(prev, curr):
    assert compute_improvement(prev, curr) is None


# ============================================================================
# compute_cagr
# ============================================================================

def test_cagr_improvement():
    # rank 从 8000 前进到 1000：8 倍，三期几何平均 = 2x/期 → +100%
    assert compute_cagr(8000, 1000, periods=3) == pytest.approx(1.0)


def test_cagr_decline():
    # rank 从 1000 退到 8000：0.125^(1/3) = 0.5 → -50%
    assert compute_cagr(1000, 8000, periods=3) == pytest.approx(-0.5)


@pytest.mark.parametrize(
    ("rank_ago", "curr", "periods"),
    [(None, 1000, 3), (1000, None, 3), (1000, 1000, 0), (0, 1000, 3), (1000, 0, 3)],
)
def test_cagr_invalid_inputs_return_none(rank_ago, curr, periods):
    assert compute_cagr(rank_ago, curr, periods) is None


# ============================================================================
# compute_keyword_metrics
# ============================================================================

def test_keyword_metrics_normal_series():
    # 旧→新：1000 → 900 → 800 → 700
    m = compute_keyword_metrics("kw", [1000, 900, 800, 700], TIERS, cagr_periods=3)
    assert m["current_rank"] == 700
    assert m["prev_rank"] == 800
    assert m["rank_ago"] == 1000
    assert m["tier"] == "T1"
    assert m["wow"] == pytest.approx((800 - 700) / 800)
    assert m["cagr"] == pytest.approx((1000 / 700) ** (1 / 3) - 1)
    assert m["is_new"] is False
    assert m["tier_jump"] is None


def test_keyword_metrics_new_keyword():
    # 历史无有效数据，仅最新一期有 rank
    m = compute_keyword_metrics("kw", [None, None, None, 500], TIERS, cagr_periods=3)
    assert m["current_rank"] == 500
    assert m["prev_rank"] is None
    assert m["rank_ago"] is None
    assert m["is_new"] is True
    assert m["cagr"] is None
    assert m["wow"] is None


def test_keyword_metrics_tier_jump_up():
    # 上期 T2(4000) → 本期 T1(2500)
    m = compute_keyword_metrics("kw", [4000, 4000, 4000, 2500], TIERS, cagr_periods=3)
    assert m["tier"] == "T1"
    assert m["tier_jump"] == ("T2", "T1")


def test_keyword_metrics_tier_jump_down():
    # 上期 T1(2500) → 本期 T2(4000)
    m = compute_keyword_metrics("kw", [2500, 2500, 2500, 4000], TIERS, cagr_periods=3)
    assert m["tier"] == "T2"
    assert m["tier_jump"] == ("T1", "T2")


def test_keyword_metrics_no_jump_same_tier():
    m = compute_keyword_metrics("kw", [1000, 1000, 1000, 1000], TIERS, cagr_periods=3)
    assert m["tier_jump"] is None


# ============================================================================
# select_top_per_tier
# ============================================================================

def _metric(keyword, curr, cagr=None, prev=None, is_new=False, rank_ago=None):
    tier = tier_of(curr, TIERS)
    return {
        "keyword": keyword,
        "current_rank": curr,
        "tier": tier,
        "prev_rank": prev,
        "rank_ago": rank_ago,
        "wow": None,
        "cagr": cagr,
        "is_new": is_new,
        "tier_jump": None,
    }


def test_select_top_per_tier_caps_top_n():
    metrics = [
        _metric("a", 1000, cagr=0.1),
        _metric("b", 2000, cagr=0.5),
        _metric("c", 1500, cagr=0.3),
    ]
    sel = select_top_per_tier(metrics, TIERS, NEW_KEYWORD_CAP)
    # T1 top_n=2，取 cagr 最大的两个：b(0.5) > c(0.3) > a(0.1)
    got = [m["keyword"] for m in sel["tiers"]["T1"]]
    assert got == ["b", "c"]


def test_select_top_per_tier_none_cagr_sorted_last():
    metrics = [
        _metric("a", 1000, cagr=None),
        _metric("b", 2000, cagr=0.2),
        _metric("c", 1500, cagr=0.4),
    ]
    sel = select_top_per_tier(metrics, TIERS, NEW_KEYWORD_CAP)
    # 有 cagr 的排前（c > b），None 排最后（a 被 top_n=2 截掉）
    got = [m["keyword"] for m in sel["tiers"]["T1"]]
    assert got == ["c", "b"]


def test_select_top_per_tier_buckets_by_tier():
    metrics = [
        _metric("t1_kw", 1000, cagr=0.5),
        _metric("t2_kw", 4000, cagr=0.5),
        _metric("t3_kw", 6000, cagr=0.5),
    ]
    sel = select_top_per_tier(metrics, TIERS, NEW_KEYWORD_CAP)
    assert [m["keyword"] for m in sel["tiers"]["T1"]] == ["t1_kw"]
    assert [m["keyword"] for m in sel["tiers"]["T2"]] == ["t2_kw"]
    assert [m["keyword"] for m in sel["tiers"]["T3"]] == ["t3_kw"]


def test_select_top_per_tier_newcomers_capped():
    metrics = [
        _metric("new_ok", 10000, is_new=True),
        _metric("new_over", 20000, is_new=True),  # > NEW_KEYWORD_CAP，不入组
    ]
    sel = select_top_per_tier(metrics, TIERS, NEW_KEYWORD_CAP)
    got = [m["keyword"] for m in sel["newcomers"]]
    assert got == ["new_ok"]


def test_select_top_per_tier_skips_out_of_range():
    metrics = [
        _metric("in", 1000, cagr=0.5),
        _metric("out", 35000, cagr=0.9),  # > 30000，tier=None，跳过
    ]
    sel = select_top_per_tier(metrics, TIERS, NEW_KEYWORD_CAP)
    all_tier_kws = [
        m["keyword"] for items in sel["tiers"].values() for m in items
    ]
    assert "in" in all_tier_kws
    assert "out" not in all_tier_kws


def test_select_top_per_tier_newcomers_sorted_by_rank():
    metrics = [
        _metric("b", 5000, is_new=True),
        _metric("a", 2000, is_new=True),
    ]
    sel = select_top_per_tier(metrics, TIERS, NEW_KEYWORD_CAP)
    assert [m["keyword"] for m in sel["newcomers"]] == ["a", "b"]


# ============================================================================
# summarize_selection
# ============================================================================

def test_summarize_selection():
    metrics = [
        _metric("a", 1000, cagr=0.5),
        _metric("b", 4000, cagr=0.5),
        _metric("new", 1000, is_new=True),
    ]
    sel = select_top_per_tier(metrics, TIERS, NEW_KEYWORD_CAP)
    summary = summarize_selection(sel)
    assert summary["total_tier"] == 2
    assert summary["total_newcomers"] == 1
    assert summary["tier_counts"]["T1"] == 1
    assert summary["tier_counts"]["T2"] == 1
