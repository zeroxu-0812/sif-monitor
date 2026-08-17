# cpc-weekly-monitor Specification

## Purpose
按周抓取趋势表 ssFow6 关键词的 cpc_median 写入新周列（货币格式、按 period 去重），并按类目做搜索量加权汇总生成飞书卡片，每周一自动推送到周报专属群，供运营追踪各品类广告成本走向。
## Requirements
### Requirement: 周数据按 period 去重写入
系统 SHALL 每周读取趋势表 ssFow6 的关键词列，调用 funnel 接口，按返回的 period 定位目标周列（列头格式 `period~period+6`）。

#### Scenario: 新一周数据
- **WHEN** funnel 返回的 period 对应列头在表中不存在
- **THEN** 在最右侧下一列追加新列，写入该周列头与各词 cpc_median

#### Scenario: 已有周数据（幂等）
- **WHEN** 目标列头已存在于表头
- **THEN** 覆盖该列数值，不新增列

### Requirement: cpc_median 精确取值
系统 SHALL 对每个关键词取 `cpc[legacyForSales_exact][0].median` 作为 cpc_median，并 MUST 排除 funnel 顺带返回的相关市场词。

#### Scenario: 批量查询精确匹配
- **WHEN** 多个关键词批量查询 funnel
- **THEN** 仅按请求词精确匹配构建 keyword→cpc_median 映射，相关市场词不入表

#### Scenario: 词无 cpc 数据
- **WHEN** 某关键词对应桶为空或无 median
- **THEN** 该单元格留空

### Requirement: 货币单元格格式
系统 SHALL 以飞书货币格式呈现 cpc_median 数值。

#### Scenario: 设格式
- **WHEN** 写入 cpc 列数据区
- **THEN** 通过飞书 appendStyle 接口（`formatter:$#,##0.00`）设货币格式，显示为 $3.12

### Requirement: 类目汇总卡片
系统 SHALL 按趋势表 A 列「类目」分组，每组用搜索量加权计算综合指标，生成飞书卡片。

#### Scenario: 卡片内容
- **WHEN** 生成卡片
- **THEN** 每个类目块展示：总搜索量、综合点击转化率、综合建议竞价（$median～$end）；多类目按表中出现顺序排列在同一卡片

### Requirement: 推送群分流
系统 SHALL 将周报卡片推送至原群（与 monitor/funnel 同群），并在任务异常时向原群发送失败告警。

#### Scenario: 群路由
- **WHEN** 推送周报卡片
- **THEN** 发送到 `monitor_config.CHAT_ID`（原群，与 monitor/funnel 一致）

#### Scenario: 失败告警
- **WHEN** 任务执行异常
- **THEN** 向原群发送失败告警卡片（尽力而为，告警失败不抛出）

### Requirement: 每周一定时自动执行
系统 SHALL 通过 launchd 每周一 10:00 自动运行写表与推卡片。

#### Scenario: 定时触发
- **WHEN** 每周一 10:00 到达且 Mac 开机联网
- **THEN** launchd 自动执行 run_weekly_cpc.sh → `sif_weekly_cpc_monitor.py --push-card`

#### Scenario: 错过补跑
- **WHEN** 触发时刻 Mac 关机/睡眠
- **THEN** 唤醒/开机后 launchd 自动补跑一次

