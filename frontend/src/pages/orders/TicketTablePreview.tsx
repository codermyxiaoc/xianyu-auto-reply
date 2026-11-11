import { useEffect, useRef, useState } from 'react'
import { Download, Loader2, Table2, X } from 'lucide-react'
import type { TicketVisitorConfiguration } from '@/api/ticketRegistration'
import { saveTicketTableImage } from './ticketTableImage'

interface Props {
  titleId: string
  orderNo: string
  visitDate: string
  phone: string | null
  visitors: Pick<TicketVisitorConfiguration, 'id' | 'name' | 'id_number' | 'ticket_type_name' | 'amount'>[]
  onBack: () => void
}

function amountInCents(value: string): number | null {
  const match = /^(\d{1,8})(?:\.(\d{1,2}))?$/.exec(value)
  return match ? Number(match[1]) * 100 + Number((match[2] || '').padEnd(2, '0')) : null
}

/** 仅接收表格字段，不接收登记链接、token 或整个订单配置。 */
export function TicketTablePreview({ titleId, orderNo, visitDate, phone, visitors, onBack }: Props) {
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const inFlight = useRef(false)
  const controller = useRef<AbortController | null>(null)
  const mounted = useRef(false)
  const amounts = visitors.map(visitor => amountInCents(visitor.amount))
  const validAmounts = amounts.every(amount => amount !== null)
  const total = amounts.reduce<number>((sum, amount) => sum + (amount || 0), 0)

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
      controller.current?.abort()
    }
  }, [])

  const saveImage = async () => {
    if (inFlight.current || !visitors.length || !validAmounts) return
    const activeController = new AbortController()
    controller.current = activeController
    inFlight.current = true
    setSaving(true)
    setError('')
    try {
      await saveTicketTableImage({
        orderNo, visitDate, phone: phone || '—',
        visitors: visitors.map(visitor => ({
          name: visitor.name || '—', idNumber: visitor.id_number || '—',
          ticketType: visitor.ticket_type_name || '—', amount: visitor.amount,
        })),
      }, { signal: activeController.signal })
    } catch (cause) {
      if (mounted.current && !activeController.signal.aborted) {
        setError(cause instanceof Error ? cause.message : '保存图片失败，请重试')
      }
    } finally {
      if (mounted.current && controller.current === activeController) {
        inFlight.current = false
        setSaving(false)
      }
    }
  }

  return (
    <>
      <div className="flex shrink-0 items-center justify-between gap-3 border-b border-slate-100 px-4 py-4 dark:border-slate-800 sm:px-6">
        <h2 id={titleId} className="flex items-center gap-2 font-semibold text-slate-900 dark:text-slate-100"><Table2 className="h-5 w-5 text-blue-500" />表格预览</h2>
        <button type="button" onClick={onBack} aria-label="关闭表格预览" className="modal-close"><X className="h-5 w-5" /></button>
      </div>
      <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-4 sm:p-6">
        <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-2 text-sm text-slate-600 dark:text-slate-300">
          <p className="min-w-0 break-all">订单号：<span className="font-mono text-slate-900 dark:text-slate-100">{orderNo}</span></p>
          <p>游玩日期：{visitDate}</p><p>共 {visitors.length} 位</p>
        </div>
        {visitors.length > 0 && <p className="text-xs text-slate-500 sm:hidden">左右滑动查看完整表格，保存图片包含全部出行人。</p>}
        {visitors.length ? (
          <div className="overflow-x-auto rounded-xl border border-slate-200 dark:border-slate-700" role="region" aria-label="门票身份信息表格，可左右滚动" tabIndex={0}>
            <table className="w-full min-w-[620px] border-collapse text-left text-sm" aria-label="门票身份信息预览">
              <thead className="bg-slate-50 text-slate-600 dark:bg-slate-800 dark:text-slate-300"><tr>
                <th scope="col" className="px-4 py-3 font-medium">姓名</th>
                <th scope="col" className="px-4 py-3 font-medium">身份证</th>
                <th scope="col" className="px-4 py-3 font-medium">票种</th>
                <th scope="col" className="px-4 py-3 text-right font-medium">金额（元）</th>
              </tr></thead>
              <tbody className="divide-y divide-slate-100 text-slate-900 dark:divide-slate-800 dark:text-slate-100">
                {visitors.map((visitor, index) => <tr key={visitor.id}>
                  <td className="max-w-52 break-words px-4 py-3">{visitor.name || '—'}</td>
                  <td className="whitespace-nowrap px-4 py-3 font-mono">{visitor.id_number || '—'}</td>
                  <td className="whitespace-nowrap px-4 py-3">{visitor.ticket_type_name || '—'}</td>
                  <td className="whitespace-nowrap px-4 py-3 text-right tabular-nums">{amounts[index] === null ? '—' : ((amounts[index] as number) / 100).toFixed(2)}</td>
                </tr>)}
              </tbody>
              <tfoot className="border-t border-slate-200 bg-slate-50 font-semibold text-slate-900 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-100"><tr>
                <th scope="row" colSpan={3} className="px-4 py-3">合计</th><td className="whitespace-nowrap px-4 py-3 text-right tabular-nums">{validAmounts ? (total / 100).toFixed(2) : '—'}</td>
              </tr></tfoot>
            </table>
          </div>
        ) : <p role="status" className="py-10 text-center text-sm text-slate-500">暂无可预览的出行人信息</p>}
        <p className="text-sm text-slate-700 dark:text-slate-200">整单手机号：<span className="font-mono">{phone || '—'}</span></p>
        {(!validAmounts || error) && <p role="alert" className="rounded-lg bg-red-50 p-3 text-sm text-red-600 dark:bg-red-950/30 dark:text-red-300">{!validAmounts ? '门票金额不完整，请返回并重新读取门票信息' : error}</p>}
      </div>
      <div className="flex shrink-0 flex-wrap items-center justify-end gap-2 border-t border-slate-100 bg-slate-50/80 px-4 py-4 dark:border-slate-800 dark:bg-slate-800/50 sm:px-6">
        <button type="button" onClick={onBack} className="btn-ios-secondary">返回门票信息</button>
        <button type="button" onClick={() => { void saveImage() }} disabled={saving || !visitors.length || !validAmounts} className="btn-ios-primary inline-flex items-center gap-2 disabled:opacity-40">{saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Download className="h-4 w-4" />}{saving ? '正在保存…' : '保存图片'}</button>
      </div>
    </>
  )
}
