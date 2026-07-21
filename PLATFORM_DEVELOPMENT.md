# Platform module development

新增平台采用代码内显式注册，不使用动态插件市场、Python entry points 或运行时包扫描。平台可以按能力渐进上线；核心调度器只安排已经注册了相应处理器的工作。

## 1. 模块边界

一个完整的 `PlatformPlugin` 可以提供以下扩展点：

```python
PlatformPlugin(
    manifest=...,
    handlers=...,
    native_models=...,
    automation_adapter=...,
    schema_installer=...,
    router_factory=...,
    health_check=...,
    close=...,
)
```

- `manifest`：稳定 ID、名称、版本、能力、默认扫描周期、安全限制和工作台路径。
- `handlers`：平台任务处理器；只注册已经实现的处理器。
- `native_models`：平台原生账号、内容、关系和指标类型。
- `automation_adapter`：将处理器结果投影到平台 Repository，并向核心提供机会和 KOL 视图。
- `schema_installer`：在核心提供的 SQLite connection 上安装平台 schema。
- `router_factory`：返回平台自己的 FastAPI `APIRouter`。
- `health_check`：读取连接健康；单个平台异常不会中断其他平台检查。
- `close`：释放延迟创建的客户端或 Repository。

平台模块应把读取、模型、Repository、评分、晋升补充规则和写入适配留在自己的包中。共享核心不应导入 `Channel`、`Tweet`、`Note` 等具体类型来分支。

## 2. 定义原生模型

账号和内容模型必须保留平台语义。例如视频平台可以定义：

```python
@dataclass(frozen=True)
class Channel:
    channel_id: str
    title: str
    subscriber_count: int


@dataclass(frozen=True)
class Video:
    video_id: str
    channel_id: str
    duration_seconds: int
    average_view_percentage: float
```

不要向 X Tweet 或小红书笔记表增加视频时长、完播率等字段，也不要把所有平台指标塞进无约束的全局 JSON。共享机会和动作只保存稳定原生引用：

```text
platform_id + native_object_type + native_object_id
```

同名账号属于不同 `platform_id` 时必须保持独立；当前系统不做跨平台人物身份合并。

## 3. 声明 Manifest 与渐进能力

使用 `PlatformManifest` 声明平台：

```python
manifest = PlatformManifest(
    platform_id="video_lab",
    name="Video Lab",
    version="1.0",
    capabilities=frozenset({
        PlatformCapability.ACCOUNT_SEARCH,
        PlatformCapability.CONTENT_SEARCH,
        PlatformCapability.ANALYTICS,
    }),
    default_scan_interval_seconds=30 * 60,
    safety_limits={"comment_daily": 5},
    workbench_path="/platforms/video-lab",
)
```

能力值包括：

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

只声明真实实现的能力。首个版本可以只有 `content_search` 和 `analytics`；没有 `comment`、`dm`、`owned_publish` 或 `media_upload` 时，策略不会生成对应动作，工作台也不会显示无效按钮。后续增加能力时同时补齐处理器、连接角色、安全额度和契约测试。当前内置的 X 与小红书都不声明 `media_upload`；能力枚举中存在该值并不代表内置平台已经支持上传。

Manifest 的能力与处理器承担不同职责：能力用于产品 UI、策略和连接配置；处理器决定核心是否可以实际调度某一类工作。两者都必须满足才应开放一条完整链路。

## 4. 共享 5 种任务与平台 5 个处理器

共享任务表只识别以下 `CoreJobType`：

| 核心任务 | 用途 | 平台边界 |
|---|---|---|
| `manual_discovery` | 用户发起的主题/账号/关系发现 | `discover` |
| `discovery_refresh` | 定时持续发现 | `discover` |
| `signal_refresh` | 增量内容、评论、趋势和指标扫描 | `scan_signals` → `build_opportunities` |
| `dispatch_actions` | claim 已排期动作并执行一次 | `execute_action` |
| `refresh_outcomes` | 只读回收不确定写入回执 | `refresh_outcomes` |

平台契约的 `PlatformTaskName` 也有 5 个值：

- `discover`
- `scan_signals`
- `build_opportunities`
- `execute_action`
- `refresh_outcomes`

不要把这两组名称混为一谈。`build_opportunities` 是平台扩展点，不是第六种核心任务；共享 `signal_refresh` 在 `scan_signals` 成功后，将扫描结果放入 `items` 并调用 `build_opportunities`。当前 X、小红书的构建处理器再调用各自 `automation_adapter.project_signals`。周期调度要求平台同时注册这两个处理器；缺少任一处理器都不会安排信号任务，也不应绕过该链路直接生成机会。

