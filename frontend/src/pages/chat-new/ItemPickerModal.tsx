import { useEffect, useId, useRef, useState } from 'react'
import { Loader2, Package, RefreshCw, Search, Send, X } from 'lucide-react'
import { getChatItems, type ChatItemsPage, type ChatSendableItem } from '@/api/chatNew'
import { fetchAllItemsFromAccount } from '@/api/items'
import { itemImageUrl, itemPageUrl, itemPriceText } from './ItemCardMessage'

interface Props {
  accountId: string
  accountLabel: string
  recipientLabel: string
  onClose: () => void
  onSend: (item: ChatSendableItem) => Promise<void>
}

export function ItemPickerModal({ accountId, accountLabel, recipientLabel, onClose, onSend }: Props) {
  const titleId = useId()
  const modalRef = useRef<HTMLDivElement>(null)
  const closeRef = useRef(onClose)
  closeRef.current = onClose
  const requestRef = useRef(0)
  const mountedRef = useRef(false)
  const sendingRef = useRef(false)
  const synchronizingRef = useRef(false)
  const [query, setQuery] = useState('')
  const [keyword, setKeyword] = useState('')
  const [page, setPage] = useState(1)
  const [revision, setRevision] = useState(0)
  const [data, setData] = useState<ChatItemsPage | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [sendError, setSendError] = useState('')
  const [sendingItemId, setSendingItemId] = useState('')
  const [synchronizing, setSynchronizing] = useState(false)

  useEffect(() => {
    mountedRef.current = true
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    modalRef.current?.querySelector<HTMLInputElement>('input')?.focus()
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); closeRef.current(); return }
      if (event.key !== 'Tab' || !modalRef.current) return
      const candidates = Array.from(modalRef.current.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), a[href]'))
        .filter(element => element.tabIndex >= 0 && element.getClientRects().length > 0)
      const first = candidates[0]
      const last = candidates[candidates.length - 1]
      if (!first) { event.preventDefault(); return }
      if (event.shiftKey && (document.activeElement === first || !modalRef.current.contains(document.activeElement))) {
        event.preventDefault(); last.focus()
      } else if (!event.shiftKey && (document.activeElement === last || !modalRef.current.contains(document.activeElement))) {
        event.preventDefault(); first.focus()
      }
    }
    document.addEventListener('keydown', onKey)
    return () => {
      mountedRef.current = false
      requestRef.current += 1
      document.body.style.overflow = previousOverflow
      document.removeEventListener('keydown', onKey)
      if (previousFocus?.isConnected) previousFocus.focus()
    }
  }, [])

  useEffect(() => {
    const request = ++requestRef.current
    setLoading(true)
    setError('')
    setData(null)
    void getChatItems(accountId, page, keyword).then(result => {
      if (request === requestRef.current) setData(result)
    }).catch(reason => {
      if (request === requestRef.current) setError(reason instanceof Error ? reason.message : '读取我的宝贝失败')
    }).finally(() => {
      if (request === requestRef.current) setLoading(false)
    })
    return () => { requestRef.current += 1 }
  }, [accountId, page, keyword, revision])

  const send = async (item: ChatSendableItem) => {
    if (sendingRef.current || synchronizingRef.current || loading || !item.canSend || !itemPageUrl(item.itemId)) return
    sendingRef.current = true
    setSendingItemId(item.itemId)
    setSendError('')
    try {
      await onSend(item)
      if (mountedRef.current) closeRef.current()
    } catch (reason) {
      if (mountedRef.current) setSendError(reason instanceof Error ? reason.message : '宝贝发送失败，请重试')
    } finally {
      sendingRef.current = false
      if (mountedRef.current) setSendingItemId('')
    }
  }

  const synchronize = async () => {
    if (synchronizingRef.current || sendingRef.current || loading) return
    synchronizingRef.current = true
    setSynchronizing(true)
    setSendError('')
    try {
      const result = await fetchAllItemsFromAccount(accountId)
      if (!mountedRef.current) return
      if (!result.success) throw new Error(result.message || '同步商品失败，请稍后重试')
      setQuery('')
      setKeyword('')
      setPage(1)
      setLoading(true)
      setRevision(value => value + 1)
    } catch (reason) {
      if (mountedRef.current) setSendError(reason instanceof Error ? reason.message : '同步商品失败，请稍后重试')
    } finally {
      synchronizingRef.current = false
      if (mountedRef.current) setSynchronizing(false)
    }
  }

  return <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/40 p-3 backdrop-blur-sm sm:p-6">
    <div ref={modalRef} role="dialog" aria-modal="true" aria-labelledby={titleId} className="flex max-h-[90vh] w-full max-w-2xl flex-col overflow-hidden rounded-2xl bg-white shadow-xl dark:bg-slate-900">
      <div className="flex shrink-0 items-start justify-between gap-3 border-b border-slate-100 p-4 dark:border-slate-800">
        <div className="min-w-0">
          <h2 id={titleId} className="font-semibold text-slate-900 dark:text-slate-100">发送我的宝贝</h2>
          <p className="mt-1 break-words text-xs text-slate-500">账号：{accountLabel}（{accountId}）</p>
          <p className="mt-1 break-words text-xs text-slate-500">接收人：{recipientLabel}</p>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          <button type="button" onClick={() => void synchronize()} disabled={loading || synchronizing || !!sendingItemId} className="inline-flex items-center gap-1 rounded-lg px-2 py-1.5 text-xs text-blue-600 hover:bg-blue-50 disabled:opacity-40 dark:text-blue-300 dark:hover:bg-blue-950/30">{synchronizing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}{synchronizing ? '正在同步…' : '同步商品'}</button>
          <button type="button" onClick={onClose} aria-label="关闭我的宝贝" className="rounded-lg p-1.5 text-slate-400 hover:bg-slate-100 dark:hover:bg-slate-800"><X className="h-5 w-5" /></button>
        </div>
      </div>
      <form className="flex shrink-0 gap-2 border-b border-slate-100 p-4 dark:border-slate-800" onSubmit={event => {
        event.preventDefault(); if (synchronizingRef.current) return; requestRef.current += 1; setPage(1); setKeyword(query.trim()); setRevision(value => value + 1)
      }}>
        <input value={query} onChange={event => setQuery(event.target.value)} disabled={synchronizing} aria-label="搜索我的宝贝" placeholder="搜索商品标题或商品 ID" maxLength={100} className="input-ios min-w-0 flex-1 disabled:opacity-40" />
        <button type="submit" aria-label="搜索宝贝" disabled={synchronizing} className="inline-flex shrink-0 items-center gap-1 rounded-lg bg-blue-600 px-3 py-2 text-sm text-white hover:bg-blue-700 disabled:opacity-40"><Search className="h-4 w-4" />搜索</button>
      </form>
      <div className="min-h-0 flex-1 overflow-y-auto p-4">
        {sendError && <p role="alert" className="mb-3 rounded-lg bg-red-50 p-3 text-sm text-red-600 dark:bg-red-950/30">{sendError}</p>}
        {loading ? <div role="status" className="flex items-center justify-center gap-2 py-12 text-sm text-slate-400"><Loader2 className="h-4 w-4 animate-spin" />正在读取宝贝…</div>
          : error ? <div role="alert" className="py-8 text-center text-sm text-red-600"><p>{error}</p><button type="button" onClick={() => setRevision(value => value + 1)} className="mt-3 text-blue-600 underline">重新加载宝贝</button></div>
          : !data?.items.length ? <p className="py-12 text-center text-sm text-slate-400">{keyword ? '没有找到匹配的宝贝' : '暂无商品，点击同步商品从闲鱼获取'}</p>
          : <div className="space-y-3">{data.items.map(item => {
            const image = itemImageUrl(item.mainPic)
            const canSend = item.canSend && !!itemPageUrl(item.itemId)
            return <div key={item.itemId} className="flex items-start gap-3 rounded-xl border border-slate-200 p-3 dark:border-slate-700">
              {image ? <img src={image} alt="宝贝图片" referrerPolicy="no-referrer" className="h-16 w-16 shrink-0 rounded-lg bg-slate-100 object-cover sm:h-20 sm:w-20" />
                : <div className="flex h-16 w-16 shrink-0 items-center justify-center rounded-lg bg-slate-100 text-slate-400 sm:h-20 sm:w-20"><Package className="h-6 w-6" /></div>}
              <div className="min-w-0 flex-1">
                <p className="line-clamp-2 break-words text-sm font-medium text-slate-800 dark:text-slate-100">{item.title || item.itemId}</p>
                <p className="mt-1 break-all text-xs text-slate-400">ID：{item.itemId}</p>
                <div className="mt-2 flex flex-wrap items-center justify-between gap-2">
                  <span className="font-semibold text-red-500">{itemPriceText(item.price)}</span>
                  <button type="button" onClick={() => void send(item)} disabled={!canSend || !!sendingItemId || synchronizing} aria-label={`发送宝贝 ${item.title || item.itemId}`} className="inline-flex items-center gap-1 rounded-lg bg-blue-600 px-3 py-1.5 text-xs text-white hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-40">{sendingItemId === item.itemId ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Send className="h-3.5 w-3.5" />}{sendingItemId === item.itemId ? '正在发送…' : '发送'}</button>
                </div>
                {!canSend && <p className="mt-2 text-xs text-amber-600">{item.unavailableReason || '商品资料不完整，请点击同步商品后重试'}</p>}
              </div>
            </div>
          })}</div>}
      </div>
      <div className="flex shrink-0 items-center justify-between gap-3 border-t border-slate-100 p-4 text-xs text-slate-500 dark:border-slate-800">
        <span>共 {data?.total ?? 0} 件 · 第 {page} / {Math.max(1, data?.total_pages ?? 1)} 页</span>
        <div className="flex gap-2"><button type="button" disabled={loading || synchronizing || page <= 1} onClick={() => setPage(value => value - 1)} className="rounded-lg border border-slate-200 px-3 py-2 disabled:opacity-40 dark:border-slate-700">上一页</button><button type="button" disabled={loading || synchronizing || !data || page >= data.total_pages} onClick={() => setPage(value => value + 1)} className="rounded-lg border border-slate-200 px-3 py-2 disabled:opacity-40 dark:border-slate-700">下一页</button></div>
      </div>
    </div>
  </div>
}
