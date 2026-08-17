## Context

项目 about-sif-main 已有 funnel 查询（`sif_keyword_funnel.py`，含 `fetch_keyword_funnel` + `compute_weighted_metrics`）和飞书表格客户端（`sif_feishu_sync.FeishuClient`）。趋势表 ssFow6 手工维护了少量关键词的周 cpc 数据，但无自动抓取与汇总推送。

## Goals / Non-Goals

**Goals:**
- 每周自动追加 cpc_median 周列，按 period 去重（幂等）
- 按类目搜索量加权汇总，推送飞书卡片供运营决策
- 不改动类目/翻译/ABA 排名/历史周列
- 与 funnel 测试项目解耦（不同推送群、不耦合卡片流程）

**Non-Goals:**
- 不做 ABA 排名更新（D 列保持原值）
- 不做月度/其他粒度（仅周）
- 不改动 funnel 主流程代码（仅复用）

## Decisions

1. **一次 funnel 查询两用**：`fetch_keyword_funnel` 返回的 records 同时用于取 cpc_median（写表）和 `compute_weighted_metrics`（卡片汇总），避免重复请求。
2. **period → 列头 `period~period+6`**：与现有周列格式（周日~周六）一致；去重按列头字符串精确匹配，命中覆盖、未命中追加。
3. **精确匹配白名单**：`build_cpc_median_map` / `compute_weighted_metrics` 均传 `requested_keywords`，排除 API 相关市场词污染。
4. **货币格式用 appendStyle**：飞书设样式正确请求体是 `{appendStyle:{range, style:{formatter}}}`，非早期尝试的 `{data:[...]}`（后者报 9499 Missing appendStyle）。新增 `FeishuClient.set_range_formatter` 固化该方法；funnel 表设格式也顺带改用它（修掉历史 400）。
5. **推送群分流**：周报用脚本常量 `WEEKLY_CPC_CHAT_ID`，funnel 等仍用 `monitor_config.CHAT_ID`，互不影响。
6. **调度走 launchd**：plist（每周一 10:00）+ `run_weekly_cpc.sh`（用 `.venv` python，`--push-card`）；项目在桌面，需给 `/bin/bash` 授予完全磁盘访问（TCC）。

## Risks / Trade-offs

- **TCC 依赖**：项目位于 `~/Desktop`，launchd 进程默认无桌面访问权，须手动授予 `/bin/bash` 完全磁盘访问；macOS 大版本升级可能重置该权限（需重新授权）。
- **funnel 数据延迟**：周一 10:00 跑时若 funnel 数据尚未推进到新一周，会覆盖最近一周（幂等无害），数据更新后任意一次跑会补新列。
- **launchd 非跨机**：依赖本地 Mac，非云端（已评估云端方案，用户选择本地 + 开机补跑）。
