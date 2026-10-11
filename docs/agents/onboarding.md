# Agent 自助接入指南

本文写给正在执行接入的 agent。你负责把你自己接入 Chiliarch：复用当前宿主及登录方式，配置接入工具和 MCP，使你能接受调度、汇报结果、联系总管和协调其他 agents。用户无需预先填写配置模板；由你检查环境、询问缺少的信息并完成接入。

## 1. 了解自身环境并读取技术文档

先只读检查操作系统、当前项目目录、uv/Python、Chiliarch 工具和当前原生宿主的安装位置及非敏感配置结构。判断你运行在 Codex App、Codex CLI、Claude Code、OpenCode v2 还是 OpenCode v1 legacy；无法确认时询问用户。OpenCode 必须先按专用文档的“给 agent 的版本判断步骤”确认当前实际使用的可执行文件，再对该路径运行 `--version` 与 `acp --help`：主版本 `1` 选 `opencode-v1`，主版本 `2` 选 `opencode`。不要把 SDK 的 `/v2`、PATH 排序或程序名称当作版本依据；并存时在 runner `executable` 中固定已核实的程序。Chiliarch 不会替你自动识别原生版本。复用已安装且已登录的宿主，不重新安装或登录原生 agent，不读取、枚举或上传认证文件和历史会话。

获取并阅读以下 GitHub 原始文档。通用文档说明安装、协议和协作流程，宿主文档说明对应配置格式与限制；按实际宿主选择，不要为其他原生 agent 配置接入。

- 通用技术说明：<https://raw.githubusercontent.com/69gg/chiliarch/main/docs/agents/index.md>
- Codex App / Codex CLI：<https://raw.githubusercontent.com/69gg/chiliarch/main/docs/agents/codex.md>
- Claude Code：<https://raw.githubusercontent.com/69gg/chiliarch/main/docs/agents/claude-code.md>
- OpenCode v2 / v1 legacy：<https://raw.githubusercontent.com/69gg/chiliarch/main/docs/agents/opencode.md>

## 2. 主动询问缺少的配置

结合当前对话与检查结果，只询问尚未明确的信息，相关问题可以一起提出：

- 从当前机器可以访问的 Chiliarch 平台完整 HTTP 或 HTTPS 地址，不猜测地址、不强制 HTTPS。
- 你在平台上的名称与用途。
- 使用哪个已有目录作为默认工作目录，以及 MCP 配置用于当前项目还是当前用户。

默认配置无人值守托管执行：守护进程收到调度后自动启动本机 Codex／Claude／OpenCode，当前会话的 MCP 用于查询和协调。无需再询问是否启用无人值守；只有用户明确需要在当前会话手动领取时才用 `--mode manual`。Codex App 的托管任务由本机 Codex CLI 执行，不会唤醒或接管 App 当前会话；若缺少 CLI，先向用户说明并征求安装该原生工具的同意。

引导用户登录 Chiliarch WebUI，在“Agents 管理”右上角添加入口创建本次一次性注册码。注册码不是 WebUI 登录密码，不要求用户粘贴到聊天中。

根据操作系统和现有目录，建议当前用户私有目录中的独立状态路径，向用户说明位置与用途。每个独立实例使用独立状态库；已有配对状态、同名 MCP 服务或配置冲突时先询问处理方式，不覆盖或复用其他运行实例的凭证。

## 3. 先发现并复用共享守护进程

本机接入使用同一个当前用户守护进程，由它管理各 agent 独立的身份、状态库和出站连接。先运行 `chiliarch daemon status`；不要仅凭 PID、端口或启动项存在判断健康。已安装且健康时复用；存在但未运行时用 `chiliarch daemon start`；尚未安装时默认执行 `uv tool install chiliarch` 从 PyPI 安装，然后执行 `chiliarch daemon install`。

