"""闲鱼 contentType=7 商品卡片的纯数据规范化，不信任消息内的跳转地址。"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import re
from urllib.parse import urlsplit


MAX_ITEM_ID = 9223372036854775807


def _item_id(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return ""
    if isinstance(value, int) and not 0 < value <= MAX_ITEM_ID:
        return ""
    raw = str(value).strip()
    if not re.fullmatch(r"[1-9][0-9]{0,18}", raw):
        return ""
    return raw if int(raw) <= MAX_ITEM_ID else ""


def _price(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return ""
    if isinstance(value, int) and not 0 <= value <= 9999999999:
        return ""
    raw = str(value).strip()
    if raw.startswith(("¥", "￥")):
        raw = raw[1:].strip()
    if not raw or len(raw) > 64:
        return ""
    try:
        amount = Decimal(raw)
        if not amount.is_finite() or amount < 0 or amount > Decimal("9999999999.99"):
            return ""
        rounded = amount.quantize(Decimal("0.01"))
        if rounded != amount:
            return ""
        return f"¥{abs(rounded):.2f}"
    except (InvalidOperation, ValueError):
        return ""


def _image_url(value: object) -> str:
    if not isinstance(value, str) or len(value) > 4096:
        return ""
    # urlsplit 会忽略某些控制字符；检查原始值，不能让危险输入在解析时被洗白。
    if "\\" in value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        return ""
    url = value.strip()
    if not url or any(char.isspace() for char in url):
        return ""
    if url.startswith("//"):
        url = "https:" + url
    try:
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
            return ""
        if parsed.username is not None or parsed.password is not None:
            return ""
        # 读取 port 可拒绝非数字端口或超范围端口。
        parsed.port
    except ValueError:
        return ""
    return url


def normalize_item_card(raw_item: object) -> dict[str, str] | None:
    """规范化 itemCard.item；ID 无效返回 None，其他无效字段置空。

    接收端仍可展示安全的商品占位卡；发送端应进一步要求标题、价格和图片齐全。
    itemId 始终为字符串，避免浏览器丢失大整数精度。
    """
    if not isinstance(raw_item, dict):
        return None
    item_id = _item_id(raw_item.get("itemId"))
    if not item_id:
        return None
    title = raw_item.get("title")
    return {
        "itemId": item_id,
        "title": title.strip() if isinstance(title, str) else "",
        "price": _price(raw_item.get("price")),
        "mainPic": _image_url(raw_item.get("mainPic")),
        "url": f"https://www.goofish.com/item?id={item_id}",
    }


def parse_item_card(content: object) -> dict[str, str] | None:
    """从已解码的 contentType=7 载荷获取安全商品信息。"""
    if not isinstance(content, dict) or content.get("contentType") not in (7, "7"):
        return None
    card = content.get("itemCard")
    return normalize_item_card(card.get("item")) if isinstance(card, dict) else None
