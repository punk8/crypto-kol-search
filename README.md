# KOL Growth OS

本地运行的多平台 KOL 发现、信号研究与分级自动执行系统。当前架构是“共享自动化内核 + 平台原生模块 + 独立平台工作台”：每个平台保留自己的账号、内容、指标和排名模型；核心只管理任务、机会、策略、动作、审计、额度与 Kill Switch。

当前内置 X 和小红书。本轮没有接入真实 YouTube 或 Instagram，但平台契约允许新平台按能力渐进上线，例如先提供频道/账号发现和内容研究，后续再增加评论、私信或自有发布。

## 快速开始

需要 Python 3.11 或更高版本。

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
kol-search web
```

打开 `http://127.0.0.1:8765`：

- `/` 或 `/platforms`：平台中心，只汇总连接、任务、机会和异常，不做跨平台影响力排名。
- `/platforms/x`：X 工作台。
- `/platforms/xiaohongshu`：小红书工作台。
- `/platforms/x/module`、`/platforms/xiaohongshu/module`：由各平台 Router 注册的模块 Manifest 信息。
- `/settings/brand`：共享品牌资料、安全表达、批准域名，以及按已注册平台动态生成的官方账号字段。

平台工作台包含自动收件箱、主题发现、长期种子入口、平台 KOL 库、能力矩阵、动作审核、回执和通道控制。没有声明的能力不会显示对应操作。

## 产品工作流

每个平台独立执行以下流程：

```text
平台种子 / Active KOL
        ↓
持续关系与主题发现
        ↓
candidate → active / review / paused / rejected
        ↓
内容、评论与趋势信号扫描
        ↓
平台内机会排序
        ↓
auto_execute / needs_review / rejected
        ↓
动作回执与确认回收
```

默认平衡晋升要求平台综合分达到 `0.70`、近期存在相关内容，并具有种子或 Active KOL 的关系证据；受保护、已停用或确定性垃圾风险超过阈值的账号会被拒绝。搜索发现但证据不足的账号保留在 `candidate` 或 `review`，长期不活跃的 Active KOL 可以降为 `paused`，不会直接删除。

可以在每个平台工作台的“KOL 库”中填写平台原生 ID 或 handle 添加长期种子。手动发现会根据 Manifest 提供账号搜索、内容搜索或种子关系扩展来源；关系扩展允许关键词留空。

X 的持续关系发现会把 seed/Active KOL 的 following、verified follower、入站回复、引用和提及转换为可追溯证据；小红书会把 seed/Active KOL 笔记的评论作者作为候选关系来源。各平台只使用自身原生信号计算 `protected / disabled / spam_risk`，不会跨平台补位。

## 共享内核与平台模块

共享任务表只接受 5 种任务类型：

- `manual_discovery`
- `discovery_refresh`
- `signal_refresh`
- `dispatch_actions`
- `refresh_outcomes`

每条任务都携带 `platform_id` 和平台 payload。Worker 通过代码内显式 `PlatformRegistry` 找到平台处理器，不通过 X/小红书条件分支选择 Pipeline。调度器只为平台实际提供的处理器和写能力创建对应任务，因此只读平台可以先上线发现或研究能力。

每个 `PlatformPlugin` 可注册：

- `PlatformManifest` 与能力声明。
- `discover`、`scan_signals`、`build_opportunities`、`execute_action`、`refresh_outcomes` 五个平台处理器。
- 平台独立的 `native_models`、schema installer、Repository 与评分逻辑。
- `automation_adapter`，负责原生结果投影、机会构建、KOL 查询/状态和种子写入。
- FastAPI `router_factory`、健康检查和关闭钩子。

`build_opportunities` 是平台处理器契约的一部分；共享 `signal_refresh` 会先调用 `scan_signals` 获取增量原生记录，再把结果交给 `build_opportunities`。内置平台的该处理器调用自己的 `automation_adapter.project_signals` 完成持久化、平台内评分和机会投影。它不是额外的核心任务；平台必须同时注册这两个处理器，调度器才会创建周期信号任务。

共享机会和动作只使用以下引用，不要求平台内容映射到通用 Post 表：

```text
platform_id + native_object_type + native_object_id
```

完整接入步骤见 [PLATFORM_DEVELOPMENT.md](PLATFORM_DEVELOPMENT.md)。

## 平台能力

能力来自 `PlatformCapability`：

- `account_search`
- `content_search`
- `timeline/feed`
- `relations`
- `native_trends`
- `comment`
- `dm`
- `owned_publish`
- `media_upload`
- `analytics`

能力声明必须与真实实现一致。缺失能力不会生成对应动作，也不会使用其他平台的数据补位。当前 X 声明完整搜索、关系、趋势、评论、私信、Postiz 自有发布和分析能力；小红书声明内容搜索、Feed、原生趋势、评论和私信，不声明账号搜索、关系发现或自有发布。`media_upload` 只是为未来平台保留的契约能力，当前 X 和小红书都未声明，因此工作台不会显示媒体上传入口，也不会生成媒体上传动作。

