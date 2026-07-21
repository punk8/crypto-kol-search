# Crypto KOL Search

本地运行的中英双语 Crypto KOL 发现与公开商务联系方式研究后台。它按主题从 X 数据源发现候选、解释排名，区分个人与机构，并且只从 X 主页及其明确链接页面提取带来源证据的公开联系方式。

## 快速开始

需要 Python 3.11 或更高版本。

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
kol-search web
```

打开 `http://127.0.0.1:8765`。默认 `TWITTER_BACKEND=mock`，因此无需外部 API 即可完成一轮演示。

## 数据后端

- `official`：配置 `X_BEARER_TOKEN`，支持用户搜索、帖子搜索和 profile 查询。
- `twitterapi_io`：配置 `TWITTERAPI_IO_API_KEY`，使用 TwitterAPI.io 的用户搜索、帖子搜索、资料、时间线、完整 following profile 和 verified follower 接口；初始本地配置也兼容 `API_KEY`。
- `opencli`：复用指定 Chrome 用户资料的 X 登录态，不需要 API Key；支持帖子搜索、资料、时间线和 following。用户搜索通过帖子作者发现模拟，verified followers 会明确降级跳过。
- `third_party`：配置供应商 URL 和密钥；只有供应商实现 `/search/users` 时才设置 `TWITTER_TP_SUPPORTS_USER_SEARCH=true`。
- `twscrape`：额外安装 `.[twscrape]`，并显式设置 `ENABLE_TWSCRAPE=true`。此方式存在平台条款和账号封禁风险，不会自动启用。
- `mock`：离线 fixtures，用于测试和本地演示。

除显式配置的 TwitterAPI.io → OpenCLI fallback 外，各后端不会自动相互降级。任务会保留实际后端、调用计数、告警和错误。

### TwitterAPI.io → OpenCLI fallback

默认可将 OpenCLI 配置为 TwitterAPI.io 的只读 fallback：

```bash
OPENCLI_COMMAND=opencli
OPENCLI_PROFILE=ddd
OPENCLI_TIMEOUT_SECONDS=90
TWITTER_FALLBACK_BACKEND=opencli
```

当 TwitterAPI.io 遇到网络错误、超时、无效响应、401/402/403/408/429 或 5xx 时，当前任务会从失败的调用开始切换到 OpenCLI，后续调用保持使用 OpenCLI。HTTP 400/404/422 不会触发 fallback。任务统计会记录实际活跃后端、两侧调用次数、切换次数和原因。

也可以直接选择独立后端：

```bash
kol-search discover "DeFi researcher" --backend opencli --limit 30
```

项目按 OpenCLI 1.8.6 的命令接口集成，并显式使用 `OPENCLI_PROFILE` 指定的 Browser Bridge profile。Chrome 需要安装并连接 Browser Bridge、登录 X，并保持至少一个窗口打开。使用 `opencli profile list` 检查连接状态，再用 `opencli --profile ddd twitter search bitcoin --product live --limit 1 -f json` 做只读验证。浏览器关闭后，定时任务无法使用该后端或 fallback。

## 人物雷达与趋势雷达

后台提供两个面向执行的工作台：

- `/radar/people`：从 approved KOL 时间线和主题搜索中生成回复机会、可编辑草稿与 24 小时处理窗口。
- `/radar/topics`：按讨论增速、规模、独立作者和重点 KOL 参与生成热搜选题与单条 X 帖提纲。

先在 `/settings/brand` 填写品牌名称、X handle、定位、语气和禁用表达，再到人物雷达手动运行一次信号扫描。系统不会自动发送回复或发布内容。OpenCLI Browser Bridge 就绪后，待处理项会显示“通过 OpenCLI 回复”；只有人工确认点击才会发送。发送后系统会读取品牌账号时间线确认真实回复链接，无法确认时进入“待确认”而不会自动重试。回填或确认实际回复链接后，启用自动信号扫描可在 1、6、24 小时复查互动和原作者回应。

自动扫描默认关闭。确认真实后端可用后，在 `.env` 设置：

