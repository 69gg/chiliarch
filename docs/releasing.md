# 开发、测试与发版

## 本地验证

Python 默认 3.13，CI 使用 Linux、macOS、Windows 与 Python 3.11、3.12、3.13 的完整矩阵；所有组合和前端验证通过后才构建并检查 wheel、sdist。3.14 可另行验证，不因 requires-python 无上限就声明所有未来版本支持。

```bash
uv sync --locked --group dev
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
pnpm --dir web install --frozen-lockfile
pnpm --dir web typecheck
pnpm --dir web test
pnpm --dir web build
```

测试验证数据库事务、实际 SDK 请求、工具副作用、审批、真实 TCP 长连接与故障恢复，前端测试驱动真实 DOM 交互。不通过搜索源码是否包含某个字符串判断功能。

跨平台测试将业务网络调用与事件循环内部通信分开检查；Windows 虚拟环境启动器的 PID 可能不同于实际 Python 进程，执行身份应在解释器内验证。测试子进程的运行记录完整写入后才发布，轮询等待本次运行的记录，避免读取半写入数据。

多轮工具测试按每轮执行进展等待，最终核对完成状态与持久化的幂等记录，不将整段任务的总耗时当成存储性能要求。测试服务器退出超时时保留未完成协程的位置，并清理服务与 lifespan 任务，避免将资源泄漏带入后续测试。

测试服务器将关闭分成网络排空和应用资源清理两段：网络仍限 5 秒，收到 ASGI `lifespan.shutdown` 后才开始单独的 20 秒清理期限。SQLite 本身允许等待锁 10 秒，Windows 上连接池关闭/WAL 清理可能超过原先的统一 5 秒；不能在请求已经排空后把数据库清理误报为网络卡死。两段超时都保留协程诊断，兜底清理仍有独立的 5 秒总期限；行为测试覆盖慢清理正常完成和真正停滞按阶段失败。

CI 和发版测试逐项输出测试名称，并列出最慢的 20 项。单项测试（含准备、清理）超过 120 秒时，使用锁文件中 pytest 的 faulthandler 看门狗输出线程栈并以失败状态结束，避免死锁占用整个 runner；这不替代协议、业务和夹具各自的更短超时。测试服务器的兜底清理也有独立时限，卡住时立即输出协程等待链，不通过无限屏蔽取消来推迟诊断。

SSE 离线检测测试持续消费服务端事件，但不发送 agent 心跳，并在离线后核实仍能收到服务端心跳。只读首条后长期停止读取，会在关闭含未读响应的连接时触发 TCP reset；已在 Windows Python 3.12/3.13 观察到 Proactor 关闭回调因 `WinError 10054` 漏掉连接回收，让 `wait_closed()` 无法完成。该测试要验证的是出站心跳与连接活性各自的含义，不能把停止消费 SSE 混入这一场景。

网络测试等待任务状态时直接读取持久记录，不反复发起会写入审计记录的管理操作，避免观察本身增加 SQLite 写入争用。降级测试校验 WS → SSE → 长轮询的完整顺序，并允许同一传输重连；仍需确认最终任务结果、处理次数、在线状态和消息 ACK，不能把正常重连误判为失败。

自动更新测试使用隔离安装环境、可控元数据和安装子进程，验证版本与 Python 约束、来源识别、并发选择、失败时保留旧运行环境、原子发布及日志脱敏；CLI 测试覆盖实际参数解析和进程转交。测试不得更新开发者真实 uv 工具环境或向原生 agent 发起推理。

生产静态资源为生成文件，不提交到 Git；`static/.gitkeep` 保留构建目录。前端 build 输出到 Python package，Hatch wheel/sdist 强制包含该目录；协议 Schema 为受版本管理的公开契约，修改模型后重新导出并验证。

## 升级版本

```bash
uv run python scripts/bump_version.py 0.3.0 --dry-run
uv run python scripts/bump_version.py 0.3.0
```

脚本使用 uv version 同步 pyproject 与 uv.lock，再同步 web/package.json；JSON 报告中的仓库相对路径在各系统统一使用 `/`。包 `__version__` 来自安装元数据，不再维护第二个源码版本字符串。同版本重跑可以修复前端版本不一致；失败时恢复受管理文件。脚本不自动提交、打 tag 或推送。

支持公开三段 PEP 440 版本，例如 0.3.0、0.3.0rc1；拒绝本地版本号和 epoch。CHANGELOG 标题使用 `## v<版本> 标题`，下方写概要、按用户影响分组的要点和 `---`。不要自动拼接提交历史，也不要把文档同步/测试数量当作发布主题。

