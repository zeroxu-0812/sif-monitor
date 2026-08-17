# ABA Trends

> Sif ABA 关键词排名监控：从飞书表读取排名矩阵 → 计算指标 → 按档位筛选优异词 → 飞书群聊卡片推送

## 项目架构

```
ABA_Trends/
├── auth.py                  # 共享鉴权层：Sif 登录 / token 缓存 / 自动重试
├── sif_config.py            # Sif 批处理默认值（批次大小、延迟）
├── feishu_config.py         # 飞书应用凭据 + 表格 token/sheet_id 配置
├── sif_feishu_sync.py       # ① 数据同步：Sif → 飞书表（周表/月表）
│
├── monitor_config.py        # 监控配置：档位定义、Top N 配额、新词阈值、chat_id
├── rank_metrics.py          # ② 纯计算层：tier_of / CAGR / 改善率 / 分档筛选
├── feishu_messenger.py      # ③ 飞书 IM 卡片推送 + token TTL 保护
├── monitor.py               # ④ 监控主入口 CLI
│
├── aba_trend_viewer/        # 本地可视化（独立子项目，不依赖监控链路）
│   ├── server.py            #   HTTP 服务（端口 8765）
│   └── static/              #   ECharts 前端
│
├── schedule/
│   ├── run_monitor.sh                          # launchd 调用入口（跨机器友好）
│   └── com.abatrends.weekly-monitor.plist      # launchd 配置：每周三 14:00
│
├── .env                     # 真实凭据（gitignore，不提交）
├── .env.example             # 凭据模板（提交）
└── .gitignore
```

## 三个独立子链路

| 子链路 | 触发 | 入口脚本 | 用途 |
|--------|------|----------|------|
| **数据同步** | 手动 / 调度 | `sif_feishu_sync.py` | 从 Sif API 抓周/月排名，写入飞书表 |
| **排名监控** | 每周三 14:00 | `monitor.py` | 读飞书表 → 算指标 → 推送飞书群聊 |
| **Funnel 查询** | 手动 | `sif_keyword_funnel.py` | 查关键词 CPC / 搜索量 / 转化率 + 搜索量加权综合指标 |
| **本地可视化** | 手动 | `aba_trend_viewer/server.py` | 本地浏览器查看单关键词趋势 |

监控链路依赖同步链路：**先跑 `sif_feishu_sync.py` 同步最新数据，`monitor.py` 才能看到最新一期排名**。建议两个脚本一起调度（同步在前，监控在后）。

## 监控算法核心

### 档位与配额

| 档位 | rank 区间 | Top N |
|------|----------|-------|
| T1 | 1-3,000 | 15 |
| T2 | 3,001-5,000 | 15 |
| T3 | 5,001-10,000 | 10 |
| T4 | 10,001-20,000 | 10 |
| T5 | 20,001-30,000 | 10 |

rank > 30,000 忽略。

### 指标

| 指标 | 公式 | 说明 |
|------|------|------|
| **三周 CAGR**（主排序） | `(rank_3期前 / rank_本期)^(1/3) - 1` | 正值代表排名改善 |
| WoW / MoM | `(rank_prev - rank_curr) / rank_prev` | 周环比 / 月环比 |

正值代表排名变好（rank 数字变小）。每个档位内按 CAGR 降序取前 N。

### 特殊分组

- **新晋词**：本期首次出现（无历史数据）且 rank ≤ 15,000，单独成组
- **跨档跃迁**：上期与本期档位不同时标记，`⬆️T4→T1` 表示提升，`⬇️T3→T5` 表示下降

## 安装与配置

### 1. 克隆项目

```bash
git clone https://github.com/Oceanwing-IDC/about-sif.git ABA_Trends
cd ABA_Trends
```

### 2. 安装 Python 依赖

```bash
# 建议用虚拟环境
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
# 如需运行单元测试，追加开发依赖（pytest）
.venv/bin/pip install -r requirements-dev.txt
```

运行时依赖：`requests` / `python-dotenv` / `openpyxl` / `lark-oapi`（清单见 `requirements.txt`）。

### 3. 配置凭据