## 命令行

```bash
# 查看显式注册的平台、读取连接状态与能力
kol-search platforms

# 单平台发现；--platform/-p 可重复，省略时显式默认使用 x
kol-search discover "RWA research" --platform x --limit 30
kol-search discover "加密货币" -p x -p xiaohongshu --limit 20

# 手动信号扫描默认启动 Worker 并等待终态，完成后输出结果
kol-search scan --platform x

# 入队后不轮询任务终态
kol-search scan --platform x --no-wait

# 安全重建数据库；默认先移动旧库到时间戳备份
kol-search db rebuild --backup

# 完整离线测试
pytest
```

`scan` 的默认值等同于 `--wait`；失败或取消会返回非零退出状态。`--no-wait` 适合已有 Web Worker 或其他常驻 Worker 正在消费同一数据库队列的场景。

## 自动化与安全

自动采集、平台内评分和草稿生成可以持续运行；真实写入默认关闭。只有同时设置以下开关后，符合确定性安全策略的动作才可能执行：

```bash
KOL_LIVE_WRITE_ENABLED=true
KOL_AUTO_EXECUTION_ENABLED=true
```

默认策略：

- 评论机会分不低于 `80` 才具备自动执行资格。
- 首次冷私信始终需要人工审核；只有 `automation_conversations` 中已绑定平台账号、原生会话 ID 和验证证据且状态为 `valid` 的后续私信才可能自动执行。历史成功出站私信本身不建立有效会话，调用方提交布尔标记也不能绕过验证。
- X 趋势内容可通过 Postiz 自动发布，默认每日最多 2 条，可配置。
- 评论默认禁止链接；私信链接必须属于批准域名。
- 品牌禁用词和批准域名可在 `/settings/brand` 维护；`KOL_APPROVED_PRODUCT_DOMAINS` 仅作为首次初始化的环境级批准域名来源。
- 评论限频 `3/h、10/day`，私信 `2/h、5/day`，同一作者冷却 7 天。
- 写操作不会自动重试。不确定回执进入 `confirmation_required`。
- 登录异常、验证码、限流、平台警告或不确定回执会暂停对应账号通道。
- Kill Switch 支持全局、平台、账号和动作类型四级控制；暂停写入不会停止只读采集。

关键配置：

```bash
KOL_ENABLED_PLATFORMS=x,xiaohongshu
KOL_DISCOVERY_INTERVAL_HOURS=24
KOL_SIGNAL_INTERVAL_MINUTES=30
KOL_X_SIGNAL_INTERVAL_MINUTES=5  # 可选：仅覆盖 X 的信号扫描周期
KOL_XIAOHONGSHU_SIGNAL_INTERVAL_MINUTES=30  # 可选：仅覆盖小红书
KOL_X_TRENDS_PER_SCAN=5  # 每轮组合查询覆盖的近期 Trend 数
KOL_X_TWEETS_PER_TREND=10  # 组合查询的目标单 Trend 推文数
KOL_X_TREND_WOEID=1  # 官方 X Trends 地区；1 表示 Worldwide
KOL_REVIEW_QUEUE_ENABLED=false  # 临界 KOL 留在 candidate 并持续自动重评
KOL_PLATFORM_ACCOUNT_BATCH_SIZE=50
KOL_ACTION_DISPATCH_SECONDS=60
KOL_AUTO_COMMENT_SCORE=80
KOL_AUTO_DM_FOLLOWUP_SCORE=80
KOL_AUTO_PUBLISH_SCORE=80
KOL_PUBLISH_DAILY_LIMIT=2
KOL_PUBLISH_WINDOWS=09:00-11:00,17:00-20:00
```

可选的 AI 分类、聚类和草稿能力需要额外安装依赖并配置密钥。AI 输出只作为平台内容相关性、聚类解释和草稿输入，不直接给出晋升、审核或执行结论；禁用词、链接、能力、会话验证、额度、冷却、幂等、Kill Switch 与执行资格仍由确定性策略校验。没有密钥或 AI 调用失败时会回退到离线分类/草稿路径，不阻断平台投影：

```bash
pip install -e ".[ai]"
OPENAI_API_KEY=
OPENAI_MODEL=gpt-5.6-luna
```

## 平台连接

### X

读取后端支持 `official`、`twitterapi_io`、`opencli`、`third_party`、显式启用的 `twscrape` 和测试专用 `mock`。`official` 可通过 Bearer Token 读取 WOEID Trends、近期 Trend 推文、账号资料和 KOL 时间线，并把持久化扫描游标作为 `start_time` 传给官方接口。配置 OpenCLI fallback 后，可恢复错误会在任务内切换到 Browser Bridge；HTTP 400/404/422 不触发切换。

### Agent 本地模型与 Telegram

