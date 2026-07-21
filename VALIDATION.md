# 多平台自动化重构验证指南

本文验证“共享自动化内核 + 平台原生模块 + 独立平台工作台”。默认验收使用 Mock、临时 SQLite 和测试内虚拟视频平台，不需要真实凭据或网络。真实写入必须另行批准，并只面向自有沙盒账号。

## 1. 自动化测试

```bash
source .venv/bin/activate
pytest
```

聚焦多平台契约时可以先运行：

```bash
pytest \
  tests/test_platform_kernel.py \
  tests/test_platform_automation_adapter_contract.py \
  tests/test_platform_automation_service.py \
  tests/test_platform_postiz_actions.py \
  tests/test_platform_postiz_service.py \
  tests/test_platform_web_cli.py \
  tests/test_platform_templates.py \
  tests/test_platform_signal_cursors.py \
  tests/test_platform_kol_lifecycle.py \
  tests/test_platform_browser_actions.py \
  tests/test_platform_native_discovery_risk.py \
  tests/test_content_intelligence.py \
  tests/test_automation_repository.py \
  tests/test_database_rebuild.py
```

完整测试应覆盖以下不变量：

- 共享队列只接受 `manual_discovery`、`discovery_refresh`、`signal_refresh`、`dispatch_actions`、`refresh_outcomes` 5 种核心任务。
- Worker 使用 `platform_id` 从显式注册表解析处理器，不根据 X/小红书名称选择旧 Pipeline。
- X 账号/Tweet 与小红书用户/笔记/评论写入不同原生表；同名账号不会合并。
- 虚拟视频平台以独立 Channel/Video 模型、Repository/schema、Adapter 和能力接入，不依赖通用 Account/Post。
- 虚拟视频信号可经过共享 `scan_signals` → `build_opportunities` 链路形成带原生 Video 引用的机会，不需要修改核心分发。
- 一个平台注册任务或健康检查失败不会阻断另一个平台的队列与健康结果。
- 缺失 capability 或 handler 不生成对应动作，也不创建无效的周期任务或 UI 入口。
- `add_seed` 在平台原生账号/KOL 表中保存长期 `seed`，不创建固定版本 seed set。
- 关系证据持久保存；候选在后续内容扫描满足阈值后仍可晋升。
- X 的 following、verified follower、回复、引用和提及均形成带原生 ID/URL 的候选关系证据；小红书 seed/Active 笔记的评论作者形成平台原生关系证据。
- X/小红书的受保护、已停用和高垃圾风险账号实际进入确定性晋升策略并被拒绝，不只是保留未接线的策略参数。
- 高置信度评论只有在真实写入与自动执行两个开关、额度、冷却、去重、内容规则和通道健康均通过时才会自动排队。
- 首次冷私信进入 `needs_review`；仅提交 `has_valid_conversation=true` 不能伪造已建立会话，历史成功出站私信也不会自动建立有效会话。后续私信必须同时匹配 `automation_conversations` 中平台、原生账号、原生会话 ID 和 `valid` 状态。
- 人工批准动作不依赖自动策略开关；真实写入关闭时动作只停留在 `scheduled`，外部执行仍受写入开关和执行前二次安全校验约束。
- 全局、平台、账号和动作类型四级 Kill Switch 均阻止新写入，不停止只读任务。
- 不确定回执进入 `confirmation_required`，失败或不确定写入不会因更换草稿而对同一目标自动重试。
- 进程中断后遗留的 `executing` 动作只会恢复为 `confirmation_required`。
- Worker 启动时，超过 15 分钟的遗留 `running` 任务会变为 `failed`，不会自动重新入队；其他平台任务仍可继续。
- 信号账号按原生 ID 稳定轮转，每账号内容游标独立、单调推进；乱序结果和旧内容不会让游标倒退，小红书旧笔记上的新评论仍可被扫描。
- 小红书搜索结果缺少原生作者 ID 时保留笔记但标记作者未解析；本地哈希不能进入 Feed 扫描、自动晋升或 DM，旧数据库中的同类哈希会由平台 migration 纠正。
- AI 缺失或失败时投影回退到离线路径；AI 输出不能绕过禁用词、链接、会话、额度、冷却、幂等、Kill Switch 或执行前复检。
- `db rebuild --backup` 在初始化核心和各平台注册 schema 前保留可恢复备份；平台 schema 失败时恢复原库及 WAL sidecar。

