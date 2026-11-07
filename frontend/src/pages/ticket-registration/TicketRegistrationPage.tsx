import { useCallback, useEffect, useRef, useState } from 'react'
import { useLocation } from 'react-router-dom'
import { AlertCircle, CalendarDays, Check, CheckCircle2, Loader2, LockKeyhole, Phone, RefreshCw, Ticket, UserRound } from 'lucide-react'
import { completePublicTicketPhone, getPublicTicketForm, submitPublicTicketForm, type PublicTicketForm } from '@/api/ticketRegistration'
import { TicketSubmitConfirmation } from './TicketSubmitConfirmation'
import { ageOnVisitDate, formatTicketAgeRange, ticketAgeError, TICKET_CATEGORIES, TICKET_CATEGORY_LABELS } from './ticketAgeRules'

type Identity = { name: string; id_number: string }
type IdentityErrors = Partial<Record<keyof Identity, string>>
type PendingSubmission = {
  generation: number
  token: string
  version: number
  phone: string
  completingPhone: boolean
  visitors: Array<Identity & { id: string }>
}

function identityNumberError(value: string): string | null {
  if (!/^[1-9]\d{16}[\dX]$/.test(value)) return '请填写18位身份证号码，末位可为 X'
  const year = Number(value.slice(6, 10))
  const month = Number(value.slice(10, 12))
  const day = Number(value.slice(12, 14))
  const birth = new Date(Date.UTC(year, month - 1, day))
  if (year < 1900 || birth.getUTCFullYear() !== year || birth.getUTCMonth() !== month - 1 || birth.getUTCDate() !== day || birth.getTime() > Date.now()) {
    return '身份证中的出生日期不正确'
  }
  const weights = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
  const sum = weights.reduce((total, weight, index) => total + Number(value[index]) * weight, 0)
  return '10X98765432'[sum % 11] === value[17] ? null : '身份证校验不通过，请核对号码'
}

function displayDate(value: string): string {
  const [year, month, day] = value.split('-')
  return `${year}年${Number(month)}月${Number(day)}日`
}

