# ABA Trend Viewer

一个独立于飞书同步脚本的小型本地可视化项目，用来快速查看一个或多个关键词的 Sif ABA 排名变化。

## 运行

在项目根目录执行：

```bash
python3 aba_trend_viewer/server.py
```

然后打开：

```text
http://127.0.0.1:8765
```

## 数据来源

- 后端复用根目录 `auth.py` 的 Sif 登录、token/cookie 缓存能力。
- 趋势接口使用 `https://new.sif.com/api/search/compare/estSearchesNumHistory`。
- 图表展示字段是 Sif 返回的 `searchesRank`。

## 时间范围

原飞书同步脚本在写表前做了固定过滤：

- 周趋势：`2025-01-01` 之后
- 月趋势：`2024-01` 之后

本可视化项目不预设这个限制。它会展示 Sif 接口当前实际返回的日期范围，并在界面顶部显示起止日期。输入开始/结束日期只会裁剪展示结果，不代表接口本身的完整历史范围。

## ECharts 视图

当前实现了三种更适合 ABA 排名的视图：

- 折线图：查看单个或多个关键词的排名变化，Y 轴反向，排名越靠上越好。
- 柱状图：比较最新一期的 ABA 排名。
- 热力图：多个关键词、多时间点的排名密度视图。

官方 ECharts 还支持 Bar、Line、Pie、Scatter、Heatmap、Tree、Treemap、Sunburst、Map、Graph、Gauge、Funnel、Sankey 等系列，以及 Tooltip、Legend、DataZoom、VisualMap、Toolbox 等组件。
