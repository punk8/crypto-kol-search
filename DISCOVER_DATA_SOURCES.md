# Discover 数据源调研

更新日期：2026-07-26

## 结论

仅靠 Web Search 可以做出可用的第一版“开放网络趋势发现 + KOL 候选发现”，但不能可靠地提供平台原生 Trend、完整互动指标、稳定账号身份和平台内关系网络。因此，生产方案应采用两阶段模型：

1. Web Search 负责广覆盖召回主题、内容链接和候选账号。
2. 平台 API 或合规 HTTP 数据商负责身份解析、原生指标和证据复核。

如果第一版只做 X，建议组合为：

- OpenAI Responses API `web_search` 或独立 Web Search API：跨站召回、摘要、来源 URL。
- X 官方 API：原生 Trends、Post Search、User Search、账号与内容指标。
- GetXAPI：当前内部版本的低成本主 HTTP 数据商，覆盖 Trends、Tweet Search、User Search、Profile、Timeline 与关注关系。
- TwitterAPI.io：在成本、配额或接口覆盖需要时作为可替换的 HTTP 数据商，而不是浏览器 fallback。
- PostgreSQL：只保存产品条款允许持久化的原始字段，以及标准化后的趋势/KOL 证据。

小红书在拿到明确授权的服务端 HTTP 数据源前保持关闭。

## X 数据接入决策：后台增量采集，前台只读结果

X API 不由浏览器直接调用，也不在每次页面请求中临时完成全量搜索。推荐的数据面分成两条有界路径：

1. 受 `CRON_SECRET` 保护的 Vercel Cron 或外部调度器，调用短时、幂等的采集端点。
2. 用户在页面主动刷新时，只触发一个受限增量任务；页面优先返回 PostgreSQL 中最近一次成功快照。

每次采集按 `provider + query/account + cursor` 保存游标，按 X Post ID 和 User ID 幂等写入 PostgreSQL。Trend、KOL Score、Hot Content 都从已保存快照计算，前端接口不持有 X 凭证，也不把 API 配额暴露给用户流量。

MVP 可继续运行在 Vercel：Pro 计划用 5–15 分钟 Cron 拉取 Recent Search、Post Counts 和 Watchlist timelines；Hobby 的 Cron 只能每日执行，不足以实现“最新趋势”，应改用外部调度器或升级计划。若未来使用 Filtered Stream 等长连接能力，再把采集器迁到独立常驻服务，展示层和数据库契约不变。

## 能力地图

| 数据途径 | Trend Discover | KOL Discover | 主要缺口 | 判断 |
|---|---|---|---|---|
| OpenAI Web Search | 跨站新闻、讨论与新词召回 | 从文章、榜单和个人页召回候选人 | 不是平台原生榜单；互动数、粉丝数和关系图不完整 | 已确认 |
| 独立 Web Search API | 可按时效、地区、域名搜索页面 | 可批量获取候选 URL 和 snippet | 同样缺少平台原生结构；存储权取决于合同 | 已确认 |
| X 官方 API | 原生 WOEID Trends、Post Search | User Search、账号指标、时间线与关系证据 | 受访问层级、配额和成本约束 | 已确认 |
| GetXAPI | X Trends、内容搜索、用户搜索、时间线、关注关系 | 单次约 $0.001；补齐 KOL Discover 候选召回 | 第三方依赖；需监控余额、字段与服务可用性 | 已接入并验证 |
| TwitterAPI.io | X 内容搜索及账号/内容字段 | 可补充账号和内容指标 | 第三方依赖；需监控字段语义、稳定性和合规性 | 已确认 |
| YouTube Data API | 关键词、时间、地区下的视频发现 | Channel 搜索与 Channel statistics | 不等价于全站“热搜榜”；配额需要预算 | 已确认 |
| Google Trends API | 搜索兴趣时间序列、地区比较 | 只能间接发现相关创作者 | 仍是限量 alpha，不适合作为当前唯一依赖 | 已确认 |
| News API | 实时 headlines 与新闻主题聚类 | 文章作者/受访者可作为候选线索 | 不是社交平台影响力指标 | 已确认 |
| Hacker News API | Top/New/Best 的技术社区原生榜单 | 可发现高质量作者，但资料和关系较弱 | 只覆盖 Hacker News 社区 | 已确认 |
| TikTok Research API | 视频与账号公共数据 | 账号、粉丝、内容指标 | 仅合资格研究者；新视频和统计存在延迟 | 已确认 |
| 小红书官方开放平台 | — | — | 当前公开文档聚焦商品、订单、库存，不构成内容/KOL Discover API | 高可信推断 |

