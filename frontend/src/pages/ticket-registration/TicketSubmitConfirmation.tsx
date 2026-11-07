import { useEffect, useRef } from 'react'
import { Check, Loader2, X } from 'lucide-react'
import type { TicketVisitorCategory } from '@/api/ticketRegistration'
import { TICKET_CATEGORY_LABELS } from './ticketAgeRules'

interface TicketSubmitConfirmationProps {
  orderNumber: string
  visitDate: string
  phone: string
  visitors: Array<{ id: string; name: string; id_number: string; ticket_type_name: string; category: TicketVisitorCategory; categoryNumber: number; age: number | null }>
  completingPhone: boolean
  loading: boolean
  onConfirm: () => void
  onCancel: () => void
}

export function TicketSubmitConfirmation({ orderNumber, visitDate, phone, visitors, completingPhone, loading, onConfirm, onCancel }: TicketSubmitConfirmationProps) {
  const dialog = useRef<HTMLDialogElement>(null)

  useEffect(() => {
    const element = dialog.current
    element?.showModal()
    if (element) element.scrollTop = 0
    return () => { element?.close() }
  }, [])

  return (
    <dialog
      ref={dialog}
      aria-labelledby="ticket-confirm-title"
      aria-describedby="ticket-confirm-description"
      onCancel={event => {
        event.preventDefault()
        if (!loading) onCancel()
      }}
      className="fixed inset-0 m-auto max-h-[90dvh] w-[calc(100%_-_2rem)] max-w-lg overflow-y-auto rounded-2xl border border-slate-200 bg-white p-0 text-slate-800 shadow-2xl backdrop:bg-slate-950/50 backdrop:backdrop-blur-sm dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100"
    >
      <div className="relative border-b border-slate-100 px-5 py-5 pr-14 dark:border-slate-800 sm:px-6">
        <h2 id="ticket-confirm-title" className="text-lg font-semibold">{completingPhone ? '确认保存手机号' : '确认提交出行信息'}</h2>
        <p id="ticket-confirm-description" className="mt-2 text-sm leading-6 text-slate-500 dark:text-slate-400">请再次核对以下信息，确认后将提交给卖家。提交后如需修改，请联系卖家。</p>
        <button type="button" aria-label="返回修改" onClick={onCancel} disabled={loading} className="absolute right-4 top-5 rounded-lg p-1.5 text-slate-400 hover:bg-slate-100 disabled:opacity-40 dark:hover:bg-slate-800"><X className="h-5 w-5" /></button>
      </div>
      <div className="space-y-4 px-5 py-5 sm:px-6">
        <dl className="space-y-2 rounded-xl bg-slate-50 p-4 text-sm dark:bg-slate-800/60">
          <div><dt className="text-xs text-slate-500">订单号</dt><dd className="mt-1 break-all font-mono">{orderNumber}</dd></div>
          <div><dt className="text-xs text-slate-500">出行日期</dt><dd className="mt-1">{visitDate}</dd></div>
        </dl>
        {visitors.map((visitor, index) => (
          <section key={visitor.id} aria-label={`核对出行人 ${index + 1}`} className="rounded-xl border border-slate-200 p-4 dark:border-slate-700">
            <h3 className="text-sm font-medium">{TICKET_CATEGORY_LABELS[visitor.category]} {visitor.categoryNumber}<span className="ml-2 font-normal text-slate-500 dark:text-slate-400">{visitor.ticket_type_name}</span></h3>
            <dl className="mt-3 grid gap-3 text-sm sm:grid-cols-2">
              <div><dt className="text-xs text-slate-500">姓名</dt><dd className="mt-1 break-words">{visitor.name}</dd></div>
              <div><dt className="text-xs text-slate-500">身份证</dt><dd className="mt-1 break-all font-mono">{visitor.id_number}</dd></div>
              {visitor.age !== null && <div><dt className="text-xs text-slate-500">游玩当天年龄</dt><dd className="mt-1">{visitor.age} 周岁</dd></div>}
            </dl>
          </section>
        ))}
        <div className="rounded-xl border border-slate-200 p-4 dark:border-slate-700"><p className="text-xs text-slate-500">整单联系电话</p><p className="mt-2 font-mono text-base">{phone}</p></div>
      </div>
      <div className="sticky bottom-0 flex gap-3 border-t border-slate-100 bg-white px-5 py-4 dark:border-slate-800 dark:bg-slate-900 sm:px-6">
        <button type="button" autoFocus onClick={onCancel} disabled={loading} className="flex-1 rounded-xl bg-slate-100 px-4 py-3 text-sm font-medium transition hover:bg-slate-200 disabled:cursor-wait disabled:opacity-50 dark:bg-slate-800 dark:hover:bg-slate-700">返回修改</button>
        <button type="button" onClick={onConfirm} disabled={loading} className="flex flex-1 items-center justify-center gap-2 rounded-xl bg-blue-600 px-4 py-3 text-sm font-medium text-white transition hover:bg-blue-700 disabled:cursor-wait disabled:opacity-60">{loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <Check className="h-4 w-4" />}{loading ? '正在提交…' : '确认提交'}</button>
      </div>
    </dialog>
  )
}
