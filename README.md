# Trend & KOL Discover

一个面向研究的 Trend Tracking + KOL Tracking 平台。当前产品界面只开放 X；未来平台继续通过独立 connector 接入，不做跨平台人物聚合。

其他平台 adapter 与离线 fixture 继续保留，但不出现在用户界面。X 数据通过独立后端 connector 获取；前端部署在 Vercel，采集、持久化、定时追踪和后续 LLM 处理运行在自有服务器。

数据采集和前端投影的稳定边界位于 `src/kol_search/backend/`。版本化接口 `/api/discover/v1` 按平台暴露趋势、内容搜索、KOL 发现与显式追踪、账号时间线和单条内容查询；每个平台 connector 再调用可替换 provider adapter。X 默认按 `fxembed,getxapi` 尝试，免费 HTTP reader 优先、付费 API 补位；后续切换官方 X API 只需修改 provider chain，无需更改前端合同。

## 快速开始

需要 Python 3.11 或更高版本。

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
kol-search web
```

独立启动后端服务：

```bash
pip install -e ".[twscrape]"
kol-search backend --host 127.0.0.1 --port 8780
```

打开 `http://127.0.0.1:8765`：

- `/` 或 `/trends`：X Trend Radar。
- `/kols?platform=x`：发现、评分和追踪 X KOL。
- `/watchlist`：汇总已追踪 X 账号的最新内容，同时保留发布时间、抓取时间和来源健康状态。
- `/platforms`：平台数据源与连接状态。
- `/platforms/x`：X 的趋势推文、KOL 推文、主题发现与 KOL 库。
- `/platforms/xiaohongshu`：小红书的主题发现与 KOL 库。
- `/health`：数据库、平台注册与执行模式健康检查。

## 产品工作流

```text
平台原生信号 + Web 信号 ──→ 分类与快照 ──→ Trend Radar

领域定义 + 选定平台 ──→ 平台账号候选 ──→ 平台内评分 ──→ Watchlist
                                                        │
                                                        └─→ 最新内容与趋势关联
```

趋势排名保留平台原生语义，不把不同平台的排名强行换算成一个虚假的全局分数。KOL 只使用所在平台的内容、关系、活跃度与风险证据评分；同一个人在不同平台的账号不会被合并成统一 KOL 或统一影响力分数。

默认晋升阈值为平台综合分 `0.70`，并要求近期相关内容和足够的关系证据。受保护、已停用或垃圾风险过高的账号会被拒绝；证据不足的账号保留在 `candidate` 或 `review`。

## 命令行

交互式发现命令在当前进程内完成；自动维护 Watchlist 和 Hot Content 的长期任务由后端服务器上的受监督调度服务执行。

```bash
# 查看数据源和读取能力
kol-search platforms

# 发现并评估 KOL
kol-search discover "RWA research" --platform x --limit 30
kol-search discover "加密货币" -p x -p xiaohongshu --limit 20

# 立即刷新一个平台的趋势与内容信号
kol-search scan --platform x

# 立即执行一次自动 KOL 发现和 Hot Content 刷新
kol-search backend-scheduler --once

# 在后端服务器上持续调度（建议交给 systemd 管理）
kol-search backend-scheduler

# 安全重建本地数据库
kol-search db rebuild --backup

# 测试
pytest
```

## 平台连接

### X

读取后端支持 `official`、`getxapi`、`twitterapi_io`、`third_party`、显式启用的 `twscrape`、免登录的 `fxembed` 和测试专用 `mock`。GetXAPI 是当前内部版本的主 HTTP 数据商；`twscrape` 与 `fxembed` 属于实验性来源，FxEmbed 仅作为免登录 fallback。

```dotenv
KOL_X_PROVIDER_CHAIN=fxembed,getxapi
GET_X_API_KEY=...
GET_X_API_BASE_URL=https://api.getxapi.com
GET_X_API_DAILY_CALL_LIMIT=120
GET_X_API_MIN_CREDITS=0.05
KOL_BACKEND_API_TOKEN=...
KOL_X_TREND_WOEID=1
```

