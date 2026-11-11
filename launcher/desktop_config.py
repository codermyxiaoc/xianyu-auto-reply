"""桌面端连接配置：启动任何本地依赖前确定本地或服务器模式。"""
from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import json
from pathlib import Path
import re
from urllib.parse import urlsplit


CONFIG_FILENAME = "desktop-config.json"
LOCAL_FRONTEND_URL = "http://127.0.0.1:9000"


class DesktopConfigError(ValueError):
    """配置错误应终止启动，不能回退到另一个后端。"""


@dataclass(frozen=True)
class DesktopConfig:
    mode: str
    base_url: str


def resolve_desktop_config(base_url: str) -> DesktopConfig:
    """接受 local 或系统首页的 HTTP(S) origin，不接受单独 API 地址。"""
    if not isinstance(base_url, str) or not base_url or len(base_url) > 4096:
        raise DesktopConfigError("base_url 必须是 local 或完整的服务器首页地址。")
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in base_url):
        raise DesktopConfigError("base_url 不能包含空格或控制字符。")
    if base_url.lower() == "local":
        return DesktopConfig(mode="local", base_url=LOCAL_FRONTEND_URL)
    if any(char in base_url for char in ("\\", "?", "#")):
        raise DesktopConfigError("base_url 只填写服务器首页地址，不能包含查询参数、片段或反斜杠。")

    try:
        parsed = urlsplit(base_url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise DesktopConfigError("base_url 的主机或端口格式不正确。") from exc
    if parsed.scheme not in ("http", "https") or not hostname:
        raise DesktopConfigError("base_url 必须使用 http:// 或 https://，或填写 local。")
    if parsed.username is not None or parsed.password is not None:
        raise DesktopConfigError("base_url 不能包含用户名或密码。")
    if not re.fullmatch(r"(?:\[[^\[\]]+\]|[^:\[\]]+)(?::[0-9]+)?", parsed.netloc):
        raise DesktopConfigError("base_url 的主机和端口格式不正确。")
    if parsed.path not in ("", "/"):
        raise DesktopConfigError("base_url 请填写系统首页地址，不要附加 /api、/api/v1 或其他路径。")
    if parsed.netloc.endswith(":") or port == 0:
        raise DesktopConfigError("base_url 的端口必须为 1–65535。")
    if "%" in hostname:
        raise DesktopConfigError("base_url 的主机名不能包含百分号或 IPv6 区域标识。")

    # ipaddress 不接受浏览器会自动补全的 127.1、十六进制 IP 等歧义写法。
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
        try:
            hostname = hostname.encode("idna").decode("ascii").lower().rstrip(".")
        except UnicodeError as exc:
            raise DesktopConfigError("base_url 的主机名格式不正确。") from exc
        labels = hostname.split(".")
        if (len(hostname) > 253 or not all(
                re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                for label in labels)
                or re.fullmatch(r"[0-9.]+", hostname)
                or re.fullmatch(r"(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|[0-9]+))*", hostname)):
            raise DesktopConfigError("base_url 的主机名不正确，请使用完整的域名或标准 IP 地址。")
    else:
        if address.is_unspecified:
            raise DesktopConfigError("0.0.0.0 或 :: 是监听地址，请填写实际服务器地址或 local。")
        hostname = address.compressed

    is_local = (hostname == "localhost" or hostname.endswith(".localhost")
                or (address is not None and address.is_loopback)
                or (isinstance(address, ipaddress.IPv6Address)
                    and address.ipv4_mapped is not None and address.ipv4_mapped.is_loopback))
    if is_local:
        if parsed.scheme != "http" or port != 9000:
            raise DesktopConfigError("本地模式请填写 local 或 http://127.0.0.1:9000，本地服务使用固定端口。")
        return DesktopConfig(mode="local", base_url=LOCAL_FRONTEND_URL)

    authority = f"[{hostname}]" if isinstance(address, ipaddress.IPv6Address) else hostname
    if port is not None and port != {"http": 80, "https": 443}[parsed.scheme]:
        authority += f":{port}"
    return DesktopConfig(mode="remote", base_url=f"{parsed.scheme}://{authority}")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DesktopConfigError(f"{CONFIG_FILENAME} 存在重复配置项：{key}。")
        result[key] = value
    return result


def load_desktop_config(project_root: Path) -> DesktopConfig:
    """仅从 EXE / 项目根目录读取配置；旧版本没有配置文件时沿用本地模式。"""
    config_path = Path(project_root) / CONFIG_FILENAME
    try:
        with config_path.open("rb") as stream:
            raw = stream.read(8193)
    except FileNotFoundError:
        return resolve_desktop_config("local")
    except OSError as exc:
        raise DesktopConfigError(f"无法读取 {CONFIG_FILENAME}，请检查文件及访问权限。") from exc
    if len(raw) > 8192:
        raise DesktopConfigError(f"{CONFIG_FILENAME} 过大，请仅保留 base_url 配置。")
    try:
        payload = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DesktopConfigError(f"{CONFIG_FILENAME} 必须是有效的 UTF-8 JSON 文件。") from exc
    if not isinstance(payload, dict) or set(payload) != {"base_url"}:
        raise DesktopConfigError(f'{CONFIG_FILENAME} 仅支持一个必填字段，例如 {{"base_url": "local"}}。')
    return resolve_desktop_config(payload["base_url"])