虚拟视频验收分布在三层：

| 测试 | 证明内容 |
|---|---|
| `test_platform_kernel.py` | 显式注册、Channel/Video 原生模型、schema、能力、失败隔离、写入只调用一次 |
| `test_platform_automation_adapter_contract.py` | 独立视频表、发现/信号投影、视频本地评分、KOL 状态与 `add_seed` |
| `test_platform_automation_service.py` | 视频平台贯穿共享任务和机会生命周期，核心没有 X/小红书分支 |

这组测试只证明平台扩展契约；它不表示真实 YouTube 或 Instagram API 已接入。

## 2. 本地 CLI 验收

在新的 shell 中使用临时数据库：

```bash
export KOL_DB_PATH="$(mktemp -d)/kol-search.db"
export TWITTER_BACKEND=mock
export KOL_ENABLE_MOCK_BACKEND=true
export KOL_ENABLED_PLATFORMS=x,xiaohongshu
export KOL_LIVE_WRITE_ENABLED=false
export KOL_AUTO_EXECUTION_ENABLED=false

kol-search platforms
kol-search discover "RWA research" --platform x --limit 20
kol-search scan --platform x
```

检查：

1. `platforms` 只列出代码内显式注册且由 `KOL_ENABLED_PLATFORMS` 启用的平台，并显示读取连接状态和 Manifest 能力。
2. `discover` 创建 `manual_discovery`，等待任务到达 `succeeded / failed / cancelled` 后退出并打印平台结果。
3. `scan --platform x` 默认等同于 `--wait`：显示进度，成功时打印结果，失败或取消时以非零状态退出。
4. 信号任务的 payload 包含平台扫描选项，结果保留平台游标和原生对象投影统计。
5. 连续运行超过一批账号时，`target_cursor` 稳定轮转，单批不超过 `KOL_PLATFORM_ACCOUNT_BATCH_SIZE`；返回的内容 `cursor` 是按账号保存的高水位映射。

再验证非等待模式：

```bash
kol-search scan --platform x --no-wait
```

该命令打印 `Queued ... signal job #...`，不轮询任务终态。它适合 Web 服务或其他常驻 Worker 正在消费同一数据库的场景；验收脚本若需要确定结果，应使用默认等待模式。

未知平台，或缺少 `scan_signals` / `build_opportunities` 任一处理器时，应得到明确错误，而不是回退到 X。

## 3. 平台中心、种子与 Router 验收

```bash
kol-search web
```

打开以下页面：

1. `/` 或 `/platforms` 是平台中心，只汇总数量、健康、任务和异常，不展示跨平台影响力总榜。
2. `/platforms/x` 与 `/platforms/xiaohongshu` 各自展示 KOL、机会、动作、能力、连接、回执和异常。
3. `/platforms/x/module` 与 `/platforms/xiaohongshu/module` 返回各平台 Router 提供的 Manifest JSON，证明 Router 由插件注册。
4. X 与小红书分别选择 `platform_x_workspace.html`、`platform_xiaohongshu_workspace.html`；两者继承共享骨架，但可以由自己的 Router/模板独立扩展。
5. `/settings/brand` 为注册表中的每个平台动态渲染一个账号字段，并保存到 `platform_handles` 映射；页面和存储层不得硬编码只有 X/小红书两个字段。

在 X 工作台执行：

1. 在“KOL 库”填写 `@seed_handle` 和可选显示名称，点击“添加长期种子”。
2. 页面应显示该平台原生账号，状态为 `seed`，评分不与其他平台合并。
3. 在“手动主题发现”中分别检查 Manifest 允许的来源：X 可选择账号、内容和种子关系扩展；关系扩展允许关键词为空。
4. 小红书当前只显示内容搜索，不显示账号搜索或关系扩展；仍可通过自己的种子表单添加原生用户 ID。
5. 当前 X 与小红书都不声明 `media_upload`，因此两者均不显示媒体上传入口；小红书还不显示自有发布。未来平台未声明的能力同样隐藏。

