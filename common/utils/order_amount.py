"""闲鱼订单金额转换：接口显示金额以元为单位，不按数值大小猜测分/元。"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation


def parse_order_amount(value: object) -> Decimal | None:
    """返回可存入订单金额列的非负金额；缺失、非法或超范围时不覆盖旧值。"""
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        amount = Decimal(raw)
        if not amount.is_finite() or amount < 0 or amount > Decimal("9999999999.99"):
            return None
        rounded = amount.quantize(Decimal("0.01"))
        return rounded if amount == rounded else None
    except (InvalidOperation, ValueError):
        return None


def extract_order_detail_amount(components: object) -> tuple[Decimal | None, bool]:
    """优先读取 orderInfoVO.priceInfo.amount.value 成交总价。

    itemInfo.price 是商品原标价，改价后不会跟随变化；只能作为缺失金额的
    估算兜底，不能覆盖已经同步的订单金额。返回值的第二项表示是否估算。
    """
    estimate = None
    if not isinstance(components, list):
        return None, False
    for component in components:
        if not isinstance(component, dict) or component.get("render") != "orderInfoVO":
            continue
        data = component.get("data")
        if not isinstance(data, dict):
            continue
        price_info = data.get("priceInfo")
        if isinstance(price_info, dict):
            amount_info = price_info.get("amount")
            if isinstance(amount_info, dict):
                amount = parse_order_amount(amount_info.get("value"))
                if amount is not None:
                    return amount, False
        item_info = data.get("itemInfo")
        if estimate is None and isinstance(item_info, dict):
            unit_price = parse_order_amount(item_info.get("price"))
            raw_quantity = item_info.get("buyAmount", 1)
            try:
                quantity = int(str(raw_quantity))
            except (TypeError, ValueError):
                quantity = 0
            if unit_price is not None and quantity > 0:
                estimate = parse_order_amount(unit_price * quantity)
    return estimate, estimate is not None
