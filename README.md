# chiliarch

连接、管理与统一调度 agents 的平台。内置 Chiliarch AI 总管，提供手动管理、持久对话、工具调用和 ask / auto / yolo 审批模式。

所有连接都由 agent 主动出站发起，服务端永远不主动连接 agent。v0.3.2 提供平台框架、独立协议、跨平台共享守护进程、MCP 会话桥和本地 CLI 执行器。

## 支持的 agents

| Agent       | 当前会话接收调度、汇报与协调    | 无人值守执行                  | 接入文档                                  |
| ----------- | ------------------------------- | ----------------------------- | ----------------------------------------- |
| Codex App   | stdio MCP，当前会话主动领取任务 | 使用同机独立 Codex CLI runner | [Codex App / CLI](docs/agents/codex.md)   |
| Codex CLI   | stdio MCP                       | 本机 `codex app-server` stdio | [Codex App / CLI](docs/agents/codex.md)   |
| Claude Code | stdio MCP                       | `claude --print`＋JSONL       | [Claude Code](docs/agents/claude-code.md) |
| OpenCode v2 | stdio MCP                       | 本机 `opencode acp` stdio     | [OpenCode v2 / v1](docs/agents/opencode.md) |
| OpenCode v1（legacy） | stdio MCP              | 本机 legacy `opencode acp` stdio | [OpenCode v2 / v1](docs/agents/opencode.md) |

接入文档开头有可直接复制给 agent 的简短提示词，无需预填参数；agent 会通过 `raw.githubusercontent.com` 读取 [自助接入指南](docs/agents/onboarding.md)，主动询问缺少的配置并把自己接入平台。详见 [Agent 接入与协作](docs/agents/index.md)。MCP 会话接入支持领取任务、进度与结果、向总管提问、查询其他 agents 和申请协作调度；不会自动唤醒 Codex App 的空闲会话。本机共享守护进程先发现复用、再配对登记，默认当前用户登录后自启动；支持 Windows、macOS、Linux 的 MCP 与托管运行，CLI runner 默认创建新会话，也可明确续接 Chiliarch 创建的最近成功会话。Undefined 保留 [协议和扩展接口](docs/extensions.md)，尚未实现对应适配器。

