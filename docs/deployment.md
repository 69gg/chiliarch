# 安装与部署

先选定平台运行的机器。它保存任务、聊天和配置，提供 WebUI；agent 机器只安装接入工具和本机守护进程。两者可以是同一台机器。

支持 Windows、macOS、Linux 与 Python 3.11–3.13。发布包自带 WebUI，正常安装无需 Node、pnpm 或源码构建。

## 1. 安装 uv

已安装 uv 可以跳过。官方安装方式如下，更多方法见 [uv 安装指南](https://docs.astral.sh/uv/getting-started/installation/)。

Linux / macOS：

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Windows PowerShell：

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

安装后重新打开终端，确认 `uv --version` 可用。

## 2. 安装 Chiliarch

三种系统都运行：

```sh
uv tool install --python 3.13 chiliarch
chiliarch --version
```

uv 会准备需要的 Python 和独立工具环境。若终端找不到 `chiliarch`，运行 `uv tool update-shell`，再重新打开终端；桌面 App 也可能需要重启才能看到新的 PATH。

默认从 PyPI 安装。需要固定版本时可使用 `uv tool install --python 3.13 "chiliarch==0.4.4"`；固定版本会保留约束，不自动跨越它。

## 3. 初始化平台

```sh
chiliarch init
```

向导依次询问：

| 设置 | 怎样填写 |
| --- | --- |
| 登录密码 | 至少 8 字符，输入两次；没有用户名或默认密码 |
| 监听地址 | 默认 `127.0.0.1`，仅本机可访问；其他机器接入通常填 `0.0.0.0` |
| 端口 | 默认 `8000` |
| 浏览器公开地址 | 直接访问可留空；使用反向代理时填写浏览器实际访问的 origin，如 `https://chiliarch.example.com`，不含路径 |
| 审批模式 | 默认 `auto`；详细区别见[总管模式](models.md#审批模式) |
| 日志级别 | 默认 `INFO`，通常保持即可 |
| 总管模型 | 可现在配置，也可在登录后的“配置”页面填写 |

配置模型需要 API 格式、模型 ID、API 根地址和 API Key，上下文窗口默认 200000 tokens。OpenAI 两种格式默认不传输出上限；Anthropic Messages 还需填写模型支持的最大输出 tokens。总管设置不会修改本机 Codex、Claude Code 或 OpenCode 的设置。

只想使用默认设置时运行 `chiliarch init --defaults`，仍需要设置密码。初始化结束会显示**配置文件的绝对路径**和启动命令。已有配置不会被覆盖；日常修改用 WebUI 或编辑 TOML，通常无需再次 init。

## 4. 启动并登录

```sh
chiliarch serve
```

默认访问 [http://127.0.0.1:8000](http://127.0.0.1:8000)，输入初始化密码登录。暂未配置模型时，你可以先打开“Agents 管理”接入机器、手动下发任务。

平台运行期间保留终端，停止时使用 Ctrl+C。需要长期后台运行，可将打印的启动命令交给你使用的进程管理器。平台应只运行一个实例；不要为同一个数据库开启多个 workers。

仅覆盖本次监听参数可以运行：

```sh
chiliarch serve --host 0.0.0.0 --port 8000
```

平台与 agent 守护进程在启动时分别静默检查新版，更新情况写入日志；已有配置缺失的新增字段会自动补全，显式设置保留。Agent 机器的后台运行和 Windows 无窗口启动见[守护进程](daemon.md)，更多更新说明见[升级指南](upgrading.md)。

## 5. 连接其他机器

如果 agent 在其他机器上：

1. 平台监听 `0.0.0.0` 或适当的网络接口。
2. 允许 agent 机器访问平台端口；给它使用实际可达的服务器地址。
3. 登录 WebUI，在“Agents 管理”点击“添加 agent”，创建一次性注册码。
4. 按[接入指南](agents/index.md)在 agent 机器完成配对。

`0.0.0.0` 是监听设置，不能当作客户端要连接的服务器地址。`127.0.0.1` 指当前机器；远程 agent 使用它会连接到自己。

Agent 无需运行 `chiliarch init` 或 `serve`，也无需开放入站端口。通信从 agent 主动建立，优先 WebSocket，受网络限制时降级为 SSE＋HTTP POST、长轮询。

## 配置文件位置

uv 工具安装默认使用当前用户目录：

| 系统 | 默认服务端配置 |
| --- | --- |
| Linux | `~/.local/state/chiliarch/server/chiliarch.toml` |
| macOS | `~/Library/Application Support/chiliarch/server/chiliarch.toml` |
| Windows | `%LOCALAPPDATA%\chiliarch\server\chiliarch.toml` |

Linux 设置了绝对路径的 `XDG_STATE_HOME` 时，以该目录为状态根。默认数据库在配置目录下的 `data/chiliarch.db`，日志在 `logs`，不受启动终端目录影响。

使用自定义路径时，初始化和启动都带上相同的 `--config`：

```sh
chiliarch --config "/你的部署目录/chiliarch.toml" init
chiliarch --config "/你的部署目录/chiliarch.toml" serve
```

以上路径请替换为自己的路径；有空格时保留引号。源码运行默认读取当前目录的 `chiliarch.toml`，源码构建方法见[开发参考](development.md)。

平台 TOML 与 agent 的守护配置分别管理。密码只保存哈希，API Key 可通过环境变量提供，具体见[配置说明](configuration.md)。

## HTTP、HTTPS 与反向代理

HTTP 可以直接使用，不强制 SSL。HTTPS 由反向代理提供；将 `server.public_url` 设置为浏览器实际访问的 origin，WebUI 与 API 使用同一个地址。

初始化填写 HTTPS 公开地址时会开启 `auth.secure_cookie`。以后切换 HTTP / HTTPS，需要同步调整此项；设为 true 的 cookie 不会通过 HTTP 发送。填写公开地址本身不会让 Chiliarch 启动 HTTPS 监听。

代理需要支持 WebSocket Upgrade，并关闭 SSE 的缓冲和缓存，避免聊天输出等到最后才出现。代理不支持 WS 时，agent 可以降级使用 SSE 或轮询。

当前没有官方容器镜像。自行容器化或接入系统服务时，持久保存配置、数据库和日志，并保持单实例运行。

## 备份与恢复

平台运行中使用：

```sh
chiliarch backup --output ./platform-backup.db
```

它创建一个独立、经过完整性校验的 SQLite 快照，包含已提交的数据和共享产物，不覆盖已有文件。不要在运行中仅复制主 `.db` 文件，未合并的 WAL 数据可能遗漏。

命令会打印来源数据库。若你修改了数据库配置但尚未重启，或终端与服务的环境不同，可明确指定实际使用的数据库：

```sh
chiliarch backup --database "/实际数据库路径/chiliarch.db" --output ./platform-backup.db
```

还需另外保存：

- 平台 `chiliarch.toml` 和必要的环境配置。
- 每台 agent 的守护目录、独立状态库与 runner TOML。
- 需要保留的应用日志、原生 agent 会话和项目文件。

备份输出目录需要支持硬链接；不支持时先保存到支持的本地文件系统，再转移快照。`--timeout` 可调整默认 30 秒备份时限。

恢复时，停止平台，恢复数据库和匹配配置，再用对应版本启动。Agent 的状态库包含长期凭证和未上传消息，不要复制到两台机器同时运行，也不要为恢复随意重新配对。

## 常见问题

| 现象 | 先做什么 |
| --- | --- |
| 找不到 `chiliarch` 命令 | `uv tool update-shell` 后重新打开终端；桌面宿主重启 |
| 登录后操作返回 403 | 核对浏览器公开地址、HTTP / HTTPS 与 secure cookie 设置 |
| WebUI 返回 503 | 正式 PyPI 包应包含页面；源码运行先构建前端 |
| Agent 一直离线 | 在 agent 机器核对平台地址与端口，再查 `chiliarch daemon agents` |
| 配对成功但本机登记失败 | 保留状态库，按[接入恢复](agents/index.md#接入失败时如何恢复)重新登记 |
| 总管报模型错误 | 查看[模型排错](models.md#模型报错时怎么处理)与配置页日志 |
| 取消后没有停止确认 | Agent 尚未确认，离线时取消请求会排队；先核对实际任务 |
| 中断或执行结果未知 | 检查实际改动和原生记录，再决定是否重试 |
| 修改配置没有生效 | 模型等设置用于后续运行；监听地址、端口和数据库需重启 |

下一步：[连接 agent](agents/index.md) · [创建目标](coordination.md) · [配置与日志](configuration.md)。


## 启用 GitHub 协作

GitHub 功能默认开启，可以在“配置 → GitHub 功能”或通过 `github.enabled` 热重载关闭。只使用普通 agent 协作时，无需为此安装 Git/gh。

完成基础安装后，在参与项目的 agent 机器上准备 Git、gh 及仓库认证，确认 Git 提交身份；需要签名时配置本机签名密钥。平台服务器无需登录 GitHub。随后按 [GitHub 项目指南](github.md) 登记仓库、绑定 agent、执行环境探测。

专用副本和 worktree 会占用 agent 机器的磁盘。备份时按需保存守护进程数据中的命令 journal、工作区、原生会话和未交付改动；不要只备份服务器数据库就假定已保全节点上的工作。
