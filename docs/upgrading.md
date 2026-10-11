# 启动检查与自动升级

从 v0.3.0 开始，通过 `uv tool install chiliarch` 安装的 Chiliarch 默认在启动服务时检查新版。平台与每台 agent 机器独立检查、独立升级，不需要重新配对，也不会升级 Codex、Claude Code 或 OpenCode。

## 什么时候检查和生效

以下入口检查 PyPI 上符合当前 Python 版本和原安装版本约束的正式发布版：

- 平台 `chiliarch serve`。
- 守护进程 `chiliarch daemon run`，包括系统登录自启动。
- `chiliarch daemon install`、`start`、`restart`，以及会启动本机接入服务的 `chiliarch agent pair`、`run`。
- 独立连接模式的 `chiliarch agent mcp --mode standalone`（或只传 `--state` 且省略 `--mode`），以及协议演示客户端 `chiliarch demo-agent`。

帮助、版本查询、状态检查，以及通过 daemon 或 mailbox 复用连接的 MCP 启动不会访问 PyPI。独立 MCP 仅在进程启动时检查，普通工具调用不检查。运行中的服务也不定时下载或替换自己；新代码在下一次启动相应进程时生效。

`daemon start`、`install` 和 `agent pair` 仍优先复用已有健康且本地 API 兼容的守护进程。即使检查到了新版本，也不会强行重启它或打断正在执行的任务。需要让已有 daemon 立即使用新版时，先结束本机全部托管任务，再执行：

```bash
chiliarch daemon restart
chiliarch daemon status
```

独立守护目录要继续传入原来的 `--directory`。`restart` 会停止这个守护进程管理的所有身份及托管任务，确认空闲后再运行。平台则正常停止并重新运行原来的 `serve` 命令；不要同时启动两个实例使用同一数据库。

## 更新如何完成

Chiliarch 使用 uv 将新版安装到当前用户的私有、持久运行环境，先校验安装结果，再原子切换后续启动使用的版本。它不在运行中的 uv 工具环境里覆盖 Python 文件或 Windows 动态库，也不删除旧运行环境，因此现有进程与服务记录的解释器路径仍可继续使用。