预发布标签会生成 GitHub prerelease，并且不更新 latest。wheel 与 sdist 明确使用 Core Metadata 2.4，兼容当前 Twine 校验器，同时保留 MIT SPDX 许可证信息。

## 构建并验证实际包

```bash
pnpm --dir web build
uv run chiliarch schema --output src/chiliarch/protocol/schema.json
uv build
uv run twine check dist/*
uv run --isolated --no-project --with dist/*.whl scripts/smoke_test.py
uv run --isolated --no-project --with dist/*.tar.gz scripts/smoke_test.py
uv run python scripts/release_notes.py validate --tag v0.3.0
uv run python scripts/release_notes.py extract --tag v0.3.0
```

隔离 smoke test 从实际分发包导入模块，验证 UI、协议 Schema、密码登录和 API。sdist 同样含已构建 UI，安装它时无需 Node；重新开发 UI 仍需 pnpm。

## 首次配置 PyPI Trusted Publishing

仓库为 [69gg/chiliarch](https://github.com/69gg/chiliarch)。现有 `.github/workflows/release.yml` 已声明发布所需的 `id-token: write`，使用 GitHub environment `pypi`；不需要改成 API Token 发布。

1. 在 GitHub 仓库的 **Settings → Environments** 创建名为 `pypi` 的环境。如果限制部署来源，添加 **Tag** 类型的 `v*` 规则，让发布标签能够使用该环境。需要每次发版人工批准时再配置 required reviewers。
2. 在 **Settings → Actions → General** 确认 GitHub Actions 已启用，并允许 workflow 使用的 `actions/*` 和 `astral-sh/*`。各 job 已声明所需权限，无需将整个仓库的默认 workflow 权限改成读写。
3. 登录 [PyPI 帐号 Publishing](https://pypi.org/manage/account/publishing/)，在 GitHub 的 **Add a new pending publisher** 表单填写下列值并添加。

| PyPI 字段                | 填写值                                                   |
| ------------------------ | -------------------------------------------------------- |
| PyPI project name        | `chiliarch`                                              |
| Owner                    | `69gg`                                                   |
| Repository name          | `chiliarch`                                              |
| Workflow name / filename | `release.yml`，只填写文件名，不填写 `.github/workflows/` |
| Environment name         | `pypi`，与 workflow 完全一致                             |

不需要创建或配置 `PYPI_API_TOKEN`，也不需要先手动上传一个版本。首次发布会创建 PyPI 项目，并将 pending publisher 转为正式 publisher。已有 `chiliarch` 项目时，在该项目的 **Manage → Publishing** 添加同样的 GitHub publisher。设置 pending publisher 不预留项目名称。[PyPI 创建项目文档](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/)

## tag 发布流程

`.github/workflows/ci.yml` 运行 Python 矩阵、前端行为测试和实际产物隔离安装；`release.yml` 在推送 `v*` tag 时：

1. 在 Python 3.11–3.13 上检查格式、类型、行为和 tag/版本/CHANGELOG 一致性。
2. 构建前端，生成 wheel/sdist，执行 twine 和隔离 smoke test。
3. build job 上传产物，publish job 下载同一产物，通过 OIDC、attestation 和 `uv publish --trusted-publishing always` 发布 PyPI。
4. PyPI 成功后创建 GitHub Release，正文直接读取对应 CHANGELOG 条目，附件为同一 wheel/sdist。

build 不拥有 PyPI OIDC 凭证；只有 publish job 有 id-token:write，GitHub Release job 单独拥有 contents:write。Actions 固定到核实过的 commit SHA，升级时核查官方来源。

确认版本、CHANGELOG、测试和签名提交后，项目维护者才执行 tag 和 push。代码完成或本地构建成功不等于已经上传 PyPI；未设置 publisher 或未推送 tag 时不会发布。

以 v0.3.0 为例，先将代码推送到上述 GitHub 仓库，确认该提交的三系统 CI 全部通过后，再推送签名标签。以下第一条命令只在尚未配置 `origin` 时执行：

```bash
git remote add origin https://github.com/69gg/chiliarch.git
git push -u origin main
git tag -s v0.3.0 -m "v0.3.0"
git push origin v0.3.0
```

在 GitHub **Actions → Release** 查看运行结果；若设置了环境审核，批准 `pypi` 部署后继续发布。成功后用户及 agent 均默认使用 `uv tool install chiliarch` 从 PyPI 安装。