## Backend source catalog

新数据服务通过以下接口公开可机读的来源策略和展示数据投影：

- `GET /api/backend/health`：确认数据服务以有界、request-scoped 模式运行。
- `GET /api/backend/sources`：按 `platform`、`purpose` 查询 primary、secondary、conditional 和禁止采集的来源。
- `POST /api/backend/process/content`：按 `platform + native_id` 聚合指标快照，保留最近一次展示投影，并返回去重统计。
- `GET /api/discover/v1/platforms`：返回已注册平台 connector、能力和 provider 状态。
- `GET /api/discover/v1/platforms/{platform}/trends`：返回平台原生趋势合同。
- `GET /api/discover/v1/platforms/{platform}/content/search`：查询并标准化公开热门内容。
- `GET /api/discover/v1/platforms/{platform}/accounts/{account}/content`：读取单个平台账号的最新公开内容。

当前来源等级：

| 等级 | 来源 | 用法 |
|---|---|---|
| Primary | X API、YouTube Data API | 平台原生身份、内容、指标和可用的趋势信号 |
| Secondary | GetXAPI、TwitterAPI.io、Web Search、YouTube channel feed | API 补充、广覆盖召回或已知账号的新内容提示 |
| Conditional | twscrape、FxEmbed、Reddit API、Instagram API | X 非官方来源仅限显式批准的内部实验；其他平台在获得审批、安装范围或账号授权后启用 |
| Not for ingestion | GetDayTrends、Nitter | 仅供人工参考，不连接生产采集流水线 |

GetDayTrends 的公开条款禁止批量提取、缓存、保存或传输站内信息；Nitter 使用非官方 X API，并且运行实例现在需要真实账号 session。因此二者不能作为生产主源或静默 fallback。

## 为什么 Web Search 不足以单独支撑产品

Web Search 返回的是“与查询相关的网页”，不是平台的完整事件流。它可以证明“这个页面被搜索索引发现”，不能直接证明：

- 某主题正在某个平台原生榜单上升；
- 某账号的粉丝数、互动率、发帖频率和关系网络是当前值；
- 多个相似用户名属于同一个人；
- 没有被索引到就代表没有发生；
- 搜索排序可以当作平台热度排序。

因此 Web Search 结果必须标记为 `source_scope=web`，平台 API 结果标记为 `source_scope=native`，UI 不能把两种 rank 混成同一榜单。

## 推荐的数据流水线

```text
用户主题
  -> Web Search 多查询召回
  -> URL / 域名 / 作者实体去重
  -> 主题聚类与增长信号
  -> 候选账号解析
  -> 平台 HTTP API 验证身份与指标
  -> 证据评分
  -> Trend / KOL 结果
```

每条证据至少保存：`provider`、`source_scope`、`source_url`、`published_at`、`captured_at`、`query`、`native_id`（如果存在）、`raw_rank`（如果存在）和 `confidence`。

## 推荐实施顺序

### Phase 1：Vercel MVP

- 默认只启用 X。
- 用 OpenAI Responses API `web_search` 做跨站召回和带引用摘要，或选择一个合同允许所需存储方式的独立搜索 API。
- 用 X 官方 API 或 TwitterAPI.io 验证账号、帖子与指标。
- Trend 卡片明确显示 `Web signal` 或 `X native`。
- X 由受保护的短时 Cron/外部调度任务增量采集并写入 PostgreSQL；页面请求只查询最近成功快照。
- 手动刷新可以触发有界增量采集，但必须做并发锁、幂等写入、预算限制和 provider diagnostics。
- 浏览器永不直接持有或调用 X API 凭证。

