# 配置与日志

通常在 WebUI 的“配置”页面修改，保存后会提示成功或具体错误。完整字段见[示例 TOML](chiliarch.example.toml)；首次安装使用 `chiliarch init`，示例里的空密码不能直接用于启动。

## 常用设置在哪里改

| 你想改什么 | 设置位置 | 生效时间 |
| --- | --- | --- |
| 总管模型、Key、格式、上下文窗口 | 配置 → 总管模型 | 后续模型运行 |
| ask / auto / yolo | 对话页或配置 → 总管 | 后续操作，旧许可不会自动批准 |
| 登录密码 | 配置页的密码修改 | 立即使已有登录失效，重新登录 |
| 监听地址、端口 | 配置 → 服务 | 重启平台 |
| 浏览器公开地址、HTTPS cookie | 配置 → 服务与认证 | 保存后用于后续请求 / 登录 |
| 数据库位置 | 配置 → 数据库 | 重启平台 |
| 日志级别、轮换大小与份数 | 配置 → 日志 | 热重载 |
| Agent 默认项目目录 | Agent 机器上的 runner TOML | 重新登记该身份 |
| Agent 登录自启动 | `chiliarch daemon autostart` | 见[守护进程](daemon.md#默认登录后自启动) |

## 优先级与路径

平台读取设置的顺序为：默认值 → TOML → 环境变量 → 本次 `serve` 的监听参数。环境变量优先于配置文件；修改文件却没有生效时，先核对实际启动环境。

uv 工具安装默认使用用户目录中的 `chiliarch/server/chiliarch.toml`，[各系统路径](deployment.md#配置文件位置)不同。`init` 会打印绝对路径。显式 `--config` 优先；源码运行默认当前目录。

数据库和日志的相对路径以配置文件目录为基准，与你从哪个终端目录启动无关。总管的 Key 可以只放在环境变量中：

Linux / macOS：

```sh
export CHILIARCH_MODEL__API_KEY='你的模型密钥'
chiliarch serve
```

Windows PowerShell：

```powershell
$env:CHILIARCH_MODEL__API_KEY = '你的模型密钥'
chiliarch serve
```

示例 Key 请替换；避免将带有真实秘密的命令留在共享记录中。服务管理器运行的平台需在服务环境中设置变量，修改另一个终端不会改变已有进程。

环境变量格式为 `CHILIARCH_<SECTION>__<FIELD>`，例如 `CHILIARCH_MODEL__CONTEXT_WINDOW`。对象设置使用 JSON。通过环境变量提供的 Key 不会因 WebUI 保存配置写回 TOML。

## 保存与热重载

保存会先校验整份设置。修改配置文件时，默认每 2 秒检查一次；无效内容会保留上一份有效配置并记录错误，修正后重新加载。

模型、审批模式、通信时间参数、公开地址和日志支持热重载。已经运行的模型调用继续使用它开始时的设置，后续运行使用新值。

以下字段需重启：`server.host`、`server.port`、`server.graceful_shutdown_seconds`、`database.url`。配置页面显示当前有效值，保存时提示哪些设置需重启。停止平台后用原启动命令重新运行即可。

API Key 输入留空会保留已有值，需要清除时使用对应清除选项。页面隐藏敏感值；原样保留 `********` 不会把它当作新 Key。自定义请求头和额外参数作为完整映射保存，提交空对象会清除旧键。

密码只保存 scrypt 哈希，不手填明文到 `auth.password_hash`。POSIX 配置权限为 0600，Windows 使用私有目录继承的 ACL。

也可以让总管读取或修改配置。总管的 `get_config`、`update_config` 返回完整有效值，包括 API Key、请求头、额外参数和已有 `auth.password_hash`；`get_platform_state` 还返回配置版本、热重载错误与等待重启的设置。它们不会把值改成页面使用的 `********`。这些工具数据会进入你配置的模型并保存在聊天记录中，配置页的掩码展示保持不变。修改配置仍按 ask / auto / yolo 处理，密码只能通过哈希验证，无法从已存哈希找回明文。

## 字段说明

### server / auth / database

| 字段 | 默认值 / 用途 |
| --- | --- |
| `server.host` / `port` | `127.0.0.1` / `8000`，监听地址与端口 |
| `server.public_url` | 浏览器实际访问的公开 origin，可留空 |
| `server.config_poll_seconds` | `2`，检查配置文件修改的间隔 |
| `server.graceful_shutdown_seconds` | `15`，停止时等待 HTTP/SSE 请求结束的上限秒数 |
| `auth.password_hash` | 初始化或修改密码生成，不填写明文 |
| `auth.session_ttl_seconds` | `86400`，登录会话有效秒数 |
| `auth.max_login_attempts` / `login_window_seconds` | `10` / `300`，同来源登录限流 |
| `auth.secure_cookie` | false；通过 HTTPS 访问时启用 |
| `database.url` | `sqlite+aiosqlite:///data/chiliarch.db` |

公开地址不含路径。HTTPS 由反向代理提供，填写公开地址不会启动 TLS。初始化时 HTTPS 地址会自动开启 secure cookie，以后两项分别保存；切换访问方式需要一起核对。

关闭请求等待达到上限后继续清理资源；它不限制整个退出流程，也不跳过后台任务和 agent 进程树清理。平台只支持单实例使用业务数据库。

### supervisor / model

总管的格式、思考参数、输出 tokens 和审批说明见[模型指南](models.md)。常用字段：

| 字段 | 默认值 / 用途 |
| --- | --- |
| `model.api_mode` | `chat_completions`，另有 `responses`、`anthropic` |
| `model.model` / `base_url` / `api_key` | 模型 ID、API 根地址、密钥 |
| `model.context_window` | `200000`，90% 时自动压缩 |
| `model.anthropic_max_tokens` | 未配置；仅 Anthropic 必填 |
| `model.timeout` | `120`，模型请求超时秒数 |
| `model.retries` | `2`，额外重试次数，即最多请求 3 次；0 不重试 |
| `model.headers` / `request_params` | 自定义请求头与当前 API 支持的额外参数 |
| `model.enable_thinking_param_enabled` | true；关闭后省略 enable_thinking |
| `supervisor.mode` | `auto`，另有 `ask`、`yolo` |
| `supervisor.run_timeout` | `600`，一段活跃运行的超时秒数，审批等待不计入 |
| `supervisor.event_poll_seconds` / `inbox_poll_seconds` | `0.25` / `1`，事件与来信检查间隔 |
| `supervisor.task_stall_seconds` | `300`，任务长期无更新时通知总管核查 |
| `supervisor.system_prompt` | 总管角色与协调提示，通常保持默认 |
| `supervisor.model_recovery_attempts` | `3`，临时模型故障的后续核查次数；0 关闭 |
| `supervisor.model_recovery_base_seconds` / `model_recovery_max_seconds` | `5` / `60`，恢复退避的首次间隔与上限 |

`model.retries` 同时覆盖建立连接时的错误和流式响应中的 API 错误，适用于普通回答、上下文压缩与自动命名。尚未显示正文或公开思考的临时故障才会重试；无效参数、认证错误及截断不自动重试。平台统一控制尝试预算，不叠加 SDK 重试。详见[模型请求重试](models.md#模型请求重试)。

`supervisor.model_recovery_attempts` 是自动协调在请求预算用尽后的新核查轮次，不重放旧工具，与 `model.retries` 独立。修改模型配置不会改变本机 agent 的原生设置。

### transport

正常连接保持默认即可。平台和接入客户端的设置分别管理，服务端不会自动把所有这些值下发给任意插件。

| 字段 | 默认值 / 用途 |
| --- | --- |
| `heartbeat_seconds` | `15`，参考客户端心跳间隔 |
| `offline_after_seconds` | `60`，活性超过此时间显示离线，需大于心跳间隔 |
| `poll_timeout_seconds` | `25`，长轮询等待秒数，最长 60 |
| `retry_seconds` | `5`，队列检查与首次重投等待 |
| `queue_limit` | `10000`，未确认消息数上限 |
| `message_ttl_seconds` | `86400`，下行消息有效秒数 |
| `max_message_bytes` | `1048576`，单条信封最大字节数 |
| `reconnect_base_seconds` / `reconnect_max_seconds` | `1` / `30`，断线重连退避范围 |
| `upgrade_interval_seconds` | `300`，降级后重新尝试 WS 的间隔，**不是软件更新间隔** |

消息未确认时按条退避重投，重连保留原消息 ID。ACK 表示持久接收，不代表任务执行成功。协议细节见[开发参考](protocol.md)。

### artifacts

| 字段 | 默认值 / 用途 |
| --- | --- |
| `max_artifact_bytes` | `67108864`（64 MiB），单文件上限 |
| `chunk_bytes` | `65536`（64 KiB），分块大小，还受信封大小限制 |
| `max_total_bytes` | `1073741824`（1 GiB），所有产物清单的合计额度 |

未完成上传也预占额度。降低配额不会删除已有文件；超额后拒绝新上传。该额度不是数据库文件的总大小限制，消息历史、索引和备份还会占空间。废弃上传或删除产物的操作见[共享文件](artifacts.md)。

### 本机 daemon 配置

Agent 机器的 `daemon.json` 独立于平台 TOML，保存在[守护目录](daemon.md#目录与本地通信)。它管理本机登记、监听、日志与传输；本地端口默认自动选择，接口只绑定回环地址。

新增或更新身份使用 `daemon register`，可以即时生效。直接编辑 daemon.json 后需要重启；本机设置不受平台热重载控制。重启会停止所有托管任务，先确认它们空闲。

顶层 `graceful_shutdown_seconds` 默认 15 秒，控制本地 HTTP 请求关闭等待，不跳过任务清理。配置与运行时发现文件包含本地认证信息，不要共享或放进版本库。

## 本机 runner 与原生配置

每个托管身份有自己的 runner TOML。自动生成文件是完整状态库名加 `.runner.toml`，例如 `codex-main.db.runner.toml`。

| 字段 | 默认值 / 用途 |
| --- | --- |
| `kind` | 必填：`codex`、`claude-code`、`opencode` 或 `opencode-v1` |
| `workdir` | 必填，已有默认目录；相对路径以 TOML 所在目录为基准 |
| `executable` | 可选的原生程序路径；未填时查找对应工具 |
| `timeout_seconds` | `1800`，单次任务运行时限 |
| `terminate_grace_seconds` | `2`，结束原生进程树的宽限秒数 |
| `max_input_bytes` | `1048576`，输入字节上限 |
| `max_output_bytes` | `524288`，输出字节上限 |
| `max_line_bytes` | `262144`，单行输出字节上限，不大于总输出上限 |

这些字节设置是本机通信与资源限制，不是模型 token 上限。并发和起始传输通过 `daemon register` 参数管理。修改后等待任务结束，用原状态库、标识与设置路径重新登记，见[目录配置](agents/index.md#默认目录与任务目录)。

runner 不提供模型或权限覆盖。原生模型、provider、认证、沙箱、审批、项目指令、skills、hooks、插件和原有 MCP 按宿主规则加载；仅追加当前任务专用的 Chiliarch mailbox。实际配置发现取决于用户、工作目录和[服务环境](daemon.md#原生配置目录与环境)。

平台调度的 `workdir` 可选择 agent 机器上的其他已有绝对目录。目录只是启动位置，不是额外沙箱；续接仍需原目录和最近成功任务，见[接入指南](agents/index.md)。

## 日志存储与轮换

| 日志 | 默认位置 |
| --- | --- |
| 平台 | 配置目录下 `logs/chiliarch.jsonl` |
| 守护进程与 daemon MCP | 守护目录下 `logs/chiliarch.jsonl` |
| 独立 MCP / runner | 状态库旁的 `logs`，可用 `--log-directory` 指定 |
| 启动更新器 | 用户状态根的 `updates/<安装标识>/update.log` |

应用日志是 UTF-8 JSON Lines，时间为 UTC，记录启停、请求、配置、审批、连接与任务等事件。配置页和总管 `get_logs` 可读取当前日志末尾，最多 1000 行。

`logs.level` 默认 INFO。`logs.max_bytes` 默认 10485760（10 MiB），`logs.backup_count` 默认 10；达到大小后轮换为 `.1`、`.2` 等，删除最旧份。stderr 同步输出日志，MCP stdout 专用于协议。

更新器日志单独按 1 MiB 轮换，保留 2 份备份，不受平台日志设置控制。

运行日志与操作审计继续对密码、Key、cookie、认证头及文件块等秘密内容脱敏；异常记录类别与栈位置，避免保存可能含认证信息的原始错误正文。总管工具数据与聊天记录保留原值，不使用日志的脱敏展示规则，也不会恢复之前已脱敏记录中的值。聊天、任务和审计是数据库里的业务记录，**不会随日志轮换删除**。数据库目前没有自动清理全部历史的保留策略，应定期备份并关注磁盘空间。

## 启动更新开关

`CHILIARCH_AUTO_UPDATE=0` 关闭新版本检查和安装，属于启动环境设置，不在 WebUI 热重载配置中。已有自启动服务需要刷新保存的环境，见[升级指南](upgrading.md#关闭自动更新)。
