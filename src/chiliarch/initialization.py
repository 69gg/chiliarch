"""Interactive setup of commonly used platform settings without external I/O."""

from __future__ import annotations

import getpass
import ipaddress
from collections.abc import Callable
from urllib.parse import urlsplit

from pydantic import SecretStr, ValidationError

from chiliarch.config import ConfigModel, Settings


def _prompt_setting(
    model: ConfigModel,
    field: str,
    label: str,
    *,
    normalize: Callable[[str], str | int] = str,
    required: bool = False,
) -> None:
    current = getattr(model, field)
    default = str(current) if current not in {"", None} else "留空"
    while True:
        value = input(f"{label} [{default}]: ").strip()
        if not value:
            if required and current in {"", None}:
                print("此项不能为空，请重新输入。")
                continue
            return
        try:
            setattr(model, field, normalize(value))
            return
        except ValidationError as error:
            messages = [
                item["msg"] for item in error.errors(include_input=False, include_url=False)
            ]
            print("输入无效：" + "; ".join(messages))
        except ValueError as error:
            print(f"输入无效：{error}")


def _listen_host(value: str) -> str:
    if any(character.isspace() for character in value) or any(
        character in value for character in "/?#@"
    ):
        raise ValueError("请输入 IP 或主机名，不要填写 URL、路径或端口。")
    if ":" in value:
        try:
            return str(ipaddress.IPv6Address(value.removeprefix("[").removesuffix("]")))
        except ValueError:
            raise ValueError("请输入 IP 或主机名；端口在下一项设置。") from None
    return value


def _http_url(value: str, *, origin: bool = False) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
        valid = (
            parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
            and (not origin or parsed.path in {"", "/"})
            and (port is None or port > 0)
            and not any(character.isspace() for character in value)
        )
    except ValueError:
        valid = False
    if not valid:
        kind = (
            "站点地址（http/https、主机与可选端口，不含路径）" if origin else "HTTP/HTTPS API 地址"
        )
        raise ValueError(f"请输入有效的{kind}，不要包含账号、密码或查询参数。")
    return value.rstrip("/")


def _public_origin(value: str) -> str:
    return _http_url(value, origin=True)


def _integer(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        raise ValueError("请输入整数。") from None


def _configure_model() -> bool:
    while True:
        answer = input("现在配置总管模型？可稍后在 WebUI 配置 [y/N]: ").strip().lower()
        if answer in {"", "n", "no", "否"}:
            return False
        if answer in {"y", "yes", "是"}:
            return True
        print("请输入 y 或 n。")


def prompt_settings(settings: Settings) -> None:
    """Update only a fresh in-memory configuration; persist after all prompts succeed."""
    print("设置常用选项，直接回车采用方括号中的默认值。")
    _prompt_setting(settings.server, "host", "监听地址", normalize=_listen_host)
    _prompt_setting(settings.server, "port", "监听端口")
    _prompt_setting(
        settings.server,
        "public_url",
        "公开访问地址（反向代理后的站点地址，可留空）",
        normalize=_public_origin,
    )
    settings.auth.secure_cookie = settings.server.public_url.startswith("https://")
    if settings.auth.secure_cookie:
        print("公开地址使用 HTTPS，登录 Cookie 已设置为仅通过 HTTPS 发送。")
    _prompt_setting(settings.supervisor, "mode", "审批模式 ask / auto / yolo", normalize=str.lower)
    _prompt_setting(
        settings.logs, "level", "日志级别 DEBUG / INFO / WARNING / ERROR", normalize=str.upper
    )
    if not _configure_model():
        return
    _prompt_setting(
        settings.model,
        "api_mode",
        "模型 API chat_completions / responses / anthropic",
        normalize=str.lower,
    )
    _prompt_setting(settings.model, "model", "模型名称", required=True)
    _prompt_setting(
        settings.model, "base_url", "模型 API 地址（留空使用服务商默认值）", normalize=_http_url
    )
    settings.model.api_key = SecretStr(
        getpass.getpass("模型 API Key（隐藏输入；可留空，稍后配置或使用环境变量）: ").strip()
    )
    if settings.model.api_mode == "anthropic":
        _prompt_setting(
            settings.model,
            "anthropic_max_tokens",
            "Anthropic max_tokens（必填，按所用模型支持范围填写）",
            normalize=_integer,
            required=True,
        )
    _prompt_setting(settings.model, "context_window", "模型上下文窗口（tokens）")
