import { useEffect, useId, useRef, useState } from 'react'
import { AlertCircle, Loader2, MessageSquare, RefreshCw, Save } from 'lucide-react'
import {
  getTicketMessageTemplate,
  saveTicketMessageTemplate,
  getPersonalTicketMessageTemplate,
  savePersonalTicketMessageTemplate,
  type TicketMessageTemplate,
} from '@/api/ticketRegistration'
import { useUIStore } from '@/store/uiStore'
import { getApiErrorMessage } from '@/utils/apiError'

function templateError(template: string): string | null {
  if (!template.trim()) return '请填写发送给买家的文案'
  if (Array.from(template).length > 2000) return '发送文案不能超过 2000 个字符'
  if (!template.includes('{infourl}')) return '发送文案必须包含 {infourl}，用于插入身份信息填写链接'
  if (/[{}]/.test(template.replace(/\{orderId\}|\{infourl\}/g, ''))) {
    return '仅支持 {orderId} 和 {infourl}，请删除未知占位符或多余、未闭合的花括号'
  }
  return null
}

export function TicketMessageTemplateSettingsCard({ scope }: { scope: 'system' | 'personal' }) {
  const personal = scope === 'personal'
  const id = useId()
  const { addToast } = useUIStore()
  const [template, setTemplate] = useState<string | null>(null)
  const [usePersonal, setUsePersonal] = useState(false)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [reload, setReload] = useState(0)
  const generation = useRef(0)
  const savingRef = useRef(false)
  const editable = !personal || usePersonal

  function applyTemplate(data: TicketMessageTemplate & { use_personal?: boolean }) {
    setTemplate(data.message_template)
    setUsePersonal(data.use_personal === true)
  }

  useEffect(() => {
    const current = ++generation.current
    setLoading(true)
    setError('')
    setTemplate(null)
    void (personal ? getPersonalTicketMessageTemplate() : getTicketMessageTemplate()).then(result => {
      if (current !== generation.current) return
      if (!result.success || !result.data) throw new Error(result.message || '加载发送文案失败')
      applyTemplate(result.data)
    }).catch(reason => {
      if (current === generation.current) setError(getApiErrorMessage(reason, '加载发送文案失败'))
    }).finally(() => {
      if (current === generation.current) setLoading(false)
    })
    return () => { generation.current += 1 }
  }, [personal, reload])

  async function save(followSystem = false) {
    if (template === null || savingRef.current || (!editable && !followSystem)) return
    const validation = followSystem ? null : templateError(template)
    if (validation) { setError(validation); return }
    const current = generation.current
    savingRef.current = true
    setSaving(true)
    setError('')
    try {
      const result = personal
        ? await savePersonalTicketMessageTemplate(followSystem ? { use_personal: false } : { use_personal: true, message_template: template })
        : await saveTicketMessageTemplate({ message_template: template })
      if (current !== generation.current) return
      if (!result.success || !result.data) throw new Error(result.message || '保存发送文案失败')
      applyTemplate(result.data)
      addToast({ type: 'success', message: followSystem ? '发送文案已恢复跟随系统' : '发送给买家的文案已保存' })
    } catch (reason) {
      if (current === generation.current) setError(getApiErrorMessage(reason, '保存发送文案失败'))
    } finally {
      savingRef.current = false
      if (current === generation.current) setSaving(false)
    }
  }

  const exampleOrderId = '202611070001'
  const exampleLink = `${window.location.origin}/ticket-registration#example-ticket-link`
  const preview = (template || '').replace(/\{orderId\}/g, exampleOrderId).replace(/\{infourl\}/g, exampleLink)

  return (
    <section aria-label="发送给买家的文案" className="vben-card">
      <div className="vben-card-header"><h2 className="vben-card-title"><MessageSquare className="h-4 w-4" />发送给买家的文案</h2></div>
      <div className="vben-card-body">
        <p className="text-sm leading-6 text-slate-500 dark:text-slate-400">{personal ? '仅用于您所属订单的发送文案，年龄校验设置单独保存。' : '未设置个人文案的用户使用系统文案。'} 保存后，在订单门票窗口点击“发送给买家”时使用此文案。</p>
        {loading ? <div role="status" className="flex items-center gap-2 py-8 text-sm text-slate-500"><Loader2 className="h-4 w-4 animate-spin" />正在读取发送文案…</div> : (
          <form onSubmit={event => { event.preventDefault(); void save() }} noValidate className="mt-5 space-y-5">
            {template !== null && <>
              {personal && <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl bg-slate-50 p-4 dark:bg-slate-800/60">
                <div><p className="text-sm font-medium">{usePersonal ? '使用个人文案' : '跟随系统文案'}</p><p className="mt-1 text-xs text-slate-500">{usePersonal ? '保存后优先使用个人文案，可随时恢复跟随系统。' : '系统文案更新后会同步生效。'}</p></div>
                {usePersonal
                  ? <button type="button" disabled={saving} onClick={() => void save(true)} className="btn-ios-secondary disabled:opacity-50">恢复系统文案</button>
                  : <button type="button" disabled={saving} onClick={() => { setUsePersonal(true); setError('') }} className="btn-ios-secondary">使用个人文案</button>}
              </div>}
              <div>
                <label htmlFor={`${id}-template`} className="input-label">发送文案</label>
                <textarea id={`${id}-template`} aria-describedby={`${id}-help ${id}-length${error ? ` ${id}-error` : ''}`} aria-invalid={Boolean(error)} disabled={saving || !editable} rows={6} value={template} onChange={event => { setTemplate(event.target.value); setError('') }} className="input-ios mt-2 min-h-36 w-full resize-y leading-6 disabled:cursor-not-allowed disabled:opacity-60" />
                <div className="mt-2 flex flex-wrap items-start justify-between gap-2">
                  <p id={`${id}-help`} className="text-xs leading-5 text-slate-500 dark:text-slate-400">{'{infourl}'} 为填写链接（必填），{'{orderId}'} 为订单号（选填）。可保留换行，最多 2000 个字符。</p>
                  <p id={`${id}-length`} className={`shrink-0 text-xs tabular-nums ${Array.from(template).length > 2000 ? 'text-red-600' : 'text-slate-500'}`}>{Array.from(template).length} / 2000</p>
                </div>
              </div>
              <section aria-label="买家消息预览" className="rounded-xl border border-slate-200 bg-slate-50 p-4 dark:border-slate-700 dark:bg-slate-800/50">
                <h3 className="text-sm font-medium">消息预览</h3>
                <p className="mt-1 text-xs leading-5 text-slate-500">示例订单号：{exampleOrderId}；以下为示例链接，仅用于预览。</p>
                <p className="mt-3 whitespace-pre-wrap break-words rounded-xl bg-white p-4 text-sm leading-6 text-slate-700 dark:bg-slate-900 dark:text-slate-200">{preview || '填写文案后在这里预览'}</p>
              </section>
            </>}
            {error && <p id={`${id}-error`} role="alert" className="flex items-start gap-2 rounded-lg bg-red-50 p-3 text-sm text-red-700 dark:bg-red-950/30 dark:text-red-300"><AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />{error}</p>}
            <div className="flex justify-end gap-3 border-t border-slate-100 pt-4 dark:border-slate-700">
              {template === null && <button type="button" onClick={() => setReload(value => value + 1)} className="btn-ios-secondary"><RefreshCw className="h-4 w-4" />重新加载发送文案</button>}
              <button type="submit" disabled={template === null || saving || !editable} className="btn-ios-primary disabled:opacity-50">{saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}{saving ? '保存中…' : '保存发送文案'}</button>
            </div>
          </form>
        )}
      </div>
    </section>
  )
}
