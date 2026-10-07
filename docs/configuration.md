# 配置、热重载与日志

完整默认字段见 [示例 TOML](chiliarch.example.toml)，权威验证模型是 `chiliarch.config.Settings`。使用 `chiliarch init` 生成真实配置，示例中的空密码哈希不能直接启动服务。

## 优先级与路径

默认值 → TOML → `CHILIARCH_<SECTION>__<FIELD>` 环境变量 → 本次 CLI 的监听参数。环境变量可提供 JSON 对象；数值和布尔值按模型校验。例如 `CHILIARCH_MODEL__API_KEY`、`CHILIARCH_MODEL__HEADERS`。

相对日志路径和 SQLite 路径相对于配置文件所在目录解析，不依赖进程从哪里启动。未指定配置文件的嵌入式应用则以创建应用时的当前目录为基准。

密钥可只通过环境变量提供，保存 WebUI 配置不会把环境变量密钥写回 TOML。环境变量在进程启动时提供；修改另一个终端里的变量不会改变已有进程的环境。

## 保存与热重载

WebUI 修改先校验整份候选配置，然后用临时文件、fsync 和原子替换保存。配置权限 0600。文件监视按 `server.config_poll_seconds` 检查修改；无效 TOML、未知字段、错误类型或不合理参数均保留上一份有效配置。

有效更新增加配置 revision。新任务/模型调用使用新快照；运行中调用保留原快照。模式与审批仍在统一操作服务实际执行时检查，不能通过修改配置把已发起的待审批操作自动批准。

`server.host`、`server.port`、`database.url` 保存后需要重启。WebUI 表单显示当前有效配置，保存时提示哪些修改需要重启；配置版本与待重启值可通过 `/api/state` 的 runtime 查询。配置页面保留校验失败和保存结果提示，不常驻展示热重载状态面板。`server.public_url`、模型、模式、投递时间参数、日志策略等支持热重载。

`model.headers` 与 `request_params` 是完整映射，提交 `{}` 可以清除旧键。读取 API Key、请求头、密码哈希与嵌套敏感参数时使用 `********`；原样回传掩码保留原秘密。空 API Key 明确清除；不提交该字段保持原值。

## 字段说明

### server / auth / database

| 字段 | 用途 |
|---|---|
| server.host / port | 监听地址和端口，需重启 |
| server.public_url | 浏览器公开 origin，用于反向代理后的 Origin 校验 |
| server.config_poll_seconds | 文件配置检查间隔 |
| auth.password_hash | init/密码修改生成的 scrypt 哈希，不手填明文 |
| auth.session_ttl_seconds | 登录会话有效期；新会话采用新值 |
| auth.max_login_attempts / login_window_seconds | 同来源登录限流 |
| auth.secure_cookie | 新签发的 cookie 是否仅随 HTTPS 发送；HTTP 部署保持 false |
| database.url | 默认 SQLite async URL，改动需重启；初版验收存储为 SQLite |

UI/API 修改密码会使现有会话失效。浏览器变更操作需要 cookie 和 `X-CSRF-Token`；登录会校验 Origin。初版为单用户，没有多用户、角色管理或默认管理员密码。

侧栏底部的主题按钮按“跟随系统 → 浅色 → 深色”循环切换。默认跟随系统，选择保存在当前浏览器中；返回跟随系统后继续响应系统明暗变化。

浅色主题使用柔和浅底，暗色主题采用炭灰背景与暖白文字，以不同的中性表面区分侧栏、消息、输入区和工具详情；蓝金色用于按钮、焦点及状态强调。主题偏好只保存在浏览器，不写入服务端 TOML。