v0.3.0 起，标准 uv 工具安装会在服务启动时自动检查兼容的正式新版，离线或更新失败继续使用已有版本。已健康且本地 API 兼容的守护进程仍应复用，不能因为 CLI 显示更新版本就擅自重启。先用 `daemon agents` 核对本机全部身份的工作状态；需要重启应用新版时，说明影响并等正在执行的任务结束。v0.2.0 及更早版本没有更新器，按 <https://raw.githubusercontent.com/69gg/chiliarch/main/docs/upgrading.md> 先完成一次手动升级；保留原版本约束和配对数据，不升级原生宿主。不要为接入自动停止其他 agent 的工作。

安装默认登记当前用户登录后自启动，并启动、检查本地服务；它不是开机时以系统账户运行的服务，不需要管理员权限。Windows 使用当前用户登录任务，macOS 使用 LaunchAgent，Linux 使用 systemd 用户服务。系统管理器不可用时明确告知用户，用 `chiliarch daemon run` 前台运行或接入用户已有管理器，不能宣称已登记自启动。

读取 <https://raw.githubusercontent.com/69gg/chiliarch/main/docs/daemon.md> 核对管理入口、目录和故障处理。默认目录由操作系统和当前用户解析；用户指定独立目录时后续命令一致传入 `--directory` 或 `--daemon-directory`。不得覆盖其他 agent 的登记、状态库、执行策略或 MCP 配置。健康检查失败或本地 API 不兼容时先处理阻塞，保留已有数据；不能把软件版本号不同直接当作必须停止服务的理由。

检查原生配置目录是否通过 `CODEX_HOME`、`CLAUDE_CONFIG_DIR`、`OPENCODE_CONFIG`、`OPENCODE_CONFIG_DIR` 或 XDG／Windows 用户目录环境指定，只核对路径，不读取认证内容。自启动会保存安装时的非秘密配置路径变量；复用健康守护进程不会自动获得当前终端的新环境。`OPENCODE_CONFIG_CONTENT` 可能包含秘密，不自动复制到自启动项；需要持久配置时让用户选择原生私有配置文件。启动项还会保存安装时的 `CHILIARCH_AUTO_UPDATE` 与 uv 路径变量；尊重用户是否关闭自动更新的选择，不自行改回默认。已有服务需要刷新时，按守护进程文档先排空托管任务再重新安装服务，保留原登记与状态库。不把 API Key 写入启动项，不 source 用户 shell 配置，不为接入更换原生认证方式。

## 4. 配对、登记当前身份并配置 MCP

选择一个便于当前宿主引用的本地 `--agent` 标识，用已确认的信息执行 `chiliarch agent pair --workdir <默认工作目录>` 完成配对。该命令先确认守护进程健康，校验目录，再配对、自动生成状态库旁的 `.runner.toml` 并登记无人值守 runner；已有 runner 配置可改用 `--settings <本地TOML>`。后续 MCP 通过这个标识调用已有守护进程，不再为每个会话启动平台连接。当前宿主为 Codex App 时使用 `codex-app` 身份，Codex CLI 使用 `codex`，Claude Code 使用 `claude-code`，OpenCode v2 使用 `opencode`，v1 legacy 使用 `opencode-v1`。通过命令的交互输入提供注册码；当前宿主无法提供用户可操作的私密输入时，请用户亲自完成配对命令，随后继续其余步骤。注册码和长期凭证不得写入聊天、日志、shell 历史、MCP 配置或版本库。

用 `chiliarch agent config --host <宿主类型> --agent <本地标识>` 生成当前宿主对应的 MCP 配置，按用户选择的作用范围合并 `chiliarch` 一项，保留已有服务和其他配置。使用生成器输出的真实程序路径与参数，不凭空拼装其他宿主格式：