```bash
KOL_ENABLE_SIGNAL_SCAN=true
KOL_SIGNAL_INTERVAL_MINUTES=30
```

## Postiz owned-content publishing

Postiz 只用于发布公司自有内容，不处理 KOL DM、人物雷达评论、X Sender 或 OpenCLI reply。
Phase 1 从已采用的 Topic Radar 草稿开始，一次选择一个 Postiz integration、提交一条帖子，
并保存一个 Postiz post ID 与最终链接。账号连接、OAuth、日历和 analytics 仍在 Postiz 中管理。

```bash
POSTIZ_BASE_URL=https://api.postiz.com
POSTIZ_API_KEY=
POSTIZ_TIMEOUT_SECONDS=30
```

API key 只从环境读取，不写入 SQLite。当前集成使用 Postiz public API 的
`GET /public/v1/integrations`、`POST /public/v1/posts` 和带日期范围的
`GET /public/v1/posts`。`now` 立即提交，`schedule` 使用明确的 ISO 时间。
提交超时或回执不完整会进入 `confirmation_required`，不会自动重试。

## 初始种子库

`seeds/crypto_seed_library.csv` 包含 140 个策展候选：100 个进入基础核验名单，40 个保留在 review 队列。基础名单固定为 80 个个人、20 个机构，满足 12 个主题、70/20/10 语言和 6/6/4/4 机构配额；`seeds/crypto_handles.txt` 只包含其中 80 个个人账号。

后台的“构建 100 基础种子”会重新取得真实数字 ID、公开状态、粉丝数和最近 20 条内容。只有全部 100 个通过活跃度、相关证据、原创/推广比例和异常账号检查，版本化种子集才会成为 `ready` 并写入 100 个 `approved`。CSV 不保存伪造 ID；当前无法联网核验的行保留 `live_validation_required` 标记。

“从 100 扩散到 200”使用时间线提及、40 个高信任种子的 following、20 个核心种子的 verified followers 和 12 个主题用户搜索聚合最多 2,000 个候选。最终新增 100 个账号以 `pending` 进入新版本，不会自动晋升。每条新增记录保留关系类型、共同种子数和证据；可在详情页人工批准或驳回，并导出 CSV/JSON。

每个构建/扩散任务使用 5 美元估算硬预算；达到 80% 会停止非必要关系扩展。Profile/时间线缓存 7 天，关系和 verified follower 缓存 14 天。HTTP 402 会把新版本保留为 `incomplete`，不批准部分核验结果，也不会自动切换数据后端。

## 可选模型增强

```bash
pip install -e ".[ai]"
```

设置 `OPENAI_API_KEY` 后，后台可选择模型增强。模型只负责账号类型、语言、主题和相关度分类；联系方式始终由确定性解析器从公开页面提取，不允许模型猜测。模型默认值可通过 `OPENAI_MODEL` 调整。

## 安全和数据边界

- Web 服务默认只绑定 `127.0.0.1`，没有登录模块；不要直接暴露到公网。
- 联系方式只来自 X bio/profile links、Linktree 类页面和明确官网的同域联系页面。
- 不推断邮箱、不收集手机号、不查 WHOIS 或数据经纪商，也不发送外联消息。
- 抓取器遵守 robots.txt，限制页面大小、跳转、超时和端口，并阻止私网、localhost、云元数据地址等 SSRF 目标。
- 自动发现的联系方式以 `pending` 保存，并随证据、来源 URL 和审核状态导出。

## 命令

```bash
# 本地后台
kol-search web

# 命令行执行一轮并等待完成
kol-search discover "DeFi, 去中心化金融" --backend mock --limit 30

# 种子构建与扩散目前从本地后台创建：
# 任务类型选择“构建 100 基础种子”；完成后将种子集 ID 填入“从 100 扩散到 200”

# 测试
pytest
```

SQLite 默认位于 `data/kol_search.db`，使用 WAL 模式。设置 `KOL_ENABLE_WEEKLY_REFRESH=true` 后，每周日 03:00（默认 `Asia/Shanghai`）会按当前默认后端创建总库刷新任务；可在 `.env` 调整计划和调用预算。