可用以下只读查询确认种子写入平台原生表：

```bash
sqlite3 "$KOL_DB_PATH" \
  "SELECT a.id, a.handle, k.status FROM x_accounts a JOIN x_kols k ON k.account_id=a.id;"

sqlite3 "$KOL_DB_PATH" \
  "SELECT u.id, u.nickname, k.status FROM xhs_users u JOIN xhs_kols k ON k.user_id=u.id;"
```

手动发现和扫描生成的共享任务必须带自己的 `platform_id`；平台工作台中的机会必须能追溯到原生对象类型、ID、URL或证据以及平台内评分原因。

## 4. 显式 schema 与数据库重建验收

先创建一个可识别的旧库，再重建：

```bash
export KOL_DB_PATH="$(mktemp -d)/rebuild.db"
sqlite3 "$KOL_DB_PATH" "CREATE TABLE before_rebuild(marker TEXT);"
kol-search db rebuild --backup
```

命令应打印新库路径和时间戳备份路径。立即检查新库：

```bash
sqlite3 "$KOL_DB_PATH" ".tables"
```

新 schema 至少包含：

- `automation_schema_migrations`
- `automation_platform_connections`
- `automation_jobs`
- `automation_opportunities`
- `automation_actions`
- `automation_policy_decisions`
- `automation_channel_controls`
- `automation_audit_events`
- `automation_brand_config`
- `automation_account_quotas`
- `automation_conversations`
- `x_schema_migrations`、`x_accounts`、`x_kols`、`x_tweets`、`x_tweet_metrics`、`x_discovery_evidence`、`x_trends`
- `xhs_schema_migrations`、`xhs_users`、`xhs_kols`、`xhs_notes`、`xhs_note_metrics`、`xhs_comments`、`xhs_discovery_evidence`、`xhs_trends`

检查备份仍含 `before_rebuild`，并可由 SQLite 打开。失败注入测试应证明任何平台注册 schema 抛错时：

- 原数据库与其 `-wal` / `-shm` sidecar 被恢复。
- 失败的新数据库及 sidecar 以 `.failed-<timestamp>` 单独保留。
- 已成功安装的平台不会掩盖后续平台的 schema 错误。

## 5. 策略边界验收

先保持真实写入关闭，创建托管账号、机会和动作，验证：

| 场景 | 预期结果 |
|---|---|
| 评论分数 79 | `needs_review` |
| 评论分数 80，含链接 | `rejected` |
| 评论分数 80，命中禁用词 | `rejected` |
| 首次冷私信分数 100 | `needs_review` |
| 仅由请求声称存在有效会话 | 仍为冷私信，`needs_review` |
| 私信含非批准域名 | `rejected` |
| `automation_conversations` 中存在账号绑定、证据和 `valid` 状态的后续私信分数 80 | 真实写入与自动执行两个开关开启时可 `scheduled` |
| 只有历史成功出站私信，没有已验证会话记录 | 仍为冷私信，`needs_review` |
| 达到小时/每日额度 | `rejected` |
| 同一评论/发布目标已有动作 | 不创建第二个自动写入 |
| 同一目标前次写入失败或未确认，草稿发生变化 | 仍不自动重试 |
| 作者仍在冷却期 | `rejected` |
| 机会已过期 | 执行前拒绝，不调用平台 |
| 任一级 Kill Switch 暂停 | 不 claim 动作 |

安全规则在动作规划时和外部写入紧前各执行一次。人工修改最终文案后，第二次校验必须使用修改后的文本。平台建议动作的 payload 应保存在 `automation_actions.payload_json`，执行时以 `platform_payload` 交回原平台。

## 6. 写入与回执验收

### X Postiz 发布

Postiz 只用于 Manifest 声明发布桥接的 X 纯文本自有内容。先在 X 工作台登记 core-native Postiz integration 连接。当前 X 不声明 `media_upload`，验收不得出现文件上传控件或媒体上传请求。默认每日最多 2 条；在 `KOL_PUBLISH_WINDOWS` 之外执行动作时，X 模块应排到下一窗口，而不是立即发布。

