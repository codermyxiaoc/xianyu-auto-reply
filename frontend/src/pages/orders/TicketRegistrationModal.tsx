import { useEffect, useId, useRef, useState } from 'react'
import { AlertCircle, CalendarDays, Check, Copy, ExternalLink, Eye, EyeOff, Loader2, Plus, RefreshCw, Save, Send, Table2, Ticket, Trash2, Users, X } from 'lucide-react'
import {
  getTicketOrderConfiguration,
  getTicketTypes,
  recollectTicketOrderInformation,
  saveTicketOrderConfiguration,
  sendTicketRegistrationLink,
  type SaveTicketConfigurationInput,
  type TicketOrderConfiguration,
  type TicketTypeOption,
  type TicketVisitorCategory,
} from '@/api/ticketRegistration'
import { ConfirmModal } from '@/components/common/ConfirmModal'
import { TicketTablePreview } from './TicketTablePreview'
import { NativeSelect } from '@/components/common/NativeSelect'
import { useUIStore } from '@/store/uiStore'
import { copyToClipboard } from '@/utils/clipboard'
import type { Order } from '@/types'
import { formatTicketAgeRange, TICKET_CATEGORIES, TICKET_CATEGORY_LABELS } from '../ticket-registration/ticketAgeRules'

interface VisitorDraft {
  key: string
  id?: string
  category: TicketVisitorCategory
  amount: string
  ticket_type_id: string
}

interface Props {
  order: Order | null
  onClose: () => void
  onSaved?: () => void
}

function beijingToday() {
  const values = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
  }).formatToParts(new Date())
  const value = (type: string) => values.find(part => part.type === type)?.value || ''
  return `${value('year')}-${value('month')}-${value('day')}`
}

function comparable(visitDate: string, visitors: SaveTicketConfigurationInput['visitors']) {
  return JSON.stringify({ visit_date: visitDate, visitors: visitors.map(visitor => ({
    id: visitor.id || '', category: visitor.category, amount: visitor.amount, ticket_type_id: visitor.ticket_type_id,
  })) })
}

function safeLink(link?: string) {
  if (!link) return ''
  try {
    const url = new URL(link)
    return ['http:', 'https:'].includes(url.protocol) ? url.href : ''
  } catch { return '' }
}

function readableTime(value: string) {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN')
}

function isLoopbackLink(link: string) {
  if (!link) return false
  return ['localhost', '127.0.0.1', '[::1]', '::1'].includes(new URL(link).hostname.toLowerCase())
}

