# 源码开发与发版

这是维护者参考。用户安装、更新与日常操作从[用户手册](index.md)开始；正式包已包含 WebUI，用户无需构建源码。

## 源码运行

准备 uv、Node.js 24 LTS 与 web/package.json 指定的 pnpm，进入检出的仓库目录：

```sh
uv sync --locked --group dev
pnpm --dir web install --frozen-lockfile
pnpm --dir web build
uv run chiliarch --config ./chiliarch.toml init
uv run chiliarch --config ./chiliarch.toml serve
```

源码配置默认是当前目录的 chiliarch.toml，显式路径便于区分用户目录中的平台配置。没有构建前端时，WebUI 会明确返回 503。

## 验证

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
pnpm --dir web typecheck
pnpm --dir web test
pnpm --dir web build
```

行为测试验证实际状态、网络和副作用，不搜索源码字符串代替功能。原生 agent 测试使用隔离测试子进程，不依赖开发者真实认证、任务、配置或付费推理。

Python 测试默认按可用 CPU 并行，最多 8 个 worker，使用 worksteal 调度。资源较少时用 `-n 2`，串行定位用 `-n 0`，高核心机器可显式 `-n 8`。保留每项测试的超时和失败，不重跑来掩盖错误。

CI 保留 Linux、macOS、Windows × Python 3.11、3.12、3.13 的 9 个组合。前端构建一次供同次 CI 打包使用；所有检查通过后，分别验证 wheel 和 sdist 的实际隔离安装。Python 3.14 不是已声明的验收版本。

网络测试应依据持久业务状态等待结果。短工具等待返回 pending 是合法状态，保留 request_id 查询最终结果，不重发请求，也不把它变成模拟任务失败。延迟 ACK、去重、降级和最终队列清空仍需真实验证。

## 同步版本与 CHANGELOG

下一补丁版本示例：

```sh
uv run python scripts/bump_version.py 0.4.5 --dry-run
uv run python scripts/bump_version.py 0.4.5
```

脚本同步 pyproject.toml、uv.lock 与 web/package.json，不自动提交、推送或打 tag。包版本来自安装元数据，无需再维护一份源码常量。

CHANGELOG 使用 `## v<版本> 标题`，按最终用户的行为与影响撰写，段落末尾用 `---`。不自动拼接提交记录。预发布可使用三段公开 PEP 440 版本，如 0.4.5rc1，标签会生成 prerelease。

## 构建分发包

```sh
pnpm --dir web build
uv run chiliarch schema --output src/chiliarch/protocol/schema.json
uv build
uv run twine check dist/*
uv run --isolated --no-project --with dist/*.whl scripts/smoke_test.py
uv run --isolated --no-project --with dist/*.tar.gz scripts/smoke_test.py
uv run python scripts/release_notes.py validate --tag v0.4.5
uv run python scripts/release_notes.py extract --tag v0.4.5
```

版本示例要换成当前准备发布的版本。清理旧 dist 或显式选择本轮文件，避免通配符同时选中不同版本。

构建产物应包含 WebUI 与独立协议 Schema。前端资源是生成文件，不提交到 Git；协议 Schema 是受版本管理的契约，变更后需重新导出并验证。Wheel 与 sdist 使用 Core Metadata 2.4 和 MIT SPDX 信息。

## PyPI Trusted Publishing

仓库 [69gg/chiliarch](https://github.com/69gg/chiliarch) 的 release.yml 使用 GitHub 环境 pypi 和 OIDC，不需要 PyPI API Token。

Publisher 对应字段：

| 字段 | 值 |
| --- | --- |
| Project | chiliarch |
| Owner | 69gg |
| Repository | chiliarch |
| Workflow filename | release.yml |
| Environment | pypi |

已有项目在 PyPI Manage → Publishing 配置 publisher；首次创建使用账户的 Pending Publisher。GitHub pypi 环境如限制部署来源，允许 Tag 类型的 v*；是否要求审核按维护者选择。

[PyPI OIDC 说明](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/)

## 发布步骤

1. 完成代码、用户文档、行为测试、版本和 CHANGELOG。
2. 使用 GPG 签名提交并推送 main，确认**该提交**的全部 CI 成功。
3. 对同一提交创建并推送签名 v* tag。
4. 在 Actions → Release 确认校验、构建、PyPI 发布和 GitHub Release 全部成功。

以待发布 v0.4.5 为例，在获得发布授权后：

```sh
git push origin main
git tag -s v0.4.5 -m "Release v0.4.5"
git push origin v0.4.5
```

Release 工作流检查 Python 3.11–3.13、tag / 版本 / CHANGELOG 一致性，构建并校验包，通过 uv Trusted Publishing 上传同一组产物，再创建 GitHub Release。发布说明读取对应 CHANGELOG 条目。

代码已推送、tag 已创建或本地构建成功，不等于 PyPI 已发布。失败时读取具体 job 日志，修复原因再继续，不能重复上传已经存在的同版本包。