界面的抽屉、消息和折叠反馈遵循操作系统的“减少动态效果”设置。聊天输入支持 Enter 发送、Shift + Enter 换行及 Ctrl / ⌘ + Enter 发送；阅读历史时暂停自动跟随，可通过“回到最新”恢复。具体交互见 [模型与总管行为](models.md#对话页面与键盘)。

### transport

| 字段 | 用途 |
|---|---|
| heartbeat_seconds | 参考客户端主动心跳间隔 |
| offline_after_seconds | 超过最后主动活性记录多久显示离线，必须大于心跳间隔 |
| poll_timeout_seconds | 长轮询最大等待，最长 60 秒 |
| retry_seconds | 未 ACK 消息重投与检查间隔 |
| queue_limit | 持久未确认消息数量上限 |
| message_ttl_seconds | 下行消息有效期，过期后显式停止交付 |
| max_message_bytes | 单条信封大小上限 |
| reconnect_base_seconds / reconnect_max_seconds | 客户端退避范围，加入抖动 |
| upgrade_interval_seconds | 降级传输定期重新尝试 WS 的间隔 |

这些客户端字段只有使用对应配置启动参考客户端时才影响其行为。跨语言插件应使用自己的配置或协商默认值；本版不自动向任意插件推送所有服务端设置。

真实 agent 的 `chiliarch agent` 命令独立于服务器 TOML，不需要 `chiliarch init` 或 WebUI 密码。`--state` 指定私有 SQLite 凭证与消息库，`--transport` 选择起始传输。CLI runner 的 `--settings` 是另一份机器本地 TOML，字段由 `RunnerSettings` 校验；工作目录相对于该文件解析，远端任务参数不能修改启动程序、目录或权限。runner 设置启动时读取，修改后重启对应 runner，不受服务器配置热重载影响。具体示例见 [接入文档](agents/index.md)。

### supervisor / model

`supervisor.mode` 为 ask/auto/yolo；`max_tool_rounds` 限制模型与工具循环，`run_timeout` 限制每段活跃执行的时间（审批等待不计入，批准恢复后重新计时），`max_context_messages` 限制上下文消息数，`max_active_runs` 限制并行运行，`inbox_poll_seconds` 决定持久下级 inbox 检查间隔，`event_poll_seconds` 决定聊天事件检查间隔，`system_prompt` 为总管角色提示。

模式跳过审批不等于跳过运行上限。达到上下文上限会要求新对话，不会粗暴截断工具调用及其结果配对。模型字段的 API 映射与兼容开关见 [模型文档](models.md)。

## 日志存储与轮换

`logs.directory` 下的 `chiliarch.jsonl` 为 UTF-8 JSON Lines，每行包含 UTC timestamp、level、logger、message 以及事件相关 ID、状态或耗时。记录平台启停、HTTP 路径/状态/耗时、操作执行/审批、agent 连接/消息/任务状态、配置更新和总管运行事件。

`logs.max_bytes` 为单文件最大字节数，`backup_count` 为保留的轮换文件数；总大小约受 `(backup_count+1)×max_bytes` 约束。达到大小后生成 `.1/.2/...` 并淘汰最旧文件。`logs.level` 可热更新。标准错误同时输出结构化日志，便于进程管理器采集。

认证 header、cookie、密码和凭证不记录；敏感键递归脱敏。HTTP 日志不记录原始请求 body。异常记录类别及栈帧的文件、行号、函数名，不保存异常正文或局部变量，防止 SDK 错误回显认证内容。流式每个 token 的正文保存在聊天业务数据中，不逐 token 写应用日志。

SQLite 审计事件、会话消息、任务和操作记录是业务历史，不随应用日志文件轮换删除；数据库应单独备份并监控磁盘。v0.1.0 不提供自动清理所有业务历史的保留策略。WebUI“读取日志”和 `get_logs` 工具读取当前日志末尾，最多 1000 行。

日志包含操作信息，聊天数据库包含用户发送给模型的内容；保护目录权限与备份。秘密参数掩码依据敏感字段名，扩展开发者必须对自定义秘密字段自行脱敏。

agent MCP/runner 默认在状态库旁的 `logs` 目录写同样的结构化轮换日志，也可用 `--log-directory` 指定。共享日志使用跨进程锁保护写入与轮换，并重开被其他进程轮换的旧文件；stdio MCP 的 stdout 只用于协议，日志写 stderr；原生 CLI 的 stderr 并发读取、计入输出限额后丢弃，不把认证诊断上传平台。执行结果只包含所需答案、会话身份、数值用量等字段；真实工作生成的敏感内容仍需由工作策略和扩展自行处理。状态库为 0600；托管任务的临时原生 MCP 配置在结束时清理，不改全局 agent 配置。
