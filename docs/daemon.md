# 本机共享守护进程

Chiliarch 的 agent 接入默认使用当前系统用户的共享守护进程。它管理本机多个 agent 的独立身份、消息队列和出站连接；原生宿主中的 stdio MCP 只向本地守护进程转发工具调用。平台服务 `chiliarch serve` 和本机守护进程是两种用途，不需要在每台 agent 机器运行 WebUI 或设置登录密码。

## 安装、发现与复用

先检查已有服务，再安装或启动，健康后才配对当前 agent：

```bash
chiliarch daemon status
```

已安装且健康时直接复用；已安装但停止时运行 `chiliarch daemon start`。尚未安装 Chiliarch 时，默认从 PyPI 安装到持久的 uv 工具环境：

```bash
uv tool install chiliarch
chiliarch daemon install
chiliarch daemon status
```

`install` 登记当前用户登录后的自启动，并启动本地守护进程。后续 `agent pair` 使用同一发现与复用流程：已有健康服务复用，没有时安装或启动；健康检查失败不兑换注册码。

健康检查验证经过认证的本地接口和版本，不能仅凭进程、端口或启动项存在判断已接入。探测使用独立短超时，不与领取任务等较长操作共用等待时间；接口不可达但守护目录仍被进程占用时，停止操作报错，不能宣称已经停止。多个安装流程由单实例锁协调；已有身份不会自动重新配对，数据与其他 agent 登记不会被覆盖。系统管理器不可用时可启动后台进程，但状态明确显示自启动尚未启用；容器或非登录环境也可运行 `chiliarch daemon run` 前台入口。

