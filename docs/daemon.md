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

移除 agent 登记会停止该身份的连接并取消、清理其托管任务，不吊销平台凭证或删除 SQLite 状态库；其他 agent 继续使用共享服务。`stop`、`restart` 与 `uninstall` 会停止此守护进程的所有身份和托管工作，等待任务进程树清理。手动 MCP 会话中的实际工作仍由原生宿主负责，守护进程不会强行关闭用户的 App 对话。恢复发现仍标为 running/cancelling 的工作时报告中断与结果未知，不自动重放副作用。

卸载移除当前用户服务及自启动，保留守护目录、登记、凭证和日志；下次安装可恢复登记。需要清理数据时先核对未上传结果、未知执行副作用与平台身份。升级前停止或排空工作，备份登记、状态库和设置，再更新 uv 工具环境与启动配置。

结构化日志按大小轮换，目录为守护目录下的 `logs`，stdio MCP 的 stdout 只承载协议。保留共享状态库与日志的权限，勿复制同一凭证到另一机器并发运行。详见 [日志配置](configuration.md#日志存储与轮换) 和 [部署维护](deployment.md)。