export function TicketRegistrationModal({ order, onClose, onSaved }: Props) {
  const { addToast } = useUIStore()
  const titleId = useId()
  const modalRef = useRef<HTMLDivElement>(null)
  const confirmRef = useRef<HTMLDivElement>(null)
  const sequence = useRef(0)
  const [reload, setReload] = useState(0)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState('')
  const [actionError, setActionError] = useState('')
  const [view, setView] = useState<TicketOrderConfiguration | null>(null)
  const [types, setTypes] = useState<TicketTypeOption[]>([])
  const [expanded, setExpanded] = useState(false)
  const [visitDate, setVisitDate] = useState('')
  const [visitors, setVisitors] = useState<VisitorDraft[]>([])
  const [busy, setBusy] = useState<'save' | 'send' | 'recollect' | 'preview' | null>(null)
  const recollectInFlight = useRef(false)
  const previewInFlight = useRef(false)
  const previewButtonRef = useRef<HTMLButtonElement>(null)
  const [previewOpen, setPreviewOpen] = useState(false)
  const previewRef = useRef(false)
  const wasPreviewOpen = useRef(false)
  const [revealed, setRevealed] = useState<Set<string>>(new Set())
  const [phoneRevealed, setPhoneRevealed] = useState(false)
  const [discardAction, setDiscardAction] = useState<'close' | 'reload' | 'recollect' | null>(null)
  const discardRef = useRef(discardAction)
  discardRef.current = discardAction
  const configuration = view?.configuration
  const submitted = configuration?.status === 'submitted'
  const previewVisible = previewOpen && submitted && !loading && !loadError && !busy && !discardAction
  previewRef.current = !!previewVisible
  const editable = !!view?.configurable && !submitted && !busy
  const dirty = configuration
    ? comparable(visitDate, visitors) !== comparable(configuration.visit_date, configuration.visitors)
    : !!(visitDate || visitors.length)
  const link = safeLink(configuration?.link)
  const linkUsable = !!link && !dirty && !busy
  const total = visitors.reduce((sum, visitor) => sum + (Number.isFinite(Number(visitor.amount)) ? Math.round(Number(visitor.amount) * 100) : 0), 0) / 100

  function applyView(next: TicketOrderConfiguration) {
    setPreviewOpen(false)
    setView(next)
    setVisitDate(next.configuration?.visit_date || '')
    setVisitors((next.configuration?.visitors || []).map(visitor => ({
      key: visitor.id, id: visitor.id, category: visitor.category, amount: visitor.amount, ticket_type_id: visitor.ticket_type_id,
    })))
    setExpanded(!!next.configuration)
    setRevealed(new Set())
    setPhoneRevealed(false)
  }

  useEffect(() => {
    if (!order) return
    const current = ++sequence.current
    setLoading(true)
    setPreviewOpen(false)
    setLoadError('')
    setActionError('')
    setView(null)
    setTypes([])
    setExpanded(false)
    setVisitDate('')
    setVisitors([])
    setBusy(null)
    setRevealed(new Set())
    setPhoneRevealed(false)
    setDiscardAction(null)
    Promise.all([getTicketOrderConfiguration(order.id), getTicketTypes()])
      .then(([result, typeResult]) => {
        if (sequence.current !== current) return
        if (!result.success || !result.data) throw new Error(result.message || '读取门票配置失败')
        if (!typeResult.success || !typeResult.data) throw new Error(typeResult.message || '读取票种失败')
        applyView(result.data)
        setTypes([...typeResult.data].sort((left, right) => {
          const duration = (name: string) => Number(name.match(/^(\d+(?:\.\d+)?)日/)?.[1] || Infinity)
          return duration(left.name) - duration(right.name)
        }))
      })
      .catch(error => {
        if (sequence.current === current) setLoadError(error instanceof Error ? error.message : '读取门票配置失败，请重试')
      })
      .finally(() => { if (sequence.current === current) setLoading(false) })
    return () => { sequence.current += 1 }
  }, [order?.id, reload])

  const closeRef = useRef(() => {})
  closeRef.current = () => {
    if (busy && busy !== 'preview') return
    if (dirty) setDiscardAction('close')
    else onClose()
  }

  useEffect(() => {
    if (!order) return
    const previouslyFocused = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    modalRef.current?.focus()
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        if (previewRef.current) { setPreviewOpen(false); return }
        if (recollectInFlight.current) return
        if (discardRef.current) setDiscardAction(null)
        else closeRef.current()
      }
      const scope = discardRef.current ? confirmRef.current : modalRef.current
      if (event.key !== 'Tab' || !scope) return
      const candidates = [...scope.querySelectorAll<HTMLElement>('button:not(:disabled), a[href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex="0"]')]
        .filter(element => element.tabIndex >= 0 && element.getClientRects().length > 0 && !element.closest('[aria-hidden="true"]'))
      const first = candidates[0]
      const last = candidates[candidates.length - 1]
      if (!first) { event.preventDefault(); return }
      if (event.shiftKey && (document.activeElement === first || !scope.contains(document.activeElement) || document.activeElement === scope)) {
        event.preventDefault(); last.focus()
      } else if (!event.shiftKey && (document.activeElement === last || !scope.contains(document.activeElement) || document.activeElement === scope)) {
        event.preventDefault(); first.focus()
      }
    }
    document.addEventListener('keydown', handleKey)
    return () => {
      document.body.style.overflow = previousOverflow
      document.removeEventListener('keydown', handleKey)
      previouslyFocused?.focus()
    }
  }, [order?.id])

  useEffect(() => {
    if (!discardAction) return
    const previouslyFocused = document.activeElement instanceof HTMLElement ? document.activeElement : null
    confirmRef.current?.querySelector<HTMLElement>('button:not(:disabled)')?.focus()
    return () => {
      if (previouslyFocused?.isConnected) previouslyFocused.focus()
      else modalRef.current?.focus()
    }
  }, [discardAction])

  useEffect(() => {
    if (previewVisible) modalRef.current?.focus()
    else if (wasPreviewOpen.current) previewButtonRef.current?.focus()
    wasPreviewOpen.current = !!previewVisible
  }, [previewVisible])

  if (!order) return null

  function requestReload() {
    if (dirty) setDiscardAction('reload')
    else setReload(value => value + 1)
  }

  function addVisitor(category: TicketVisitorCategory) {
    if (!editable || visitors.length >= 50) return
    const matchingTypes = types.filter(type => type.category === category)
    const preferred = matchingTypes.find(type => type.name === `1日${TICKET_CATEGORY_LABELS[category]}票`) || matchingTypes[0]
    setVisitors(previous => [...previous, {
      key: `new-${Date.now()}-${Math.random().toString(36).slice(2)}`,
      category,
      amount: '',
      ticket_type_id: preferred?.id || '',
    }])
    setActionError('')
  }

  function updateVisitor(key: string, patch: Partial<Pick<VisitorDraft, 'amount' | 'ticket_type_id'>>) {
    if (!editable) return
    setVisitors(previous => previous.map(visitor => visitor.key === key ? { ...visitor, ...patch } : visitor))
    setActionError('')
  }

  async function save() {
    if (!order || !editable) return
    setActionError('')
    if (!visitDate || visitDate < beijingToday()) return setActionError('请选择今天或之后的游玩日期')
    if (visitors.length === 0 || visitors.length > 50) return setActionError('请添加 1 至 50 位出行人')
    const invalid = visitors.findIndex(visitor => !/^\d{1,8}(\.\d{1,2})?$/.test(visitor.amount) || !types.some(type => type.id === visitor.ticket_type_id && type.category === visitor.category))
    if (invalid !== -1) return setActionError('请为每位出行人选择对应类别的票种，并填写非负金额（最多两位小数）')
    const current = sequence.current
    setBusy('save')
    try {
      const result = await saveTicketOrderConfiguration(order.id, {
        version: configuration?.version || 0,
        visit_date: visitDate,
        visitors: visitors.map(visitor => ({
          ...(visitor.id ? { id: visitor.id } : {}), category: visitor.category,
          amount: Number(visitor.amount).toFixed(2), ticket_type_id: visitor.ticket_type_id,
        })),
      })
      if (sequence.current !== current) return
      if (!result.success || !result.data) throw new Error(result.message || '保存失败')
      applyView(result.data)
      addToast({ type: 'success', message: '门票信息已保存，填写链接已生成' })
      onSaved?.()
    } catch (error) {
      if (sequence.current === current) setActionError(error instanceof Error ? error.message : '保存失败，请稍后重试')
    } finally { if (sequence.current === current) setBusy(null) }
  }

  async function send() {
    if (!order || !configuration || !linkUsable || !view?.configurable) return
    const current = sequence.current
    setBusy('send')
    setActionError('')
    try {
      const result = await sendTicketRegistrationLink(order.id, configuration.version)
      if (sequence.current !== current) return
      if (!result.success || !result.data) throw new Error(result.message || '发送失败，请确认闲鱼账号在线')
      applyView(result.data)
      addToast({ type: 'success', message: result.message || '填写链接已发送给买家' })
    } catch (error) {
      if (sequence.current === current) setActionError(error instanceof Error ? error.message : '发送失败，请稍后重试')
    } finally { if (sequence.current === current) setBusy(null) }
  }

  async function recollect() {
    if (!order || !configuration || !submitted || !view?.configurable || busy || recollectInFlight.current) return
    const current = sequence.current
    recollectInFlight.current = true
    setBusy('recollect')
    setActionError('')
    try {
      const result = await recollectTicketOrderInformation(order.id, configuration.version)
      if (sequence.current !== current) return
      if (!result.success || !result.data) throw new Error(result.message || '重新收集失败，请重新读取门票信息后重试')
      applyView(result.data)
      setDiscardAction(null)
      addToast({ type: 'success', message: '旧身份信息已清空，订单已恢复待收集' })
      onSaved?.()
    } catch (error) {
      if (sequence.current === current) {
        setDiscardAction(null)
        setActionError(error instanceof Error ? error.message : '重新收集失败，请稍后重试')
      }
    } finally {
      recollectInFlight.current = false
      if (sequence.current === current) setBusy(null)
    }
  }

  async function openTablePreview() {
    if (!order || !submitted || busy || discardRef.current || previewInFlight.current || recollectInFlight.current) return
    const current = sequence.current
    previewInFlight.current = true
    setBusy('preview')
    setActionError('')
    try {
      // 只读回读，避免其他管理员已重新收集时继续展示旧实名信息。
      const result = await getTicketOrderConfiguration(order.id)
      if (sequence.current !== current) return
      if (!result.success || !result.data) throw new Error(result.message || '读取表格预览失败，请重试')
      if (result.data.order_id !== order.id) throw new Error('订单信息不匹配，请重新读取门票信息')
      applyView(result.data)
      if (result.data.configuration?.status !== 'submitted') {
        setActionError('当前订单暂无已收集的身份信息，请等待买家提交')
      } else if (!result.data.configuration.visitors.length) {
        setActionError('暂无可预览的出行人信息')
      } else {
        setPreviewOpen(true)
      }
    } catch (error) {
      if (sequence.current === current) {
        // 回读失败（包括权限变更）时丢弃旧实名数据，只允许重新加载。
        setPreviewOpen(false)
        setView(null)
        setVisitors([])
        setVisitDate('')
        setExpanded(false)
        setRevealed(new Set())
        setPhoneRevealed(false)
        setLoadError(error instanceof Error ? error.message : '读取表格预览失败，请重试')
      }
    } finally {
      previewInFlight.current = false
      if (sequence.current === current) setBusy(null)
    }
  }

  async function copyLink() {
    if (!linkUsable) return
    const ok = await copyToClipboard(link)
    addToast({ type: ok ? 'success' : 'error', message: ok ? '填写链接已复制' : '复制失败，请手动选择链接复制' })
  }

  const sendDisabledReason = !configuration ? '请先保存并生成链接'
    : dirty ? '有未保存的修改，请先保存'
      : !view?.configurable ? '当前订单状态不支持发送'
        : !link ? '填写链接无效，请重新加载' : ''

  return (
    <div className="fixed inset-0 z-[120] flex items-center justify-center bg-slate-950/50 p-3 backdrop-blur-sm sm:p-6">
      <div ref={modalRef} role="dialog" aria-modal="true" aria-hidden={!!discardAction} aria-labelledby={titleId} tabIndex={-1}
        className={`flex max-h-[92dvh] w-full ${previewVisible ? 'max-w-4xl' : 'max-w-2xl'} flex-col overflow-hidden rounded-2xl bg-white shadow-2xl outline-none dark:bg-slate-900`}>
        {previewVisible && configuration ? (
          <TicketTablePreview titleId={titleId} orderNo={view?.order_no || order.order_id} visitDate={configuration.visit_date}
            phone={configuration.phone} visitors={configuration.visitors.map(({ id, name, id_number, ticket_type_name, amount }) => ({ id, name, id_number, ticket_type_name, amount }))}
            onBack={() => setPreviewOpen(false)} />
        ) : <>
        <div className="flex shrink-0 items-center justify-between gap-3 border-b border-slate-100 px-5 py-4 dark:border-slate-800 sm:px-6">
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-blue-50 text-blue-600 dark:bg-blue-950 dark:text-blue-300"><Ticket className="h-5 w-5" /></div>
            <div><h2 id={titleId} className="font-semibold text-slate-900 dark:text-slate-100">门票信息</h2><p className="mt-0.5 text-xs text-slate-500">配置门票，邀请买家填写出行人</p></div>
          </div>
          <div className="flex items-center gap-1">
            <button type="button" onClick={requestReload} disabled={loading || !!busy} aria-label="重新读取门票信息" title="重新读取门票信息" className="modal-close disabled:opacity-40"><RefreshCw className="h-4 w-4" /></button>
            <button type="button" onClick={() => closeRef.current()} disabled={!!busy && busy !== 'preview'} aria-label="关闭门票信息" className="modal-close disabled:opacity-40"><X className="h-5 w-5" /></button>
          </div>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-5 sm:px-6">
          <div className="rounded-xl border border-slate-200 bg-slate-50 px-4 py-3 dark:border-slate-700 dark:bg-slate-800/60">
            <div className="text-xs text-slate-500">订单号</div>
            <div className="mt-1 break-all font-mono text-sm font-semibold text-slate-800 dark:text-slate-100">{view?.order_no || order.order_id}</div>
          </div>

          {loading ? (
            <div className="flex items-center justify-center gap-2 py-14 text-sm text-slate-500" role="status"><Loader2 className="h-5 w-5 animate-spin" />正在读取门票信息…</div>
          ) : loadError ? (
            <div className="mt-5 rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-700 dark:border-red-900 dark:bg-red-950/30 dark:text-red-300" role="alert">
              <p>{loadError}</p><button type="button" onClick={() => setReload(value => value + 1)} className="mt-3 font-medium underline underline-offset-4">重新加载</button>
            </div>
          ) : view && (
            <div className="mt-5 space-y-5">
              {!view.configurable && <p role="alert" className="rounded-xl bg-amber-50 p-3 text-sm text-amber-700 dark:bg-amber-950/30 dark:text-amber-300">当前订单状态不支持配置门票，请刷新订单后重试。</p>}
              {submitted && <div className="flex items-start gap-2 rounded-xl bg-emerald-50 p-3 text-sm text-emerald-800 dark:bg-emerald-950/30 dark:text-emerald-300"><Check className="mt-0.5 h-4 w-4 shrink-0" /><div>买家已提交出行人信息，门票配置已锁定。{configuration?.submitted_at && <div className="mt-1 text-xs opacity-75">提交时间：{readableTime(configuration.submitted_at)}</div>}</div></div>}
              {!expanded ? (
                <button type="button" onClick={() => setExpanded(true)} disabled={!view.configurable}
                  className="flex w-full items-center justify-center gap-2 rounded-xl border border-dashed border-blue-300 bg-blue-50/40 px-4 py-7 text-sm font-medium text-blue-600 transition hover:bg-blue-50 disabled:cursor-not-allowed disabled:opacity-40 dark:border-blue-800 dark:bg-blue-950/20 dark:text-blue-300">
                  <Plus className="h-4 w-4" />设置门票信息
                </button>
              ) : (
                <>
                  <div>
                    <label htmlFor={`${titleId}-date`} className="mb-2 flex items-center gap-2 text-sm font-medium text-slate-700 dark:text-slate-200"><CalendarDays className="h-4 w-4 text-slate-400" />游玩日期</label>
                    <input id={`${titleId}-date`} type="date" value={visitDate} min={beijingToday()} disabled={!editable}
                      onChange={event => { setVisitDate(event.target.value); setActionError('') }} className="input-ios w-full disabled:opacity-70" />
                  </div>
                  {TICKET_CATEGORIES.map(category => {
                    const group = visitors.filter(visitor => visitor.category === category)
                    const label = TICKET_CATEGORY_LABELS[category]
                    const matchingTypes = types.filter(type => type.category === category)
                    return (
                      <section key={category} className="overflow-hidden rounded-xl border border-slate-200 dark:border-slate-700" aria-label={`${label}门票`}>
                        <div className="flex items-center justify-between gap-3 bg-slate-50 px-4 py-3 dark:bg-slate-800/70">
                          <h3 className="flex items-center gap-2 text-sm font-semibold text-slate-800 dark:text-slate-100"><Users className="h-4 w-4 text-slate-400" />{label}<span className="rounded-full bg-white px-2 py-0.5 text-xs text-slate-500 dark:bg-slate-700 dark:text-slate-300">{group.length} 位</span></h3>
                          {!submitted && <button type="button" onClick={() => addVisitor(category)} disabled={!editable || visitors.length >= 50}
                            className="inline-flex items-center gap-1 rounded-lg px-2 py-1.5 text-xs font-medium text-blue-600 hover:bg-blue-100 disabled:opacity-40 dark:text-blue-300 dark:hover:bg-blue-900/40"><Plus className="h-4 w-4" />添加{label}</button>}
                        </div>
                        {view.age_validation_enabled !== false && view.age_rules && <p className="px-4 pt-3 text-xs text-slate-500 dark:text-slate-400">游玩当天年龄：{formatTicketAgeRange(view.age_rules[category])}</p>}
                        {group.length === 0 ? <p className="px-4 py-5 text-center text-xs text-slate-400">还未添加{label}</p> : (
                          <div className="divide-y divide-slate-100 dark:divide-slate-800">
                            {group.map((visitor, index) => {
                              const saved = configuration?.visitors.find(person => person.id === visitor.id)
                              const showIdentity = revealed.has(visitor.key)
                              return (
                                <div key={visitor.key} className="space-y-3 p-4">
                                  <div className="flex items-center justify-between"><span className="text-xs font-medium text-slate-500">{label} {index + 1}</span>
                                    {!submitted && <button type="button" onClick={() => setVisitors(previous => previous.filter(person => person.key !== visitor.key))} disabled={!editable} aria-label={`删除${label} ${index + 1}`} title={`删除${label} ${index + 1}`} className="rounded-md p-1 text-slate-400 hover:bg-red-50 hover:text-red-500 disabled:opacity-40 dark:hover:bg-red-950/30"><Trash2 className="h-4 w-4" /></button>}
                                  </div>
                                  <div className="grid gap-3 sm:grid-cols-[1fr_1.5fr]">
                                    <label className="block text-xs text-slate-500">金额（元）<input type="text" inputMode="decimal" aria-label={`${label} ${index + 1} 金额`} value={visitor.amount} placeholder="0.00" maxLength={11} disabled={!editable} onChange={event => updateVisitor(visitor.key, { amount: event.target.value })} className="input-ios mt-1.5 w-full disabled:opacity-70" /></label>
                                    <label className="block text-xs text-slate-500">票种<NativeSelect aria-label={`${label} ${index + 1} 票种`} value={visitor.ticket_type_id} disabled={!editable} onChange={event => updateVisitor(visitor.key, { ticket_type_id: event.target.value })} className="input-ios mt-1.5 w-full disabled:opacity-70"><option value="">请选择{label}票种</option>{matchingTypes.map(type => <option key={type.id} value={type.id}>{type.name}</option>)}{saved && visitor.ticket_type_id === saved.ticket_type_id && !matchingTypes.some(type => type.id === saved.ticket_type_id) && <option value={saved.ticket_type_id} disabled>{saved.ticket_type_name}（历史配置）</option>}</NativeSelect></label>
                                  </div>
                                  {!submitted && saved && visitor.ticket_type_id === saved.ticket_type_id && !matchingTypes.some(type => type.id === saved.ticket_type_id) && <p className="text-xs leading-5 text-amber-700 dark:text-amber-300">此历史票种不属于当前可选{label}票，请重新选择；若需变更出行人类别，请删除此项后在对应类别添加。</p>}
                                  {submitted && saved && <div className="rounded-lg bg-slate-50 p-3 dark:bg-slate-800/60">
                                    <div className="mb-2 flex items-center justify-between text-xs"><span className="font-medium text-slate-500">买家填写</span><button type="button" aria-label={showIdentity ? `隐藏${label} ${index + 1}身份信息` : `查看${label} ${index + 1}身份信息`} onClick={() => setRevealed(previous => { const next = new Set(previous); if (next.has(visitor.key)) next.delete(visitor.key); else next.add(visitor.key); return next })} className="flex items-center gap-1 text-blue-600 dark:text-blue-300">{showIdentity ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}{showIdentity ? '隐藏' : '查看完整信息'}</button></div>
                                    <dl className="grid gap-2 text-sm"><div className="flex gap-3"><dt className="w-12 shrink-0 text-slate-400">姓名</dt><dd className="break-all text-slate-700 dark:text-slate-200">{showIdentity ? saved.name || '—' : saved.name ? `${saved.name.slice(0, 1)}${'*'.repeat(Math.max(saved.name.length - 1, 1))}` : '—'}</dd></div><div className="flex gap-3"><dt className="w-12 shrink-0 text-slate-400">身份证</dt><dd className="break-all font-mono text-slate-700 dark:text-slate-200">{showIdentity ? saved.id_number || '—' : saved.id_number ? `${saved.id_number.slice(0, 3)}***********${saved.id_number.slice(-4)}` : '—'}</dd></div></dl>
                                  </div>}
                                </div>
                              )
                            })}
                          </div>
                        )}
                      </section>
                    )
                  })}
                  <div className="flex items-center justify-between text-sm"><span className="text-slate-500">共 {visitors.length} 位出行人{visitors.length >= 50 ? '（已达上限）' : ''}</span><span className="text-slate-700 dark:text-slate-200">门票合计 <strong className="ml-1 text-base">¥{total.toFixed(2)}</strong></span></div>
                  <section aria-label="订单联系电话" className="rounded-xl bg-slate-50 px-4 py-3 dark:bg-slate-800/60">
                    <div className="flex items-center justify-between gap-2"><h3 className="text-xs font-medium text-slate-500">联系电话</h3>{configuration?.phone && <button type="button" aria-label={phoneRevealed ? '隐藏联系电话' : '查看完整联系电话'} onClick={() => setPhoneRevealed(value => !value)} className="inline-flex items-center gap-1 text-xs text-blue-600 dark:text-blue-300">{phoneRevealed ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}{phoneRevealed ? '隐藏' : '查看完整号码'}</button>}</div>
                    <p className="mt-2 font-mono text-sm text-slate-700 dark:text-slate-200">{configuration?.phone ? phoneRevealed ? configuration.phone : `${configuration.phone.slice(0, 3)}****${configuration.phone.slice(-4)}` : '等待买家填写'}</p>
                  </section>
                  {!submitted && <p className="text-xs leading-5 text-slate-400">在此设置日期、人数、金额和票种。保存后，买家通过专属链接填写每位出行人的姓名、身份证及本订单的联系电话。</p>}
                </>
              )}

              {configuration && <section className="rounded-xl border border-blue-100 bg-blue-50/40 p-4 dark:border-blue-900 dark:bg-blue-950/20" aria-label="买家填写链接">
                <h3 className="mb-2 text-sm font-medium text-slate-800 dark:text-slate-100">买家填写链接</h3>
                <input aria-label="订单专属填写链接" readOnly value={dirty ? '有未保存的修改，请先保存后使用链接' : link} onFocus={event => { if (!dirty) event.target.select() }} className="input-ios w-full text-xs" />
                <div className="mt-3 flex flex-wrap items-center gap-3 text-xs">
                  <button type="button" onClick={copyLink} disabled={!linkUsable} className="inline-flex items-center gap-1.5 font-medium text-blue-600 disabled:opacity-40 dark:text-blue-300"><Copy className="h-3.5 w-3.5" />复制链接</button>
                  {linkUsable ? <a href={link} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1.5 font-medium text-blue-600 dark:text-blue-300"><ExternalLink className="h-3.5 w-3.5" />预览买家页面</a> : <span className="inline-flex items-center gap-1.5 text-slate-400"><ExternalLink className="h-3.5 w-3.5" />预览买家页面</span>}
                </div>
                {isLoopbackLink(link) && <p className="mt-3 text-xs leading-5 text-slate-500 dark:text-slate-400">当前为本机地址，外部买家需通过可访问的前端地址打开后台后生成链接。</p>}
                {configuration.sent_at && <p className="mt-2 text-xs text-slate-500">上次发送：{readableTime(configuration.sent_at)}</p>}
              </section>}
              {actionError && <div role="alert" className="flex items-start gap-2 rounded-xl bg-red-50 p-3 text-sm text-red-700 dark:bg-red-950/30 dark:text-red-300"><AlertCircle className="mt-0.5 h-4 w-4 shrink-0" /><p className="break-words">{actionError}</p></div>}
            </div>
          )}
        </div>

        <div className="flex shrink-0 flex-wrap items-center justify-end gap-2 border-t border-slate-100 bg-slate-50/80 px-5 py-4 dark:border-slate-800 dark:bg-slate-800/50 sm:px-6">
          {dirty && <span className="mr-auto text-xs text-amber-600 dark:text-amber-400">有未保存的修改</span>}
          {expanded && !submitted && !loading && !loadError && <button type="button" onClick={save} disabled={!editable || (!!configuration && !dirty)} className="btn-ios-secondary inline-flex items-center gap-2 disabled:opacity-40">{busy === 'save' ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}{busy === 'save' ? '保存中…' : configuration ? '保存修改' : '保存并生成链接'}</button>}
          {submitted && !loading && !loadError && <button ref={previewButtonRef} type="button" onClick={() => { void openTablePreview() }} disabled={!!busy} className="btn-ios-secondary inline-flex items-center gap-2 disabled:opacity-40">{busy === 'preview' ? <Loader2 className="h-4 w-4 animate-spin" /> : <Table2 className="h-4 w-4" />}{busy === 'preview' ? '正在读取…' : '表格预览'}</button>}
          {submitted && !loading && !loadError && <button type="button" onClick={() => { setActionError(''); setDiscardAction('recollect') }} disabled={!view?.configurable || !!busy} className="btn-ios-secondary inline-flex items-center gap-2 disabled:opacity-40"><RefreshCw className="h-4 w-4" />重新收集</button>}
          <button type="button" onClick={send} disabled={loading || !!loadError || !!sendDisabledReason || !!busy} title={sendDisabledReason || undefined}
            className="btn-ios-primary inline-flex items-center justify-center gap-2 disabled:cursor-not-allowed disabled:opacity-40">{busy === 'send' ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}{busy === 'send' ? '正在发送…' : '发送给买家'}</button>
        </div>
        </>}
      </div>
      <div ref={confirmRef} className={discardAction ? 'fixed inset-0 z-[200]' : undefined} role={discardAction ? 'alertdialog' : undefined} aria-modal={discardAction ? true : undefined} aria-label={discardAction ? discardAction === 'recollect' ? '确认重新收集身份信息' : '确认放弃未保存的门票修改' : undefined}>
        <ConfirmModal
          isOpen={!!discardAction}
          title={discardAction === 'recollect' ? '重新收集身份信息？' : discardAction === 'reload' ? '重新读取门票信息？' : '关闭门票信息？'}
          message={discardAction === 'recollect' ? <div className="space-y-2 text-left"><p>确认后将清空本订单所有出行人的姓名、身份证、联系电话和导出记录，恢复为“待收集”。</p><p>游玩日期、人数、金额和票种保留。买家可以使用原链接重新填写。</p></div> : '当前修改尚未保存，继续后这些修改不会保留。'}
          confirmText={discardAction === 'recollect' ? '确认重新收集' : discardAction === 'reload' ? '重新读取' : '放弃修改并关闭'}
          cancelText={discardAction === 'recollect' ? '取消' : '继续编辑'}
          loading={busy === 'recollect'}
          onCancel={() => { if (!recollectInFlight.current) setDiscardAction(null) }}
          onConfirm={() => {
            if (discardAction === 'recollect') { void recollect(); return }
            const action = discardAction
            setDiscardAction(null)
            if (action === 'reload') setReload(value => value + 1)
            else onClose()
          }}
        />
      </div>
    </div>
  )
}
