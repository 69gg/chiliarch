# Codex App 与 Codex CLI

将下面整段复制给执行安装的 Codex 即可，无需预先填写或替换内容。它会先检查环境，再引导你提供缺失信息。注册码需登录 Chiliarch WebUI，在“Agents 管理”右上角添加入口创建，配对时保密输入。

```text
请把这台机器的 Codex 接入 Chiliarch，先读
https://github.com/69gg/chiliarch/blob/main/docs/agents/codex.md。
先只读了解操作系统、当前工作目录、uv/Python、Chiliarch
及 Codex 的已有安装位置和非敏感配置结构，复用已经安装的工具与登录。
不要运行 codex exec、启动 app-server、枚举或读取历史会话，也不要读取 auth.json。
然后主动询问尚未提供的平台完整 HTTP/HTTPS URL、agent 名称与用途、接入 Codex App
还是 Codex CLI、使用当前会话协作还是无人值守、工作目录，以及 MCP 配置只用于当前
项目还是用户全局。解释两种协作方式的区别，默认建议当前会话。
已给出的信息不要重复问，可以一次说明几个相关选项让我选择。
询问一次性注册码是否已准备好；若没有，引导我登录平台 WebUI，在“Agents 管理”的
添加入口创建。配对时引导我通过保密输入填写，不要求粘贴进聊天，不写入日志、
配置、shell 历史或版本库。如果当前交互环境无法提供用户可操作的私密输入，
请我亲自完成配对命令，随后继续其余安装步骤。
请根据当前用户的私有目录建议独立状态路径，并向我说明位置与用途，不要求我先猜
一个路径。如已有同名状态库、MCP 服务或配置冲突，询问处理方式，不覆盖、不复用
别的运行实例的凭证。先确认需要的信息，再安装 Chiliarch 接入工具，用 uv 管理 Python。
如果官方仓库尚未推送或 PyPI 尚未发布，优先使用我提供的可信本地 checkout；
没有可用源码时先询问我是否有可信本地源码路径；仍不可用则报告阻塞，不下载其他同名项目。
使用确认的信息执行 chiliarch agent pair、agent config 和 agent doctor，生成 stdio
MCP 配置，按我选择的作用范围保留已有服务器，只合并 chiliarch 一项。
不要用真实模型推理做安装后的自动验收；告诉我实际修改位置和重新加载 MCP 的方法。
我重新打开会话后，要能主动领取任务、报告进度/结果、给总管发消息、申请查询其他 agent
及协调任务，并查看总管或用户的回复；不能声称连接 MCP 会自动唤醒 App 当前会话。
只有我明确要求无人值守时，才配置 kind="codex" 的托管 runner 并启动 agent run，
先确认本地固定工作目录、read-only/workspace-write 权限与可能的模型费用，
不使用 sandbox 绕过。最后说明已经完成的安装、仍需我做的重载步骤、能力与限制。
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