只读处理器返回 `PlatformTaskResult`；`execute_action` 返回最小写入回执 `ActionExecutionResult`：

```python
ActionExecutionResult(
    success=True,
    external_id="native-comment-id",
    receipt_url="https://...",
    raw_receipt={"provider": "..."},
    confirmed=True,
)
```

Registry 对一次 `dispatch` 只调用处理器一次。写入超时、未知状态或缺少可确认 ID 时返回未确认结果，使动作进入 `confirmation_required`；平台处理器和共享 Worker 都不得自动重试写操作。

`refresh_outcomes` 会收到该平台待确认动作，以及动作所属连接的只读快照。平台只在结果已经明确成功或明确失败时返回 `PlatformOutcomeUpdate`；仍无法确认时应返回 warning 而不是未确认 update。结果回收只能查询回执，不能再次调用写接口。

## 5. 实现 `automation_adapter` 契约

注册非空 Adapter 时，`PlatformPlugin` 会按 `PlatformAutomationAdapter` 运行时协议检查它，并校验 `adapter.platform_id == manifest.platform_id`。当前契约包含：

```python
class PlatformAutomationAdapter(Protocol):
    platform_id: str

    def project_discovery(
        self,
        items: Iterable[object],
        *,
        payload: Mapping[str, Any],
    ) -> PlatformProjectionResult: ...

    def project_signals(
        self,
        items: Iterable[object],
        *,
        payload: Mapping[str, Any],
    ) -> PlatformProjectionResult: ...

    def list_kols(
        self,
        *,
        status: str = "all",
        limit: int = 100,
    ) -> list[dict[str, Any]]: ...

    def get_kol(self, native_id: str) -> dict[str, Any] | None: ...

    def list_scan_targets(
        self,
        *,
        statuses: Iterable[str],
        after: str | None,
        limit: int,
    ) -> tuple[list[dict[str, Any]], str | None]: ...

    def summary(self) -> Mapping[str, int]: ...

    def set_kol_status(self, native_id: str, status: str) -> None: ...

    def add_seed(
        self,
        native_id: str,
        display_name: str | None = None,
    ) -> None: ...
```

各方法的职责：

- `project_discovery`：验证处理器返回的原生对象、持久化到平台表、记录发现证据并运行平台内晋升逻辑。
- `project_signals`：持久化增量内容和指标，生成 `PlatformOpportunityProposal`；机会必须包含 `NativeObjectReference`、0–100 优先级/分数、证据和评分原因。
- 晋升输入必须由平台原生模型实际提供 `protected`、`disabled` 和确定性的 `spam_risk`/异常证据；不能只依赖共享策略中的默认值，也不能让 AI 直接决定晋升资格。
- `list_kols` / `get_kol` / `summary`：向通用工作台、动作服务提供最小平台视图和单个原生账号查询，不暴露跨平台排名。
- `list_scan_targets`：按平台原生 ID 返回稳定轮转批次及下一游标；必须支持状态过滤、`after` 和 `limit`，使核心能用 `KOL_PLATFORM_ACCOUNT_BATCH_SIZE` 分批扫描而不固定配额。内容扫描还应返回每账号独立的单调高水位，不能用一个全局游标跳过其他账号的新内容。
- `set_kol_status`：支持 `seed / candidate / active / review / paused / rejected`。
- `add_seed`：规范化平台原生 ID 或 handle，确保平台原生账号记录存在，并以 `seed` 状态长期保存。它不是一次性 seed set 或固定扩散配额。

不得用作者名称哈希或本地摘要冒充平台原生账号 ID。如果内容快照缺少可解析的账号 ID，可以保留内容对象，但必须显式标记账号引用未解析，并将其排除在持续账号扫描、自动晋升和 DM 之外。

若平台只注册底层处理器而省略 Adapter，Registry 仍可直接 dispatch；但共享 KOL 工作台、种子入口和自动投影服务不可用。要接入完整产品工作流，应实现全部 Adapter 方法。

`PlatformProjectionResult` 中的 `native_counts`、`opportunities`、`warnings` 和 `metadata` 是平台到核心的稳定边界。动作建议通过 `PlatformActionProposal.payload` 传递平台特有参数，核心将其持久化并在执行时作为 `platform_payload` 原样交回平台；连接配置则通过独立的 `connection` 快照提供。发布桥、平台排期和回执字段都由平台处理器解释，不要把平台专用字段提升为共享 action 列。

## 6. 独立 Repository 与 schema

平台表使用清晰稳定的前缀，例如 `video_channels`、`video_videos` 或 `video_schema_migrations`。生产 `schema_installer(connection)` 应满足：