```bash
cp .env.example .env
# 编辑 .env 填入真实值：
#   SIF_PHONE         Sif 登录手机号
#   SIF_PASSWORD      Sif 登录密码
#   FEISHU_APP_ID     飞书应用 App ID
#   FEISHU_APP_SECRET 飞书应用 App Secret
```

### 4. 配置飞书表格 ID（如与默认不同）

编辑 `feishu_config.py`，填入你的：
- `KEYWORDS_WIKI_TOKEN` / `KEYWORDS_SHEET_ID`：关键词所在表
- `TRENDS_WIKI_TOKEN` / `TRENDS_SHEET_ID_WEEKLY` / `TRENDS_SHEET_ID_MONTHLY`：排名数据写入位置

### 5. 配置推送目标群

编辑 `monitor_config.py`，把 `CHAT_ID` 改成你的目标群：

```python
CHAT_ID = "oc_xxxxxxxxxxxxxxxxxxxxxxxxx"
```

> 获取 chat_id：飞书开放平台 → 应用 → 加群后调 `/open-apis/im/v1/chats` 查询，或群设置里的链接解析。

## 飞书应用权限

部署前必须在 [飞书开发者后台](https://open.feishu.cn/app) 为应用开通以下权限并发布新版本：

| Scope | 用途 |
|-------|------|
| `im:message:send_as_bot` | **监控推送必需**：以机器人身份发送消息 |
| `sheets:spreadsheet` | 读写飞书表（同步脚本 + 监控读取）|

权限开通后，把应用机器人**拉入目标群**才能发消息。

## 使用方法

### 数据同步（一次性）

```bash
# 全量同步（默认 10 个/批，批次间 0.5s）
python3 sif_feishu_sync.py

# 测试模式：只处理前 N 个关键词
python3 sif_feishu_sync.py --limit 50
```

### 监控推送

```bash
# 干跑（打印卡片 JSON，不推送）
python3 monitor.py --dry-run

# 实推到飞书群
python3 monitor.py

# 月度报告
python3 monitor.py --granularity month

# 只跑某一档（调试用）
python3 monitor.py --dry-run --limit T1
```

### 本地可视化

```bash
python3 aba_trend_viewer/server.py
# 打开 http://127.0.0.1:8765
```

### 鉴权自检

```bash
python3 auth.py --check           # 用缓存
python3 auth.py --check --force-login   # 强制重新登录
```

### Funnel 数据查询（CPC / 转化率）

```bash
# 单词（API 会顺带返回 87 个相关市场词）
python3 sif_keyword_funnel.py --keywords "magnesium cream"

# 多词（严格按输入返回）+ exact/broad 双策略
python3 sif_keyword_funnel.py --keywords "vitamin d3 k2,d3 k2" --strategy both

# 从文件读关键词
python3 sif_keyword_funnel.py --keywords-file keywords.txt

# 输出：
# - funnel_<strategy>_<ts>.csv  原始字段（CPC / 搜索量 / 转化率 / TopASIN 等 36 列）
# - funnel_<ts>.xlsx            多 sheet 工作簿
# - funnel_summary_<strategy>_<ts>.csv  搜索量加权综合指标（每词权重 + 合计行）
```

默认 CPC 桶为 `legacyForSales_exact`，可用 `--cpc-match` 切换其他 5 种匹配类型对比。

## 定时部署（部署到另一台 Mac）

监控脚本通过 macOS 原生 **launchd** 调度，比 cron 更可靠（休眠唤醒后会补跑）。

### 步骤 1：在新 Mac 上完成基础安装

按上面"安装与配置"章节做一遍：
- 克隆项目
- `pip3 install requests python-dotenv`
- 配置 `.env`
- `python3 auth.py --check` 验证 Sif 鉴权 OK
- `python3 monitor.py --dry-run` 看一眼卡片内容

### 步骤 2：修改调度脚本里的项目路径

编辑 `schedule/run_monitor.sh`，把 `PROJECT_DIR` 改成新机器上的实际路径：

```bash
PROJECT_DIR="/Users/新用户名/ABA_Trends"
```

这个脚本会自动：
- cd 到项目目录
- 选择合适的 python（项目 `.venv` → homebrew → 系统）
- 写日志到 `~/Library/Logs/aba-trends/`

### 步骤 3：修改 plist 里的两处绝对路径

编辑 `schedule/com.abatrends.weekly-monitor.plist`，修改这两行：

```xml
<string>/Users/新用户名/ABA_Trends/schedule/run_monitor.sh</string>
...
<key>WorkingDirectory</key>
<string>/Users/新用户名/ABA_Trends</string>

<key>StandardOutPath</key>
<string>/Users/新用户名/Library/Logs/aba-trends/monitor.out.log</string>

<key>StandardErrorPath</key>
<string>/Users/新用户名/Library/Logs/aba-trends/monitor.err.log</string>
```

> 调度时间在 `StartCalendarInterval` 里：`Weekday=3` 表示周三（0=周日，1=周一...），`Hour=14 Minute=0` 表示 14:00。可按需调整。

### 步骤 4：安装 launchd 任务

```bash
# 创建日志目录
mkdir -p ~/Library/Logs/aba-trends

# 拷贝 plist 到 LaunchAgents
cp schedule/com.abatrends.weekly-monitor.plist ~/Library/LaunchAgents/

# 加载任务
launchctl load ~/Library/LaunchAgents/com.abatrends.weekly-monitor.plist

# 验证已加载
launchctl list | grep abatrends
```

### 步骤 5：手动触发一次验证

```bash
# 立即触发一次（不等时间到）
launchctl start com.abatrends.weekly-monitor

# 看实时日志
tail -f ~/Library/Logs/aba-trends/monitor.out.log
tail -f ~/Library/Logs/aba-trends/monitor.err.log
```

日志里看到 `✅ 推送成功: code=0 msg=success` 就 OK，去飞书群里确认卡片已到达。

### 卸载调度

```bash
launchctl unload ~/Library/LaunchAgents/com.abatrends.weekly-monitor.plist
rm ~/Library/LaunchAgents/com.abatrends.weekly-monitor.plist
```

## 推荐调度组合

**监控脚本依赖同步脚本先跑完**。建议两个任务都装上：

```bash
# 同步脚本（每周三 09:00 跑）
# （参考上面 plist，把 ProgramArguments 改成 sif_feishu_sync.py，时间改 Hour=9）

# 监控脚本（每周三 14:00 跑）
# schedule/com.abatrends.weekly-monitor.plist
```

间隔 5 小时，确保同步完成后再做监控推送。

另有独立的 CPC 周监控任务 `schedule/com.abatrends.weekly-cpc.plist`（每周一
10:00，向 ssFow6 趋势表追加 cpc 周列并推送类目汇总卡片），不依赖同步/监控链路，
详见 `openspec/specs/cpc-weekly-monitor/spec.md`。

## 故障排查

| 现象 | 可能原因 | 处理 |
|------|---------|------|
| `SIF 鉴权失败` | `.env` 里账号密码错或过期 | 检查 `.env`，或删除 `.sif_*_cache` 强制重新登录 |
| `缺少环境变量 FEISHU_APP_ID` | `.env` 没被加载 | 在项目根目录运行命令，或确认 `.env` 文件存在 |
| `code=230099 invalid card` | 卡片 JSON 字段不合法 | 看错误里的 `ErrMsg`，通常是飞书字段取值不在允许范围 |
| `code=99991663` 等 token 错 | 应用权限未开通或 token 过期 | 后台开通 `im:message:send_as_bot`，发布新版本 |
| launchd 不触发 | plist 路径错或权限问题 | 看 `~/Library/Logs/aba-trends/monitor.err.log` |
| 推送的内容是旧数据 | 同步脚本还没跑 | 先手动 `python3 sif_feishu_sync.py` 再推 |

## 数据来源说明

- 排名接口：`https://new.sif.com/api/search/compare/estSearchesNumHistory`
- 排名字段：`searchesRank`（数字越小越靠前）
- 同步脚本固定过滤：周数据保留 `2025-01-01` 之后，月数据保留 `2024-01` 之后
- 监控脚本不过滤时间范围，展示飞书表实际内容

## 文档

- 实施进度与决策记录：`todo.md`（本地，不入仓库）
- Claude Code 协作指南：`CLAUDE.md`
- 本地可视化子项目说明：`aba_trend_viewer/README.md`