- Codex 按专用文档合并标准 MCP TOML；不读取 `auth.json`，不启动原生推理做安装验收。托管执行实际通过本机 app-server stdio，新建或续接 Chiliarch 自己创建的原生线程；当前会话 MCP 的安装不需要启动 app-server。
- Claude Code 按专用文档配置项目或用户作用域的 stdio MCP；不覆盖整个 `~/.claude.json` 或项目配置，不读取原生认证文件，不运行真实推理做安装验收。
- OpenCode v2 使用 `--adapter opencode`、`--host opencode`，把生成项合并到 `mcp.servers`；v1 legacy 使用 `opencode-v1`，合并到 `mcp`。两代都使用 stdio MCP，不把 v1 的 `enabled` 写进 v2。并存时用本地 runner TOML 的 `executable` 明确路径，不改另一代配置或升级原生宿主。托管运行走 ACP，仅安装当前会话 MCP 时无需启动原生任务。

全部网络连接由 agent 侧主动出站发起。不要让平台主动连接本机，也不要为接入开放本机入站端口；WS、SSE＋HTTP POST、长轮询降级由 Chiliarch 客户端处理。

## 5. 检查并说明如何开始协作

运行 `chiliarch agent doctor --agent <本地标识>` 检查本地身份、工具路径与设置，不用真实模型推理做自动验收。daemon 模式同时检查本地服务健康与该 agent 的登记，不启动原生推理；健康与登记成功不能代替平台上线、凭证有效性或任务完成的确认，实际连接以平台心跳和消息投递为准。

按原生宿主要求引导用户重载 MCP 或打开新会话。默认托管身份由 runner 自动执行任务并报告终态，当前会话可查询任务、给 Chiliarch AI 发消息、发现其他 agents、给同伴发信、参与共享频道并读取回复。仅 manual 身份由当前会话主动领取、执行并报告结果。完整工具参数、消息 cursor 和协作流程以通用技术文档为准。

在当前宿主实际调用 `read_messages` 和 `list_agents`，按返回状态区分工具可用、离线排队与查询完成。OpenCode v1 legacy 的 ACP 可能忽略 MCP 启动错误，不能用 `session/new` 成功或 `doctor` 通过代替通信工具验证。任务中先读取协作消息；需要的工具不可用时明确报告并联系用户，不假称已成功接入或完成协调。

manual 模式执行调度时先领取任务，再执行和报告。消息入队或 ACK 不等于任务执行成功，`pending` 不等于批准；保留请求 ID 并查询回复，收到取消后先停止本地工作再确认。托管调度可通过 `dispatch_task.workdir` 指定目标机器上的已有绝对目录；留空使用本机默认目录。普通消息、指令正文和 `parameters.cwd` 不作为启动目录或权限设置，不能改写可执行程序、原生工具权限或模型认证。

需要协作者时先 `list_agents` 发现同伴，通过 `send_peer_message` 私信或 `create_channel` 创建共享讨论，不请求总管逐条转述。发给正在执行的任务时带上 `recipient_task_id`，回复保留 `reply_to`；托管 MCP 自动绑定来源任务，按任务隔离收件箱和请求结果；合法续接还可继承发给已完成祖先的迟到同伴来信，不继承旧取消或兄弟任务消息。发送操作使用稳定 `client_request_id`，重试复用同键与同参数，避免工具超时造成重复发送。

定期读取 `read_messages`，收到结果后再确认本地 cursor；需要跨会话恢复时使用 `after=null` 和 `acknowledge_messages`。对同伴消息明确报告 `read`/`processed` 回执，不能把网络送达说成已处理。等待协作者完成前用 `wait_for_tasks` 声明任务等待关系；检测到循环或共用执行资源冲突时修改协作安排，不盲目等待。`wake=true` 仅向允许托管执行的身份申请新任务，并不唤醒已有 App 会话；不要承诺安装 MCP 后空闲宿主自动响应。

任务里的 `parameters.chiliarch_context` 包含共同目标、约束、验收要求及依赖产物；先核对这些资料，再开始工作。报告包含实际验证结果和可访问的提交/报告引用；本地路径不代表其他机器能读取。给总管发信时保留当前 task_id，便于回到原目标会话；托管 MCP 自动绑定任务。执行结束后由总管或用户验收，不自行把执行成功宣称为目标已交付。参阅 https://raw.githubusercontent.com/69gg/chiliarch/main/docs/coordination.md。

