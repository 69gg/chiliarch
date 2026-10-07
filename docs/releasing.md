# 开发、测试与发版

## 本地验证

Python 默认 3.13，CI 必须覆盖 3.11、3.12、3.13；3.14 可另行验证，不因 requires-python 无上限就声明所有未来版本支持。

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

生产静态资源为生成文件，不提交到 Git；`static/.gitkeep` 保留构建目录。前端 build 输出到 Python package，Hatch wheel/sdist 强制包含该目录；协议 Schema 为受版本管理的公开契约，修改模型后重新导出并验证。

## 升级版本

```bash
uv run python scripts/bump_version.py 0.2.0 --dry-run
uv run python scripts/bump_version.py 0.2.0
```

脚本使用 uv version 同步 pyproject 与 uv.lock，再同步 web/package.json。包 `__version__` 来自安装元数据，不再维护第二个源码版本字符串。同版本重跑可以修复前端版本不一致；失败时恢复受管理文件。脚本不自动提交、打 tag 或推送。

支持公开三段 PEP 440 版本，例如 0.2.0、0.2.0rc1；拒绝本地版本号和 epoch。CHANGELOG 标题使用 `## v<版本> 标题`，下方写概要、按用户影响分组的要点和 `---`。不要自动拼接提交历史，也不要把文档同步/测试数量当作发布主题。

预发布标签会生成 GitHub prerelease，并且不更新 latest。wheel 与 sdist 明确使用 Core Metadata 2.4，兼容当前 Twine 校验器，同时保留 MIT SPDX 许可证信息。

## 构建并验证实际包

```bash
pnpm --dir web build
uv run chiliarch schema --output src/chiliarch/protocol/schema.json
uv build
uv run twine check dist/*
uv run --isolated --no-project --with dist/*.whl scripts/smoke_test.py
uv run --isolated --no-project --with dist/*.tar.gz scripts/smoke_test.py
uv run python scripts/release_notes.py validate --tag v0.1.0
uv run python scripts/release_notes.py extract --tag v0.1.0
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

首次发布 v0.1.0，先将代码推送到上述 GitHub 仓库，再推送签名标签。以下第一条命令只在尚未配置 `origin` 时执行：

```bash
git remote add origin https://github.com/69gg/chiliarch.git
git push -u origin main
git tag -s v0.1.0 -m "v0.1.0"
git push origin v0.1.0
```

在 GitHub **Actions → Release** 查看运行结果；若设置了环境审核，批准 `pypi` 部署后继续发布。成功后用户及 agent 均默认使用 `uv tool install chiliarch` 从 PyPI 安装。
