# X + 小红书四项能力验证文档

## 1. 范围与证据原则

本轮只验收 X 和小红书：近期 KOL 搜索、Trend 内容、授权账号评论、官方账号私信。运行环境禁止启用 Mock；Mock/fixtures 只用于 pytest，不能作为产品验收证据。

每项真实证据必须记录：平台、数据源、浏览器配置别名、目标账号/帖子原始链接、采集或发送时间、任务/审计 ID、平台回执及截图。一个平台失败时保留另一个平台结果，并明确显示失败原因；不得用其他平台数据补位。

## 2. 环境与登录

```bash
source .venv/bin/activate
cp .env.example .env

# 运行时禁止 Mock
KOL_ENABLE_MOCK_BACKEND=false
TWITTER_BACKEND=opencli
OPENCLI_PROFILE=x-brand
XIAOHONGSHU_OPENCLI_PROFILE=xhs-brand

# 只有沙盒实发时才开启
KOL_LIVE_WRITE_ENABLED=false
KOL_APPROVED_PRODUCT_DOMAINS=product.example.com

opencli profile list
opencli --profile x-brand twitter whoami -f json
opencli --profile xhs-brand xiaohongshu whoami -f json
kol-search web
```

- X、小红书每个授权账号使用独立 Browser Bridge profile。
- 系统只保存 profile 别名，不保存密码或 Cookie。
- 登记账号时，平台账号 ID、用户名必须与 `whoami` 一致；发送前会再次校验。
- 本机访问可以不设置管理员密码；通过 VPN 访问必须配置 `KOL_ADMIN_PASSWORD` 和 `KOL_SESSION_SECRET`。

## 3. 场景一：搜索“加密货币/RWA”热门 KOL

### 操作

1. 在首页选择“X + 小红书真实搜索”。
2. 输入 `加密货币/RWA`，每平台目标数设为 `10`。
3. 启动任务并等待完成。

### 预期

- 自动扩展中英文查询，包括 `RWA`、`real world assets`、`tokenization`、`现实世界资产`、`资产代币化`。
- 每个平台先取最近 24 小时；不足时回退 7 天，再不足回退 30 天。
- 无发布时间内容不会冒充近期结果，并计入告警。
- X、小红书分别排名，每个平台目标 10 个；不足时任务仍完成并显示实际数量和原因。
- 每个结果包含 `platform`、平台外部 ID、原始主页、来源、采集时间、评分分项和 1–3 条代表内容。
- 相同用户名可以同时存在于两个平台；唯一身份为 `(platform, external_id)`。

### 通过标准

- 两个平台均返回真实结果，或页面明确标记未登录/真实结果不足。
- Top 10 中至少 8 个候选的代表内容与 RWA 明确相关。
- 重跑不会因账号改名或跨平台同名生成错误合并。
- 导出中不包含 `source_provider=mock`。

## 4. 场景二：点击 Trend

### 操作

1. 点击导航栏“趋势雷达”，首次进入触发真实刷新。
2. 分别选择金融、科技、AI、加密/RWA，以及 X、小红书过滤条件。
3. 点击某张趋势卡进入详情。
4. 15 分钟内重复进入，再执行一次“强制刷新”。

### 预期

- 首次刷新读取 X 原生趋势与搜索内容、小红书 Feed 与搜索内容。
- 方向默认为 `finance/technology/ai/crypto-rwa`，关键词来自 `trend_categories`，可扩展。
- 每平台目标 20 条内容，使用 24h→7d→30d 回退；每条都有作者、时间、平台、原始链接及指标快照。
- 详情按平台展示组成帖子，不跨平台直接比较原始互动数。
- 15 分钟缓存内快速返回；强制刷新重新采集。
- URL、平台 ID 和聚类指纹阻止重复主题/帖子。
- 生命周期只使用 `emerging/rising/breakout/declining`。

### 通过标准

- Top 20 至少 90% 位于页面标记的实际回退窗口。
- 缓存结果显示上次采集时间；平台失败不清空已有结果。
- 每张趋势卡可追溯到详情中的组成帖子。

