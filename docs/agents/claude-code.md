# Claude Code

将下面整段发给当前 Claude Code，无需预填配置。它会读取 [Agent 自助接入指南](onboarding.md)，向你询问缺少的信息并完成接入。

```text
你负责把你自己接入 Chiliarch。
请获取并阅读 https://raw.githubusercontent.com/69gg/chiliarch/main/docs/agents/onboarding.md，按文档完成接入。
缺少的配置请主动向我询问。
```

## 安装与身份

按 [通用安装说明](index.md#安装接入工具) 先发现当前用户守护进程，健康时复用；缺失时默认用 `uv tool install chiliarch` 从 PyPI 安装，再启动守护进程。默认登录后自启动，三系统管理入口见 [本机守护进程](../daemon.md)。官方仓库为 [69gg/chiliarch](https://github.com/69gg/chiliarch)。

Chiliarch 复用本机 `claude` 与其原生认证配置，不读取或上传认证文件，不负责订阅、模型服务或原生安装。默认托管 runner 自动接收并执行任务，当前 Claude Code 会话通过同一 daemon 身份的 MCP 查询和协调；仅手动领取时选择 `--mode manual`。不要另起客户端连接同时占用同一状态库。

```bash
chiliarch agent pair \
  --server "http://平台主机:8000" \
  --name "机器名 / Claude Code" \
  --adapter claude-code \
  --agent claude-main \
  --workdir "/绝对路径/已有项目目录" \
  --state "/绝对路径/私有目录/claude-agent.db"
```

交互输入一次性注册码；长期凭证存入状态库，自动生成 `claude-agent.runner.toml` 并登记无人值守执行。省略 `--workdir` 时询问默认目录；已有 TOML 可改用 `--settings`。仅手动领取时指定 `--mode manual` 并省略目录参数。保密、备份、吊销和重新配对约定见 [通用说明](index.md#配对与本地身份)。

## 当前会话 MCP

生成与本地安装环境匹配的配置，不会改写 Claude Code 设置：

```bash
chiliarch agent config --host claude-code --agent claude-main
```

标准 MCP JSON 结构如下。使用生成器给出的真实 `command` 和 `args`，避免桌面宿主的程序搜索路径与 shell 不同：

```json
{
  "mcpServers": {
    "chiliarch": {
      "type": "stdio",
      "command": "chiliarch",
      "args": [
        "agent",
        "mcp",
        "--mode",
        "daemon",
        "--daemon-directory",
        "/绝对路径/当前用户守护目录",
        "--agent",
        "claude-main"
      ]
    }
  }
}
```

项目作用域使用项目根目录 `.mcp.json`，只合并 `mcpServers.chiliarch`，不覆盖其他服务；不要把凭证内嵌到 JSON。用户作用域可使用原生注册命令；确认命令在 Claude 的 PATH 中可用时：

```bash
claude mcp add --transport stdio --scope user chiliarch -- \
  chiliarch agent mcp --mode daemon --agent claude-main \
    --daemon-directory "/绝对路径/当前用户守护目录"
```

这条命令会修改 Claude Code 的 MCP 配置，安装提示词授权接入后再执行。`--` 后是 MCP 服务程序和参数；没有它时原生 CLI 可能把 Chiliarch 参数误当自己的选项。上述两种注册方式选一种。项目 MCP 的信任确认、组织策略和允许/拒绝规则仍由 Claude Code 管理。[官方 MCP 配置](https://code.claude.com/docs/en/mcp)

重新打开 Claude Code 会话或按原生提示重连，在 `/mcp` 中检查工具。默认托管身份可在当前会话查询任务和协调，由 runner 自动执行调度。下面的领取示例仅用于 `--mode manual` 身份：

```text
用 chiliarch MCP 领取当前机器的待处理任务，汇报进度与完成结果。
不清楚依赖时联系 Chiliarch AI；需要其他 agent 时提交平台查询或协作申请，
保留请求 ID 并检查回复。批准前不要把申请当成已经执行。
没有待处理工作时告诉我状态，不伪造完成记录。
```

manual 身份的 MCP 连接不会保证空闲原生会话持续自动领取。默认 managed 身份使用 runner 执行，原生 Claude Code 保留自己的工具审批边界。

工具包括 `status`、`claim_task`、`report_progress`、`complete_task`、`fail_task`、`send_message`、`list_agents`、`request_operation`、`get_request_result`、`read_messages` 和 `list_tasks`。用途、参数、cursor 和业务状态直接写在当前工具定义中；长任务要按工具说明定期主动检查来信，升级后刷新工具列表或重连 MCP。可选参考见 [运行中工具与通信指引](tools.md) 和 [完整工作流](index.md#mcp-工具与完整工作流)。保存 `read_messages` 返回的 cursor；申请未批准时不要把 `pending` 当作允许执行。

## 托管 runner 配置

本版 runner 支持 Windows、macOS、Linux，需要已安装的 `claude`。Windows 复用原生 `.exe`、标准 npm `.cmd` 的实际 Node 入口或明确的 Python wrapper；不执行任意自定义 `.cmd`/`.bat`，需要时指定实际可执行入口。默认配对已自动生成并登记本地 TOML；需要调整时编辑生成的文件，或提前准备以下设置并在配对时传 `--settings`，目录必须已经存在：

```toml
kind = "claude-code"
workdir = "/绝对路径/已有项目目录"
timeout_seconds = 1800
terminate_grace_seconds = 2
max_input_bytes = 1048576
max_output_bytes = 524288
max_line_bytes = 262144

# 可选：指定本机可执行程序；省略时查找 claude。
# executable = "/绝对路径/claude"
```

修改已有 runner 设置后，用实际状态路径、本机标识和 TOML 路径更新登记；首次配对已完成此步骤，不需要再配对：

```bash
chiliarch daemon register \
  --state "/绝对路径/私有目录/claude-runner.db" \
  --agent claude-worker \
  --settings "/绝对路径/local-agent.toml" \
  --max-concurrent-tasks 1
chiliarch agent doctor --agent claude-worker --settings "/绝对路径/local-agent.toml"
```

模型、服务商、权限和工具允许规则由 Claude Code 的原生配置管理。runner 不传 `--model`、`--permission-mode` 或 `--allowedTools`，也不自动允许 Chiliarch MCP 工具。runner 的配置字段见 [本机 runner 与原生配置](../configuration.md#本机-runner-与原生配置)。[Claude CLI 参数](https://code.claude.com/docs/en/cli-reference)

原生权限、项目信任与组织策略继续生效。当前 Chiliarch 尚无跨平台原生审批或用户输入回复桥；print 模式中需要交互的操作可能被原生拒绝。Claude 可以在已有权限内换一种方法继续，单个工具被拒绝不等于整个任务失败；runner 记录拒绝次数，最终按原生终态判断任务成功或失败。平台 ask/auto/yolo 只管理平台业务操作，不替你批准原生工具，也不改为跳过权限检查。需要人工交互的工作可以使用当前会话 MCP，在 Claude Code 内处理。

## 执行与结果

默认调度创建全新的原生会话，使用普通 stdin 输入任务，选择 `--print`、`--output-format stream-json`、`--verbose`、`--include-partial-messages`。临时 `--mcp-config` 只追加一个任务专用、名称唯一的 Chiliarch mailbox MCP，任务提示明确指定该名称；不替换用户已有的 `chiliarch` 或其他 MCP 服务。runner 不使用 `--strict-mcp-config` 或 `--bare`，按原生规则保留已有 MCP、skills、hooks、`CLAUDE.md`、认证和其他设置，也不改写全局配置。[官方程序化运行](https://code.claude.com/docs/en/headless)

原生配置是否可见取决于守护进程的用户、本轮工作目录与配置目录环境，见 [守护进程环境](../daemon.md#原生配置目录与环境)。WebUI 或 `dispatch_task.workdir` 可为任务选择已有绝对目录，留空使用 TOML 默认值，不改写 TOML；会话续接留空则继承原会话目录。目录锁、默认值更新和重试规则见 [目录配置](index.md#默认目录与任务目录)。Claude Code 已原生登录且没有更高优先级 API 来源时可由原生工具使用订阅；API Key、第三方服务商或组织配置仍按原生优先级选择。Chiliarch 不切换认证方式，也不保证任务一定使用订阅额度。

平台 `dispatch_task` 可用 `continue_task_id` 指向当前身份最近成功完成的托管任务。runner 只从自己的本地持久记录读取原生 session ID，以 `claude --print --resume <session-id>` 启动独立后续轮次，遵循原生续接语义并追加本轮专用 mailbox。每轮有独立平台 task ID，结果提供 `continued_task_id`；不枚举、不接管用户的交互会话，不接受远端任意 session ID。[官方会话续接](https://code.claude.com/docs/en/sessions)

续接要求平台身份、适配器与工作目录一致，前任务成功且原生进程树已确认停止。关联持久化在该身份 SQLite 中，支持守护进程重启；新的续轮启动后必须引用最新成功任务继续，失败、取消或结果未知时不能借更早的成功 task ID 绕过恢复核查。若原生会话文件被删除，失败会正常上报，不静默新建对话。

runner 按结构化原生结果汇报：要求最终 `result` 为成功 subtype、`is_error=false` 且进程退出成功。限制超时、输出总量和单行大小；无终态、无效 JSONL、非零退出或原生失败事件均不会假报完成。结果包含答案、原生会话 ID、可解析的用量与原生返回的费用统计/权限拒绝信息。usage 和费用估算不是实际扣费凭证，具体额度与账单以原生服务商为准。[原生终态说明](https://code.claude.com/docs/en/agent-sdk/agent-loop)

任务内 MCP 可联系总管、用 `list_agents` 发现同伴、用 `send_peer_message` 通气，或申请 `dispatch_task` 协作；读取回信和操作结果时保留各请求 ID。初始提示要求在开始与合适的工作间隙读取当前任务的协作消息。消息驱动的任务应通过 `reply_to` 关联原消息，并向其发送者回信，普通回复保持 `wake=false`，避免自动循环。需等待同伴且对方需要当前执行槽或工作目录时，报告依赖并结束本轮，之后由独立任务续接。临时 mailbox 通过 `--managed-task` 绑定当前任务，不导出 `claim_task`、`report_progress`、`complete_task`、`fail_task`，不能跨任务冒用身份。任务最终状态由父 runner 在原生过程结束后确认，不应要求任务内模型提前标记完成。输出里的文件路径只是汇报，不自动上传文件。

默认 workdir 约束启动 cwd，不能把它当作操作系统沙盒。Claude Code 能访问哪些文件或执行哪些工具，由原生权限、已有配置、当前系统用户及隔离环境决定。需要更强机器隔离时，应由机器所有者另用专用用户、容器或其他隔离方式配置。

## 取消、断线与恢复

只有 agent 所在机器主动连接平台；代理不支持 WS 时降级 SSE＋POST、长轮询，服务器不联系你的机器 IP。断网消息和结果先入本地队列，重连再上传。

取消与超时会停止当前原生进程树并清理后台进程。平台取消请求尚未被客户端确认时，不能视为已停止。Chiliarch 不保留原生任务启动的后台服务，也不自动 resume 用户现有会话。托管能力心跳报告 `supports_resume=true`、`notification_mode=followup_task`，唤醒通过独立平台任务实现；不会向正在运行的原生回合追加 stdin 或强行接管。当前交互式 MCP 仍需宿主主动读取任务和消息。

异常退出或断电后，上次执行中的任务结果未知，客户端不自动再次执行。先核对本地改动、原生任务记录及平台状态，再人工决定恢复或新派发。凭证吊销后客户端停止通信；同一状态库的单连接规则、诊断和日志见 [接入总览](index.md)。
