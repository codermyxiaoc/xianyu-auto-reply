import { useCallback, useEffect, useRef, useState } from 'react'
import { markConversationRead, type ChatMessage, type Conversation, type ConversationReadReceipt } from '@/api/chatNew'
import { toTimestampMs } from './chat_history_merge'

type Boundary = { time: number; ids: Set<string>; lastMessageId: string }
const keyOf = (account: string, cid: string) => JSON.stringify([account, cid])
type MessagePosition = Pick<ChatMessage, 'messageId' | 'time' | 'isSelf'>
// 闲鱼真实 ID 是长十进制字符串，不能转 Number 丢失末位精度。
const compareMessageIds = (left: string, right: string): number | undefined => {
  if (!/^\d+$/.test(left) || !/^\d+$/.test(right)) return undefined
  const a = left.replace(/^0+(?=\d)/, '')
  const b = right.replace(/^0+(?=\d)/, '')
  return a.length - b.length || (a === b ? 0 : a > b ? 1 : -1)
}
const afterBoundary = (message: MessagePosition, boundary?: Boundary) => !boundary ||
  toTimestampMs(message.time) > boundary.time ||
  (toTimestampMs(message.time) === boundary.time && !boundary.ids.has(message.messageId))

/** 已读确认记录消息边界，不能将请求期间新到的消息一并清空。 */
export function useConversationRead(options: {
  accountId: string; cid: string; messages: ChatMessage[]; visible: boolean; loading: boolean
  onError: (message: string) => void
}) {
  const current = useRef(options)
  current.current = options
  const boundaries = useRef(new Map<string, Boundary>())
  const confirmedIds = useRef(new Map<string, Set<string>>())
  const serverUnread = useRef(new Map<string, { count: number; version: number; time: number }>())
  const serverSequence = useRef(0)
  const validatedVersion = useRef(0)
  const recentMessages = useRef(new Map<string, (MessagePosition & { sequence: number })[]>())
  const arrivalSequence = useRef(0)
  const inFlight = useRef(new Set<string>())
  const attempted = useRef(new Map<string, string>())
  const loadedKey = useRef('')
  const mounted = useRef(true)
  const [revision, setRevision] = useState(0)
  const [loadedRevision, setLoadedRevision] = useState(0)
  const [requiredRevision, setRequiredRevision] = useState(0)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])

  // 未打开的会话也要记住推送边界，否则同毫秒的新消息可能被迟到已读 ACK 清掉。
  const observe = useCallback((accountId: string, cid: string, message: ChatMessage) => {
    const key = keyOf(accountId, cid)
    const previous = recentMessages.current.get(key) ?? []
    const remaining = message.messageId ? previous.filter(item => item.messageId !== message.messageId) : previous
    const sequence = previous.find(item => !!message.messageId && item.messageId === message.messageId)?.sequence ?? ++arrivalSequence.current
    recentMessages.current.set(key, [...remaining, { messageId: message.messageId, time: message.time, isSelf: message.isSelf, sequence }].slice(-100))
    if (recentMessages.current.size > 500) recentMessages.current.delete(recentMessages.current.keys().next().value!)
  }, [])

  const acknowledge = useCallback((receipt: ConversationReadReceipt, verifiedVersion = 0) => {
    if (!mounted.current || !receipt.lastMessageId || !receipt.messageIds.includes(receipt.lastMessageId) || receipt.lastMessageTime <= 0) return
    const key = keyOf(receipt.account_id, receipt.cid)
    const time = toTimestampMs(receipt.lastMessageTime)
    const previous = boundaries.current.get(key)
    const confirmed = confirmedIds.current.get(key) ?? new Set<string>()
    const previousSize = confirmed.size
    receipt.messageIds.forEach(id => confirmed.add(id))
    confirmedIds.current.set(key, confirmed)
    const server = serverUnread.current.get(key)
    const clearedServer = !!server && (verifiedVersion >= server.version || time >= server.time)
    if (clearedServer) serverUnread.current.delete(key)
    if (previous && time < previous.time) {
      if (confirmed.size !== previousSize || clearedServer) setRevision(value => value + 1)
      return
    }
    const ids = new Set(time === previous?.time ? previous.ids : [])
    receipt.messageIds.forEach(id => ids.add(id))
    if (previous?.time === time && previous.ids.size === ids.size && confirmed.size === previousSize && !clearedServer) return
    const order = previous && time === previous.time
      ? compareMessageIds(receipt.lastMessageId, previous.lastMessageId) : undefined
    const lastMessageId = order !== undefined && order < 0 ? previous!.lastMessageId : receipt.lastMessageId
    boundaries.current.set(key, { time, ids, lastMessageId })
    // 会话数量很大时也不无限保留已读快照；服务端已持久化相同边界。
    if (boundaries.current.size > 5000) boundaries.current.delete(boundaries.current.keys().next().value!)
    setRevision(value => value + 1)
  }, [])

  // 平台正未读是补拉正文的依据，不能因为缓存中有更晚的 self 消息就覆盖为零。
  const recordServer = useCallback((accountId: string, conversations: Conversation[]) => {
    let changed = false
    for (const conversation of conversations) {
      const key = keyOf(accountId, conversation.cid)
      if (conversation.unreadCount > 0) {
        const version = ++serverSequence.current
        serverUnread.current.set(key, { count: conversation.unreadCount, version, time: toTimestampMs(conversation.lastMessageTime) })
        changed = true
      } else serverUnread.current.delete(key)
    }
    if (changed) setRequiredRevision(value => value + 1)
  }, [])
  const serverVersion = useCallback((accountId: string, cid: string) =>
    serverUnread.current.get(keyOf(accountId, cid))?.version ?? 0, [])
  const needsLatest = useCallback((accountId: string, cid: string) =>
    loadedKey.current !== keyOf(accountId, cid) || validatedVersion.current < serverVersion(accountId, cid), [serverVersion])

  const reconcile = useCallback((accountId: string, conversations: Conversation[], getMessages: (cid: string) => ChatMessage[]) =>
    conversations.map(conversation => {
      const boundary = boundaries.current.get(keyOf(accountId, conversation.cid))
      if (!boundary) return conversation
      const cached = getMessages(conversation.cid)
      const knownIds = new Set(cached.map(message => message.messageId).filter(Boolean))
      const messages: MessagePosition[] = [...cached, ...(recentMessages.current.get(keyOf(accountId, conversation.cid)) ?? [])
        .filter(message => message.messageId && !knownIds.has(message.messageId))]
      const newerIncoming = messages.filter(message => !message.isSelf && afterBoundary(message, boundary)).length
      const summaryTime = toTimestampMs(conversation.lastMessageTime)
      const knownLatest = messages.reduce((latest, message) => Math.max(latest, toTimestampMs(message.time)), 0)
      // 摘要比我们知道的正文还新时，保留服务器计数，等待最新页补齐，不能凭旧 ACK 归零。
      const knownUnread = summaryTime > boundary.time && summaryTime > knownLatest
        ? Math.max(conversation.unreadCount, newerIncoming) : newerIncoming
      const unreadCount = Math.max(knownUnread, serverUnread.current.get(keyOf(accountId, conversation.cid))?.count ?? 0)
      return unreadCount === conversation.unreadCount ? conversation : { ...conversation, unreadCount }
    }), [])

  const invalidate = useCallback(() => { loadedKey.current = '' }, [])
  const latestLoaded = useCallback((accountId: string, cid: string, version: number) => {
    const state = current.current
    if (!state.visible || accountId !== state.accountId || cid !== state.cid) return
    const key = keyOf(accountId, cid)
    loadedKey.current = key
    validatedVersion.current = version
    attempted.current.delete(key)
    setLoadedRevision(value => value + 1)
  }, [])

  const attemptRef = useRef<() => void>(() => {})
  const attempt = useCallback(() => {
    const state = current.current
    const key = keyOf(state.accountId, state.cid)
    if (!mounted.current || !state.visible || document.visibilityState !== 'visible' || state.loading || !state.accountId || !state.cid ||
      loadedKey.current !== key || inFlight.current.has(key)) return
    const boundary = boundaries.current.get(key)
    const confirmed = confirmedIds.current.get(key)
    const pendingServer = serverUnread.current.get(key)
    if (pendingServer && validatedVersion.current < pendingServer.version) return
    const messages = state.messages.filter(message => message.isSelf === false && !!message.messageId &&
      !message.failed && toTimestampMs(message.time) > 0 && !confirmed?.has(message.messageId))
      .sort((a, b) => toTimestampMs(a.time) - toTimestampMs(b.time) || compareMessageIds(a.messageId, b.messageId) || 0).slice(0, 100)
    // 历史页中新看到的旧 ID 也逐条上报，但 clearRedPoint 保持最新已确认边界。
    let latest: { messageId: string; time: number } | undefined = messages[messages.length - 1]
    const latestTime = latest ? toTimestampMs(latest.time) : 0
    const boundaryIndex = boundary ? state.messages.findIndex(message => message.messageId === boundary.lastMessageId) : -1
    const latestIndex = latest ? state.messages.findIndex(message => message.messageId === latest?.messageId) : -1
    const observed = recentMessages.current.get(key) ?? []
    const latestArrival = observed.find(message => message.messageId === latest?.messageId)?.sequence
    const boundaryArrival = observed.find(message => message.messageId === boundary?.lastMessageId)?.sequence
    const sameTimeOrder = latest && boundary ? compareMessageIds(latest.messageId, boundary.lastMessageId) ??
      (latestArrival || boundaryArrival ? (latestArrival ?? 0) - (boundaryArrival ?? 0) : latestIndex - boundaryIndex) : 0
    // 同毫秒的新 ID 仍推进 clear 边界；补入较早历史或重复推送不能倒退。
    const keepBoundary = boundary && (!latest || latestTime < boundary.time ||
      (latestTime === boundary.time && sameTimeOrder <= 0))
    if (boundary && keepBoundary) {
      latest = { messageId: boundary.lastMessageId, time: boundary.time }
    }
    if (!latest || (!messages.length && !pendingServer)) return
    let messageIds = [...new Set(messages.map(message => message.messageId))]
    if (!messageIds.includes(latest.messageId)) messageIds = [...messageIds.slice(0, 99), latest.messageId]
    const verifiedVersion = validatedVersion.current
    const signature = JSON.stringify([messageIds, pendingServer?.version ?? 0])
    if (attempted.current.get(key) === signature) return
    attempted.current.set(key, signature)
    inFlight.current.add(key)
    void markConversationRead(state.accountId, { cid: state.cid, messageIds,
      lastMessageId: latest.messageId, lastMessageTime: toTimestampMs(latest.time) })
      .then(receipt => {
        // 请求始终属于发出时的账号会话，切换后的窗口不能接收错位结果。
        if (receipt.account_id === state.accountId && receipt.cid === state.cid) acknowledge(receipt, verifiedVersion)
      }).catch((error: Error) => {
        const active = current.current
        if (mounted.current && active.visible && active.accountId === state.accountId && active.cid === state.cid) {
          active.onError(error.message || '同步已读失败，请刷新聊天重试')
        }
      }).finally(() => {
        inFlight.current.delete(key)
        if (mounted.current) attemptRef.current()
      })
  }, [acknowledge])
  attemptRef.current = attempt
  useEffect(() => { attempt() }, [attempt, options.messages, options.visible, options.loading,
    options.accountId, options.cid, loadedRevision])
  return { acknowledge, reconcile, observe, invalidate, latestLoaded, revision,
    recordServer, serverVersion, needsLatest, requiredRevision }
}
