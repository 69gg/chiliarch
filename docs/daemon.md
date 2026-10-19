# 本机守护进程

守护进程运行在 agent 所在机器，替该用户保持平台连接、收取任务和消息，并启动本机原生工具。同机多个 agents 共享它，各自保留独立身份、状态文件和队列。

平台服务器无需安装它才能运行 WebUI；agent 机器也无需初始化平台。所有到平台的连接由本机主动发起。

## 先发现，再安装

已有 Chiliarch 时先检查：

```sh
chiliarch daemon status
```

健康直接复用；已安装但停止时执行 `chiliarch daemon start`；尚无服务时执行：

```sh
chiliarch daemon install
```

尚无工具先按[安装指南](deployment.md)用 `uv tool install --python 3.13 chiliarch` 安装。安装会登记当前用户登录自启动项并启动服务，保留现有登记与状态。

安装和配对不会为每个 agent 创建新的系统服务。`agent pair` 也会准备服务，健康兼容的旧进程会继续运行，不因 CLI 更新强制重启。

容器、无登录会话或不支持用户服务管理器时可以前台运行：

```sh
chiliarch daemon run
```

系统管理器不可用时，后台运行仍可用，但状态会明确显示自启动未启用。不要同时启动两个进程占用同一守护目录。

## 默认登录后自启动

| 系统 | 使用方式 | 运行身份 |
| --- | --- | --- |
| Windows | 当前用户登录触发的无窗口计划任务 | 当前用户，普通权限 |
| macOS | LaunchAgent | 当前用户 |
| Linux | systemd 用户服务 | 当前用户，不默认开启 linger |

这是**用户登录后启动**，不保证机器无人登录时已运行。它方便复用该用户的 Codex、Claude Code 或 OpenCode 配置和登录。需要未登录也运行时，自行配置适当运行用户和进程管理器。

```sh
chiliarch daemon autostart status
chiliarch daemon autostart disable
chiliarch daemon autostart enable
```

禁用自启动保留服务数据和配对身份，仍可以 `daemon start` 手动运行。自启动登记和健康检查分别报告，设置完成后以 status 的实际结果为准。

这三种自启动方式都在后台运行，无需保留终端。Windows 更新、启动原生 agent 和执行 GitHub 仓库命令也不会额外弹出控制台。旧版 Chiliarch 的 Windows 启动项在新版首次运行时自动调整，保留原环境和禁用状态；若当次窗口已由旧启动器打开，下一次重启即使用新方式。详见[自动升级](upgrading.md)。

启动项刷新在守护进程内独立执行，不阻塞回环健康接口就绪。Windows 只连接本机当前用户的 Task Scheduler，再按当前守护目录对应的精确任务名读取状态，不枚举全机任务。健康状态为正常并不代表启动项已刷新成功；失败仍记录 `daemon.startup_refresh_failed`。关闭时会等待已开始的刷新结束后再释放目录锁，重复取消不会把仍在执行的原生命令遗留到目录所有权之外；每条原生命令仍保留 30 秒超时。

你手动执行 `daemon run` 时仍在前台运行，直接关闭它的终端会中断服务。需要停止时优先在另一个终端执行 `chiliarch daemon stop`；要长期运行则使用 `daemon start`，确认健康后可关闭发出命令的终端。

## 目录与本地通信

| 系统 | 默认守护目录 |
| --- | --- |
| Linux | `~/.local/state/chiliarch/daemon` |
| macOS | `~/Library/Application Support/chiliarch/daemon` |
| Windows | `%LOCALAPPDATA%\chiliarch\daemon` |

Linux 尊重绝对路径的 `XDG_STATE_HOME`。目录保存 daemon.json、发现信息、登记与日志；每个 agent 的凭证和消息另存到配对时选择的 SQLite 文件。

MCP 只访问本机回环接口，配置中使用本机标识与守护目录，不包含平台凭证。本地端口可自动选择，不需对外开放。

独立目录也支持，但所有命令保持一致：daemon 管理使用 `--directory`，agent 的 pair / config / doctor / mcp 使用 `--daemon-directory`。例如：