`official` 可读取 WOEID Trends、近期 Trend 推文、账号资料和 KOL 时间线。`getxapi` 覆盖趋势、推文搜索、用户搜索、资料、时间线、内容详情和关注关系；`twitterapi_io` 与 `third_party` 保持为可替换 HTTP 数据商。缺少凭据或主 provider 失败时，只会按显式 chain 降级，不会调用本地浏览器。

主要 API：

```text
GET /api/discover/v1/platforms
GET /api/discover/v1/platforms/x/providers/usage
GET /api/discover/v1/platforms/x/trends
GET /api/discover/v1/platforms/x/content/search?q=AI&sort=top
GET /api/discover/v1/platforms/x/accounts/search?q=AI%20agents
POST /api/discover/v1/platforms/x/accounts/{account-id}/tracking
GET /api/discover/v1/platforms/x/accounts/{handle-or-id}/content
GET /api/discover/v1/platforms/x/content/{post-id}
GET /api/discover/v1/platforms/x/hot-content?source=discover&domain=AI
POST /api/discover/v1/platforms/x/content/{post-id}/reply-draft
```

后端非本机监听时，调用方必须携带 `Authorization: Bearer <KOL_BACKEND_API_TOKEN>`。完整扩展方式见 [Connector Architecture](docs/CONNECTOR_ARCHITECTURE.md)。

### 小红书

小红书目前默认关闭。只有接入具备明确授权、稳定配额和服务端 HTTP 接口的数据商后才启用：

```dotenv
KOL_ENABLED_PLATFORMS=x,xiaohongshu
```

接入方必须返回稳定的平台原生用户 ID、内容链接、采集时间和来源标识。身份未解析的数据只能作为内容证据，不能进入持续 KOL 生命周期。

## 存储与部署

默认使用 `data/kol_search.db`。也可以配置 PostgreSQL：

```dotenv
KOL_DATABASE_URL=postgresql://...
```

部署分成 Vercel 前端与自有服务器后端。平台和 LLM 凭据只放在后端 secret 环境；Vercel 仅保存后端 URL 和服务间 token。后端可以运行受监督的采集任务，但不依赖 Chrome、OpenCLI 或浏览器 profile。

默认自动管理 `AI`、`Crypto`、`Financial` 三个领域。候选账号必须连续两轮满足评分、证据和近期相关内容阈值才会自动进入 Watchlist；Hot Content 按互动速度、互动量、时效性、领域相关性和作者质量综合过滤与排序。领域列表、阈值和刷新频率均可通过后端环境变量调整。

前端/BFF 通过 `KOL_BACKEND_URL` 调用版本化的 `/api/discover/v1`；本地不配置该变量时，会直接使用相同 connector 与标准化响应模型，便于单进程开发。Hot Content 使用 `KOL_X_HOT_CONTENT_QUERY`，X Watchlist 可先用 `KOL_X_WATCH_HANDLES` 引导，之后也会读取已入库的 X KOL。完整拓扑与环境变量见 [DEPLOYMENT.md](DEPLOYMENT.md)。

关键数据表：

- `automation_jobs`：保存每次请求内发现的执行记录与结果。
- `x_*`：X 账号、Tweet、指标、趋势和发现证据。
- `xhs_*`：小红书用户、笔记、评论、趋势和发现证据。
- `x_kols` / `xhs_kols`：平台独立的 KOL 生命周期与评分。

## 开发约束

- 新平台必须通过显式 `PlatformRegistry` 注册，并声明真实读取能力。
- 测试不得依赖真实网络或凭据；Mock 必须通过 `KOL_ENABLE_MOCK_BACKEND=true` 显式启用。
- Web 默认只绑定 `127.0.0.1`；非本机监听必须配置管理员密码和 session secret。
- 不得提交 `.env`、API Key、数据库或导出文件。
- 平台原生 schema、Repository 和评分逻辑保持独立，统一首页只聚合展示。

平台扩展见 [PLATFORM_DEVELOPMENT.md](PLATFORM_DEVELOPMENT.md)，验收步骤见 [VALIDATION.md](VALIDATION.md)。
