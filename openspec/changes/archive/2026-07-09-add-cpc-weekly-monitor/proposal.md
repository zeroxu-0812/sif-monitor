## Why

运营需要按周追踪一批跨类目关键词的 cpc_median 趋势，并在每周一早晨自动收到「按类目汇总的竞价/转化概览」飞书卡片，以便快速判断各品类广告成本走向、辅助出价与选品决策。此前趋势表（ssFow6）只有少量手工周数据，且无自动抓取与汇总推送。

## What Changes

新增 `sif_weekly_cpc_monitor.py`：每周一读取趋势表 ssFow6 的关键词列，调用 funnel 接口取每个词的 cpc_median，按 funnel 返回的 period 在表中追加/覆盖对应周列（货币格式）；同时按「类目」列分组做搜索量加权汇总，生成飞书卡片推送到周报专属群。配套 launchd 定时（每周一 10:00）。

## Capabilities

### New Capabilities
- `cpc-weekly-monitor`: 按周抓取关键词 cpc_median 写入趋势表，并按类目汇总推送飞书卡片

### Modified Capabilities
<!-- 无：本变更新增 capability，不改动既有 spec -->

## Impact

- 新增代码：`sif_weekly_cpc_monitor.py`
- 复用：`sif_keyword_funnel.fetch_keyword_funnel` / `compute_weighted_metrics`、`FeishuClient`（新增 `set_range_formatter` 用 appendStyle）、`feishu_messenger._markdown_div / _hr`
- 数据：飞书表 ssFow6 每周新增一列 cpc_median
- 消息：每周一推送卡片到周报群 `oc_5e077...`（与 funnel 测试群 `oc_64ffbd...` 分流）
- 调度：新增 launchd `com.abatrends.weekly-cpc`（plist + sh，每周一 10:00）
- 运行依赖：macOS TCC 需授予 `/bin/bash` 完全磁盘访问（项目位于 `~/Desktop`）
