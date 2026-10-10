# 部署、初始化与维护

## 从 PyPI 安装并运行

默认使用持久的 uv 工具环境，从 PyPI 安装 Chiliarch。发布包已经包含 WebUI，无需 Node、pnpm 或前端开发服务器：

```bash
uv tool install chiliarch
chiliarch --config ./chiliarch.toml init
chiliarch --config ./chiliarch.toml serve
```

需要固定版本时，使用 `uv tool install chiliarch==<版本>`。

`init` 交互输入并确认至少 8 字符的密码，服务端保存 scrypt 哈希，不保存明文。没有默认密码。已有配置不会被覆盖，除非显式指定 `init --force`。不要对正在使用的配置随意重新初始化；日常修改使用 WebUI 或编辑 TOML。

服务默认监听 `127.0.0.1:8000`。需要 agent 所在机器访问时，配置适当的监听地址与端口，并确保客户端能主动访问平台；agent 不需要开放端口。服务器不需要访问 agent IP。

```bash
chiliarch --config ./chiliarch.toml serve --host <监听地址> --port <端口>
```

CLI 覆盖用于本次启动。平台只支持一个服务进程；不要添加多 workers 或并行启动两个实例共同使用同一业务数据库。

启动未完成或被取消时，平台会释放已创建的数据库连接、后台任务和日志资源，并保留原始启动错误。关闭某个组件失败时，其他组件仍继续关闭。

## 源码开发运行

修改或开发 Chiliarch 源码时，准备 uv、Node.js 24 LTS 和项目指定的 pnpm 版本。进入检出的仓库目录后：

```bash
uv sync --locked --group dev
pnpm --dir web install --frozen-lockfile
pnpm --dir web build
uv run chiliarch --config ./chiliarch.toml init
uv run chiliarch --config ./chiliarch.toml serve
```

源码构建必须先运行前端 build；缺少资源时 WebUI 返回明确 503，API 不假装页面已构建。

## Agent 机器：共享守护进程

Agent 机器无需运行 `init`、WebUI 或平台服务。先 `chiliarch daemon status` 发现当前用户服务；健康时复用，已安装但停止时 `chiliarch daemon start`。没有工具时默认执行 `uv tool install chiliarch` 从 PyPI 安装，再执行 `chiliarch daemon install`：默认注册当前用户登录后自启动并启动本地服务。

Windows 使用当前用户登录任务，macOS 使用 LaunchAgent，Linux 使用 systemd 用户服务；无可用管理器时后台或前台运行仍可用，状态明确显示自启动未启用。详细入口、目录、维护、旧 standalone 迁移和 Windows 原生程序边界见 [本机守护进程](daemon.md)。

服务健康后，在平台创建注册码，再在 agent 机器执行：

```bash
chiliarch agent pair --server "http://平台主机:8000" --name "工作机 / Codex App" \
  --adapter codex-app --agent codex-main --state "/绝对路径/私有目录/agent.db"
chiliarch agent config --host codex-app --agent codex-main
chiliarch agent doctor --agent codex-main
```

`agent pair` 先确认守护进程健康再配对、登记；注册码交互输入。每个身份保留独立状态库，多个宿主 MCP 复用本机守护进程连接；MCP 配置携带本机标识，不携带长期平台凭证。Codex CLI/Claude Code 的宿主参数、安装提示词和固定 runner 配置见 [Agent 接入文档](agents/index.md)。当前会话不会自动被唤醒，明确需要无人值守时登记独立 runner。

## HTTP、HTTPS 与反向代理

HTTP 可直接使用，不强制 TLS。HTTPS 可以在反向代理终止；设置 `server.public_url` 为浏览器实际访问的公开 origin，避免代理后的 Origin 校验使用内部地址。启用 HTTPS 后可设置 `auth.secure_cookie=true`。

代理需要允许 WebSocket Upgrade；SSE 禁用缓冲和缓存，读超时应长于心跳/轮询间隔。即使代理不支持 WS，agent 仍可使用 SSE 或长轮询。对于浏览器聊天流，平台同样发送 `X-Accel-Buffering: no`。同源部署 WebUI 和 API；本版没有跨域登录功能。

运行可自行接入 systemd、容器或其他进程管理器，使用持久卷保存配置、数据库和日志，并只启动一个实例。v0.1.0 没有提供容器镜像发布。

## 配对与验收

1. 登录 WebUI，在“Agents 管理”右上角点击添加按钮，在弹窗内创建一次性注册码。
2. 在 agent 所在机器执行 `chiliarch pair --server <平台地址> --name <名称> --state <本地数据库>`，交互输入注册码。
3. 执行 `chiliarch demo-agent --state <本地数据库>`。可使用 `--transport sse` 或 `--transport poll` 排查网络。
4. 手动派发测试任务；parameters 可设 `{"steps":5,"delay":0.2}`，观察进度和完成结果中的 `simulated:true`。
5. 停止客户端后派发任务，重新启动验证离线交付；吊销凭证后该客户端应停止，不能继续收发。