## 5. 场景三：授权账号评论

### 沙盒准备

1. 为 X、小红书各准备一个项目方自有账号和一个自有测试目标帖子。
2. 在“推广操作”登记账号，角色选择 `engagement`，绑定独立 browser profile。
3. 保持 `KOL_LIVE_WRITE_ENABLED=false`，先完成草稿与审批测试。

### 流程

1. 从人物雷达选择回复机会，或在推广操作台输入精确目标链接。
2. 创建草稿；评论必须回应原帖且默认不包含链接。
3. 管理员核对平台、账号、目标和最终文案后逐条批准。
4. 将 `KOL_LIVE_WRITE_ENABLED=true`，重启服务并执行一条已批准沙盒任务。
5. X 使用精确单推文 reply；小红书使用可见浏览器定位评论框和发送按钮。
6. 发送后回读页面/平台回执；无法确认时进入 `confirmation_required`，不得自动重试。

### 强制验证

- 未审批、真实写入开关关闭、Kill Switch 开启、账号暂停时均不能发送。
- 默认每账号评论 `3/h、10/24h`；同一作者冷却 7 天。
- 同一帖子只允许一个授权账号成功评论。
- 评论链接、重复幂等键、拒绝联系目标、第三次连续账号失败均被阻止。
- 验证码、限流、登录异常或平台警告立即停止，不实现任何绕过。

## 6. 场景四：官方账号私信

### 沙盒准备

- X、小红书各准备一个角色为 `official_dm` 的官方测试账号和一个允许接收私信的自有目标账号。
- 在品牌设置中填写品牌名称、两个平台账号和产品 URL。
- 产品域名必须同时存在于 `KOL_APPROVED_PRODUCT_DOMAINS`。

### 流程

1. 从高分 KOL 榜单点击“创建官方私信草稿”，系统带入平台 ID、主页和个性化首次触达文案。
2. 首次私信说明官方身份、联系原因、产品链接和拒绝继续联系的方法。
3. 每条首次私信及后续回复都必须逐条批准。
4. 执行时浏览器必须定位到精确目标主页/会话；不使用 OpenCLI 的批量 `reply-dm`。
5. 平台不允许发起会话时记录 `target_not_messageable`，不得换号绕过。

### 强制验证

- 只有 `official_dm` 账号可以创建私信任务。
- 默认每账号私信 `2/h、5/24h`。
- 未标记有效会话的目标只能进行一次首次触达。
- 非白名单产品链接被拒绝。
- 退订、拒绝或投诉加入 `do_not_contact`，之后所有账号都不能再联系该平台目标。
- 发送与后续回复保存会话、消息、审批、平台回执和审计记录。

## 7. Kill Switch 与回执状态

状态机固定为：

```text
draft -> approved -> sending -> sent
                         |----> confirmation_required
                         |----> failed
                         |----> target_not_messageable
draft/approved/failed -> cancelled
```

`sending` 之后的未知结果永不自动重试。Kill Switch 只阻止新执行，不删除草稿、回执或审计记录。

## 8. Postiz 边界

项目内的 `PostizPublisher` 和“主动发布”页面只用于 X 自有账号的主动内容：

- 不参与 KOL/Trend 发现；
- 不用于第三方帖子评论；
- 不用于私信；
- 当前明确拒绝 `platform=xiaohongshu`。

### 环境与账号同步

```bash
POSTIZ_API_URL=https://api.postiz.com/public/v1
POSTIZ_API_KEY=<仅写入本地 .env>
POSTIZ_INTEGRATION_CACHE_MINUTES=15
KOL_PUBLISHING_MEDIA_DIR=data/publishing_media
```

1. 在 Postiz Cloud 连接至少一个自有 X 账号。
2. 打开“主动发布”，点击“手动同步账号”。
3. 确认页面只列出 `identifier=x` 且未禁用的账号，并显示 handle、integration ID 和实际默认账号。
4. API Key 只允许来自环境变量；数据库、页面、审计事件和截图不得出现 Key。