- 使用传入的同一个 `sqlite3.Connection`，不自行替换数据库。
- 可重复运行，并记录自己的 schema 版本。
- 只创建/迁移本平台表，不修改其他平台的原生表。
- 在失败时抛出异常，让 `db rebuild --backup` 恢复旧数据库。

核心启动和数据库重建会按显式注册顺序调用 `registry.install_schemas`。Repository 负责持久化与查询；平台评分、晋升和机会构建放在 Adapter/服务层。当前 X、小红书 Adapter 在一次 discovery/signal 投影中持有各自 Repository 的 `unit_of_work`，同一投影内的多次原生表写入会共同提交或回滚。这个 Unit of Work 只覆盖该平台 Repository；平台原生投影与随后写入共享 `automation_*` 机会表不是一个跨 Repository/跨数据库事务，新平台不得假设两侧天然具备全局原子性。

## 7. 注册 Router 与工作台

`router_factory` 必须返回 FastAPI `APIRouter`，并使用唯一平台前缀，例如：

```python
def router_factory() -> APIRouter:
    router = APIRouter(prefix="/platforms/video-lab", tags=["platform:video_lab"])

    @router.get("/module")
    def module_info() -> dict[str, object]:
        return manifest.as_dict()

    return router
```

Web lifespan 会按注册顺序挂载这些 Router。当前 X、小红书各自注册 `/module` 路由，并由 Manifest 选择自己的工作台模板；两者目前继承共享工作台骨架，但可以独立扩展。新平台可以先复用通用模板，也可以通过 `metadata.workspace_template` 和自己的 Router 增加平台专属页面。

工作台只根据 Manifest 展示发现来源和动作。若平台声明 `relations`，手动发现可以使用无关键词的种子关系扩展；任何平台都可以通过 Adapter 的 `add_seed` 接入长期种子。

## 8. 显式加入默认注册表

内置平台必须在 `build_default_registry` 中显式导入、构建并注册：

```python
registry = PlatformRegistry()
if "video_lab" in selected:
    registry.register(create_video_plugin(settings))
```

同时将平台 ID 加入允许的内置平台集合。平台 Repository 应从平台配置解析自己的数据库连接，不向插件工厂传入已移除的通用业务 Store。新增平台注册后，核心 Worker、schema 安装和平台中心无需新增按平台分支。不要引入 entry-point 自动发现。

## 9. 契约与验收测试

至少覆盖：

- Manifest ID、capability、handler 和 Adapter 校验。
- Channel/Video 等原生模型不依赖通用 Account/Post。
- schema 可重复安装，并且只创建平台表。
- `project_discovery` 持久化原生对象与 KOL 状态。
- `add_seed` 创建或更新长期平台种子。
- `project_signals` 生成可追溯的原生机会、评分原因和建议动作。
- 缺失 capability/handler 不生成动作或调度无效任务。
- Router 可以注册且不会要求修改核心路由分发。
- 单个平台健康检查或任务失败不影响其他平台。
- `execute_action` 只调用一次；不确定回执保持未确认。

仓库中的虚拟视频平台测试是接入验收基线：

- `tests/test_platform_kernel.py`：用独立 `Channel`/`Video` 模型验证显式注册、原生 schema、能力、健康隔离和单次写入。
- `tests/test_platform_automation_adapter_contract.py`：用独立视频表和 Adapter 验证发现投影、视频本地指标评分、KOL 查询/状态与 `add_seed` 契约。
- `tests/test_platform_automation_service.py`：让视频风格平台通过共享 `scan_signals` → `build_opportunities` 链路进入核心机会表，证明核心不需要 X/小红书分支或通用 Post 模型。
- `tests/test_platform_templates.py`：验证通用工作台按能力隐藏不支持的入口。

真实 YouTube 和 Instagram 不属于当前内置注册表；通过上述虚拟平台测试表示扩展点成立，不表示真实凭据、API 限制或写入能力已经接入。

## 10. 接入检查清单

1. 定义原生模型和 Repository。
2. 编写可重复的 schema installer 与平台迁移记录。
3. 声明最小真实 Manifest 能力和安全上限。
4. 只注册当前阶段需要的处理器。
5. 实现完整 `automation_adapter`，包括 `get_kol`、`list_scan_targets` 和 `add_seed`。
6. 注册健康检查、Router 与关闭钩子。
7. 在默认注册表中显式加入平台。
8. 复用或扩展工作台，并验证缺失能力隐藏。
9. 使用虚拟凭据/离线 fixture 跑完契约测试。
10. 最后才进行受控的真实只读验证；写入能力需单独验收。
