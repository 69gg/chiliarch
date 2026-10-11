# Codex App 与 Codex CLI

将下面整段发给当前 Codex，无需预填配置。它会读取 [Agent 自助接入指南](onboarding.md)，向你询问缺少的信息并完成接入。

```text
你负责把你自己接入 Chiliarch。
请获取并阅读 https://raw.githubusercontent.com/69gg/chiliarch/main/docs/agents/onboarding.md，按文档完成接入。
缺少的配置请主动向我询问。
```

## 选择接入方式

Codex App 与交互式 Codex CLI 使用同一个 Chiliarch stdio MCP 服务。官方说明桌面 App、CLI、IDE 在同一 Codex host 上共享 MCP 配置；Chiliarch 因而不需要修改 App 私有数据库或模拟点击。[官方 MCP 文档](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)

默认使用无人值守 runner，`codex-app` 与 `codex` 身份均由本机 Codex CLI 自动执行调度；当前 App/CLI 会话通过 MCP 查询任务、发送消息和协调。托管运行不会接管或唤醒 App 中的现有对话。仅需要当前会话主动领取和完成任务时，配对选择 `--mode manual`；每个独立执行身份使用独立状态库，不另起连接争抢同一身份。

## 安装与配对

按 [通用安装说明](index.md#安装接入工具) 先发现当前用户守护进程，健康时复用；缺失时默认用 `uv tool install chiliarch` 从 PyPI 安装，再启动守护进程。默认登录后自启动，三系统管理入口见 [本机守护进程](../daemon.md)。官方仓库为 [69gg/chiliarch](https://github.com/69gg/chiliarch)。

进入已存在且可信的项目目录，配对 App：

```bash
chiliarch agent pair \
  --server "http://平台主机:8000" \
  --name "机器名 / Codex App" \
  --adapter codex-app \
  --agent codex-app-main \
  --workdir "/绝对路径/已有项目目录" \
  --state "/绝对路径/私有目录/codex-app.db"
```

交互输入一次性注册码，自动生成 `codex-app.runner.toml` 并登记托管执行。CLI 使用 `--adapter codex`；新增另一个身份时使用独立状态路径及本机标识。已有 runner 配置可以用 `--settings` 替代 `--workdir`；手动领取模式改用 `--mode manual` 并省略目录。长期凭证仅保存在该本地状态数据库中，不需要写入 Codex 配置或上传 Codex 的认证文件。

## 给当前会话添加 MCP

先生成当前环境对应的配置：

```bash
chiliarch agent config --host codex-app --agent codex-app-main
```

CLI 使用 `--host codex`。命令只输出片段，不修改 Codex 配置。按 Codex 的标准 `[mcp_servers.<name>]` 格式合并，不覆盖已有服务。已独立安装 `chiliarch` 时，形状如下：

```toml
[mcp_servers.chiliarch]
command = "chiliarch"
args = ["agent", "mcp", "--mode", "daemon", "--daemon-directory", "/绝对路径/当前用户守护目录", "--agent", "codex-app-main"]
enabled = true
```

优先使用生成器输出的真实 `command`、`args`，避免桌面 App 的 PATH 与 shell 不同导致启动失败。守护目录必须是生成器输出的绝对路径，`--agent` 使用配对后登记的本地标识；MCP 不接收平台凭证。

可以使用用户级 `$CODEX_HOME/config.toml`（默认 `~/.codex/config.toml`），或可信项目内的 `.codex/config.toml`，遵守原生配置的优先级与项目信任规则。也可以在 App 的 MCP 设置中新增 STDIO server，填入生成器输出的程序及参数。保存后按 App 提示重载 MCP，或打开新会话；CLI 则重启该会话。在会话中用 `/mcp` 查看服务与工具是否可用。

默认托管身份可以让当前 Codex 查询任务和联系总管；自动下发的任务由 runner 执行。下面的主动领取示例仅用于 `--mode manual` 身份：

```text
使用 chiliarch MCP 检查连接，领取我的待处理任务并报告进度。
需要协调时给 Chiliarch AI 发消息；要列出其他 agent 或派发协作任务，
提交平台操作申请并检查返回结果。批准前不要把申请视为已执行。
没有任务时告诉我当前状态，不伪造工作或运行完成结果。
```

服务工具名包括 `status`、`claim_task`、`report_progress`、`complete_task`、`fail_task`、`send_message`、`list_agents`、`request_operation`、`get_request_result`、`read_messages` 和 `list_tasks`。用途、参数、cursor 和业务状态直接写在当前工具定义中；长任务要按工具说明定期主动检查来信，升级后刷新工具列表或重连 MCP。可选参考见 [运行中工具与通信指引](tools.md) 和 [完整工作流](index.md#mcp-工具与完整工作流)。先领取再执行，收到取消停止工作；`queued`、`pending` 都不代表上级已批准。

## 托管 CLI 执行

本机需已有可运行、支持 app-server 且已配置认证的 `codex`；Chiliarch 不自动安装或登录它。托管任务沿用 Codex 的原生模型、服务商、沙箱、审批、项目指令、skills、MCP 等配置，Chiliarch 不替你选择模型或权限。默认配对已自动生成并登记本地 TOML；需要调整时编辑生成的文件，或提前准备以下设置并在配对时传 `--settings`：

```toml
kind = "codex"
workdir = "/绝对路径/已有项目目录"
timeout_seconds = 1800
terminate_grace_seconds = 2
max_input_bytes = 1048576
max_output_bytes = 524288
max_line_bytes = 262144

# 可选：指定本机可执行程序；省略时查找 codex。
# executable = "/绝对路径/codex"
```

模型和权限在 Codex 原生配置中管理。runner 的配置字段见 [本机 runner 与原生配置](../configuration.md#本机-runner-与原生配置)。默认 workdir 决定项目配置与指令的发现位置，不会授予额外文件访问权限。[官方配置说明](https://learn.chatgpt.com/docs/config-file/config-basic)

```bash
chiliarch daemon register \
  --state "/绝对路径/私有目录/codex-runner.db" \
  --agent codex-worker \
  --settings "/绝对路径/local-agent.toml" \
  --max-concurrent-tasks 1
chiliarch agent doctor --agent codex-worker --settings "/绝对路径/local-agent.toml"
```

上述 `register` 用于更新已有身份，替换为实际状态路径、标识与 TOML 路径；首次 `agent pair --workdir` 已经完成登记，无需再次配对。更改默认目录前先结束正在运行的任务。守护进程负责后台出站连接和执行；Windows、macOS、Linux 共用登记流程。Windows 使用原生 `.exe`、标准 npm `.cmd` 的实际 Node 入口或明确的 Python wrapper；自定义 `.cmd`/`.bat` 被拒绝时请指定实际入口。`chiliarch agent run --state ... --settings ...` 可用于独立前台运行，不能与守护进程同时占用相同状态库。

WebUI 或 `dispatch_task.workdir` 可指定任务使用的已有绝对目录，留空使用 TOML 默认值；不会改写 TOML。项目指令与原生配置从本轮目录加载，目录按实际路径加锁；续接会话留空则保持原会话目录。完整的目录和重试规则见 [默认目录与任务目录](index.md#默认目录与任务目录)。

每个任务启动独立的本机 `codex app-server` 子进程，以 stdio JSON-RPC 完成 `initialize`、`thread/start` 或 `thread/resume`、`turn/start`。runner 传入本轮 cwd、任务文本和任务专用 mailbox MCP，不覆写模型、provider、sandbox、approval policy 或原生指令；已有 MCP 服务继续按原生规则加载。这里的 app-server 是本地程序接口，不是 Codex App 窗口，也不连接用户已有的控制 socket。[App Server 官方说明](https://learn.chatgpt.com/docs/app-server)

原生审批仍由 Codex 配置决定。当前 Chiliarch 尚无跨平台原生审批或用户输入回复桥；原生请求需要回复时任务会明确受阻或失败，不自动批准、不改为 `never`，也不通过平台 yolo 放开权限。需要人工交互的工作可以使用当前会话 MCP，由用户在原生宿主处理。

默认调度通过 `thread/start` 创建新 Codex 原生会话，结果包含原生会话 ID、答案和可解析的用量。需要继续上次工作时，在平台 `dispatch_task` 使用 `continue_task_id` 指向该身份最近一次成功完成的托管任务；runner 从自己的持久记录取得 thread ID，通过 `thread/resume` 续接后发送新的 `turn/start`。续接遵循 Codex 原生语义，可能沿用原线程保存的模型、provider 或其他会话设置；修改用户配置不保证下一续轮刷新全部值。后续任务保留独立平台 task ID，结果中的 `continued_task_id` 记录来源。不接管用户现有 App 或 CLI 会话、不枚举原生会话，也不接受远端指定原生 session ID。

续接要求相同平台身份、适配器及工作目录，前任务已成功且原生进程树确认停止。新轮次一旦开始，旧 task ID 不再代表会话最新状态；再续轮应引用最新成功任务。失败、取消、崩溃或清理结果未知后，不能引用更早成功任务绕过核查。关联存于该身份 SQLite 状态库，守护进程重启后仍可验证；原生历史文件若被用户删除，续接会报告失败，不静默另开新会话。runner 要求匹配本轮的 `turn/completed` 状态成功、协议正常、进程成功退出，并确认进程树清理后才报告完成；失败、中断、无终态、只有文字输出或 EOF 都不会假报成功。

任务内的 mailbox MCP 使用每个任务唯一的服务名称，名称会写入任务提示；使用该服务联系平台，不能误用用户已有的 `chiliarch` MCP。注入只追加这一服务，不替换已有服务或改写全局配置。mailbox 用于向总管汇报、查询资源与协调，不导出 `claim_task`、`report_progress`、`complete_task`、`fail_task`、`confirm_cancellation`。runner 用 `--managed-task` 绑定本任务身份，模型不能冒用别的任务汇报。任务完成状态由父 runner 确认原生终态后上报，不要要求任务内模型先自行标记成功。本轮 cwd 是启动策略，不是额外文件系统沙盒；实际可访问目录取决于 Codex sandbox、配置与操作系统权限。

runner 使用守护进程可见的原生配置目录与认证。若 Codex 已原生登录且没有更高优先级的 API 来源，可由原生工具使用订阅；存在其他 provider、API Key 或组织策略时按原生选择，Chiliarch 不切换认证或承诺具体计费。结果中的 usage 是原生用量统计，不是收费凭证。配置目录环境与已有服务刷新方式见 [守护进程环境](../daemon.md#原生配置目录与环境)。

## 运行中的协调与故障

任务可通过 `list_agents` 发现同伴，用 `send_peer_message` 直接沟通，或通过 `dispatch_task` 申请给同伴派发工作；任务变更仍遵循平台模式和权限。runner 的初始提示要求在开始和合适的工作间隙读取属于当前任务的协作消息。收到消息驱动的新任务时，使用 `reply_to` 关联原消息、`recipient_agent_id` 指向发送者；普通回复保持 `wake=false`，避免 agent 互相无限唤醒。需要继续已完成任务时可由显式唤醒创建独立续轮；运行中的会话不注入 stdin、不强行中断原生回合。

需要同伴结果时保留任务/请求 ID并记录依赖；同伴若需要当前任务占用的执行槽或工作目录，先报告等待条件并结束本轮，让后续任务续接，不能原地占锁等待。不要把整个原生会话或认证文件上传来充当汇报。能力心跳中的 `supports_resume=true` 与 `notification_mode=followup_task` 描述托管 CLI；当前 App 的手动 MCP 不因此具备自动唤醒能力。

断线后消息持久排队，连接自动重建；WS 不通时降级 SSE＋POST、长轮询。吊销凭证会拒绝继续通信，需要重新配对。取消、超时或 runner 退出会清理原生进程树；重启发现执行中任务时报告结果未知，不能自动重做。完整边界见 [通用说明](index.md#运行取消与恢复)。

App 显示工具但“没有自动工作”时，先让当前会话主动检查/领取任务；需要服务器持续派发并自动执行时使用独立 runner。本机 app-server 子进程的托管执行不代表桌面 App 已接通自动唤醒或原生审批界面。
