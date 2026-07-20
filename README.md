# Crypto KOL Search

本地运行的 X + 小红书双平台 KOL 发现、趋势研究与受控运营后台。它按中英文主题从真实平台内容发现候选，使用 24 小时、7 天、30 天分层回退并解释排名；评论和官方私信必须绑定授权账号、精确目标并逐条审批。

## 快速开始

需要 Python 3.11 或更高版本。

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
kol-search web
```

打开 `http://127.0.0.1:8765`。默认使用 `TWITTER_BACKEND=twitterapi_io`；请配置
`TWITTERAPI_IO_API_KEY`，或保持已连接的 OpenCLI profile 作为只读 fallback。小红书使用
`XIAOHONGSHU_OPENCLI_PROFILE` 指定的独立登录配置。正常运行时不会显示或自动选择离线 Mock 数据。

## X + 小红书真实搜索

首页选择“X + 小红书真实搜索”，输入 `加密货币/RWA`。系统会扩展中英文主题，分别从两个平台取得帖子并按作者聚合；每个平台独立排名，目标 10 个 KOL。24 小时数据不足时依次回退 7 天和 30 天，无发布时间内容不会被计为近期结果。

```bash
OPENCLI_PROFILE=x-research
XIAOHONGSHU_OPENCLI_PROFILE=xhs-research

opencli --profile x-research twitter whoami -f json
opencli --profile xhs-research xiaohongshu whoami -f json
```

数据库使用 `(platform, external_id)` 标识账号和帖子；X 与小红书同名账号不会被错误合并。运行时单个平台失败会以告警展示，不会用 Mock 或另一个平台的数据补位。

## 数据后端

- `official`：配置 `X_BEARER_TOKEN`，支持用户搜索、帖子搜索和 profile 查询。
- `twitterapi_io`：配置 `TWITTERAPI_IO_API_KEY`，使用 TwitterAPI.io 的用户搜索、帖子搜索、资料、时间线、完整 following profile 和 verified follower 接口；初始本地配置也兼容 `API_KEY`。
- `opencli`：复用指定 Chrome 用户资料的 X 登录态，不需要 API Key；支持帖子搜索、资料、时间线和 following。用户搜索通过帖子作者发现模拟，verified followers 会明确降级跳过。
- `third_party`：配置供应商 URL 和密钥；只有供应商实现 `/search/users` 时才设置 `TWITTER_TP_SUPPORTS_USER_SEARCH=true`。
- `twscrape`：额外安装 `.[twscrape]`，并显式设置 `ENABLE_TWSCRAPE=true`。此方式存在平台条款和账号封禁风险，不会自动启用。
- `mock`：仅用于自动化测试。必须显式设置 `KOL_ENABLE_MOCK_BACKEND=true` 才能启用，
  正常运行默认关闭。

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
- `/radar/topics`：15 分钟缓存的双平台 Trend，按金融、科技、AI、加密/RWA及平台筛选，并可进入详情追溯组成帖子。
- `/operations`：授权账号登记、评论/私信草稿、逐条审批、真实执行、回执、审计和 Kill Switch。

先在 `/settings/brand` 填写品牌名称、X/小红书官方账号、产品链接、定位、语气和禁用表达，再在 `/operations` 登记授权账号。评论角色为 `engagement`，官方私信角色为 `official_dm`；一个 POC 账号可以兼任两个角色。只有 `KOL_LIVE_WRITE_ENABLED=true`、任务已逐条批准、账号健康且 Kill Switch 关闭时才会执行真实发送。未知回执进入 `confirmation_required`，不会自动重试。

默认频控为评论 `3/h、10/24h`，私信 `2/h、5/24h`，同一作者冷却 7 天。评论默认禁止链接；私信中的链接必须属于 `KOL_APPROVED_PRODUCT_DOMAINS`。验证码、登录异常、限流或平台警告会停止账号，不提供任何绕过机制。

自动扫描默认关闭。确认真实后端可用后，在 `.env` 设置：

```bash
KOL_ENABLE_SIGNAL_SCAN=true
KOL_SIGNAL_INTERVAL_MINUTES=30
```

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

- Web 服务默认只绑定 `127.0.0.1`。VPN/非本机监听必须同时设置 `KOL_ADMIN_PASSWORD` 和 `KOL_SESSION_SECRET`；管理 Cookie 使用 HttpOnly、SameSite=Strict，并检查同源写请求。
- 联系方式只来自 X bio/profile links、Linktree 类页面和明确官网的同域联系页面。
- 不推断邮箱、不收集手机号、不查 WHOIS；外联只允许授权账号、精确目标、逐条审批、配额和拒绝联系名单。
- 抓取器遵守 robots.txt，限制页面大小、跳转、超时和端口，并阻止私网、localhost、云元数据地址等 SSRF 目标。
- 自动发现的联系方式以 `pending` 保存，并随证据、来源 URL 和审核状态导出。

## Postiz

Dashboard 的“主动发布”已接入 Postiz Cloud，只用于自有 X 内容：

- 从 Trend 的“创建 X 帖子”或独立编辑器创建单帖、图片帖和线程；
- 自动同步 `identifier=x` 的 integrations，显示并选择实际发布账号，可设置一个默认账号；
- 每段支持本地 JPEG/PNG/GIF/WebP 或公开 HTTPS 图片 URL；媒体只在审批后上传；
- 支持审批后立即发布或按 `Asia/Shanghai` 排期，数据库与 Postiz 请求同时保留 UTC；
- 支持定时帖暂停、恢复、删除，以及通过帖子查询回读 `releaseURL`；
- 创建结果不确定时进入 `confirmation_required`，不会自动重试而造成重复发帖。

本地配置：

```bash
POSTIZ_API_URL=https://api.postiz.com/public/v1
POSTIZ_API_KEY=             # 只放本地 .env，禁止提交或录入 Dashboard
POSTIZ_INTEGRATION_CACHE_MINUTES=15
KOL_PUBLISHING_MEDIA_DIR=data/publishing_media
```

Postiz 不参与 KOL/Trend 发现、第三方评论或私信，并明确拒绝小红书发布。未配置 API Key 时仍可查看页面，但真实同步与提交会被阻止；未审批草稿永远只保存在本地。

完整真实验收步骤、沙盒实发要求以及官方/持牌供应商迁移清单见 [VALIDATION.md](VALIDATION.md)。

## 命令

```bash
# 本地后台
kol-search web

# 命令行执行一轮并等待完成
kol-search discover "DeFi, 去中心化金融" --backend twitterapi_io --limit 30

# 种子构建与扩散目前从本地后台创建：
# 任务类型选择“构建 100 基础种子”；完成后将种子集 ID 填入“从 100 扩散到 200”

# 测试
pytest
```

SQLite 默认位于 `data/kol_search.db`，使用 WAL 模式。设置 `KOL_ENABLE_WEEKLY_REFRESH=true` 后，每周日 03:00（默认 `Asia/Shanghai`）会按当前默认后端创建总库刷新任务；可在 `.env` 调整计划和调用预算。
