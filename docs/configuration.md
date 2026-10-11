# 配置、热重载与日志

完整默认字段见 [示例 TOML](chiliarch.example.toml)，权威验证模型是 `chiliarch.config.Settings`。使用 `chiliarch init` 生成真实配置，示例中的空密码哈希不能直接启动服务。

## 优先级与路径

默认值 → TOML → `CHILIARCH_<SECTION>__<FIELD>` 环境变量 → 本次 CLI 的监听参数。环境变量可提供 JSON 对象；数值和布尔值按模型校验。例如 `CHILIARCH_MODEL__API_KEY`、`CHILIARCH_MODEL__HEADERS`。

相对日志路径和 SQLite 路径相对于配置文件所在目录解析，不依赖进程从哪里启动。未指定配置文件的嵌入式应用则以创建应用时的当前目录为基准。

密钥可只通过环境变量提供，保存 WebUI 配置不会把环境变量密钥写回 TOML。环境变量在进程启动时提供；修改另一个终端里的变量不会改变已有进程的环境。

## 保存与热重载

WebUI 修改先校验整份候选配置，然后将临时文件 fsync 后原子替换；POSIX 还会同步父目录，Windows 不使用不受支持的目录文件描述符。POSIX 配置权限为 0600；Windows 继承配置所在目录的 ACL，请保存在当前用户私有目录。文件监视按 `server.config_poll_seconds` 检查修改；无效 TOML、未知字段、错误类型或不合理参数均保留上一份有效配置。

有效更新增加配置 revision。新任务/模型调用使用新快照；运行中调用保留原快照。模式与审批仍在统一操作服务实际执行时检查，不能通过修改配置把已发起的待审批操作自动批准。

`server.host`、`server.port`、`server.graceful_shutdown_seconds`、`database.url` 保存后需要重启。WebUI 表单显示当前有效配置，保存时提示哪些修改需要重启；配置版本与待重启值可通过 `/api/state` 的 runtime 查询。配置页面保留校验失败和保存结果提示，不常驻展示热重载状态面板。`server.public_url`、模型、模式、投递时间参数、日志策略等支持热重载。

配置表单在工作区内居中，长表单可以滚动。左侧导航支持收起为图标栏，收起后仍可切换页面、主题和退出登录；展开偏好保存在当前浏览器。小屏幕用同一按钮收起或展开导航。

`model.headers` 与 `request_params` 是完整映射，提交 `{}` 可以清除旧键。读取 API Key、请求头、密码哈希与嵌套敏感参数时使用 `********`；原样回传掩码保留原秘密。空 API Key 明确清除；不提交该字段保持原值。

## 字段说明

### server / auth / database

| 字段                                           | 用途                                                     |
| ---------------------------------------------- | -------------------------------------------------------- |
| server.host / port                             | 监听地址和端口，需重启                                   |
| server.public_url                              | 浏览器公开 origin，用于反向代理后的 Origin 校验          |
| server.config_poll_seconds                     | 文件配置检查间隔                                         |
| server.graceful_shutdown_seconds               | 关闭时等待 HTTP/SSE 连接与请求结束的上限，正整数秒，默认 15，需重启 |
| auth.password_hash                             | init/密码修改生成的 scrypt 哈希，不手填明文              |
| auth.session_ttl_seconds                       | 登录会话有效期；新会话采用新值                           |
| auth.max_login_attempts / login_window_seconds | 同来源登录限流                                           |
| auth.secure_cookie                             | 新签发的 cookie 是否仅随 HTTPS 发送；HTTP 部署保持 false |
| database.url                                   | 默认 SQLite async URL，改动需重启；初版验收存储为 SQLite |

UI/API 修改密码会使现有会话失效。浏览器变更操作需要 cookie 和 `X-CSRF-Token`；登录会校验 Origin。初版为单用户，没有多用户、角色管理或默认管理员密码。

侧栏底部的主题按钮按“跟随系统 → 浅色 → 深色”循环切换。默认跟随系统，选择保存在当前浏览器中；返回跟随系统后继续响应系统明暗变化。

浅色主题使用柔和浅底，暗色主题采用炭灰背景与暖白文字，以不同的中性表面区分侧栏、消息、输入区和工具详情；蓝金色用于按钮、焦点及状态强调。主题偏好只保存在浏览器，不写入服务端 TOML。