### 草稿、审批与发布

1. 从 Trend 卡片点击“创建 X 帖子”，确认 Trend ID 和可编辑草稿已复制；再从“主动发布”独立创建一条草稿。
2. 创建两段线程，每段分别验证文字、本地图片或公开 HTTPS 图片 URL。PDF、私网/localhost URL、非 HTTPS URL、不支持的 MIME 和单张超过 10MB 必须在上传前被拒绝。
3. 选择实际 X integration、`now` 或 `schedule`、回复权限以及每帖 `Made with AI` 标记。排期输入统一为北京时间，记录中同时显示 UTC。
4. 保存后确认状态为 `draft`，Postiz 尚无媒体上传和创建请求；点击“逐条审批”后状态为 `approved`；再次明确点击“提交 Postiz”才允许写入。
5. 单帖、图片帖和线程分别检查 Postiz payload：`__type=x`、`who_can_reply_post`、`community=""`、`paid_partnership=false`、管理员选择的 `made_with_ai`，线程段按 `value` 顺序排列。
6. `now` 发布后保存 Postiz `postId`，通过 `GET /posts` 回读 `releaseURL` 并标记 `published`；来自 Trend 的帖子同时回写 `published_url`。
7. `schedule` 发布后验证 `scheduled -> paused -> scheduled`，最后按需删除并进入 `cancelled`；每个操作都必须有独立审计事件。

### 不确定结果与真实验收证据

- 创建请求超时、网络中断或 5xx 时进入 `confirmation_required`；按日期、integration 和首段内容查询 Postiz，禁止直接重试。
- 401/403、429、明确 4xx、媒体上传失败和失效 integration 进入 `failed`，编辑并重新审批前不得提交。
- 真实验收必须记录：北京时间与 UTC、管理员、integration ID/handle、本地记录 ID、Postiz `postId`、X `releaseURL`、X 回读正文、审计事件和截图。
- 真实测试只发布一条用户批准的测试内容。是否删除真实 X 帖子由用户决定；系统不会自动清理已发布内容。

Postiz 官方的平台/API 清单未列出小红书；其 MCP 也没有评论读取/回复工具。参考：

- <https://docs.postiz.com/public-api/posts/create>
- <https://docs.postiz.com/public-api/integrations/list>
- <https://docs.postiz.com/public-api/uploads/upload-file>
- <https://docs.postiz.com/mcp/introduction>

## 9. 官方或持牌数据商接入指南

长期替换 Reader 时必须完成以下步骤：

1. 确认合同允许的地区、账号/内容字段、存储期限、再分发和营销用途。
2. 完成 DPA、数据删除、审计、子处理方、SLA、限流及事故通知审查。
3. 让供应商实现相同 `PlatformReader` 契约，不让业务层依赖供应商私有字段。
4. 映射 `(platform, external_id)`、原始链接、发布时间、互动指标和采集时间；缺失字段必须显式为空。
5. 在供应商沙盒完成空结果、分页、限流、过期凭证和数据删除测试。
6. 与浏览器 Reader 双读至少一个验收周期，对比覆盖率、时效、重复率和字段偏差。
7. 先按平台/查询灰度切流；保留 feature flag 和回滚，不覆盖已有原始回执。

小红书公开资料当前主要覆盖电商开放接口与内容分享 SDK，不能据此假设具有公开搜索、第三方评论或私信权限：

- <https://school.xiaohongshu.com/en/open/index.html>
- <https://agora.xiaohongshu.com/doc>

## 10. 本轮结果记录

