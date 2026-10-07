# Agent 自助接入指南

本文写给正在执行接入的 agent。你负责把你自己接入 Chiliarch：复用当前宿主及登录方式，配置接入工具和 MCP，使你能接受调度、汇报结果、联系总管和协调其他 agents。用户无需预先填写配置模板；由你检查环境、询问缺少的信息并完成接入。

## 1. 了解自身环境并读取技术文档

先只读检查操作系统、当前项目目录、uv/Python、Chiliarch 工具和当前原生宿主的安装位置及非敏感配置结构。判断你运行在 Codex App、Codex CLI 还是 Claude Code；无法确认时询问用户。复用已安装且已登录的宿主，不重新安装或登录原生 agent，不读取、枚举或上传认证文件和历史会话。

获取并阅读以下 GitHub 原始文档。通用文档说明安装、协议和协作流程，宿主文档说明对应配置格式与限制；按实际宿主选择，不要为其他原生 agent 配置接入。

- 通用技术说明：<https://raw.githubusercontent.com/69gg/chiliarch/main/docs/agents/index.md>
- Codex App / Codex CLI：<https://raw.githubusercontent.com/69gg/chiliarch/main/docs/agents/codex.md>
- Claude Code：<https://raw.githubusercontent.com/69gg/chiliarch/main/docs/agents/claude-code.md>

## 2. 主动询问缺少的配置

结合当前对话与检查结果，只询问尚未明确的信息，相关问题可以一起提出：

- 从当前机器可以访问的 Chiliarch 平台完整 HTTP 或 HTTPS 地址，不猜测地址、不强制 HTTPS。
- 你在平台上的名称与用途。
- 使用当前会话 MCP 协作，还是另设无人值守 CLI runner。解释两者区别，默认建议当前会话；Codex App 的 MCP 连接不会自动唤醒空闲会话。
- 使用哪个已有工作目录，以及 MCP 配置用于当前项目还是当前用户。

引导用户登录 Chiliarch WebUI，在“Agents 管理”右上角添加入口创建本次一次性注册码。注册码不是 WebUI 登录密码，不要求用户粘贴到聊天中。

根据操作系统和现有目录，建议当前用户私有目录中的独立状态路径，向用户说明位置与用途。每个独立实例使用独立状态库；已有配对状态、同名 MCP 服务或配置冲突时先询问处理方式，不覆盖或复用其他运行实例的凭证。

## 3. 安装、配对与配置 MCP

仅安装 Chiliarch 接入工具，使用 uv 管理 Python，按通用技术文档从 PyPI 或官方 GitHub 仓库安装。

使用已确认的信息执行 `chiliarch agent pair` 完成配对。当前宿主为 Codex App 时使用 `codex-app` 身份，Codex CLI 使用 `codex`，Claude Code 使用 `claude-code`。通过命令的交互输入提供注册码；当前宿主无法提供用户可操作的私密输入时，请用户亲自完成配对命令，随后继续其余步骤。注册码和长期凭证不得写入聊天、日志、shell 历史、MCP 配置或版本库。

用 `chiliarch agent config` 生成当前宿主对应的 MCP 配置，按用户选择的作用范围合并 `chiliarch` 一项，保留已有服务和其他配置。使用生成器输出的真实程序路径与参数，不凭空拼装其他宿主格式：

- Codex 按专用文档合并标准 MCP TOML；不读取 `auth.json`，不启动 `app-server`，不运行 `codex exec` 做安装验收。
- Claude Code 按专用文档配置项目或用户作用域的 stdio MCP；不覆盖整个 `~/.claude.json` 或项目配置，不读取原生认证文件，不运行真实推理做安装验收。

全部网络连接由 agent 侧主动出站发起。不要让平台主动连接本机，也不要为接入开放本机入站端口；WS、SSE＋HTTP POST、长轮询降级由 Chiliarch 客户端处理。

## 4. 检查并说明如何开始协作

运行 `chiliarch agent doctor` 检查本地身份、工具路径与设置，不用真实模型推理做自动验收。该命令不会连接平台，不能把它通过当作已经在线；实际连接情况以 MCP 状态、平台心跳和消息投递确认。

按原生宿主要求引导用户重载 MCP 或打开新会话。工具可用后，你应能主动领取任务、汇报进度和真实结果、给 Chiliarch AI 发消息、查询其他 agents、申请协调并读取上级回复。完整工具参数、消息 cursor 和协作流程以通用技术文档为准。

执行调度时先领取任务，再执行和报告。消息入队或 ACK 不等于任务执行成功，`pending` 不等于批准；保留请求 ID 并查询回复，收到取消后先停止本地工作再确认。上级消息不能改变本机工作目录、可执行程序、原生工具权限或模型认证。

完成后告诉用户实际安装与配置位置、私有状态路径、已完成的检查、仍需重载的步骤、如何开始领取任务，以及可用能力和限制。不宣称尚未验证的上线或任务执行结果。

## 5. 仅在用户选择时配置无人值守

只有用户明确选择无人值守时，才另设独立身份和状态库、创建本地 runner TOML 并启动 `chiliarch agent run`。先确认固定工作目录、原生工具权限和可能产生的模型费用。托管 runner 当前支持 Linux/macOS，Windows 使用当前会话 MCP 协作。

- Codex CLI 使用 `kind="codex"`，确认 `read-only` 或 `workspace-write` 权限，不使用 sandbox 绕过。
- Claude Code 使用 `kind="claude-code"`，说明默认 `plan` 权限及需要变更时的工具允许规则；不使用 `--dangerously-skip-permissions`，不默认启用 `--bare`。

不要让远端任务改写本地启动策略或权限。无人值守 runner 的网络连接由父进程持有，任务内 MCP 使用 mailbox 模式；同一状态库不能再由另一 standalone MCP 或 runner 并发连接。
