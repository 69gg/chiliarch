# chiliarch

连接、管理与统一调度 agents 的平台。内置 chiliarch AI 总管，提供手动管理、持久对话、工具调用和 ask / auto / yolo 审批模式。

所有连接都由 agent 主动出站发起，服务端永远不主动连接 agent。v0.1.0 提供平台框架、独立协议、跨平台共享守护进程、MCP 会话桥和本地 CLI 执行器。

## 支持的 agents

| Agent       | 当前会话接收调度、汇报与协调    | 无人值守执行                  | 接入文档                                  |
| ----------- | ------------------------------- | ----------------------------- | ----------------------------------------- |
| Codex App   | stdio MCP，当前会话主动领取任务 | 使用同机独立 Codex CLI runner | [Codex App / CLI](docs/agents/codex.md)   |
| Codex CLI   | stdio MCP                       | `codex exec --json`           | [Codex App / CLI](docs/agents/codex.md)   |
| Claude Code | stdio MCP                       | `claude --print`＋JSONL       | [Claude Code](docs/agents/claude-code.md) |

接入文档开头有可直接复制给 agent 的简短提示词，无需预填参数；agent 会通过 `raw.githubusercontent.com` 读取 [自助接入指南](docs/agents/onboarding.md)，主动询问缺少的配置并把自己接入平台。详见 [Agent 接入与协作](docs/agents/index.md)。MCP 会话接入支持领取任务、进度与结果、向总管提问、查询其他 agents 和申请协作调度；不会自动唤醒 Codex App 的空闲会话。本机共享守护进程先发现复用、再配对登记，默认当前用户登录后自启动；支持 Windows、macOS、Linux 的 MCP 与托管运行，CLI runner 每次调度创建新的原生会话。OpenCode、Undefined 保留 [协议和扩展接口](docs/extensions.md)，尚未实现对应适配器。

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
chiliarch agent pair --server <平台HTTP或HTTPS地址> --name <agent名称> --adapter codex-app --agent codex-main --state /绝对路径/私有目录/agent.db
chiliarch agent config --host codex-app --agent codex-main
chiliarch agent doctor --agent codex-main
```

将生成的 MCP 配置合并到原生 agent 配置中；Codex CLI 使用 `codex`，Claude Code 使用 `claude-code`。当前会话通过 MCP 主动领取任务，自动执行则按接入文档配置本地 runner。客户端复用原生 agent 的安装与登录，不上传原生认证文件；注册码交互输入，长期凭证保存在本地状态库。默认不覆盖已配对状态。`agent pair` 先确认本机守护进程健康，再配对并登记；多个 agent 保留独立身份与队列。MCP 配置只携带本机标识，不携带平台凭证。管理、自启动及旧版 standalone 迁移见 [本机守护进程](docs/daemon.md)。

只想验证协议、不运行真实厂商 agent 时，可使用原有模拟客户端：

```bash
chiliarch pair --server <平台HTTP或HTTPS地址> --name <agent名称> --state ./agent.db
chiliarch demo-agent --state ./agent.db
```

注册码交互输入；长期凭证只保存在本地状态文件，不在终端显示。`demo-agent` 只模拟进度和结果，不运行命令、不操作真实厂商 agent。客户端优先 WS，其次 SSE＋HTTP POST，最后长轮询，支持持久去重、离线队列和重连。

## 目标与多 agent 协作

在“目标”页面创建需要持续推进的工作，记录约束和验收条件；也可以直接让 Chiliarch AI 创建目标并分工。每项任务可关联目标、前置任务和自己的验收要求。前置任务只有执行完成且验收通过后，后续任务才会投递；产物可登记为提交、PR、报告或其他引用，随依赖上下文交给后续 agent。

任务完成、失败、中断，以及用户更新目标会进入持久协调队列，自动交给总管继续处理。任务相关来信回到原目标会话；会话正在运行或等待审批时，新事件排队等待，同一会话积压的协调事件会合并处理。总管的回复不会反过来反复唤醒自己。模型暂未配置时先保留事件；用户停止或模型失败后暂停自动唤醒，之后发送新消息或在目标页点击“继续协调”恢复。

执行成功与验收通过分别记录。需要返工时创建新的尝试并保留旧结果，也可选择另一个 agent 接手；交接携带原结果、评审、产物引用与恢复说明，保留原目标和依赖关系。仍在执行或取消未确认的任务不能直接转交。目标只能在有效任务均验收通过后完成。服务重启后先将最新中断的协调轮次与实际任务状态核对，不重放旧工具调用，也不越过后来的用户停止。agent 心跳区分等待领取、忙碌和工作区阻塞；相同实际目录的托管任务互斥执行。详细工具、恢复流程和保证边界见 [目标、依赖与验收](docs/coordination.md)。

## 总管模式

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
uv run python scripts/bump_version.py 0.2.0 --dry-run
uv run chiliarch schema --output src/chiliarch/protocol/schema.json
uv build
```

版本升级脚本同步 pyproject、uv.lock 和前端元数据；CHANGELOG 需要按最终用户行为单独撰写。`v*` tag 流水线验证、构建并通过 PyPI Trusted Publishing 发布，配置步骤见 [发版文档](docs/releasing.md)。

## 文档

- [部署、初始化与维护](docs/deployment.md)
- [配置、热重载与日志](docs/configuration.md)
- [架构与保证边界](docs/architecture.md)
- [目标、任务依赖、产物与验收](docs/coordination.md)
- [本机共享守护进程、登录自启动与维护](docs/daemon.md)
- [agent 协议与 JSON Schema](docs/protocol.md)
- [Agent 接入、安装提示词与协作工作流](docs/agents/index.md)
- [供 agent 读取的自助接入指南](docs/agents/onboarding.md)
- [Codex App / Codex CLI](docs/agents/codex.md) · [Claude Code](docs/agents/claude-code.md)
- [扩展操作与参考客户端](docs/extensions.md)
- [模型兼容与总管行为](docs/models.md)
- [开发、测试与发版](docs/releasing.md)
- [CHANGELOG](CHANGELOG.md) · [开发规范](AGENTS.md)

作者：Null · pylindex@qq.com。MIT License。