真实沙盒验证前必须满足：

```bash
KOL_LIVE_WRITE_ENABLED=true
KOL_AUTO_EXECUTION_ENABLED=true
POSTIZ_API_KEY=<local-only>
```

验证记录需包含本地 action ID、integration ID、排期 UTC、Postiz external ID、回执 URL 和审计事件。新链路不得创建已移除的 `owned_posts` 记录；策略或额度拒绝时不应先调用 Postiz。请求超时、5xx 或缺少可确认 ID 时只允许结果回收，不允许再次创建。结果回收优先使用 external ID；无 external ID 时，只有 integration ID、完整内容、动作时间前后 15 分钟都匹配且候选唯一才可确认。历史同文案、缺少 provider 时间戳或同窗口存在多条候选时必须保持 `confirmation_required`。`tests/test_platform_postiz_service.py` 还应证明完整计划、单次写入和回执回收不会创建已移除的业务 schema。

### 浏览器评论与私信

浏览器评论/私信验收必须证明不可逆发送按钮最多点击一次。点击调用超时或报错后不得尝试备用发送 selector；这类结果进入 `confirmation_required`。私信/评论 DOM 回读只有在平台定义的精确消息/评论元素相对发送前快照新增最终文本、且编辑器已清空该文本时才算确认；正文、旧同文消息或仍停留在编辑器里的草稿都不算回执。浏览器链路未取得平台原生 ID 时必须保持 `external_id` 为空，不能用本地摘要冒充平台回执。

## 7. 失败隔离与渐进平台验收

构造两个测试插件：一个健康检查或 `scan_signals` 抛错，另一个正常返回。确认：

- 两个平台都通过同一个显式 Registry 注册。
- 失败任务只把对应平台读取连接标记为 `degraded`；部分成功任务保留 warning 并同样把该读取连接标为 `degraded`，首页显示告警。
- Worker 下一轮仍能 claim 并完成另一平台任务。
- 只读平台没有写能力时，调度器不创建 `dispatch_actions`，工作台也不出现评论、私信或发布按钮。
- 只有 `discover` 的早期平台不会被安排信号、动作或结果回收任务；只有 `scan_signals` 而没有 `build_opportunities` 的平台也不会被安排信号任务。
- 后续加入 `scan_signals` 或写能力时不需要修改核心状态机或任务分发。

## 8. 真实连接验收原则

- 运行时禁止启用 Mock 作为真实证据。
- 每个平台使用独立凭据或 Browser Bridge profile，只保存别名，不保存密码/Cookie。
- 每项证据记录平台、原生对象 URL、采集/执行时间、任务或动作 ID、回执和截图。
- 平台登录异常、验证码、限流或警告不得绕过；暂停该账号通道后，其他平台和只读任务继续。
- 任何真实写入只面向双方自有沙盒目标，并在测试结束后恢复 `KOL_LIVE_WRITE_ENABLED=false`。

## 9. 已移除旧流程的验收

- `/tasks`、`/runs`、`/library`、`/contacts`、`/radar/*`、`/publishing` 和 `/operations` 不再注册，访问应返回 `404`。
- 联系人网页爬取、联系人审核/导出、固定 seed build/expand、旧 Pipeline、旧 Worker、直发回复和 `OutboundService` 已从运行代码删除。
- `db rebuild --backup`、正常 Web 启动以及新 CLI 都只初始化共享自动化表和已注册平台 schema，不会创建 `runs / contacts / seed_sets / owned_posts`。
- X、小红书各自注册 Router、原生 schema 和工作台模板；平台模板继承同一基础骨架，以保留一致的共享交互。
- `build_opportunities` 属于平台处理器契约，但不是第六种核心任务；核心 `signal_refresh` 明确执行 `scan_signals` → `build_opportunities`，内置构建处理器再调用 Adapter 投影。
- 当前 X/小红书 Adapter 用平台 Repository 的 `unit_of_work` 保证一次原生投影内共同提交/回滚；平台原生表与共享 `automation_*` 表之间仍没有跨 Repository 的全局事务。