| 项目 | 状态 | 时间 | 证据/说明 |
|---|---|---|---|
| 单元与 Web 回归测试 | 通过 | 2026-07-20 23:55 CST | `61 passed`；覆盖平台映射、迁移、窗口回退、审批、幂等、配额、Kill Switch、Postiz payload、媒体延迟上传、排期状态机和 Web 路由 |
| Postiz X 主动发布 UI | 通过（无真实写入） | 2026-07-20 23:55 CST | 本地浏览器验证“主动发布”导航、Key 缺失提示、账号空态、线程段动态增删/重排和北京时间排期必填；页面无自身前端错误 |
| Postiz X 真实发布 | 通过 | 2026-07-21 00:26 CST | 本地记录 `owned_post #1` 使用默认 integration `@coffe_cat__`（ID `cmrtfjo0b05z4qj0yiv5eoogf`）发布英文 Loop agent harness 文案和 imagegen 配图；首版被 Postiz 明确以 400/超长拒绝，编辑并重新审批后成功，Postiz ID `cmrtfsut1061bqj0yosd8f7l3`，X 回执 <https://x.com/coffe_cat__/status/2079241589977997630>。OpenCLI 回读确认作者、完整正文、`has_media=true` 和真实 X 图片 URL一致；本地图片 `data/publishing_media/loop-agent-harness-20260721.png` |
| X RWA KOL | 通过 | 2026-07-20 00:37–00:40 CST | 最终真实 Run `#16`；账号别名 `ddd` / 登录账号 `coffe_cat__`；150 条原始内容中有 77 条相关内容位于 24 小时窗口，返回 10/10 KOL；截图 `output/validation/run-16-kol.png` |
| 小红书 RWA KOL | 通过（使用回退窗口） | 2026-07-20 00:37–00:40 CST | 同一 Run `#16`；账号别名 `ddd` / 登录账号 `punk`；108 条原始内容，按相关性过滤后在 30 天窗口取得 20 条内容，返回 10/10 KOL；未补 Mock 或无关候选 |
| 双平台 Trend | 通过 | 2026-07-20 00:32 CST | 真实采集生成 cluster `19,20,21,22,23,24,17,26`；页面按四方向和两平台显示 8 簇。X 各方向 20 条/24 小时；小红书 AI 20、金融 12、加密/RWA 1、科技 3，均回退至 30 天；截图 `output/validation/trend-20260719.png` |
| Trend 非阻塞与缓存 | 通过 | 2026-07-20 00:24–00:32 CST | 强制刷新请求约 1.4 秒返回并显示“刷新任务已排队”；后台完成后采集时间更新；普通请求只显示最近一次刷新的 8 个簇 |
| X 沙盒评论 | 通过 | 2026-07-20 00:49 CST | 用户确认自有目标 `@punk2sang`；Action `#1` 由授权账号 `@coffe_cat__` 精确回复推文 `2078532652957921605`，回执为 <https://x.com/coffe_cat__/status/2078884960824647768>；Thread 回读确认作者、全文及 `in_reply_to` 一致 |
| 小红书沙盒评论 | 未实发（安全阻断） | 2026-07-20 | 未提供双方自有沙盒笔记，且真实写入开关关闭；可见浏览器执行器已实现 |
| X 沙盒私信 | `target_not_messageable` | 2026-07-20 01:02 CST | 用户完成 X Chat Passcode 初始化后，Action `#2` 精确进入 `@punk2sang` 会话；平台明确提示仅认证账号可向未关注自己的用户发送私信请求。`@coffe_cat__` 未认证且目标未关注发送方，因此消息未填写、未发送；未升级 Premium、换号或绕过限制 |
| 小红书沙盒私信 | 未实发（安全阻断） | 2026-07-20 | 未提供允许接收的自有沙盒目标；精确目标/会话执行器、审批和回执模型已实现 |
| 推广操作台 | 通过 | 2026-07-20 00:34 CST | 内置浏览器确认账号注册、评论/私信草稿、审批、执行、DNC 与全局开关表单可见；页面明确提示真实写入关闭；截图 `output/validation/operations-live-write-off.png` |
| Kill Switch | 通过（自动化） | 2026-07-20 00:34 CST | 单元与 Web 测试确认开启后阻止执行并记录控制状态；由于没有沙盒 action，未伪造平台实发回执 |

真实写入只允许面向双方自有沙盒目标。完成验证后应恢复 `KOL_LIVE_WRITE_ENABLED=false`。