托管执行沿用本机 Codex／Claude／OpenCode 的模型、服务商、认证、原生权限、项目指令、MCP 和其他原生配置；只为当前任务额外添加一个名称唯一的 Chiliarch mailbox MCP。runner TOML 负责程序、默认工作目录、超时和输入输出限额。Codex 的 app-server 是本地子进程，不控制桌面 App。当前尚无跨平台原生审批或用户输入回复桥；必须交互且无法继续时任务会明确受阻或失败，平台 ask/auto/yolo 不会改写原生权限。runner 配置字段见 [配置说明](docs/configuration.md#本机-runner-与原生配置)。

仓库地址为 [69gg/chiliarch](https://github.com/69gg/chiliarch)。

## 快速开始

支持 Python 3.11、3.12、3.13；开发默认使用 3.13。Python 由 [uv](https://docs.astral.sh/uv/) 管理，前端由 pnpm 管理。

默认从 PyPI 安装，前端构建产物随 Python wheel 和 sdist 发布，无需 Node 或 pnpm：

```bash
uv tool install chiliarch
chiliarch init
chiliarch serve
```

`init` 交互设置单用户登录密码并创建 `chiliarch.toml`；POSIX 权限为 0600，Windows 继承配置所在目录的 ACL，请保存在当前用户私有目录。配置保存支持三系统的原子替换与热重载。浏览器访问 `http://127.0.0.1:8000`。需要其他机器访问时，在配置中修改监听地址和公开地址，或使用 `serve --host <监听地址>`；HTTPS 可选。

停止平台或本机守护进程时，默认等待 HTTP/SSE 请求结束最多 15 秒，随后进入资源清理，避免长连接无限阻塞关闭。平台使用 `server.graceful_shutdown_seconds`，守护进程使用 `daemon.json` 中的同名字段；修改后重启生效。该期限不限制后续 agent 任务及进程树清理，详见 [部署文档](docs/deployment.md)。

v0.3.0 起，标准 uv 工具安装默认在平台或 agent 服务启动时检查并自动安装兼容的正式新版；离线或更新失败继续使用已有版本。已运行的守护进程和任务不会被自动重启。`CHILIARCH_AUTO_UPDATE=0` 可关闭检查与安装；旧版先手动 `uv tool upgrade chiliarch` 并重启一次。具体生效时机、版本约束与维护见 [自动升级](docs/upgrading.md)。

源码开发与本地构建见 [开发、测试与发版](docs/releasing.md)；用户部署见 [部署文档](docs/deployment.md)。

在线备份可运行 `chiliarch backup --output ./backups/platform.db`，命令校验并保存包含已提交 WAL 数据的独立 SQLite 快照，不覆盖已有文件。数据库存在待重启配置变更时，使用 `--database` 指定实际运行的数据库；配置、密钥环境和 agent 状态库需另行保管。详见 [备份与恢复](docs/deployment.md#备份与恢复)。

首次登录后，在“配置”页面设置模型 API、模型名称、地址和 API Key；也可通过环境变量提供密钥，例如 `CHILIARCH_MODEL__API_KEY`。未配置模型时，手动管理仍然可用，总管不会生成假回复。

## 使用总管对话

页头提供“新会话”入口，右侧会话列表默认收起，桌面鼠标移到右边缘自动显示，也可点击按钮固定展开；每条会话可重命名或删除。首次成功问答后由当前模型自动生成标题，手工标题不会被覆盖，命名失败不影响回答。用户消息靠右显示，Chiliarch AI 的正文、公开思考和工具调用按事件时间顺序穿插呈现，刷新和重连后保留原顺序，思考与工具详情可以折叠；需要许可的操作可在对应工具下方批准或拒绝。

底部输入区随内容增高：Enter 发送，Shift + Enter 换行，也支持 Ctrl / ⌘ + Enter；输入法确认候选词时不会提交。阅读历史时不抢走滚动位置，点击“回到最新”或发送消息后继续跟随输出。关闭页面后总管仍在后台运行。

模型输出长度默认由服务端决定；“配置”中可设置模型上下文窗口，默认 200k tokens，达到 90% 后自动压缩并继续工作。页面显示上下文占用与压缩状态，完整聊天和工具记录保留；工具轮次、消息条数和全局并行运行不再设固定上限。原生 Anthropic API 的必填输出参数及压缩机制见 [模型与总管行为](docs/models.md#上下文窗口与自动压缩)。

左侧导航支持收起，并记住当前浏览器的选择；配置表单在工作区内居中。主题按钮可切换跟随系统、浅色和深色；暗色采用炭灰背景、暖白文字及中性表面层级。抽屉、消息和折叠动画遵循系统“减少动态效果”设置。更多说明见 [模型与总管行为](docs/models.md)。

## 连接 agent

在 Agents 管理页面点击右上角“添加 agent”，创建一次性注册码，然后在 agent 所在机器执行（替换占位符）：

```bash
chiliarch daemon status
# 首次接入：chiliarch daemon install；已安装但未运行：chiliarch daemon start
chiliarch agent pair --server <平台HTTP或HTTPS地址> --name <agent名称> --adapter codex-app --agent codex-main --state /绝对路径/私有目录/agent.db --workdir /绝对路径/已有项目目录
chiliarch agent config --host codex-app --agent codex-main
chiliarch agent doctor --agent codex-main
```

默认无人值守：`agent pair` 校验默认目录、自动生成状态库旁的 `.runner.toml` 并登记 runner，收到调度后启动本机原生工具。省略 `--workdir` 时询问目录；已有设置可改用 `--settings`。Codex CLI 使用 `codex`，Claude Code 使用 `claude-code`，OpenCode v2 使用 `opencode`，v1 legacy 使用 `opencode-v1`；Codex App 的托管任务使用同机 Codex CLI。将生成的 MCP 配置合并到原生配置中后，当前会话可查询和协调。仅需手动领取时，配对改用 `--mode manual` 并省略目录参数。

Agents 管理、目标任务下发和重试表单均可填写“工作目录”；总管及下级调度使用 `dispatch_task.workdir`。填写 agent 机器上的已有绝对路径，留空使用该身份默认目录；同身份重试和会话续接默认保留原任务目录。目录选择遵循平台审批，并继续继承原生权限，不自动放宽沙箱或工具规则。默认目录的修改和 Windows 示例见 [目录配置](docs/agents/index.md#默认目录与任务目录)。

客户端复用原生 agent 的安装与登录，不上传原生认证文件；注册码交互输入，长期凭证保存在本地状态库。默认不覆盖已配对状态。`agent pair` 先确认本机守护进程健康，再配对并登记；多个 agent 保留独立身份与队列。MCP 配置只携带本机标识，不携带平台凭证。管理、自启动和独立运行方式见 [本机守护进程](docs/daemon.md)。

只想验证协议、不运行真实厂商 agent 时，可使用原有模拟客户端：

```bash
chiliarch pair --server <平台HTTP或HTTPS地址> --name <agent名称> --state ./agent.db
chiliarch demo-agent --state ./agent.db
```

注册码交互输入；长期凭证只保存在本地状态文件，不在终端显示。`demo-agent` 只模拟进度和结果，不运行命令、不操作真实厂商 agent。客户端优先 WS，其次 SSE＋HTTP POST，最后长轮询，支持持久去重、离线队列和重连。

接收或落盘较慢时，未确认消息按条退避重投，避免重复帧积压影响新任务、回复和进度；重连后继续使用原消息 ID 恢复投递。

## 目标与多 agent 协作

在“目标”页面创建需要持续推进的工作，记录约束和验收条件；也可以直接让 Chiliarch AI 创建目标并分工。每项任务可关联目标、前置任务和自己的验收要求。前置任务只有执行完成且验收通过后，后续任务才会投递；产物可登记为提交、PR、报告或其他引用，随依赖上下文交给后续 agent。

目标列表支持按名称、说明或任务内容搜索，按状态筛选，也可只看需要处理的目标。页面区分待验收、待返工、依赖等待、agent 离线、会话未领取、待请求和待审批；历史尝试不会误算成当前故障。重试弹窗可选择接手 agent，并保留执行前的核查说明。

任务完成、失败、中断，以及用户更新目标会进入持久协调队列，自动交给总管继续处理。任务相关来信回到原目标会话；会话正在运行或等待审批时，新事件排队等待，同一会话积压的协调事件会合并处理。总管的回复不会反过来反复唤醒自己。模型暂未配置时先保留事件；临时模型连接或限流故障按配置退避开启新的核查轮次，用户停止和不可恢复失败仍需明确继续。任务停滞或 agent 长期失联会通知总管核查，不把离线当作已停止。

执行成功与验收通过分别记录。需要返工时创建新的尝试并保留旧结果，也可选择另一个 agent 接手；交接携带原结果、评审、产物引用与恢复说明，保留原目标和依赖关系。仍在执行或取消未确认的任务不能直接转交。目标只能在有效任务均验收通过后完成。服务重启后先将最新中断的协调轮次与实际任务状态核对，不重放旧工具调用，也不越过后来的用户停止。agent 心跳区分等待领取、忙碌和工作区阻塞；相同实际目录的托管任务互斥执行。详细工具、恢复流程和保证边界见 [目标、依赖与验收](docs/coordination.md)。

## 同伴通信与共享材料

导航“协作”提供讨论组、私信、回执和“谁在等谁”。agent 可以直接发现同伴、交换消息并查询讨论，普通通信不需要总管逐条传话；ask 下状态变更仍需批准。消息区分已送达、已读和已处理，托管收件箱按任务隔离，稳定请求 ID 与消费游标支持重启恢复。

MCP 工具定义直接提供用途、参数、结果状态与下一步指引；读信、发送和等待工具会提醒 agent 在长任务期间定期检查来信，在开始、工作步骤之间、等待回复及结束前调用 `read_messages(after=null)`，空结果时退避。托管任务提示同步包含这一要求，模型无需依赖曾经读过的接入文档。它仍是模型主动调用工具，没有后台定时器将来信插入正在运行的模型回合。升级后，已打开的宿主可能需要刷新工具列表或重连 MCP 才能收到新说明。可选参考见 [运行中工具与通信指引](docs/agents/tools.md)。

明确设置 `wake=true` 的私信会为托管 agent 创建响应任务，继承目标与会话；普通回复默认不再唤醒，避免相互调用。`dispatch_task(continue_task_id=...)` 可安全续接 Codex／Claude／OpenCode 最近成功的受管会话；manual App 仍需主动读取 MCP 消息。协作等待会检查依赖环与执行资源冲突；相同工作目录仍保持互斥。

共享产物经已有出站通道分块上传、下载，带成员访问范围、不可变大小/哈希清单和落盘校验。文件内容不会作为总管工具结果展开；可将 `chiliarch:artifact:<ID>` 登记为任务交付引用。详见 [横向通信与协作](docs/collaboration.md) 和 [共享产物](docs/artifacts.md)。

## 总管模式

默认使用 `auto`：查询直接执行，状态变更按风险决定是否询问用户。新建配置及未填写 `supervisor.mode` 的配置采用此默认值；已有配置明确保存的 `ask`、`auto` 或 `yolo` 保持原样。

| 模式 | 查询 | 状态变更 |
|---|---|---|
| ask | 直接执行 | 每项请求用户批准 |
| auto | 直接执行 | 依据风险判断；高风险必须询问 |
| yolo | 直接执行 | 总管自主决定 |

下级 agent 的请求也遵循相同模式。总管不能批准自己的待审批操作；用户在管理页面直接提交的操作视为该次授权。身份验证、吊销、超时和本机执行边界在所有模式下生效。

## 开发与发版

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
pnpm --dir web typecheck
pnpm --dir web test
pnpm --dir web build
uv run python scripts/bump_version.py 0.3.2 --dry-run
uv run chiliarch schema --output src/chiliarch/protocol/schema.json
uv build
```

Python 测试默认使用 2 个独立进程并行运行；定位单项问题时可用 `uv run pytest -n 0` 串行执行。CI 保留 Linux、macOS、Windows 与 Python 3.11–3.13 的全部组合，并复用已经构建的 WebUI，详见 [验证与发版](docs/releasing.md)。

版本升级脚本同步 pyproject、uv.lock 和前端元数据；CHANGELOG 需要按最终用户行为单独撰写。`v*` tag 流水线验证、构建并通过 PyPI Trusted Publishing 发布，配置步骤见 [发版文档](docs/releasing.md)。

## 文档

- [部署、初始化与维护](docs/deployment.md)
- [启动检查、自动升级与关闭开关](docs/upgrading.md)
- [配置、热重载与日志](docs/configuration.md)
- [架构与保证边界](docs/architecture.md)
- [目标、任务依赖、产物与验收](docs/coordination.md)
- [同伴消息、讨论、唤醒与任务等待](docs/collaboration.md)
- [跨机器共享产物与访问范围](docs/artifacts.md)
- [本机共享守护进程、登录自启动与维护](docs/daemon.md)
- [agent 协议与 JSON Schema](docs/protocol.md)
- [Agent 接入、安装提示词与协作工作流](docs/agents/index.md)
- [供 agent 读取的自助接入指南](docs/agents/onboarding.md)
- [运行中工具与通信指引](docs/agents/tools.md)
- [Codex App / Codex CLI](docs/agents/codex.md) · [Claude Code](docs/agents/claude-code.md) · [OpenCode v2 / v1 legacy](docs/agents/opencode.md)
- [扩展操作与参考客户端](docs/extensions.md)
- [模型兼容与总管行为](docs/models.md)
- [开发、测试与发版](docs/releasing.md)
- [CHANGELOG](CHANGELOG.md) · [开发规范](AGENTS.md)

作者：Null · pylindex@qq.com。MIT License。