```sh
chiliarch daemon install --directory "/你的私有目录/daemon"
chiliarch agent config --host codex --agent codex-main --daemon-directory "/你的私有目录/daemon"
```

配对与登记时也需要传同一目录。不要把私有配置、发现信息或状态库放进共享仓库。

## 注册当前 agent

首次接入用 `chiliarch agent pair`，自动配对、生成 runner 设置并登记，完整流程见[接入指南](agents/index.md#配对与本地身份)。每个身份使用独立 `--agent` 标识和状态文件。

已有配对状态、修改设置，或配对后登记失败时用 register，**不用重新兑换注册码**：

```sh
chiliarch daemon register --state "/你的私有目录/codex-main.db" --agent codex-main --settings "/你的私有目录/codex-main.db.runner.toml"
chiliarch daemon agents
```

相同状态和设置重复登记幂等，冲突会拒绝。先等该身份的任务结束再更新，因为更新会重启它的连接和 runner。保留原来自定义并发、传输与目录选项。

带 settings 登记为 managed；省略则是 manual。正常首次配对已经登记，无需多做一次。不要同时用 standalone MCP 或 agent run 连接 daemon 管理的同一状态库。

## 当前会话与托管执行

| 模式 | 收到调度后 |
| --- | --- |
| managed（默认） | Runner 自动启动本机原生工具并报告结果 |
| manual | 当前会话主动用 MCP 领取、执行和报告 |

两种模式都能通过 MCP 读信、联系总管、查询同伴与协调。当前会话不会因后台收信自动被唤醒；Codex App 的无人值守工作使用同机独立 Codex CLI，不操纵 App 窗口。

原生模型、认证、项目指令与权限按本机设置加载，任务只额外获得自己的 Chiliarch mailbox。平台 ask / auto / yolo 不替代原生审批。受支持的 Codex/OpenCode 原生交互可在平台待处理入口答复；其他原生交互仍需在宿主处理。

任务可以选择已有绝对工作目录；同一实际目录的 Chiliarch 托管任务互斥。锁不影响手动 App、其他用户或外部程序。不要删除锁文件来并行写同一项目。

取消、超时或退出会清理整个原生进程树。清理结果未知或意外断电时，工作区保留隔离标记，后续任务不会直接运行。先只读查看：

```sh
chiliarch agent workspace-recover --workdir "/实际工作目录"
```

**亲自确认原生进程与子进程停止，并检查实际改动后**，才能解除：

```sh
chiliarch agent workspace-recover --workdir "/实际工作目录" --confirm-stopped
```

恢复命令不会检查或终止进程，也不会重做任务；仍被 Chiliarch worker 占用时会拒绝解除。文件锁是否存在不能替代任务或隔离状态检查。

成功托管会话可以通过 `continue_task_id` 安排后续轮次，只允许同身份、同适配器、同目录的最近成功任务。失败或结果未知后先核查，不能借更早成功任务绕过恢复。详见[任务与协作](coordination.md)。

## 原生配置目录与环境

服务以安装它的当前用户运行，保存安装时的 PATH 与非秘密配置目录变量，例如 `CODEX_HOME`、`CLAUDE_CONFIG_DIR`、`OPENCODE_CONFIG`、`OPENCODE_CONFIG_DIR` 和 XDG / Windows 用户目录变量。项目指令由实际工作目录发现。

服务不会自动获取新终端的环境，也不复制 API Key、source shell 配置或改写原生认证。OpenCode 的 `OPENCODE_CONFIG_CONTENT` 可能含秘密，不保存在自启动项；需要持久配置时使用原生私有文件与配置路径。

若原生程序后来安装到新的 PATH，或你更改了配置目录，先排空所有托管工作，在具有正确环境的终端重新安装启动项：

```sh
chiliarch daemon uninstall
chiliarch daemon install
chiliarch daemon status
```

两次使用同一守护目录，独立目录每次都传原 `--directory`。卸载会停止本机全部身份，但保留登记、状态库与日志。自动升级启动方式只更新程序管理的启动逻辑，start / restart / install 不会用新终端变量覆盖已有环境快照。

自启动也保存 `CHILIARCH_AUTO_UPDATE` 和已设置的 uv 路径变量；更新开关改变时同样按上述步骤刷新。前台运行则从预期环境重新启动即可。

## 状态、维护与卸载

| 命令 | 用途 |
| --- | --- |
| `chiliarch daemon status` | 本机健康、版本与系统服务状态 |
| `chiliarch daemon agents` | 各身份连接、模式、任务与可公开错误 |
| `chiliarch agent doctor --agent codex-main` | 只读检查本机登记、原生路径与设置 |
| `chiliarch daemon stop` / `start` | 停止 / 启动共享服务 |
| `chiliarch daemon restart` | 重启全部身份，应用新运行版本 |
| `chiliarch daemon remove --agent codex-main` | 移除本机这一身份的登记 |
| `chiliarch daemon uninstall` | 移除当前用户服务和自启动 |

stop / restart / uninstall 会停止全部托管工作，先等任务结束。remove 只影响指定身份，保留状态库，不吊销平台凭证；彻底断开还需在平台吊销。手动 MCP 会话里的工作仍由原生宿主负责，不会强行关闭 App。

健康不代表平台在线或原生登录有效。执行状态常见值：

| 状态 | 含义 |
| --- | --- |
| `idle` | 托管身份空闲 |
| `busy` | 有任务正在执行 |
| `waiting_for_claim` | Manual 身份等待当前会话领取 |
| `blocked` | 目录占用、旧执行未确认或其他本机阻塞 |
| 离线 | 平台近期未收到活性记录，不能证明进程停止 |

当前用户登录服务跨 Windows、macOS、Linux 工作。Windows runner 支持原生 exe、标准 npm 安装入口和明确的 Python wrapper；自定义 cmd / bat 被拒绝时指定实际可执行入口，不拼接任务到 shell 命令。

默认日志位于守护目录的 `logs/chiliarch.jsonl`，配置与轮换见[日志说明](configuration.md#日志存储与轮换)。升级见[升级指南](upgrading.md)，备份时保留守护目录、每个身份的状态库和 runner 设置。

### Windows 升级后仍出现窗口

先运行 `chiliarch daemon status`。若出现 `daemon.startup_refresh_failed`，说明后台启动项未能更新；服务继续运行不代表启动方式已更新成功。日志中的 `reason` 会区分启动参数、用户身份、运行权限不匹配，以及系统命令或文件访问失败，并记录异常类型和调用位置，不记录启动参数里的环境数据。

Windows 任务计划程序可能省略默认的 `RunLevel` 字段；缺失表示普通权限，不能据此认定启动项属于其他应用。从 v0.4.9 起，Chiliarch 接受这种正常导出，仍拒绝提权任务、其他用户及不匹配的启动命令。新版启动成功更新任务后，空闲时执行 `chiliarch daemon restart` 应用无窗口方式，无需删除数据或重新配对。

若仍提示启动项不匹配，请保留原任务并提供 status 输出及日志里的 `reason`。不要通过删除所有任务或提升为管理员运行来绕过身份检查。


## GitHub 工作区与原生交互

登记项目并绑定 agent 后，守护进程在自身数据目录的 `projects/` 下保存专用仓库、工作区、命令 journal 和发布验证目录。它使用当前用户的 Git/gh 登录与签名配置。各身份仍独立配对；同一守护进程集中执行仓库机械操作，不需要额外模型或入站端口。

已停止且满足安全清理条件的工作区可以通过 `cleanup_project_workspace` 删除；脏文件、仍在运行的任务和结果未知的操作会阻止清理，不强制移除。

Codex 支持命令/文件许可及信息问题的转发，OpenCode v1/v2 支持 ACP 权限请求。平台的原生请求入口收到请求后，按原选项答复；这不改变用户原生权限配置。Claude 当前 print 通道、Codex 扩展权限配置或其他未支持的原生交互仍需在宿主处理，不能静默批准。运行中取消会关闭等待并清理原生进程，不重开原生会话。
