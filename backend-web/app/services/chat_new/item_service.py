"""在线聊天的本账号商品目录，只输出卡片所需字段，不接受客户端商品内容。"""
from __future__ import annotations

import json

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from common.models import XYAccount, XYCatalogItem
from common.utils.xianyu_item_card import normalize_item_card


def _detail_object(metadata: object) -> dict:
    if not isinstance(metadata, dict):
        return {}
    detail = metadata.get("detail")
    if isinstance(detail, dict):
        return detail
    if isinstance(detail, str):
        try:
            value = json.loads(detail)
            return value if isinstance(value, dict) else {}
        except (ValueError, TypeError):
            pass
    return {}


def serialize_chat_item(item: XYCatalogItem) -> dict:
    """读取同步商品目录的标题、元价格与 pic_info，不从浏览器回传值构造卡片。"""
    detail = _detail_object(item.metadata_json)
    pic_info = detail.get("pic_info")
    main_pic = pic_info.get("url") if isinstance(pic_info, dict) else ""
    # 卖家平台同步结构也保留 image_url。
    main_pic = main_pic or detail.get("image_url") or ""
    card = normalize_item_card({
        "itemId": item.item_id, "title": item.title or "",
        "price": item.price or "", "mainPic": main_pic,
    })
    if card is None:
        return {"itemId": str(item.item_id or ""), "title": item.title or "", "price": "",
                "mainPic": "", "url": "", "canSend": False, "unavailableReason": "商品编号无效，请重新同步商品"}
    reason = ""
    if not card["title"]:
        reason = "缺少商品标题，请点击同步商品后重试"
    elif not card["price"]:
        reason = "商品价格无效，请点击同步商品后重试"
    elif not card["mainPic"]:
        reason = "缺少有效商品图片，请点击同步商品后重试"
    return {**card, "canSend": not reason, **({"unavailableReason": reason} if reason else {})}


class ChatItemService:
    def __init__(self, db: AsyncSession):
        self.db = db

    @staticmethod
    def _scope(account: XYAccount):
        # 管理员也必须限定选定闲鱼账号及其owner，不能误发别人的商品。
        return (XYCatalogItem.account_pk == account.id, XYCatalogItem.owner_id == account.owner_id)

    async def list_items(self, account: XYAccount, page: int, page_size: int, keyword: str) -> dict:
        conditions = list(self._scope(account))
        if keyword.strip():
            # 搜索字面关键词，%/_ 不扩大匹配范围。
            pattern = keyword.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            conditions.append(or_(XYCatalogItem.title.like(f"%{pattern}%", escape="\\"),
                                  XYCatalogItem.item_id.like(f"%{pattern}%", escape="\\")))
        total = int((await self.db.execute(select(func.count()).select_from(XYCatalogItem).where(*conditions))).scalar() or 0)
        rows = (await self.db.execute(select(XYCatalogItem).where(*conditions)
            .order_by(XYCatalogItem.created_at.desc(), XYCatalogItem.id.desc())
            .offset((page - 1) * page_size).limit(page_size))).scalars().all()
        return {"items": [serialize_chat_item(item) for item in rows], "total": total,
                "page": page, "page_size": page_size, "total_pages": (total + page_size - 1) // page_size}

    async def get_item(self, account: XYAccount, item_id: str) -> dict | None:
        row = (await self.db.execute(select(XYCatalogItem).where(*self._scope(account),
            XYCatalogItem.item_id == item_id).order_by(XYCatalogItem.id.desc()).limit(1))).scalar_one_or_none()
        return serialize_chat_item(row) if row is not None else None