export function TicketRegistrationPage() {
  const location = useLocation()
  const token = location.hash.slice(1)
  const [form, setForm] = useState<PublicTicketForm | null>(null)
  const [entries, setEntries] = useState<Record<string, Identity>>({})
  const [errors, setErrors] = useState<Record<string, IdentityErrors>>({})
  const [phone, setPhone] = useState('')
  const [phoneError, setPhoneError] = useState('')
  const [loading, setLoading] = useState(true)
  const [submitting, setSubmitting] = useState(false)
  const [message, setMessage] = useState('')
  const [pendingSubmission, setPendingSubmission] = useState<PendingSubmission | null>(null)
  const requestGeneration = useRef(0)
  const submissionInFlight = useRef(false)
  const ageValidationEnabled = form?.age_validation_enabled !== false

  const loadForm = useCallback(async () => {
    const generation = ++requestGeneration.current
    setLoading(true)
    setMessage('')
    setErrors({})
    setEntries({})
    setPhone('')
    setPhoneError('')
    setForm(null)
    setPendingSubmission(null)
    setSubmitting(false)
    submissionInFlight.current = false
    if (!/^[A-Za-z0-9_-]{32,128}$/.test(token)) {
      setMessage('登记链接不完整或已失效，请联系卖家获取订单专属链接。')
      setLoading(false)
      return
    }
    try {
      const result = await getPublicTicketForm(token)
      if (generation !== requestGeneration.current) return
      if (!result.success || !result.data) {
        setMessage(result.message || '暂时无法打开登记页面，请联系卖家。')
        return
      }
      setForm(result.data)
      if (result.data.status === 'draft') {
        setEntries(Object.fromEntries(result.data.visitors.map(visitor => [visitor.id, { name: '', id_number: '' }])))
      }
    } catch {
      if (generation === requestGeneration.current) setMessage('页面加载失败，请检查网络后重试。')
    } finally {
      if (generation === requestGeneration.current) setLoading(false)
    }
  }, [token])

  useEffect(() => {
    void loadForm()
    return () => { requestGeneration.current += 1 }
  }, [loadForm])

  useEffect(() => {
    const previousTitle = document.title
    document.title = '门票出行信息登记'
    const previousReferrer = document.querySelector<HTMLMetaElement>('meta[name="referrer"]')
    const previousValue = previousReferrer?.content
    const meta = previousReferrer || document.createElement('meta')
    meta.name = 'referrer'
    meta.content = 'no-referrer'
    if (!previousReferrer) document.head.appendChild(meta)
    return () => {
      document.title = previousTitle
      if (previousReferrer) previousReferrer.content = previousValue || ''
      else meta.remove()
    }
  }, [])

  const updateIdentity = (id: string, field: keyof Identity, value: string) => {
    setEntries(previous => ({ ...previous, [id]: { ...previous[id], [field]: value } }))
    setErrors(previous => ({ ...previous, [id]: { ...previous[id], [field]: undefined } }))
  }

  const handleSubmit = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!form || (form.status === 'submitted' && form.phone_masked) || pendingSubmission || submissionInFlight.current) return
    const completingPhone = form.status === 'submitted'
    const contactPhone = phone.trim()
    const nextPhoneError = /^1[3-9]\d{9}$/.test(contactPhone) ? '' : '请填写正确的11位手机号'
    setPhoneError(nextPhoneError)
    const nextErrors: Record<string, IdentityErrors> = {}
    const visitors = (completingPhone ? [] : form.visitors).map(visitor => {
      const entry = entries[visitor.id] || { name: '', id_number: '' }
      const name = entry.name.trim()
      const idNumber = entry.id_number.trim().toUpperCase()
      const fields: IdentityErrors = {}
      if (name.length < 2 || name.length > 50) fields.name = '请填写2至50个字的真实姓名'
      const idError = identityNumberError(idNumber) || (ageValidationEnabled ? ticketAgeError(idNumber, form.visit_date, visitor.category, form.age_rules) : null)
      if (idError) fields.id_number = idError
      if (Object.keys(fields).length) nextErrors[visitor.id] = fields
      return { id: visitor.id, name, id_number: idNumber }
    })
    const seen = new Set<string>()
    for (const visitor of visitors) {
      if (visitor.id_number && seen.has(visitor.id_number)) {
        nextErrors[visitor.id] = { ...nextErrors[visitor.id], id_number: '此身份证已填写，请核对每位出行人' }
      }
      seen.add(visitor.id_number)
    }
    setErrors(nextErrors)
    if (Object.keys(nextErrors).length || nextPhoneError) {
      setMessage('还有信息需要核对，请检查下方标出的内容。')
      const firstId = Object.keys(nextErrors)[0]
      if (firstId) {
        const firstField = nextErrors[firstId].name ? 'name' : 'id_number'
        document.getElementById(`visitor-${firstId}-${firstField}`)?.focus()
      } else document.getElementById('ticket-contact-phone')?.focus()
      return
    }
    setMessage('')
    setPendingSubmission({ generation: requestGeneration.current, token, version: form.version, phone: contactPhone, completingPhone, visitors })
  }

  const confirmSubmission = async () => {
    if (!pendingSubmission || !form || submissionInFlight.current) return
    const { generation, version, phone: contactPhone, completingPhone, visitors } = pendingSubmission
    if (generation !== requestGeneration.current || pendingSubmission.token !== token || version !== form.version) {
      setPendingSubmission(null)
      return
    }
    submissionInFlight.current = true
    setSubmitting(true)
    setMessage('')
    try {
      const result = completingPhone
        ? await completePublicTicketPhone(token, { version, phone: contactPhone })
        : await submitPublicTicketForm(token, { version, phone: contactPhone, visitors })
      if (generation !== requestGeneration.current) return
      if (!result.success || !result.data) {
        setMessage(result.message || '提交失败，请稍后重试。')
        return
      }
      setForm(result.data)
      setEntries({})
      setErrors({})
      setPhone('')
      setPhoneError('')
      window.scrollTo({ top: 0, behavior: 'smooth' })
    } catch {
      if (generation === requestGeneration.current) setMessage('提交结果暂时无法确认，请刷新页面查看是否已登记。')
    } finally {
      if (generation === requestGeneration.current) {
        setPendingSubmission(null)
        setSubmitting(false)
        submissionInFlight.current = false
      }
    }
  }

  const submitted = form?.status === 'submitted'
  const needsPhone = submitted && !form?.phone_masked
  const adultCount = form?.visitors.filter(visitor => visitor.category === 'adult').length || 0
  const childCount = form?.visitors.filter(visitor => visitor.category === 'child').length || 0
  const seniorCount = form?.visitors.filter(visitor => visitor.category === 'senior').length || 0
  const totalCents = form?.visitors.reduce((sum, visitor) => sum + Math.round(Number(visitor.amount) * 100), 0) || 0

  return (
    <main className="min-h-screen bg-slate-50 px-4 py-6 text-slate-800 dark:bg-slate-950 dark:text-slate-100 sm:py-10">
      <div className="mx-auto max-w-2xl">
        <header className="mb-7 flex items-center gap-3">
          <div className="flex h-12 w-12 shrink-0 items-center justify-center rounded-2xl bg-blue-600 text-white shadow-sm"><Ticket className="h-6 w-6" /></div>
          <div>
            <p className="text-xs font-medium tracking-widest text-blue-600 dark:text-blue-400">门票订单</p>
            <h1 className="mt-1 text-2xl font-semibold tracking-tight">出行信息登记</h1>
          </div>
        </header>

        {loading ? (
          <div role="status" className="rounded-2xl border border-slate-200 bg-white px-6 py-16 text-center dark:border-slate-800 dark:bg-slate-900">
            <Loader2 className="mx-auto mb-3 h-6 w-6 animate-spin text-blue-600" />正在读取订单信息…
          </div>
        ) : !form ? (
          <section className="rounded-2xl border border-slate-200 bg-white p-8 text-center dark:border-slate-800 dark:bg-slate-900">
            <AlertCircle className="mx-auto mb-4 h-9 w-9 text-amber-500" />
            <h2 className="text-lg font-semibold">暂时无法登记</h2>
            <p role="alert" className="mt-3 break-words text-sm leading-6 text-slate-500 dark:text-slate-400">{message}</p>
            <button type="button" onClick={() => void loadForm()} className="mt-6 inline-flex items-center gap-2 rounded-xl bg-blue-600 px-5 py-2.5 text-sm font-medium text-white hover:bg-blue-700"><RefreshCw className="h-4 w-4" />重新加载</button>
          </section>
        ) : (
          <>
            {submitted && (
              <section role="status" className="mb-5 flex gap-3 rounded-2xl border border-emerald-200 bg-emerald-50 p-5 dark:border-emerald-900 dark:bg-emerald-950/40">
                <CheckCircle2 className="mt-0.5 h-6 w-6 shrink-0 text-emerald-600" />
                <div><h2 className="font-semibold text-emerald-800 dark:text-emerald-300">{needsPhone ? '出行人已登记，请补填手机号' : '出行信息已提交'}</h2><p className="mt-1 text-sm leading-6 text-emerald-700 dark:text-emerald-400">{needsPhone ? '出行人信息已保存，无需重复填写。请在页面底部补充本订单的联系电话。' : '卖家已可查看登记信息，无需重复填写。如需更正，请联系卖家。'}</p></div>
              </section>
            )}

            <section aria-label="订单信息" className="mb-5 overflow-hidden rounded-2xl border border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900">
              <div className="border-b border-slate-100 px-5 py-4 dark:border-slate-800 sm:px-6"><p className="text-xs text-slate-500">订单号</p><p className="mt-1 break-all font-mono text-sm font-medium">{form.order_no}</p></div>
              <div className="grid gap-4 px-5 py-5 sm:grid-cols-2 sm:px-6">
                <div><p className="mb-2 text-xs text-slate-500">出行日期</p><p className="flex items-center gap-2 font-medium"><CalendarDays className="h-4 w-4 text-blue-600" />{displayDate(form.visit_date)}</p></div>
                <div className="sm:text-right"><p className="mb-2 text-xs text-slate-500">出行人数 · 门票合计</p><p className="text-sm"><span>{adultCount} 位成人 · {childCount} 位儿童 · {seniorCount} 位老人</span><span className="ml-3 font-semibold tabular-nums">¥{(totalCents / 100).toFixed(2)}</span></p></div>
              </div>
              <p className="flex items-start gap-2 bg-slate-50 px-5 py-3 text-xs leading-5 text-slate-500 dark:bg-slate-800/50 dark:text-slate-400 sm:px-6"><LockKeyhole className="mt-0.5 h-3.5 w-3.5 shrink-0" />日期、人数、票种和金额由卖家确认。如有疑问，请联系卖家。</p>
            </section>

            <form onSubmit={handleSubmit} noValidate>
              <fieldset disabled={submitting} className="space-y-4">
                <legend className="mb-3 text-sm font-semibold">{submitted ? '已登记出行人' : '填写每位出行人的信息'}</legend>
                {TICKET_CATEGORIES.map(categoryKey => {
                  const categoryVisitors = form.visitors.filter(visitor => visitor.category === categoryKey)
                  if (!categoryVisitors.length) return null
                  const category = TICKET_CATEGORY_LABELS[categoryKey]
                  return (
                    <section key={categoryKey} aria-label={`${category}出行人`} className="overflow-hidden rounded-2xl border border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900">
                      <div className="flex gap-3 border-b border-slate-100 bg-slate-50/80 px-5 py-4 dark:border-slate-800 dark:bg-slate-800/40 sm:px-6">
                        <div className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-xl ${categoryKey === 'adult' ? 'bg-blue-50 text-blue-600 dark:bg-blue-950' : categoryKey === 'senior' ? 'bg-violet-50 text-violet-600 dark:bg-violet-950' : 'bg-orange-50 text-orange-600 dark:bg-orange-950'}`}><UserRound className="h-4 w-4" /></div>
                        <div><h2 className="text-base font-semibold">{category}<span className="ml-2 rounded-full bg-white px-2 py-0.5 text-xs font-medium text-slate-500 dark:bg-slate-800 dark:text-slate-400">{categoryVisitors.length} 位</span></h2>{!submitted && ageValidationEnabled && form.age_rules && <p className="mt-1 text-xs leading-5 text-slate-500 dark:text-slate-400">游玩当天须为 {formatTicketAgeRange(form.age_rules[categoryKey])}</p>}</div>
                      </div>
                      <div className="divide-y divide-slate-100 dark:divide-slate-800">
                        {categoryVisitors.map((visitor, index) => {
                          const identity = entries[visitor.id] || { name: '', id_number: '' }
                          const fieldErrors = errors[visitor.id] || {}
                          const age = submitted || !ageValidationEnabled ? null : ageOnVisitDate(identity.id_number.trim(), form.visit_date)
                          return (
                            <div key={visitor.id} role="group" aria-label={`${category} ${index + 1}`} className="px-5 py-5 sm:px-6">
                              <div className="flex flex-wrap items-start justify-between gap-3">
                                <div><h3 className="text-sm font-semibold">{category} {index + 1}</h3><p className="mt-1 text-sm text-slate-500 dark:text-slate-400">{visitor.ticket_type_name}</p></div>
                                <p className="pt-1 text-sm font-semibold tabular-nums">¥{Number(visitor.amount).toFixed(2)}</p>
                              </div>
                              <div className="mt-4 grid gap-4 sm:grid-cols-2">
                                {submitted ? (
                                  <><div><p className="text-xs text-slate-500">姓名</p><p className="mt-2 break-words text-sm font-medium">{visitor.name || '已登记'}</p></div><div><p className="text-xs text-slate-500">身份证</p><p className="mt-2 font-mono text-sm">{visitor.id_number_masked || '已登记'}</p></div></>
                                ) : (
                                  <>
                                    <div>
                                      <label htmlFor={`visitor-${visitor.id}-name`} className="mb-2 block text-sm font-medium">姓名<span className="ml-1 text-red-500">*</span></label>
                                      <input id={`visitor-${visitor.id}-name`} value={identity.name} onChange={event => updateIdentity(visitor.id, 'name', event.target.value)} maxLength={50} autoComplete="off" placeholder="与身份证上的姓名一致" aria-invalid={Boolean(fieldErrors.name)} aria-describedby={fieldErrors.name ? `error-${visitor.id}-name` : undefined} className={`w-full rounded-xl border bg-white px-3 py-3 text-base outline-none transition focus:ring-2 dark:bg-slate-950 sm:text-sm ${fieldErrors.name ? 'border-red-400 focus:ring-red-100' : 'border-slate-200 focus:border-blue-500 focus:ring-blue-100 dark:border-slate-700 dark:focus:ring-blue-900'}`} />
                                      {fieldErrors.name && <p id={`error-${visitor.id}-name`} className="mt-1.5 text-xs text-red-600 dark:text-red-400">{fieldErrors.name}</p>}
                                    </div>
                                    <div>
                                      <label htmlFor={`visitor-${visitor.id}-id_number`} className="mb-2 block text-sm font-medium">身份证<span className="ml-1 text-red-500">*</span></label>
                                      <input id={`visitor-${visitor.id}-id_number`} value={identity.id_number} onChange={event => updateIdentity(visitor.id, 'id_number', event.target.value.toUpperCase())} onBlur={() => { const value = identity.id_number.trim().toUpperCase(); if (!value) return; const error = identityNumberError(value) || (ageValidationEnabled ? ticketAgeError(value, form.visit_date, visitor.category, form.age_rules) : null); setErrors(previous => ({ ...previous, [visitor.id]: { ...previous[visitor.id], id_number: error || undefined } })) }} maxLength={18} autoComplete="off" autoCapitalize="characters" spellCheck={false} placeholder="18位身份证号码" aria-invalid={Boolean(fieldErrors.id_number)} aria-describedby={fieldErrors.id_number ? `error-${visitor.id}-id_number` : ageValidationEnabled ? `age-${visitor.id}` : undefined} className={`w-full rounded-xl border bg-white px-3 py-3 text-base outline-none transition focus:ring-2 dark:bg-slate-950 sm:text-sm ${fieldErrors.id_number ? 'border-red-400 focus:ring-red-100' : 'border-slate-200 focus:border-blue-500 focus:ring-blue-100 dark:border-slate-700 dark:focus:ring-blue-900'}`} />
                                      {fieldErrors.id_number && <p id={`error-${visitor.id}-id_number`} className="mt-1.5 text-xs text-red-600 dark:text-red-400">{fieldErrors.id_number}</p>}
                                      {ageValidationEnabled && <p id={`age-${visitor.id}`} className="mt-1.5 text-xs leading-5 text-slate-500 dark:text-slate-400">{age === null ? '按身份证出生日期计算游玩当天的周岁' : `游玩当天为 ${age} 周岁`}</p>}
                                    </div>
                                  </>
                                )}
                              </div>
                            </div>
                          )
                        })}
                      </div>
                    </section>
                  )
                })}
              </fieldset>

              <section aria-label="订单联系电话" className="mt-5 rounded-2xl border border-slate-200 bg-white px-5 py-5 dark:border-slate-800 dark:bg-slate-900 sm:px-6">
                <div className="mb-2 flex items-center gap-2 text-sm font-semibold"><Phone className="h-4 w-4 text-blue-600" /><label htmlFor="ticket-contact-phone">手机号{(!submitted || needsPhone) && <span className="ml-1 text-red-500">*</span>}</label></div>
                {submitted && form.phone_masked ? (
                  <p className="mt-3 font-mono text-base" aria-label="已登记手机号">{form.phone_masked}</p>
                ) : (
                  <><input id="ticket-contact-phone" type="tel" inputMode="numeric" autoComplete="tel-national" maxLength={11} disabled={submitting} value={phone} onChange={event => { setPhone(event.target.value); setPhoneError('') }} placeholder="请输入11位手机号" aria-invalid={Boolean(phoneError)} aria-describedby={phoneError ? 'ticket-phone-error' : 'ticket-phone-hint'} className={`w-full rounded-xl border bg-white px-3 py-3 text-base outline-none transition focus:ring-2 disabled:opacity-60 dark:bg-slate-950 sm:text-sm ${phoneError ? 'border-red-400 focus:ring-red-100' : 'border-slate-200 focus:border-blue-500 focus:ring-blue-100 dark:border-slate-700 dark:focus:ring-blue-900'}`} />
                    {phoneError && <p id="ticket-phone-error" className="mt-1.5 text-xs text-red-600 dark:text-red-400">{phoneError}</p>}</>
                )}
                <p id="ticket-phone-hint" className="mt-2 text-xs leading-5 text-slate-500 dark:text-slate-400">整单填写一个联系电话，方便卖家联系。</p>
              </section>

              {message && <div role="alert" className="mt-4 rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm leading-6 text-amber-800 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-300"><p>{message}</p><button type="button" disabled={submitting} onClick={() => void loadForm()} className="mt-2 font-medium underline underline-offset-4 disabled:opacity-50">重新读取订单信息</button></div>}

              {(!submitted || needsPhone) && <div className="mt-6 pb-4"><p className="mb-3 text-center text-xs leading-5 text-slate-500 dark:text-slate-400">{needsPhone ? '请核对联系电话，保存后如需更正请联系卖家。' : '请核对所有出行人信息和联系电话，提交后如需更正请联系卖家。'}</p><button type="submit" disabled={submitting} className="flex w-full items-center justify-center gap-2 rounded-xl bg-blue-600 px-5 py-3.5 text-base font-medium text-white shadow-sm transition hover:bg-blue-700 disabled:cursor-wait disabled:opacity-60">{submitting ? <Loader2 className="h-5 w-5 animate-spin" /> : <Check className="h-5 w-5" />}{submitting ? '正在提交…' : needsPhone ? '保存手机号' : '确认并提交出行信息'}</button></div>}
            </form>
          </>
        )}
        <p className="mt-6 text-center text-xs text-slate-400">门票订单 · 出行信息登记</p>
      </div>
      {pendingSubmission && form && (
        <TicketSubmitConfirmation
          orderNumber={form.order_no}
          visitDate={displayDate(form.visit_date)}
          phone={pendingSubmission.phone}
          visitors={pendingSubmission.visitors.map(visitor => {
            const configured = form.visitors.find(item => item.id === visitor.id)!
            const categoryNumber = form.visitors.filter(item => item.category === configured.category).findIndex(item => item.id === visitor.id) + 1
            return { ...visitor, ticket_type_name: configured.ticket_type_name, category: configured.category, categoryNumber, age: ageValidationEnabled ? ageOnVisitDate(visitor.id_number, form.visit_date) : null }
          })}
          completingPhone={pendingSubmission.completingPhone}
          loading={submitting}
          onConfirm={() => void confirmSubmission()}
          onCancel={() => { if (!submissionInFlight.current) setPendingSubmission(null) }}
        />
      )}
    </main>
  )
}
