import { NativeSelect } from '@/components/common/NativeSelect'
import { useEffect, useRef, useState } from 'react'
import { Loader2, Search, X } from 'lucide-react'
import { getMessages, nextChatHistorySearch, startChatHistorySearch,
  type ChatHistoryHit, type ChatHistorySearchBatch, type ChatMessage } from '@/api/chatNew'
import { mergeMessages, toTimestampMs } from './chat_history_merge'
import { ItemCardMessage } from './ItemCardMessage'

function when(time: number) {
  const timestamp = Number(time)
  return Number.isFinite(timestamp) && timestamp > 0 ? new Date(toTimestampMs(timestamp)).toLocaleString('zh-CN', { hour12: false }) : ''
}

function Highlight({ text, query }: { text: string; query: string }) {
  const index = text.toLocaleLowerCase().indexOf(query.toLocaleLowerCase())
  if (!query || index < 0) return <>{text}</>
  return <>{text.slice(0, index)}<mark className="rounded bg-amber-200 text-slate-900">{text.slice(index, index + query.length)}</mark>{text.slice(index + query.length)}</>
}

export function ChatHistorySearch({ accountId, onModeChange, onOpenConversation }: {
  accountId: string
  onModeChange: (searching: boolean) => void
  onOpenConversation: (hit: ChatHistoryHit) => void
}) {
  const [query, setQuery] = useState('')
  const [scope, setScope] = useState('all')
  const [batch, setBatch] = useState<ChatHistorySearchBatch | null>(null)
  const [hits, setHits] = useState<ChatHistoryHit[]>([])
  const [visibleCount, setVisibleCount] = useState(50)
  const [starting, setStarting] = useState(false)
  const [running, setRunning] = useState(false)
  const [error, setError] = useState('')
  const generation = useRef(0)
  const polling = useRef(false)
  const [pollSettled, setPollSettled] = useState(0)
  const [selected, setSelected] = useState<ChatHistoryHit | null>(null)
  const [context, setContext] = useState<ChatMessage[]>([])
  const [contextError, setContextError] = useState('')
  const [loadingContext, setLoadingContext] = useState(false)
  const contextGeneration = useRef(0)
  const targetRef = useRef<HTMLDivElement>(null)

  const active = starting || !!batch || !!error
  useEffect(() => { onModeChange(active) }, [active, onModeChange])
  useEffect(() => () => { generation.current += 1; contextGeneration.current += 1 }, [])

  const clear = () => {
    generation.current += 1
    contextGeneration.current += 1
    setSelected(null)
    setContext([])
    setContextError('')
    setLoadingContext(false)
    setRunning(false)
    setStarting(false)
    setBatch(null)
    setHits([])
    setVisibleCount(50)
    setError('')
  }

  // 当前账号范围切换后，不把旧账号的结果留在新搜索条件下。
  const previousAccount = useRef(accountId)
  useEffect(() => {
    if (previousAccount.current !== accountId && scope === 'current') clear()
    previousAccount.current = accountId
  }, [accountId, scope]) // eslint-disable-line react-hooks/exhaustive-deps

  const start = async () => {
    if (!query.trim() || starting) return
    clear()
    const request = generation.current
    setStarting(true)
    try {
      const result = await startChatHistorySearch(query.trim(), scope === 'current' ? accountId : undefined)
      if (request !== generation.current) return
      setBatch(result)
      setRunning(true)
    } catch (reason) {
      if (request === generation.current) setError(reason instanceof Error ? reason.message : '无法开始搜索')
    } finally {
      if (request === generation.current) setStarting(false)
    }
  }

  useEffect(() => {
    if (!batch || !running || batch.complete || polling.current) return
    const request = generation.current
    polling.current = true
    void nextChatHistorySearch(batch.search_id, batch.revision).then(result => {
      if (request !== generation.current) return
      setBatch(result)
      setHits(previous => {
        const unique = new Map(previous.map(hit => [hit.key, hit]))
        result.results.forEach(hit => unique.set(hit.key, hit))
        return [...unique.values()]
      })
      setError(result.error || '')
      if (result.complete || result.error) {
        setRunning(false)
      }
    }).catch(reason => {
      if (request !== generation.current) return
      setError(reason instanceof Error ? reason.message : '搜索暂时失败，请继续搜索')
      setRunning(false)
    }).finally(() => {
      polling.current = false
      // 新查询可在旧请求结束后开始；暂停时仍接收当前批结果，但不再读下一批。
      setPollSettled(value => value + 1)
    })
  }, [batch, running, pollSettled])

  const openHit = async (hit: ChatHistoryHit) => {
    if (!hit.message) { onOpenConversation(hit); return }
    const request = ++contextGeneration.current
    setSelected(hit)
    setContext([hit.message])
    setContextError('')
    setLoadingContext(true)
    try {
      // 原始 IM 游标和消息时间使用相同单位；加 1 包含命中记录本身。
      const cursor = Number(hit.message.time) + 1
      if (!Number.isSafeInteger(cursor) || cursor <= 0) throw new Error('该消息时间无效，无法读取附近记录，请打开会话查看')
      const page = await getMessages(hit.account.account_id, hit.conversation.cid, cursor, 20)
      if (request === contextGeneration.current) setContext(mergeMessages(page.messages, [hit.message]))
    } catch (reason) {
      if (request === contextGeneration.current) setContextError(reason instanceof Error ? reason.message : '暂时无法读取上下文')
    } finally {
      if (request === contextGeneration.current) setLoadingContext(false)
    }
  }

  useEffect(() => { targetRef.current?.scrollIntoView({ block: 'center' }) }, [context])
  useEffect(() => {
    if (!selected) return
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { contextGeneration.current += 1; setSelected(null) }
    }
    window.addEventListener('keydown', closeOnEscape)
    return () => window.removeEventListener('keydown', closeOnEscape)
  }, [selected])

  return (
    <div className={`flex min-h-0 flex-col ${active ? 'flex-1' : 'shrink-0'}`}>
      <form onSubmit={event => { event.preventDefault(); void start() }} className="space-y-2 border-b border-gray-200 p-3 dark:border-gray-700">
        <div className="flex gap-1.5">
          <input aria-label="搜索全部聊天记录" value={query} maxLength={100} onChange={event => { clear(); setQuery(event.target.value) }} placeholder="搜索昵称、用户ID、聊天内容" className="min-w-0 flex-1 rounded-lg border border-gray-300 bg-transparent px-2 py-2 text-xs outline-none focus:border-blue-500 dark:border-gray-600" />
          <button type="submit" aria-label="开始搜索聊天记录" disabled={!query.trim() || starting || (scope === 'current' && !accountId)} className="rounded-lg bg-blue-600 px-2 text-white disabled:opacity-40">{starting ? <Loader2 className="h-4 w-4 animate-spin" /> : <Search className="h-4 w-4" />}</button>
        </div>
        <div className="flex items-center justify-between gap-2">
          <NativeSelect aria-label="聊天搜索范围" value={scope} onChange={event => { clear(); setScope(event.target.value) }} className="min-w-0 rounded border border-gray-200 bg-transparent px-1 py-1 text-xs dark:border-gray-600 dark:bg-gray-800">
            <option value="all">全部已连接账号</option><option value="current" disabled={!accountId}>当前账号全部会话</option>
          </NativeSelect>
          {active && <button type="button" onClick={clear} className="shrink-0 text-xs text-gray-500 hover:text-blue-600">退出搜索</button>}
        </div>
        <p className="text-[11px] leading-4 text-gray-400">逐页搜索全部用户历史，未连接账号需先连接。</p>
      </form>
      {active && <>
        <div role="status" className="space-y-2 border-b border-gray-100 px-3 py-2 text-xs dark:border-gray-700">
          {batch && <><div className="flex items-center justify-between"><span className="font-medium text-blue-600">{batch.complete ? '搜索完成' : running ? '正在搜索…' : '搜索已暂停'} · {hits.length} 条结果</span>{!batch.complete && <button type="button" onClick={() => { setRunning(!running); setError('') }} className="text-blue-600 hover:underline">{running ? '暂停' : '继续搜索'}</button>}</div><p className="text-gray-500">已查 {batch.scanned_conversations} 个会话 / {batch.scanned_messages} 条消息；账号 {batch.finished_accounts}/{batch.total_accounts}</p>{batch.skipped_accounts > 0 && <p className="text-amber-600">另有 {batch.skipped_accounts} 个账号未连接，未纳入本次搜索。</p>}</>}
          {error && <p role="alert" className="break-words text-amber-600">{error}</p>}
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto">
          {hits.slice(0, visibleCount).map(hit => <button type="button" key={hit.key} onClick={() => void openHit(hit)} className="block w-full border-b border-gray-100 px-3 py-3 text-left hover:bg-blue-50 dark:border-gray-700 dark:hover:bg-gray-700">
            <div className="truncate text-sm font-medium"><Highlight text={hit.conversation.otherUserName || hit.conversation.otherUserId || '未知用户'} query={batch?.query || ''} /></div>
            {hit.message?.type === 'item' && hit.message.itemCard
              ? <div className="mt-2"><ItemCardMessage card={hit.message.itemCard} interactive={false} /></div>
              : <p className="mt-1 line-clamp-3 break-words text-xs leading-5 text-gray-600 dark:text-gray-300"><Highlight text={hit.message?.text || `用户 ID：${hit.conversation.otherUserId}`} query={batch?.query || ''} /></p>}
            <p className="mt-1 truncate text-[11px] text-gray-400">{hit.account.display_name || hit.account.remark || hit.account.account_id}{hit.message ? ` · ${when(hit.message.time)}` : ' · 会话匹配'}</p>
          </button>)}
          {hits.length > visibleCount && <button type="button" onClick={() => setVisibleCount(count => count + 50)} className="w-full px-3 py-4 text-xs text-blue-600 hover:bg-blue-50 dark:hover:bg-gray-700">显示更多结果（还有 {hits.length - visibleCount} 条）</button>}
          {!hits.length && <p className="px-3 py-10 text-center text-xs text-gray-400">{batch?.complete ? '没有找到匹配记录' : starting || running ? '正在读取历史，匹配记录会陆续显示' : '当前尚未找到记录，可继续搜索'}</p>}
        </div>
      </>}
      {selected && <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/40 p-4" role="dialog" aria-modal="true" aria-label="搜索记录上下文">
        <div className="flex max-h-[85vh] w-full max-w-2xl flex-col rounded-xl bg-white shadow-xl dark:bg-gray-800">
          <div className="flex items-center justify-between border-b p-4 dark:border-gray-700"><div className="min-w-0"><h2 className="font-semibold">{selected.conversation.otherUserName || selected.conversation.otherUserId}</h2><p className="mt-1 truncate text-xs text-gray-400">搜索记录 · {selected.account.display_name || selected.account.account_id}</p></div><button type="button" aria-label="关闭搜索记录" onClick={() => { contextGeneration.current += 1; setSelected(null) }} className="rounded p-2 hover:bg-gray-100 dark:hover:bg-gray-700"><X className="h-5 w-5" /></button></div>
          <div className="min-h-0 flex-1 space-y-3 overflow-y-auto p-4">
            {loadingContext && <p className="text-center text-xs text-gray-400">正在读取附近聊天记录…</p>}
            {contextError && <p role="alert" className="text-xs text-amber-600">{contextError}</p>}
            {context.map((message, index) => { const match = !!selected.message && (message.messageId ? message.messageId === selected.message.messageId : message.time === selected.message.time && message.text === selected.message.text); return <div key={message.messageId || message.clientMessageId || index} ref={match ? targetRef : undefined} className={`rounded-lg border p-3 ${match ? 'border-blue-400 bg-blue-50 dark:bg-blue-950/30' : 'border-gray-100 dark:border-gray-700'}`}><p className="mb-2 text-xs text-gray-400">{message.isSelf ? '我' : message.senderName || selected.conversation.otherUserName} · {when(message.time)}{match && <span className="ml-2 text-blue-600">匹配消息</span>}</p>{message.type === 'item' && message.itemCard ? <ItemCardMessage card={message.itemCard} /> : <p className="whitespace-pre-wrap break-words text-sm"><Highlight text={message.text || (message.type === 'image' ? '[图片]' : '[消息]')} query={batch?.query || query} /></p>}</div> })}
          </div>
          <div className="flex justify-end border-t p-4 dark:border-gray-700"><button type="button" onClick={() => { onOpenConversation(selected); contextGeneration.current += 1; setSelected(null) }} className="rounded-lg bg-blue-600 px-4 py-2 text-sm text-white">打开会话</button></div>
        </div>
      </div>}
    </div>
  )
}
