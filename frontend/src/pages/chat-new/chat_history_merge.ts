/** 聊天历史合并：消息 ID 优先，只有无 ID 的本地发送占位允许按内容对齐。 */
import type { ChatMessage, Conversation } from '@/api/chatNew'

export const toTimestampMs = (timestamp: number) =>
  timestamp < 1_000_000_000_000 ? timestamp * 1000 : timestamp

/** failed=false 标记本地发送结果或服务端发送确认；历史消息没有该字段。 */
const isLocalPlaceholder = (message: ChatMessage) =>
  !message.messageId && message.isSelf && message.failed === false

const matchesPlaceholder = (message: ChatMessage, placeholder: ChatMessage) =>
  (!message.clientMessageId || !placeholder.clientMessageId || message.clientMessageId === placeholder.clientMessageId) &&
  message.isSelf && message.type === placeholder.type &&
  (message.type !== 'item' || (!!message.itemCard?.itemId && message.itemCard.itemId === placeholder.itemCard?.itemId)) &&
  message.text === placeholder.text &&
  message.images.length === placeholder.images.length &&
  message.images.every((url, index) => url === placeholder.images[index]) &&
  Math.abs(toTimestampMs(message.time) - toTimestampMs(placeholder.time)) < 5000

/** 合并两组消息，优先保留 latest 中同 ID 的记录，保留不同 ID 的真实消息。 */
export function mergeMessages(latest: ChatMessage[], existing: ChatMessage[]): ChatMessage[] {
  const identified = new Map<string, number>()
  const confirmations = new Map<string, number>()
  const distinct: ChatMessage[] = []
  for (const message of new Set([...latest, ...existing])) {
    const index = (message.messageId ? identified.get(message.messageId) : undefined) ??
      (message.clientMessageId ? confirmations.get(message.clientMessageId) : undefined)
    const position = index ?? distinct.length
    if (index === undefined) distinct.push(message)
    else if (!distinct[index].messageId && message.messageId) {
      // 真实 IM 消息优先于没有真实 ID 的发送确认。
      distinct[index] = { ...message, clientMessageId: message.clientMessageId || distinct[index].clientMessageId }
    } else if (!distinct[index].clientMessageId && message.clientMessageId) {
      distinct[index] = { ...distinct[index], clientMessageId: message.clientMessageId }
    }
    if (message.messageId) identified.set(message.messageId, position)
    if (message.clientMessageId) confirmations.set(message.clientMessageId, position)
  }
  const merged = distinct.filter((message) => !isLocalPlaceholder(message))
  const matchedIds = new Set<string>()
  for (const message of distinct.filter(isLocalPlaceholder)) {
    const match = merged.find((candidate) =>
      candidate.messageId && !matchedIds.has(candidate.messageId) &&
      matchesPlaceholder(candidate, message),
    )
    if (match) {
      // 一条服务端消息只能替换一个本地占位，不能吞掉连续发送的相同内容。
      matchedIds.add(match.messageId)
      if (message.clientMessageId && !match.clientMessageId) {
        merged[merged.indexOf(match)] = { ...match, clientMessageId: message.clientMessageId }
      }
    } else {
      merged.push(message)
    }
  }
  return merged.sort((a, b) => toTimestampMs(a.time) - toTimestampMs(b.time))
}

/** 实时推送和发送结果使用同一套去重规则，回填占位消息的真实 ID。 */
export const appendMsg = (messages: ChatMessage[], message: ChatMessage) =>
  mergeMessages([message], messages)

/** 按会话 ID 合并，并保留 HTTP 请求期间收到的更新实时摘要。 */
export function mergeConversations(latest: Conversation[], existing: Conversation[]): Conversation[] {
  const merged = new Map(existing.map((conversation) => [conversation.cid, conversation]))
  for (const conversation of latest) {
    const previous = merged.get(conversation.cid)
    merged.set(conversation.cid, previous &&
      toTimestampMs(previous.lastMessageTime) > toTimestampMs(conversation.lastMessageTime)
      ? { ...conversation, lastMessageTime: previous.lastMessageTime,
        lastMessageSummary: previous.lastMessageSummary, unreadCount: previous.unreadCount }
      : conversation)
  }
  return [...merged.values()].sort((a, b) =>
    toTimestampMs(b.lastMessageTime) - toTimestampMs(a.lastMessageTime),
  )
}
