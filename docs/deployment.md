# 部署、初始化与维护

## 源码运行

准备 uv、Node.js 24 LTS 和项目指定的 pnpm 版本。进入检出的仓库目录后：

```bash
uv sync --locked --group dev
pnpm --dir web install --frozen-lockfile
pnpm --dir web build
uv run chiliarch --config ./chiliarch.toml init
uv run chiliarch --config ./chiliarch.toml serve
```

`init` 交互输入并确认至少 8 字符的密码，服务端保存 scrypt 哈希，不保存明文。没有默认密码。已有配置不会被覆盖，除非显式指定 `init --force`。不要对正在使用的配置随意重新初始化；日常修改使用 WebUI 或编辑 TOML。

服务默认监听 `127.0.0.1:8000`。需要 agent 所在机器访问时，配置适当的监听地址与端口，并确保客户端能主动访问平台；agent 不需要开放端口。服务器不需要访问 agent IP。

```bash
uv run chiliarch --config ./chiliarch.toml serve --host <监听地址> --port <端口>
```

CLI 覆盖用于本次启动。平台只支持一个服务进程；不要添加多 workers 或并行启动两个实例共同使用同一业务数据库。

## 已发布包运行

PyPI 发布完成后可以用 `uv tool install chiliarch` 或 `uv tool install chiliarch==<版本>` 安装。发布包已包含 WebUI，不需要 Node、pnpm 或前端开发服务器。源码构建必须先运行前端 build；缺少资源时 WebUI 返回明确 503，API 不假装页面已构建。

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

模拟器只测试通信和任务生命周期，不操作真实 Codex、Claude Code、OpenCode 或 Undefined。接入真实工具应实现参考客户端 handler 或同协议插件。

Agents 管理列表以机器卡片展示实际连接状态、任务和待处理数；待处理徽标统计仍需处理的请求和审批。点击卡片进入详情，上方查看连接、心跳、能力与任务状态，下方派发指令、处理请求、查看任务或管理凭证。CPU、内存等机器遥测仅在 agent 实际上报时显示。

## 备份与恢复

服务端业务数据库、配置和日志是不同资源。SQLite 在运行中有 WAL 文件；不要只复制主 `.db` 文件并假设已得到一致快照。可停止本实例后复制数据库及配置，或使用 SQLite backup API 获取在线快照；保护备份中的会话、业务消息与配置密钥。

恢复时停止实例，恢复匹配的数据库和配置，再运行同版本服务。数据库 schema version 不兼容会明确拒绝启动，不能默默重建。升级前保留快照；本版只有初始 schema，未来迁移应明确版本、备份和回滚步骤。

客户端本地状态同样需要保留；丢失它意味着丢失长期凭证、待上传结果和任务去重记录。重新配对前吊销旧身份；不要把同一个客户端数据库复制到两台机器同时运行。

## 常见问题

| 现象 | 检查 |
|---|---|
| 启动缺少密码 | 使用正确 `--config` 文件，先执行 init |
| 登录后操作 403 | 同源访问、公开地址、CSRF token 与 HTTPS cookie 设置 |
| WebUI 503 | 源码运行先执行 pnpm build，安装包检查是否使用正式构建产物 |
| agent 无法上线 | 从 agent 机器测试平台可达性；确认注册码未过期、凭证未吊销 |
| agent 始终离线 | 确认客户端在运行并发送心跳；服务端不会主动联系机器 |
| 总管没有回复 | 配置模型名称/地址/认证，检查 run 状态和脱敏日志 |
| 任务 cancel_requested | agent 尚未确认取消；不能等同于已经停止 |
| interrupted | 执行结果未知，先检查任务/操作记录，避免重复副作用 |
| 热重载拒绝 | 修正配置类型和取值，上一份有效配置继续生效 |