主动型 Agent 的模型与 Telegram 凭据只由 Mac Worker 读取。复制
`config/agent-config.example.json` 到 `~/.config/kol-search/config.json`，填入真实值并执行：

```bash
chmod 600 ~/.config/kol-search/config.json
export KOL_LIVE_WRITE_ENABLED=true
kol-search worker
```

所有 Agent 共用这一个 OpenAI-compatible API key；数据库只保存 Agent 选择的模型名称、
人设、目标和运行参数。修改本地 JSON 后必须重启 Worker。不要把真实配置复制回项目目录。

### 小红书

当前通过独立 OpenCLI Browser Bridge profile 读取和执行受控互动：

```bash
OPENCLI_PROFILE=x-research
XIAOHONGSHU_OPENCLI_PROFILE=xhs-research
```

同名 X 与小红书账号保持完全独立，不进行跨平台身份推断。

小红书搜索快照如果没有返回平台原生用户 ID，会保留笔记用于内容研究，但作者会明确标记为 `unresolved`；该作者不会进入持续 Feed 扫描、自动晋升或私信，避免把本地哈希误当成平台账号 ID。后续取得真实用户 ID 后才能进入这些流程。

### Postiz

Postiz 只负责声明了对应发布桥接的 X 纯文本自有内容、排期和回执，不参与 KOL 读取、评论或私信。当前 X Manifest 未声明 `media_upload`，因此这条链路不接受或上传媒体：

```bash
POSTIZ_API_URL=https://api.postiz.com/public/v1
POSTIZ_API_KEY=
```

在“配置 → 账号通道管理”中登记 Browser Profile 和 Postiz integration ID；同一平台可登记多个 Browser 互动账号与多个 Postiz 发布账号。系统只从已连接、未暂停且声明对应能力的账号中选择通道，并按近期动作使用量分配，使用量相同时优先选择标记为优先的账号。新自动化链路把 Postiz integration 直接保存为 X 的 `owned_publish` 连接，不创建旧 `owned_posts` 草稿，也不会从旧 Postiz 表隐式导入连接。发布窗口由 X 模块在实际执行前计算；超时或缺少可确认回执时只进行 `refresh_outcomes` 回读，绝不重发。结果回收优先按 external ID 精确匹配；缺少 external ID 时，只接受 integration、完整内容和动作时间前后 15 分钟同时匹配且结果唯一的记录，零条或多条都保持 `confirmation_required`。

## 数据结构

- `automation_*`：共享连接、5 种任务、机会引用、动作、策略决策、已验证会话、通道控制、额度、品牌配置和审计；`automation_conversations` 独立保存平台原生会话的验证/撤销状态和证据。
- `x_*`：X 原生账号、KOL 状态、Tweet、指标快照、趋势和发现证据。
- `xhs_*`：小红书原生用户、KOL 状态、笔记、评论、指标快照、趋势和发现证据。
- `x_schema_migrations`、`xhs_schema_migrations`：平台模块自己的 schema 版本记录。
- 旧 `runs / contacts / seed_sets / owned_posts / accounts / posts` 业务 schema 与对应 Web 入口已删除；新运行时不会创建或读取这些表。

数据库默认位于 `data/kol_search.db`，使用 WAL。重建命令不会静默覆盖旧库；初始化失败时会恢复备份并保留失败的新文件。

信号扫描按平台原生账号 ID 稳定轮转，每批最多 `KOL_PLATFORM_ACCOUNT_BATCH_SIZE` 个账号，并保存每账号独立内容游标；一个账号的高水位不会跳过另一个账号的新内容。Worker 启动时会把超过 15 分钟仍为 `running` 的遗留任务标为 `failed`，要求检查平台状态后再人工决定是否重跑；遗留 `executing` 写动作恢复为 `confirmation_required`，两者都不会自动重试。

浏览器写入在不可逆的发送点击上只尝试一次。点击超时/报错可能发生在平台已接收写入之后，因此一律视为不确定；评论或私信只有精确回执元素相对发送前快照新增了最终文本、且编辑器中已无该文本等平台定义信号同时成立时才确认。页面正文、旧同文消息、仍停留在编辑器里的草稿都不能作为成功证据；浏览器链路也不会用本地摘要伪造平台 external ID。

## 开发约束

- 新平台必须通过代码内显式注册加入，不使用动态插件市场或 Python entry points。
- 测试不得依赖真实网络或凭据；Mock 必须通过 `KOL_ENABLE_MOCK_BACKEND=true` 显式启用。
- Web 默认只绑定 `127.0.0.1`；非本机监听必须配置管理员密码和 session secret。
- API Key、Browser profile 数据库、运行数据库和导出均不得提交。
- AI 只提供分类、聚类、相关性和草稿输入；所有安全门、审批与自动执行资格由确定性策略控制，执行前会用最终文本再次校验。

验收步骤见 [VALIDATION.md](VALIDATION.md)。
