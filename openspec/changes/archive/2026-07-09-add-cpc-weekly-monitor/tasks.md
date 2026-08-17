## 1. 脚本开发

- [x] 1.1 `sif_weekly_cpc_monitor.py` · `read_sheet`（读 A 类目 / B 关键词）
- [x] 1.2 `build_cpc_median_map`（fetch_keyword_funnel + 精确匹配 exact 桶 median）
- [x] 1.3 `week_header_from_period` + `resolve_target_column`（去重覆盖/追加）
- [x] 1.4 `write_week_column` + `set_range_formatter`（货币格式）
- [x] 1.5 `build_category_summaries`（按 A 列分组 `compute_weighted_metrics`）
- [x] 1.6 `build_weekly_cpc_card` + `card_to_markdown`（多类目同卡，竞价合并 $m～$e）
- [x] 1.7 `main` 串联（读表→查询→写表→卡片→可选推送）

## 2. 飞书集成

- [x] 2.1 `FeishuClient.set_range_formatter`（appendStyle + formatter）
- [x] 2.2 `WEEKLY_CPC_CHAT_ID` 常量分流（不动 `monitor_config.CHAT_ID`）
- [x] 2.3 `--push-card` 推送新群，机器人入群授权（230002 → 入群后 code=0）

## 3. 定时调度

- [x] 3.1 `schedule/com.abatrends.weekly-cpc.plist`（每周一 10:00，Weekday=1）
- [x] 3.2 `schedule/run_weekly_cpc.sh`（`.venv` python + `--push-card`）
- [x] 3.3 macOS 授予 `/bin/bash` 完全磁盘访问（TCC，解决 Operation not permitted / 退出码 126）
- [x] 3.4 `launchctl load` 部署 + 手动触发验证（状态码 0、新群收到卡片）

## 4. 验证

- [x] 4.1 写表幂等（同 period 覆盖、新 period 追加）
- [x] 4.2 卡片终端预览 + 飞书实际渲染确认
- [x] 4.3 funnel 等测试项目仍推原群（未受影响）
