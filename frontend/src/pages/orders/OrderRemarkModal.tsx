import { useEffect, useId, useRef, useState } from 'react'
import { Loader2, NotebookPen, X } from 'lucide-react'
import { updateOrderRemark, type OrderRemarkResult } from '@/api/orders'
import type { Order } from '@/types'
import { getApiErrorMessage } from '@/utils/apiError'

interface Props {
  order: Order
  onClose: () => void
  onSaved: (result: OrderRemarkResult) => void
}

/** 每次打开固定一张订单；保存期间禁止关闭，过期响应不会更新另一张订单。 */
export function OrderRemarkModal({ order, onClose, onSaved }: Props) {
  const titleId = useId()
  const inputId = useId()
  const helpId = useId()
  const dialogRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const inFlight = useRef(false)
  const sequence = useRef(0)
  const closeRef = useRef(onClose)
  closeRef.current = onClose
  const [remark, setRemark] = useState(order.remark || '')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const characterCount = Array.from(remark).length
  const tooLong = characterCount > 10000

  useEffect(() => {
    sequence.current += 1
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    inputRef.current?.focus()
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        if (!inFlight.current) closeRef.current()
      }
      if (event.key !== 'Tab' || !dialogRef.current) return
      const scope = dialogRef.current
      const candidates = [...scope.querySelectorAll<HTMLElement>('button:not(:disabled), textarea:not(:disabled)')]
      const first = candidates[0]
      const last = candidates[candidates.length - 1]
      if (!first) { event.preventDefault(); scope.focus(); return }
      if (event.shiftKey && (document.activeElement === first || !scope.contains(document.activeElement))) {
        event.preventDefault(); last.focus()
      } else if (!event.shiftKey && (document.activeElement === last || !scope.contains(document.activeElement))) {
        event.preventDefault(); first.focus()
      }
    }
    document.addEventListener('keydown', handleKey)
    return () => {
      sequence.current += 1
      document.body.style.overflow = previousOverflow
      document.removeEventListener('keydown', handleKey)
      if (previousFocus?.isConnected) previousFocus.focus()
    }
  }, [])

  const close = () => { if (!inFlight.current) onClose() }
  const save = async () => {
    if (inFlight.current || tooLong) return
    const current = sequence.current
    const orderId = order.id
    inFlight.current = true
    setSaving(true)
    setError('')
    try {
      const response = await updateOrderRemark(orderId, remark)
      if (sequence.current !== current) return
      if (!response.success || !response.data) throw new Error(response.message || '保存备注失败，请重试')
      if (response.data.order_id !== orderId || typeof response.data.remark !== 'string') {
        throw new Error('订单备注返回异常，请刷新订单后重试')
      }
      onSaved(response.data)
    } catch (cause) {
      if (sequence.current === current) setError(getApiErrorMessage(cause, '保存备注失败，请重试'))
    } finally {
      if (sequence.current === current) {
        inFlight.current = false
        setSaving(false)
      }
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/50 p-3 sm:p-6">
      <div ref={dialogRef} role="dialog" aria-modal="true" aria-labelledby={titleId} tabIndex={-1} className="flex max-h-[90dvh] w-full max-w-2xl flex-col overflow-hidden rounded-2xl bg-white shadow-2xl outline-none dark:bg-slate-900">
        <div className="flex shrink-0 items-center justify-between gap-3 border-b border-slate-100 px-5 py-4 dark:border-slate-800">
          <div className="min-w-0">
            <h2 id={titleId} className="flex items-center gap-2 text-lg font-semibold text-slate-900 dark:text-slate-100"><NotebookPen className="h-5 w-5 text-blue-500" />订单备注</h2>
            <p className="mt-1 break-all text-xs text-slate-500">订单号：{order.order_id}</p>
          </div>
          <button type="button" onClick={close} disabled={saving} aria-label="关闭订单备注" className="shrink-0 rounded-lg p-2 text-slate-400 hover:bg-slate-100 disabled:opacity-40 dark:hover:bg-slate-800"><X className="h-5 w-5" /></button>
        </div>
        <form onSubmit={event => { event.preventDefault(); void save() }} className="flex min-h-0 flex-col">
          <div className="space-y-3 overflow-y-auto p-5">
            <label htmlFor={inputId} className="block text-sm font-medium text-slate-700 dark:text-slate-200">备注内容</label>
            <textarea ref={inputRef} id={inputId} value={remark} onChange={event => { setRemark(event.target.value); setError('') }} disabled={saving} rows={11} aria-describedby={helpId} aria-invalid={tooLong || undefined} placeholder="填写订单备注，支持多行内容" className="input-ios min-h-48 w-full resize-y whitespace-pre-wrap disabled:opacity-60" />
            <div id={helpId} className="flex flex-wrap justify-between gap-2 text-xs text-slate-500">
              <span>支持换行；清空内容后保存即可删除备注。</span>
              <span className={tooLong ? 'text-red-600' : ''}>{characterCount} / 10000 字</span>
            </div>
            {(error || tooLong) && <p role="alert" className="rounded-lg bg-red-50 p-3 text-sm text-red-600 dark:bg-red-950/30 dark:text-red-300">{tooLong ? '备注最多 10000 字，请缩短后保存' : error}</p>}
          </div>
          <div className="flex shrink-0 items-center justify-between gap-2 border-t border-slate-100 bg-slate-50/70 px-5 py-4 dark:border-slate-800 dark:bg-slate-950/30">
            <button type="button" onClick={() => { setRemark(''); setError(''); inputRef.current?.focus() }} disabled={saving || !remark} className="btn-ios-secondary btn-sm">清空内容</button>
            <div className="flex items-center gap-2">
              <button type="button" onClick={close} disabled={saving} className="btn-ios-secondary btn-sm">取消</button>
              <button type="submit" disabled={saving || tooLong} className="btn-ios-primary btn-sm">{saving && <Loader2 className="h-4 w-4 animate-spin" />}{saving ? '保存中…' : '保存备注'}</button>
            </div>
          </div>
        </form>
      </div>
    </div>
  )
}