界面的抽屉、消息和折叠反馈遵循操作系统的“减少动态效果”设置。聊天输入支持 Enter 发送、Shift + Enter 换行及 Ctrl / ⌘ + Enter 发送；阅读历史时暂停自动跟随，可通过“回到最新”恢复。具体交互见 [模型与总管行为](models.md#对话页面与键盘)。

### transport

| 字段                                           | 用途                                               |
| ---------------------------------------------- | -------------------------------------------------- |
| heartbeat_seconds                              | 参考客户端主动心跳间隔                             |
| offline_after_seconds                          | 超过最后主动活性记录多久显示离线，必须大于心跳间隔 |
| poll_timeout_seconds                           | 长轮询最大等待，最长 60 秒                         |
| retry_seconds                                  | 队列检查间隔及未 ACK 消息首次重投等待              |
| queue_limit                                    | 持久未确认消息数量上限                             |
| message_ttl_seconds                            | 下行消息有效期，过期后显式停止交付                 |
| max_message_bytes                              | 单条信封大小上限                                   |
| reconnect_base_seconds / reconnect_max_seconds | 客户端退避范围，加入抖动                           |
| upgrade_interval_seconds                       | 降级传输定期重新尝试 WS 的间隔                     |

这些客户端字段只有使用对应配置启动参考客户端时才影响其行为。跨语言插件应使用自己的配置或协商默认值；本版不自动向任意插件推送所有服务端设置。

客户端出站和服务端 WS/SSE 下行对每条未确认消息独立指数退避，首次间隔为 `retry_seconds`，封顶为 `max(retry_seconds, offline_after_seconds)`。这使接收与持久化较慢时不会持续堆积相同消息；新消息仍可及时发送，重连后使用原消息 ID 立即恢复投递。退避不改变离线判定、消息有效期或 ACK 的含义。

真实 agent 的 `chiliarch agent` 与 `chiliarch daemon` 命令独立于服务器 TOML，不需要 `chiliarch init` 或 WebUI 密码。默认守护目录由当前用户与操作系统解析，目录内登记独立身份及可选 runner 设置；`agent pair/config/doctor/mcp` 用 `--daemon-directory`，管理命令用 `--directory`，保持同一目录。推荐 MCP 使用 `--agent` 引用本机登记，避免每个会话建立平台连接；`doctor` 检查本地健康与登记，不隐式启停。`--state` 指定私有 SQLite 凭证与消息库，`--transport` 选择起始传输。CLI runner 的 `--settings` 是另一份机器本地 TOML，字段由 `RunnerSettings` 校验；工作目录相对于该文件解析，普通任务参数不能修改启动程序或权限；只有正式调度的 `workdir` 字段可以覆盖本轮目录。runner 设置启动时读取，修改后通过 `daemon register --settings <路径>` 更新登记；本机 daemon/runner 设置不受平台服务器热重载影响。standalone MCP 和独立 `agent run` 不与守护身份同时占用状态库。具体示例见 [接入文档](agents/index.md)。

### artifacts

跨机器产物经已有出站请求分块存入 SQLite，不要求服务端访问 agent 的文件路径或 URL。配置支持热重载：

| 字段 | 默认值 | 用途 |
| --- | --- | --- |
| max_artifact_bytes | 67108864（64 MiB） | 新上传的单文件上限 |
| chunk_bytes | 65536（64 KiB） | 新上传的分块大小；自动适配 `transport.max_message_bytes` 的信封开销和 base64 扩张 |
| max_total_bytes | 1073741824（1 GiB） | 所有产物清单声明大小的合计配额；未完成上传也预占额度 |

已开始上传的清单、成员、SHA256、分块大小保持不变；降低配额不会删除已有内容，超额后拒绝新的上传。`max_total_bytes` 不是 SQLite 磁盘大小上限：出入站消息、请求历史、索引和备份也占用空间。废弃上传使用 `discard_artifact_upload`，删除已发布内容使用需审批的 `delete_artifact`；这些操作释放产物清单配额，但不回收已交付副本、传输历史或既有备份，也不承诺物理擦除。见 [产物共享](artifacts.md)。

### 本机 daemon 配置

共享守护目录中的 `daemon.json` 是独立私有配置，不属于平台 `chiliarch.toml`。它保存格式版本、agent 登记、本地调用凭证、监听端口、启动超时、HTTP 关闭等待和日志/传输策略；`port=0` 让系统选择可用回环端口，发现信息供本机客户端读取。不要把该文件、运行时发现信息或每个 agent 的 SQLite 状态库写入仓库或共享给其他用户。

顶层 `graceful_shutdown_seconds` 为正整数秒，默认 15，修改后重启守护进程生效。它与平台同名设置均只限制停止时等待连接与 HTTP 请求结束的阶段：超时后取消剩余请求，再继续应用资源清理。这不是整个退出过程的总时限，也不会跳过或缩短 agent 任务和子进程的清理。

新增或更新单个身份使用 `daemon register`，设置独立状态路径、可选 runner TOML、并发和起始传输，登记后即时生效。直接修改 `daemon.json` 需重启该守护进程；它没有平台配置的文件热重载功能。重启会取消本机托管工作并等待进程树清理，未知结果不会自动再次执行。具体管理命令见 [本机守护进程](daemon.md)。

### supervisor / model

`supervisor.mode` 为 ask/auto/yolo；`run_timeout` 限制每段活跃执行的时间（审批等待不计入，批准恢复后重新计时），`inbox_poll_seconds` 决定持久下级 inbox 检查间隔，`event_poll_seconds` 决定聊天事件检查间隔，`system_prompt` 为总管角色提示。

任务长期不更新由 `supervisor.task_stall_seconds` 控制，默认 **300 秒**；失联仍使用 `transport.offline_after_seconds`（默认 60 秒）。服务端维护循环按 `transport.retry_seconds` 扫描，仅生成持久核查事件，不认定任务失败，也不重新派发。每个任务进度时间、每次 agent 活性时间分别去重；进度恢复或重连后可开启新的监护周期。未配置总管模型或会话暂停时事件仍保留。

模型传输故障的恢复策略也可热重载：

| supervisor 字段 | 默认值 | 用途 |
| --- | --- | --- |
| model_recovery_attempts | 3 | 单次连续故障最多新增的核查轮次，范围 0–10；0 禁用 |
| model_recovery_base_seconds | 5 | 首次恢复等待秒数，此后按指数退避 |
| model_recovery_max_seconds | 60 | 单次等待上限，必须不小于 base |

这与 `model.retries` 不同：后者是 SDK 请求层重试；总管恢复在原轮次已失败后另开核查轮次，携带已有历史、当前任务快照与操作结果，不直接重放工具。仅连接/传输异常、HTTP 408/429/5xx 可自动恢复；认证错误、无效参数、模型返回不完整、逻辑错误和用户停止仍要求用户处理。失败次数与下次核查时间持久保存，重启不会重置预算。到达上限后自动协调暂停，用户新消息或明确“继续协调”可恢复。用户停止可撤销等待中的恢复；新消息会取代旧恢复计划。

`model.context_window` 为正整数，默认 **200000 tokens**；填写服务商模型实际支持的上下文窗口。达到 **90%** 时，总管自动将历史压缩成接续摘要，保留最近的完整调用与结果，并继续当前工作。页面展示上下文占用，估算值标注“约”；压缩不会删除完整聊天记录。优先使用服务商当前轮次的输入和输出用量，缓存输入同样计入窗口；没有 usage 时采用本地估算。修改窗口热重载，后续新运行使用新值，已运行或等待审批的执行仍使用自己的模型配置快照。

平台不设置最大工具轮次、上下文消息条数或全局并行运行上限；同一会话仍只能有一个活跃运行，运行超时与审批规则继续生效。并行能力受机器资源及模型服务的限流约束。

默认不发送最大输出 token 参数，使用服务端默认输出长度。需要服务商特有上限时，在 `model.request_params` 显式配置；**原生 Anthropic Messages 要求 `max_tokens`，须在该参数表中填写对应模型支持的值**。兼容网关若支持省略，可保持参数表为空。

模型字段的 API 映射、压缩过程与失败处理见 [模型文档](models.md#上下文窗口与自动压缩)。

## 本机 runner 与原生配置

托管 agent 的本地 runner TOML 独立于平台 `chiliarch.toml`。平台 `[model]` 配置只供 Chiliarch AI 总管使用，不会下发给 Codex／Claude，也不会覆盖它们的模型、服务商或认证。runner 字段如下：

| 字段 | 默认值 | 用途 |
| --- | --- | --- |
| kind | 必填 | `codex` 或 `claude-code` |
| workdir | 必填 | 已存在的默认项目目录；相对路径按 runner TOML 所在目录解析；任务可显式指定其他已有绝对目录 |
| executable | 未指定 | 可选的本机程序路径；否则查找对应原生工具 |
| timeout_seconds | 1800 | 一次托管任务最长运行秒数 |
| terminate_grace_seconds | 2 | 终止原生进程树时的宽限秒数 |
| max_input_bytes | 1048576 | 输入大小上限，字节 |
| max_output_bytes | 524288 | 输出总量上限，字节 |
| max_line_bytes | 262144 | 单行输出上限，字节，不得大于输出总量上限 |

这些字节限额用于本地协议与资源控制，不是模型输出 token 设置。并发与传输通过 `daemon register` 参数配置。runner 不提供模型或权限覆盖；模型、provider、沙箱、审批、工具规则、原生指令、skills、hooks 与 MCP 均按对应原生工具的配置和优先级加载。默认 `workdir` 在任务未指定目录时使用；本轮目录决定启动位置及项目配置发现位置，不构成额外操作系统沙盒。

`agent pair` 默认 `--mode managed`，`--workdir <已有目录>` 自动生成 runner TOML 并登记；也可用 `--settings <TOML>`，都省略时交互询问目录。仅当前会话手动领取时选择 `--mode manual`。修改默认目录后重新 `daemon register --settings` 生效，不受平台热重载影响。调度 `dispatch_task` 与 `retry_task` 的可选 `workdir` 是 agent 机器上的绝对路径，保存到任务并经过统一审批；客户端校验目录存在，不自动创建或回退。具体续接、重试和重新登记步骤见 [目录配置](agents/index.md#默认目录与任务目录)。

Codex runner 通过独立本机 `codex app-server` stdio 完成初始化、新建或续接线程与启动回合；Claude Code runner 使用 `--print` JSONL，保留原生配置加载。两者只动态追加本任务唯一名称的 Chiliarch mailbox MCP，任务提示指定该名称，不覆盖用户已有的 `chiliarch` 服务。Codex `thread/resume` 遵循原生语义，可能保留原线程设置，并非每轮强制刷新所有当前用户值。

平台 ask/auto/yolo 只管理平台业务审批，不调整原生权限。当前没有跨平台原生审批或用户输入回复桥；必须交互且无法继续时任务会明确受阻或失败，不自动批准、不偷偷变更审批模式。配置目录与原生认证的可见性取决于守护进程环境；安装时保存的非秘密目录变量、已有服务的刷新步骤见 [守护进程环境](daemon.md#原生配置目录与环境)。用量统计不是收费凭证，订阅与 API 来源由原生工具选择。

完整 TOML 和运行流程见 [Codex](agents/codex.md#托管-cli-执行) 与 [Claude Code](agents/claude-code.md#托管-runner-配置)。修改本地 TOML 后重新执行 `daemon register --settings <路径>` 更新对应身份；先确认正在执行的工作已结束，避免更新时中断托管任务。

## 日志存储与轮换

`logs.directory` 下的 `chiliarch.jsonl` 为 UTF-8 JSON Lines，每行包含 UTC timestamp、level、logger、message 以及事件相关 ID、状态或耗时。记录平台启停、HTTP 路径/状态/耗时、操作执行/审批、agent 连接/消息/任务状态、配置更新和总管运行事件。

`logs.max_bytes` 为单文件最大字节数，`backup_count` 为保留的轮换文件数；总大小约受 `(backup_count+1)×max_bytes` 约束。达到大小后生成 `.1/.2/...` 并淘汰最旧文件。`logs.level` 可热更新。标准错误同时输出结构化日志，便于进程管理器采集。

认证 header、cookie、密码和凭证不记录；敏感键递归脱敏。HTTP 日志不记录原始请求 body。异常记录类别及栈帧的文件、行号、函数名，不保存异常正文或局部变量，防止 SDK 错误回显认证内容。流式每个 token 的正文保存在聊天业务数据中，不逐 token 写应用日志。

SQLite 审计事件、会话消息、任务和操作记录是业务历史，不随应用日志文件轮换删除；数据库应单独备份并监控磁盘。当前版本不提供自动清理所有业务历史的保留策略。WebUI“读取日志”和 `get_logs` 工具读取当前日志末尾，最多 1000 行。

日志包含操作信息，聊天数据库包含用户发送给模型的内容；保护目录权限与备份。秘密参数掩码依据敏感字段名，扩展开发者必须对自定义秘密字段自行脱敏。

共享守护进程与 daemon MCP 默认在守护目录下的 `logs` 写同样的结构化轮换日志；独立 MCP/runner 默认在状态库旁的 `logs`，也可用 `--log-directory` 指定。共享日志使用跨进程锁保护写入与轮换，并重开被其他进程轮换的旧文件；stdio MCP 的 stdout 只用于协议，日志写 stderr；原生 CLI 的 stderr 并发读取、计入输出限额后丢弃，不把认证诊断上传平台。执行结果只包含所需答案、会话身份、数值用量等字段；真实工作生成的敏感内容仍需由工作策略和扩展自行处理。POSIX 上状态库权限为 0600；Windows 使用当前用户私有目录及继承的 ACL，`chmod` 不替代 Windows 权限配置；托管任务的临时原生 MCP 配置在结束时清理，不改全局 agent 配置。