版本查询的连接与读取超时为 5 秒；创建运行环境、安装和校验分别有 30、120、20 秒时限。daemon 健康等待为启动准备额外预留最多 180 秒，普通健康探测仍使用短超时。并发启动遇到其他进程正在更新时，使用已经验证的版本，不等待另一个长时间安装。离线、PyPI 不可达、uv 不可用、安装失败或校验失败时，保留上一份可用版本并继续启动；更新失败不等于服务无法启动。运行环境切换不改写平台 TOML、数据库、守护登记、agent 凭证或原生宿主配置。它也不承诺新版本完成数据库迁移后，可以直接降级程序读取同一数据库；重要部署仍应保留 [一致性备份](deployment.md#备份与恢复)。

包来源必须是可识别的标准 uv 工具安装。源码检出、editable、Git/URL 安装及无法确认来源的环境不会自动替换。自定义镜像、额外 constraints/overrides 等安装设置无法按原语义复用时，会跳过自动安装并提示手动升级，不把这些设置静默改为默认值。普通版本范围、extras 及可识别的 registry `--with` 依赖会保留。候选版本会排除预发布、开发版本、已撤回文件和不支持当前 Python 的发行文件；缺少可安装的二进制包或依赖时保持原版本。自动升级选择正式版本，不自动安装预发布版本，并保留原来安装时的版本限制；例如 `chiliarch==0.3.0` 不会自动跳到 0.3.1，`chiliarch>=0.3,<0.4` 只在该范围内升级。

自动更新的运行环境与 uv 最初安装的外层工具环境分开保存。因此 `uv tool list` 可能仍显示最初安装版本；`chiliarch --version` 显示后续 CLI 实际选中的版本，`chiliarch daemon status` 则用于核对已经运行的守护进程。两者暂时不同可能只是已有 daemon 正在继续完成工作。

## 运行环境与更新日志

更新数据放在当前用户默认 Chiliarch 状态根目录的 `updates/<安装标识>/`，与 `daemon` 目录同级；安装标识是原 uv 工具环境绝对路径的 SHA-256 前 24 个字符。即使使用独立 `--directory`，也不会为每个 daemon 复制一套更新环境：同一工具安装共用一个更新选择。各系统默认状态根目录见 [守护目录](daemon.md#目录与本地通信)。

该目录中的 `runtimes/` 保存按版本创建的独立环境，`active.json` 只在安装校验成功后原子更新。校验包含核心模块导入、包版本、WebUI 和协议 Schema；它不启动平台、打开业务数据库或调用原生 agent。旧环境暂不自动清理，避免删除仍被进程或启动项使用的解释器。

更新提示写入 stderr 和同目录的 `update.log`，stdout 保留给 CLI 结果或 MCP JSON-RPC。日志单文件上限 1 MiB，保留当前文件及 2 个轮换文件；不会原样保存安装器输出或带凭证的索引错误。平台和 daemon 的应用日志仍使用各自的日志配置。没有新版时正常启动，不把每次版本查询写成错误。

## 从 v0.2.0 或更早版本启用

旧版本没有启动更新器，需要先手动升级一次。先结束正在执行的任务，正常停止平台和本机 daemon，再运行：

```bash
uv tool upgrade chiliarch
chiliarch --version
```

随后按原来的命令启动平台，agent 机器执行 `chiliarch daemon start`。v0.2.0 的 daemon 尚无独立本地 API 版本字段，升级后必须重新启动才能使用新的兼容机制，不能留下旧 daemon 继续接入。配置、登记与状态库保留，不要重新 `init` 或消耗新注册码。若手动升级改变了系统启动项记录的解释器路径，按 [守护进程维护](daemon.md#原生配置目录与环境) 重新安装启动项。

`uv tool upgrade` 会保留原安装约束；原来固定了旧版本时，需要用 `uv tool install` 显式换成希望的约束，例如：

```bash
uv tool install 'chiliarch>=0.3,<0.4'
```

这里选择的是 0.3 系列。希望继续跟随后续正式版时，可重新安装不带版本限制的 `chiliarch`。[uv 工具升级说明](https://docs.astral.sh/uv/guides/tools/#upgrading-tools)

## 关闭自动检查和安装

在实际启动 Chiliarch 的环境中设置 `CHILIARCH_AUTO_UPDATE=0`。例如 Linux/macOS 当前终端：

```bash
CHILIARCH_AUTO_UPDATE=0 chiliarch serve
```

Windows PowerShell 当前终端：

```powershell
$env:CHILIARCH_AUTO_UPDATE = '0'
chiliarch serve
```

这会关闭本次启动的新版本检查和安装，仍使用此前已经选中的运行版本；它不是降级或恢复命令。删除该环境变量或改为 `1` 可恢复默认行为。平台由 systemd 等管理时，应在对应服务的启动环境设置；修改终端变量不会改变已经运行的服务。

Chiliarch 自己管理的 daemon 自启动项保存安装时的该变量。若服务已安装，后续 `install`、`start` 和 `restart` 不会重写环境快照；应先排空托管任务，再在设置了所需变量的终端按 [服务环境维护](daemon.md#原生配置目录与环境) 重新安装启动项。

这个开关属于启动器，独立于平台 TOML、WebUI 配置和配置热重载。`transport.upgrade_interval_seconds` 只控制降级传输重新尝试 WS 的间隔，与软件升级无关。

## 常见情况

| 情况 | 处理方式 |
| --- | --- |
| 检查或下载失败，但旧版正常运行 | 检查启动提示、网络和 uv；修复后下一次启动会再检查 |
| 新 CLI 与健康 daemon 的软件版本不同 | 本地 API 兼容时可以继续协作；空闲后重启 daemon 应用新版 |
| daemon 不健康或本地 API 不兼容 | 保留数据，核对实际运行版本和服务环境；不要为了安装新 agent 强停忙碌服务 |
| 固定版本后没有自动升级 | 按预期保留了版本约束；需要跨越约束时显式重新安装 |
| 源码 checkout 没有自动更新 | 使用自己的 Git/uv 开发流程；启动更新器不会改写源码 |
| 想清理旧运行环境 | 先确认没有进程或服务定义仍在使用；不要在运行期间手动删除 |

