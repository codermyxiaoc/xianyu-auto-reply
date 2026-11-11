import { Package } from 'lucide-react'
import type { ChatItemCard } from '@/api/chatNew'

export const itemPageUrl = (itemId: string) => /^\d+$/.test(itemId)
  ? `https://www.goofish.com/item?id=${encodeURIComponent(itemId)}` : ''

export function itemPriceText(value: string) {
  const price = value?.trim() || ''
  return !price ? '价格未提供' : /^\d+(?:\.\d+)?$/.test(price) ? `¥${price}` : price
}

export function itemImageUrl(value: string) {
  try {
    const url = new URL(value, window.location.origin)
    return value && ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password ? url.href : ''
  } catch { return '' }
}

export function ItemCardMessage({ card, interactive = true }: { card: ChatItemCard; interactive?: boolean }) {
  const image = itemImageUrl(card.mainPic)
  const href = itemPageUrl(card.itemId)
  const content = <>
    <div className="flex min-w-0 gap-3 p-3">
      {image ? <img src={image} alt="宝贝图片" referrerPolicy="no-referrer" className="h-16 w-16 shrink-0 rounded-lg bg-slate-100 object-cover" />
        : <div className="flex h-16 w-16 shrink-0 items-center justify-center rounded-lg bg-slate-100 text-slate-400"><Package className="h-6 w-6" /></div>}
      <div className="min-w-0 flex-1">
        <p className="line-clamp-2 break-words text-sm font-medium text-slate-800 dark:text-slate-100">{card.title || card.itemId}</p>
        <p className="mt-2 text-sm font-semibold text-red-500">{itemPriceText(card.price)}</p>
      </div>
    </div>
    <div className="border-t border-slate-100 px-3 py-1.5 text-xs text-slate-400 dark:border-slate-700">闲鱼宝贝</div>
  </>
  const className = 'block w-72 max-w-full overflow-hidden rounded-xl border border-slate-200 bg-white text-left dark:border-slate-600 dark:bg-slate-800'
  return interactive && href
    ? <a href={href} target="_blank" rel="noopener noreferrer" aria-label={`查看宝贝：${card.title || card.itemId}`} className={`${className} transition hover:border-blue-400`}>{content}</a>
    : <div className={className}>{content}</div>
}