### Phase 2：扩展开放 HTTP 平台

- YouTube：`search.list` 召回视频/频道，`channels.list` 补统计。
- Hacker News：Top/New/Best 作为技术类原生趋势源。
- News API：补新闻速度和来源多样性。
- Google Trends：获得 alpha 权限后补搜索兴趣曲线。

### Phase 3：受限平台

- TikTok 只有在主体和用途满足 Research API 资格时接入。
- 小红书只有在获得合规、稳定、服务端 HTTP 数据商与可持久化条款后接入。

## 关键卡点与反证检查

1. **存储许可**：搜索 API 能返回结果，不代表允许长期保存或建立结果数据库。Brave 当前标准条款对缓存、数据库化和衍生使用有限制，采用前必须确认订单条款或企业合同。
2. **身份解析**：snippet 中的 `@handle` 只是候选；进入 KOL 库前必须由平台 API 返回稳定 native ID。
3. **新鲜度**：网页发布日期、索引时间和平台发布时间不是同一个时间。必须同时记录 `published_at` 与 `captured_at`。
4. **排名语义**：Web relevance、新闻 headline、X Trend rank、YouTube views 不能直接相加。统一首页应展示证据，评分规则按平台独立计算。
5. **覆盖偏差**：搜索引擎偏向可抓取、可索引和权威域名；封闭或登录态内容天然缺失。
6. **供应商切换**：标准化 provider interface，保留原始 provider、字段版本和 diagnostics，避免把某一家返回结构写进领域模型。

## 官方证据

- [OpenAI Web Search](https://developers.openai.com/api/docs/guides/tools-web-search)：Responses API 可启用 `web_search`，返回引用、完整 sources，并支持域名过滤。
- [X Trends by WOEID](https://docs.x.com/x-api/trends/get-trends-by-woeid)：返回指定地区的原生趋势名与 Post 数。
- [X User Search](https://docs.x.com/x-api/users/search/introduction)：按姓名、用户名和 bio 搜索账号，并可返回公开指标。
- [X Post Search query guide](https://docs.x.com/x-api/posts/search/integrate/build-a-query)：Recent/Archive Post Search 的查询与访问层级边界。
- [TwitterAPI.io Advanced Search](https://docs.twitterapi.io/api-reference/endpoint/tweet_advanced_search)：HTTP 搜索返回 Post、作者与互动字段。
- [YouTube Search](https://developers.google.com/youtube/v3/docs/search/list) 与 [Channels](https://developers.google.com/youtube/v3/docs/channels/list)：关键词召回后按 channel ID 补频道资料和 statistics。
- [Google Trends API alpha](https://developers.google.com/search/apis/trends)：滚动五年、地区和固定时间粒度的搜索兴趣数据，目前需申请 alpha。
- [News API endpoints](https://newsapi.org/docs/endpoints)：Everything 与 Top Headlines 两类新闻发现接口。
- [Hacker News official API](https://github.com/HackerNews/API)：提供 Top/New/Best 等实时榜单。
- [TikTok Research API](https://developers.tiktok.com/products/research-api/) 与 [FAQ](https://developers.tiktok.com/doc/research-api-faq)：资格限制、配额与数据延迟。
- [小红书开放平台](https://school.xiaohongshu.com/en/open/index.html)：公开 API 文档当前列出订单、商品、库存等商家能力。
- [Brave Search API](https://api-dashboard.search.brave.com/app/documentation/web-search/get-started) 与 [服务条款](https://api-dashboard.search.brave.com/app/documentation/general/terms-of-service)：支持 freshness、地区和域名搜索，但持久化前必须核对许可。
- [Google Custom Search JSON API](https://developers.google.com/custom-search/v1/overview)：已关闭新客户，并要求现有客户在 2027-01-01 前迁移，不建议作为新系统依赖。
- [Bing Search API retirement](https://learn.microsoft.com/en-us/lifecycle/announcements/bing-search-api-retirement)：已于 2025-08-11 退役，不应纳入候选。