系统启动项记录实际 Python 解释器的绝对路径、工作目录和必要环境，不依赖调用时的 shell 或项目 cwd。优先用 `uv tool install` 的持久环境；修改或删除其安装环境后应重新检查启动配置。[uv 工具管理](https://docs.astral.sh/uv/concepts/tools/)

## 注册当前 agent

登录平台 WebUI，在“Agents 管理”右上角添加入口创建一次性注册码，然后在 agent 所在机器执行：

```bash
chiliarch agent pair \
  --server "http://平台主机:8000" \
  --name "机器名 / Codex App" \
  --adapter codex-app \
  --agent codex-app-main \
  --state "/绝对路径/私有目录/codex-app.db"
chiliarch agent config --host codex-app --agent codex-app-main
chiliarch agent doctor --agent codex-app-main
```

示例中的多行续行适用于 POSIX shell；Windows 可将同一命令写成一行并替换为实际路径。注册码交互输入，不放入聊天、MCP 配置或日志。`--agent` 是本机稳定标识（ASCII 字母、数字、下划线、点或短横线，最多 128 字符）；省略时使用该身份的平台 agent ID，配对完成后打印实际标识。每个独立 agent 保留自己的状态库和凭证。同类型 agent 可以登记多个实例，同一个本地标识不能指向不同状态库。

已有配对状态、配对完成但登记失败，或需要更改 runner 设置时，使用登记入口，不要再次兑换注册码：

```bash
chiliarch daemon register \
  --state "/绝对路径/私有目录/codex-app.db" \
  --agent codex-app-main
chiliarch daemon agents
```

相同状态重复登记是幂等操作；状态或标识冲突明确拒绝。登记后守护进程持有该 agent 的唯一出站连接。不要同时用 legacy `agent mcp --mode standalone` 或 `agent run` 连接同一状态库。

## 默认登录后自启动

| 系统    | 当前用户服务                            | 默认运行身份                |
| ------- | --------------------------------------- | --------------------------- |
| Windows | 任务计划程序，绑定当前用户登录触发器    | 当前用户，普通权限          |
| macOS   | `Library/LaunchAgents` 中的 LaunchAgent | 当前用户                    |
| Linux   | systemd 用户服务                        | 当前用户，不默认启用 linger |

Windows 登录任务绑定当前用户 SID、交互登录令牌，不以 SYSTEM 运行；任务允许长期运行，不因为切换电池供电立即停止。macOS 使用 launchd，Linux 先检查 systemd 用户管理器可用性。自启动注册成功和本机健康检查是不同事实；没有原生管理器、禁止注册启动项或无用户登录会话时，不伪报自启动成功。

```bash
chiliarch daemon autostart status
chiliarch daemon autostart disable
chiliarch daemon autostart enable
```

禁用自启动不等于删除已配对状态。禁用后仍可用 `daemon start` 手动运行，此时使用后台进程并保留自启动禁用设置。默认登录后运行，注销和操作系统休眠行为由当前用户服务管理器决定；需要未登录就运行的部署，应明确配置专用运行用户及自己的进程管理方式。

## 目录与本地通信

默认目录按当前用户和操作系统解析：Linux 使用 `XDG_STATE_HOME`（未设置则为 `~/.local/state`），macOS 使用 `~/Library/Application Support`，Windows 使用 `LOCALAPPDATA`，其下为 `chiliarch/daemon`。状态、服务发现信息、登记与日志归当前用户所有，不放入项目仓库或共享目录。

需要独立目录时，所有相关命令保持一致：

```bash
chiliarch daemon install --directory "/绝对路径/私有目录/daemon"
chiliarch agent pair --daemon-directory "/绝对路径/私有目录/daemon" \
  --server "http://平台主机:8000" --name "工作机" --adapter codex \
  --agent worker --state "/绝对路径/私有目录/worker.db"
chiliarch agent config --host codex --agent worker \
  --daemon-directory "/绝对路径/私有目录/daemon"
```

每个 agent 的凭证仍保存在其独立 SQLite 状态库；MCP 配置只携带本地标识和守护目录，不携带平台凭证。本地接口仅绑定回环地址，由私有发现信息和本地认证保护，不为外部机器开放。当前系统用户能访问的本地权限边界仍由操作系统及目录权限决定。

到平台的网络连接始终由守护进程主动出站发起，降级顺序为 WS → SSE＋HTTP POST → 长轮询。多个 agent 共用进程，不共用平台身份或凭证，也不合并为一条未定义身份边界的物理连接。平台永远不主动连接 agent 的地址。

## 当前会话与托管执行

当前会话 MCP 通过 `--mode daemon --agent <标识>` 接入。你可以主动领取工作、汇报进度与结果、联系总管和查询上级回复；守护进程常驻并不意味着它能自动唤醒空闲 Codex App 或 Claude Code 会话。

只有明确需要无人值守时，为独立身份配置固定工作目录和执行策略，将本地 TOML 交给守护进程：

```bash
chiliarch daemon register \
  --state "/绝对路径/私有目录/codex-runner.db" \
  --agent codex-worker \
  --settings "/绝对路径/local-agent.toml" \
  --max-concurrent-tasks 1 \
  --transport ws
```

本地设置控制启动程序、工作目录、原生权限与并发，远端任务不能改写这些边界。任务内临时 MCP 使用 mailbox 模式，由父连接传输；模型不能跨任务冒用身份，也不能抢先标记执行成功。工作目录只约束启动 cwd，不是额外文件系统沙盒。具体 TOML 和原生权限说明见 [Codex](agents/codex.md) 与 [Claude Code](agents/claude-code.md)。

托管续轮使用平台 `dispatch_task.continue_task_id`，指向同身份最近成功完成的托管任务。`parameters.chiliarch_context` 是平台注入的关联信息，普通任务参数不能代替本地会话所有权校验。runner 把自己启动的原生会话与任务关系保存在身份状态库 `runner_sessions` 表；核对平台身份、服务地址、适配器、工作目录、本地任务完成状态后，才使用 Codex `exec resume` 或 Claude `--print --resume`。新的任务有独立 ID；开始续轮后旧任务关联失效，须引用最新成功任务继续。运行中、失败、取消、结果未知，以及来自其他身份或用户手动启动的会话均不可续接。原生历史被删除时报告失败，不另开会话冒充恢复。

`send_peer_message` 普通来信由 daemon 持久接收，运行中的模型在适当时机主动读信。显式唤醒走正式任务调度，已完成托管任务可续轮；不向仍运行的原生 stdin 注入信息，也不代表当前 App 的手动会话能被自动唤醒。普通回信默认不唤醒，防止双方无限自动回复。任务若在等待另一个需要相同执行槽或工作目录的任务，应保存依赖并结束本轮，之后再续接，而不是一边占锁一边等。

托管 runner 按实际解析后的工作目录取得跨进程锁。同一系统用户下，不同身份、独立 runner 或不同守护进程使用同一个工作目录时依次执行；不同目录可以并行。锁文件保存在当前用户默认 Chiliarch 状态根目录的 `workspace-locks` 中，不写入项目目录，不改变原生工具权限。等待期间会报告“等待其他任务释放工作区”，任务尚未执行完成，可以取消；取消或结束会先清理原生进程树，再释放工作区。不要手动删除占用中的锁文件。此协调仅覆盖 Chiliarch 托管 runner，不控制手动 App 会话、其他用户或在父子目录分别启动的外部程序。

原生启动前会持久保存工作区隔离标记，只有确认进程树清理完成后才删除。清理抛错、进程意外退出或启动结果无法确认时，标记会保留；即使系统已释放文件锁，其他身份也不能在该目录继续启动。心跳会显示 `blocked` 和 `workspace_unverified`。这也意味着本地启动失败后可能需要人工核对一次，而不能直接假设没有任何子进程或副作用。

Linux/macOS 发送终止信号后还会确认同一进程组没有运行成员，独立关闭输出的后台子进程也包括在内；已退出、尚未回收的僵尸进程不继续占用工作区。发送 `SIGKILL` 本身不视为完成清理，未能在期限内确认退出时保留隔离标记。

使用以下命令只读查看标记位置；它不会检查进程，也不会把标记存在解释为进程一定仍在运行：

```bash
chiliarch agent workspace-recover --workdir "/实际工作目录"
```

先确认对应原生进程及其子进程均已停止，并检查未确认的文件修改或其他副作用，然后显式解除隔离：

```bash
chiliarch agent workspace-recover --workdir "/实际工作目录" --confirm-stopped
```

解除操作要求独占工作区锁；还有 Chiliarch worker 占用时会拒绝。它只主动删除隔离标记，不终止任何进程，不替你验证已停止，也不重做中断任务。正常运行中的标记由 runner 自己清理，不能以恢复命令绕过仍在运行的任务。

锁文件的生命周期由 `filelock` 的系统实现管理：Linux/macOS 通常保留 `.lock` 文件，Windows 释放锁后会尝试删除它。文件存在不代表正在占用，不存在也不能代替隔离标记检查。恢复验证应检查任务能重新执行、执行中仍保持互斥以及隔离标记正常清理。

Windows、macOS、Linux 复用同一登记与调度流程。POSIX 清理原生进程组；Windows 使用 Job Object 清理整个任务进程树。Windows 支持原生 `.exe`、标准 npm 安装的 `.cmd`（解析原生包的对应入口并直接启动 Node）及明确的 Python wrapper；自定义 `.cmd`/`.bat` 无法保证参数边界时拒绝运行，请设置实际可执行入口。路径包含空格或中文时仍作为参数传递，不拼接任务 shell 命令。

macOS 在进程组仅剩僵尸进程时可能返回权限错误；清理会通过系统 `ps` 再确认该组没有运行成员。仍有运行进程或无法读取状态时保留清理失败，不能把权限错误直接当作已经停止。

取消、超时和退出先停止并清理本地任务进程，再确认取消。崩溃后执行结果未知的任务保持中断，不因自启动或恢复连接而自动重做；普通断线继续持久排队，不重复启动任务。三系统的原生 CI 测试进程清理，测试使用测试子进程，不能以配置文件存在或 Linux mock 代替实际登录后验收。

## 状态、维护与卸载

```bash
chiliarch daemon status
chiliarch daemon agents
chiliarch daemon stop
chiliarch daemon start
chiliarch daemon restart
chiliarch daemon remove --agent codex-app-main
chiliarch daemon uninstall
```

`status` 区分本地健康与系统服务状态；`agents` 展示独立身份、manual/managed 模式、连接、传输及可公开的错误。健康接口报告实际守护进程 PID；Windows 虚拟环境可能先启动解释器启动器，该启动器 PID 可以不同。`doctor --agent` 是只读检查，不隐式安装、启动或登录原生宿主；本地健康和登记成功不代替平台在线、凭证有效或任务成功的确认。

每个 agent 的 `execution` 状态与发给平台的心跳元数据一致，包含执行模式、状态、活动任务 ID、排队数量、容量，以及已知的适配器和工作目录。托管 runner 额外提供物理目录对应的 `workspace_id`、`supports_resume=true`、`notification_mode=followup_task`，表示能在停止后通过独立平台任务续接；同机相同 `workspace_id` 共享目录锁。manual 模式未领取任务时为 `waiting_for_claim`，领取后为 `busy`；managed 空闲时为 `idle`，运行时为 `busy`，等待工作区时为 `blocked`，并提供 `blocked_reason=workspace_busy` 和 `blocked_task_ids`。托管工作区一次只执行一项任务，报告容量为 1；手动会话的容量无法从连接推断，显示为未知。心跳只报告这些可观察状态，不检查或承诺 Codex／Claude 的登录、额度、原生权限或未来任务一定成功。

WebUI 将目录不可访问、目录占用、旧执行未确认及缺少执行器显示为中文原因，并给出对应的本机检查或恢复指引。扩展执行器上报的未知原因保留原文。离线和取消待确认不会被解释为进程已经停止。

移除 agent 登记会停止该身份的连接并取消、清理其托管任务，不吊销平台凭证或删除 SQLite 状态库；其他 agent 继续使用共享服务。`stop`、`restart` 与 `uninstall` 会停止此守护进程的所有身份和托管工作，等待任务进程树清理。手动 MCP 会话中的实际工作仍由原生宿主负责，守护进程不会强行关闭用户的 App 对话。恢复发现仍标为 running/cancelling 的工作时报告中断与结果未知，不自动重放副作用。

卸载移除当前用户服务及自启动，保留守护目录、登记、凭证和日志；下次安装可恢复登记。需要清理数据时先核对未上传结果、未知执行副作用与平台身份。升级前停止或排空工作，备份登记、状态库和设置，再更新 uv 工具环境与启动配置。

结构化日志按大小轮换，目录为守护目录下的 `logs`，stdio MCP 的 stdout 只承载协议。保留共享状态库与日志的权限，勿复制同一凭证到另一机器并发运行。详见 [日志配置](configuration.md#日志存储与轮换) 和 [部署维护](deployment.md)。