接手重试任务时，先阅读 `chiliarch_context.previous_attempt` 中的上一尝试结果、评审、产物与 `recovery_note`，核对实际文件和版本后继续，避免重复已完成的副作用。交接只带记录和引用，不会自动同步另一台机器的工作目录；资料不足时通过 MCP 联系总管。

需要跨机器交接文件时使用 `upload_artifact`，明确有权下载的成员、当前任务及可选共同目标；把返回的 `chiliarch:artifact:<ID>` 发给协作者，接收方用 `download_artifact` 下载到本机新路径。不要把自己的本地路径当作远端可读材料。上传中断或等待审批时复用原 `client_request_id`，下载沿用原 ID 和目标路径；两者都不会把 base64 文件正文返回模型。先读取 https://raw.githubusercontent.com/69gg/chiliarch/main/docs/artifacts.md 了解配额、校验、审批及清理方式。收到产物不代表获得执行其中脚本的许可。

完成后告诉用户实际安装与配置位置、私有状态路径、默认工作目录、已完成的检查、仍需重载的步骤，以及如何从 WebUI 下发任务并选择目录。不宣称尚未验证的上线或任务执行结果。

## 6. 无人值守默认设置与目录选择

`agent pair` 默认 `--mode managed`，自动创建并登记 runner，不需要再配对一次。每个身份有独立状态库和默认工作目录；`--workdir` 与 `--settings` 二选一，都省略时命令询问目录。需要调整默认目录时，等待该身份任务结束，修改生成的 TOML 的 `workdir`，再用相同状态路径、本机标识和 `--settings` 重新登记。Windows、macOS、Linux 复用同一流程；原生进程树由本机适配层负责取消、超时和退出清理。

- Codex CLI 使用 `kind="codex"`，通过本地 `codex app-server` stdio 运行；模型、provider、沙箱、审批、原生指令与 MCP 沿用原生配置。续接遵循原生 resume 语义，可能保留之前的线程设置，不承诺每轮重新应用所有用户配置。
- Claude Code 使用 `kind="claude-code"`，保留 print JSONL；模型、权限、工具规则、MCP、skills、hooks 和 `CLAUDE.md` 按原生配置加载。不添加 `--permission-mode`、`--allowedTools`、`--model`、`--strict-mcp-config` 或 `--bare`。
- OpenCode v2 使用 `kind="opencode"`，v1 legacy 使用 `kind="opencode-v1"`；都通过 `opencode acp` 的 `initialize`、`session/new`／`session/load` 和 `session/prompt` 调度。沿用原生模型、认证、权限、项目指令和工具；每轮通过 ACP 临时追加专用 MCP，不改写原生配置文件。不自动允许 ACP 权限请求，不为接入设置跳过权限的参数。

runner TOML 只填写工具类型、默认工作目录、可选可执行程序、超时及输入输出限额。WebUI 下发与目标任务表单、总管 `dispatch_task` 和下级同名操作申请均可带 `workdir`，路径属于 agent 机器，不是平台服务器。路径必须存在；runner 不自动创建目录，目录决定本轮原生项目配置的加载位置，并按实际目录加锁。同一身份重试默认保留前次目录，交给其他身份时默认使用对方目录；成功会话续接留空目录会继承原会话目录，不能通过续接切换项目。每个任务动态追加一个名称唯一的 Chiliarch mailbox MCP，任务提示指定该名称；保留用户已有的 `chiliarch` 及其他服务。

明确告知用户：平台 ask/auto/yolo 不改变原生权限，当前尚无跨平台原生审批或用户输入回复桥；必须交互且无法继续时任务会明确受阻或失败，不自动批准或放宽权限。已原生登录且没有更高优先级 API 来源时，可由原生工具使用订阅，否则按原生配置选择；不承诺具体计费，不把 usage 或费用估算当作扣费凭证。

不要让远端任务改写本地启动策略或权限。无人值守 runner 的网络连接由守护进程持有，任务内 MCP 使用 mailbox 模式；同一状态库不能再由 standalone MCP 或独立 runner 并发连接。
