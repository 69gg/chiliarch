# Codex App 与 Codex CLI

将下面整段发给当前 Codex，无需预填配置。它会读取 [Agent 自助接入指南](onboarding.md)，向你询问缺少的信息并完成接入。仓库尚未推送时，可让它读取可信本地 checkout 中的同一文档。

```text
你负责把你自己接入 Chiliarch。
请获取并阅读 https://raw.githubusercontent.com/69gg/chiliarch/main/docs/agents/onboarding.md，按文档完成接入。
缺少的配置请主动向我询问。
```

## 选择接入方式

Codex App 与交互式 Codex CLI 使用同一个 Chiliarch stdio MCP 服务。官方说明桌面 App、CLI、IDE 在同一 Codex host 上共享 MCP 配置；Chiliarch 因而不需要修改 App 私有数据库或模拟点击。[官方 MCP 文档](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)

当前会话可以主动领取和完成任务、发送上级消息、申请平台操作、读取结果。它不会因为服务器新增任务就必然开始一个模型回合。需要持续无人值守时，单独注册 `codex` 身份并运行 CLI runner；App 内的协作身份用 `codex-app`。不要把一个状态数据库同时交给两者。

## 安装与配对

按 [通用安装说明](index.md#安装接入工具) 安装 Chiliarch。仓库地址为 [69gg/chiliarch](https://github.com/69gg/chiliarch)，当前尚未推送到 GitHub、尚未发布 PyPI；远程安装示例要等发布完成，现阶段使用可信源码 checkout。

进入已存在且可信的项目目录，配对 App：

```bash
chiliarch agent pair \
  --server "http://平台主机:8000" \
  --name "机器名 / Codex App" \
  --adapter codex-app \
  --state "/绝对路径/私有目录/codex-app.db"
```

交互输入一次性注册码。CLI 协作或托管执行使用 `--adapter codex`，并换成独立的状态路径。长期凭证仅保存在该本地状态数据库中，不需要写入 Codex 配置或上传 Codex 的认证文件。

## 给当前会话添加 MCP

先生成当前环境对应的配置：

```bash
chiliarch agent config --host codex-app --state "/绝对路径/私有目录/codex-app.db"
```

CLI 使用 `--host codex`。命令只输出片段，不修改 `~/.codex/config.toml`。按 Codex 的标准 `[mcp_servers.<name>]` 格式合并，不覆盖已有服务。已独立安装 `chiliarch` 时，形状如下：

```toml
[mcp_servers.chiliarch]
command = "chiliarch"
args = ["agent", "mcp", "--state", "/绝对路径/私有目录/codex-app.db", "--mode", "standalone"]
enabled = true
```

优先使用生成器的真实 `command`、`args`；源码安装通常使用其 Python 解释器启动模块，避免桌面 App 的 PATH 和你的 shell 不同导致启动失败。状态路径必须是绝对路径。

可以使用用户级 `~/.codex/config.toml`，或可信项目内的 `.codex/config.toml`。也可以在 App 的 MCP 设置中新增 STDIO server，填入生成器输出的程序及参数。保存后按 App 提示重载 MCP，或打开新会话；CLI 则重启该会话。在会话中用 `/mcp` 查看服务与工具是否可用。

请让 Codex 开始协作，例如：

```text
使用 chiliarch MCP 检查连接，领取我的待处理任务并报告进度。
需要协调时给 Chiliarch AI 发消息；要列出其他 agent 或派发协作任务，
提交平台操作申请并检查返回结果。批准前不要把申请视为已执行。
没有任务时告诉我当前状态，不伪造工作或运行完成结果。
```

服务工具名包括 `status`、`claim_task`、`report_progress`、`complete_task`、`fail_task`、`send_message`、`list_agents`、`request_operation`、`get_request_result`、`read_messages` 和 `list_tasks`。具体参数、cursor 和业务状态见 [完整工作流](index.md#mcp-工具与完整工作流)。先领取再执行，收到取消停止工作；`queued`、`pending` 都不代表上级已批准。

## 托管 CLI 执行

本机需已有可运行、已配置认证的 `codex`；Chiliarch 不自动安装或登录它。创建本地 TOML，替换工作目录：

```toml
kind = "codex"
workdir = "/绝对路径/已有项目目录"
codex_sandbox = "read-only"
timeout_seconds = 1800
terminate_grace_seconds = 2
max_input_bytes = 1048576
max_output_bytes = 524288
max_line_bytes = 262144

# 可选：指定本机可执行程序和已经配置好的模型；不填写则使用原生默认值。
# executable = "/绝对路径/codex"
# model = "你的模型名称"
```

默认只读，需要修改项目时由机器所有者在本地改为 `codex_sandbox = "workspace-write"`。运行器没有提供绕过 sandbox 的设置。默认遵守 Codex 的 Git 仓库检查；只有已确认可信的非 Git 工作目录才设置 `codex_skip_git_repo_check = true`。

```bash
chiliarch agent doctor \
  --state "/绝对路径/私有目录/codex-runner.db" \
  --settings "/绝对路径/local-agent.toml"

chiliarch agent run \
  --settings "/绝对路径/local-agent.toml" \
  --state "/绝对路径/私有目录/codex-runner.db" \
  --max-concurrent-tasks 1
```

先按前述方式使用 `--adapter codex` 配对该 runner 的独立状态库。CLI runner 目前支持 Linux/macOS；命令占用前台，可接入你自己的进程管理器。

每个任务使用 stdin 传入指令，启动 `codex exec --json`，显式选择 sandbox 和固定 cwd，注入临时 Chiliarch mailbox MCP。审批设置为 `approval_policy="never"`：原生运行不等待交互式审批；sandbox 或其他规则拒绝的操作会失败，不会因此放开权限。平台的 ask/auto/yolo 审批是另一层边界。[非交互执行文档](https://learn.chatgpt.com/docs/non-interactive-mode)

每次调度创建新 Codex 原生会话，结果包含原生会话 ID、答案和可解析的用量。不自动 resume 用户已存在的 App 或 CLI 会话，也不默认枚举它们。runner 只在原生成功终态、JSONL 正常、进程退出成功共同成立时报告完成；只有文字输出或 EOF 不会假报成功。

任务内的 mailbox MCP 用于向总管汇报、查询资源与协调，不导出 `claim_task`、`report_progress`、`complete_task`、`fail_task`、`confirm_cancellation`。runner 用 `--managed-task` 绑定本任务身份，模型不能冒用别的任务汇报。任务完成状态由父 runner 确认原生终态后上报，不要要求任务内模型先自行标记成功。固定 cwd 是启动策略，不是额外文件系统沙盒；实际可访问目录取决于 Codex sandbox、配置与操作系统权限。

## 运行中的协调与故障

任务可以请求 `list_agents` 查询可用 agent，或通过 `dispatch_task` 申请给同伴派发工作；这些业务操作仍经总管、模式和权限处理。需要同伴结果时保留平台任务/请求 ID，给总管发送清楚的阻塞项与依赖关系，再查询回复。不要把整个原生会话或认证文件上传来充当汇报。

断线后消息持久排队，连接自动重建；WS 不通时降级 SSE＋POST、长轮询。吊销凭证会拒绝继续通信，需要重新配对。取消、超时或 runner 退出会清理原生进程组；重启发现执行中任务时报告结果未知，不能自动重做。完整边界见 [通用说明](index.md#运行取消与恢复)。

App 显示工具但“没有自动工作”时，先让当前会话主动检查/领取任务；需要服务器持续派发并自动执行时使用独立 runner。较新的 Codex 提供实验性 App Server 及共享 daemon 能力，但本适配器不连接用户已有控制 socket、不接管当前 App 对话。[App Server 官方说明](https://learn.chatgpt.com/docs/app-server)