模拟器只测试通信和任务生命周期，不操作真实 Codex、Claude Code、OpenCode 或 Undefined。真实 Codex App、Codex CLI、Claude Code 使用上述守护进程接入；其他工具可实现参考客户端 handler 或同协议插件。

Agents 管理列表以机器卡片展示实际连接状态、任务和待处理数；待处理徽标统计仍需处理的请求和审批。点击卡片进入详情，上方查看连接、心跳、能力与任务状态，下方派发指令、处理请求、查看任务或管理凭证。CPU、内存等机器遥测仅在 agent 实际上报时显示。

多 agent 上线后可在“协作”页创建讨论、发送私信、查看回执与等待关系。普通传话无需总管模型逐条转述；明确勾选唤醒时会向托管身份派发正式任务。跨机器文件由 MCP 上传、获授权同伴主动下载，不开放新的 agent 入站端口。操作与限制见 [横向协作](collaboration.md) 和 [共享产物](artifacts.md)。

## 备份与恢复

服务端业务数据库、配置和日志是不同资源。SQLite 在运行中有 WAL 文件；不要只复制主 `.db` 文件并假设已得到一致快照。平台运行时可使用内置命令获取一致快照：

```bash
chiliarch --config ./chiliarch.toml backup --output ./backups/platform-snapshot.db
```

命令使用 SQLite backup API，包含已提交的 WAL 数据，不包含尚未提交的事务。输出经过完整性及 schema version 校验，是无需原库 WAL/SHM 的独立数据库文件；默认 30 秒超时，可用 `--timeout` 调整。只有成功后才原子发布输出，已存在的文件永不覆盖，失败时清理临时快照。输出目录需要支持硬链接；不支持时命令明确失败，应选择本机支持的文件系统保存后再转移。仅支持普通本地 SQLite 文件，不支持内存库、SQLite URI 选项或其他数据库；其他数据库使用对应厂商备份工具。

默认来源由磁盘上的 TOML 与备份命令当前环境变量解析，命令会显示源路径，**不会自动识别运行实例的有效配置**。如果数据库设置存在待重启变更，或后台服务环境变量与当前终端不同，请核对运行配置中的数据库路径，并显式选择：

```bash
chiliarch backup --database /实际运行的数据库路径/chiliarch.db --output ./backups/platform-snapshot.db
```

`--database` 优先于配置文件和环境覆盖，也不要求存在配置文件；本机路径包含空格时需加引号。

此命令只备份平台数据库，不打包配置、环境变量、日志、agent 状态库或原生 agent 会话。另行保存对应 TOML 和必要的环境配置；保护快照中的会话、业务消息与凭证哈希。Unix 快照权限为 `0600`，Windows 继承目标目录 ACL，应选择当前用户的私有目录。需要停止实例做文件级备份时，先完成正常关闭，再复制数据库和配置。

讨论记录、逐人回执、共享产物清单及文件块也在平台数据库内，因此一致性快照包含它们。产物会增加数据库和备份大小；配置 `artifacts.max_total_bytes` 只限制文件清单额度，传输消息历史、索引和已有备份另占空间。Agent 机器上尚未完成的下载 `.part` 文件属于本地文件，平台备份不会保存它们。

恢复时停止实例，恢复匹配的数据库和配置，再运行同版本服务。数据库 schema version 不兼容会明确拒绝启动，不能默默重建。升级前保留快照；本版只有初始 schema，未来迁移应明确版本、备份和回滚步骤。

本机守护目录的登记与设置、各 agent 状态库和 runner TOML 同样需要保留；丢失它意味着丢失长期凭证、待上传结果和任务去重记录。重新配对前吊销旧身份；不要把同一个客户端数据库复制到两台机器同时运行。

## 常见问题

| 现象                  | 检查                                                      |
| --------------------- | --------------------------------------------------------- |
| 启动缺少密码          | 使用正确 `--config` 文件，先执行 init                     |
| 登录后操作 403        | 同源访问、公开地址、CSRF token 与 HTTPS cookie 设置       |
| WebUI 503             | 源码运行先执行 pnpm build，安装包检查是否使用正式构建产物 |
| agent 无法上线        | 从 agent 机器测试平台可达性；确认注册码未过期、凭证未吊销 |
| agent 始终离线        | 确认客户端在运行并发送心跳；服务端不会主动联系机器        |
| 总管没有回复          | 配置模型名称/地址/认证，检查 run 状态和脱敏日志           |
| 任务 cancel_requested | agent 尚未确认取消；不能等同于已经停止                    |
| interrupted           | 执行结果未知，先检查任务/操作记录，避免重复副作用         |
| 热重载拒绝            | 修正配置类型和取值，上一份有效配置继续生效                |
