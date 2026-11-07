import { useEffect, useRef, useState } from 'react'
import { AlertCircle, Users, Loader2, RefreshCw, Save } from 'lucide-react'
import { getTicketAgePolicy, saveTicketAgePolicy, getPersonalTicketAgePolicy, savePersonalTicketAgePolicy, type TicketAgePolicy, type TicketAgeRules, type TicketVisitorCategory } from '@/api/ticketRegistration'
import { useUIStore } from '@/store/uiStore'
import { getApiErrorMessage } from '@/utils/apiError'
import { formatTicketAgeRange, TICKET_CATEGORY_LABELS, validateTicketAgeRules } from '../ticket-registration/ticketAgeRules'
import { TicketMessageTemplateSettingsCard } from './TicketMessageTemplateSettingsCard'

const CATEGORIES: TicketVisitorCategory[] = ['child', 'adult', 'senior']
type AgeDraft = Record<TicketVisitorCategory, { min: string; max: string }>

function toDraft(rules: TicketAgeRules): AgeDraft {
  return Object.fromEntries(CATEGORIES.map(category => [category, {
    min: String(rules[category].min_age), max: rules[category].max_age === null ? '' : String(rules[category].max_age),
  }])) as AgeDraft
}

export function IdentitySettingsTab({ scope = 'system' }: { scope?: 'system' | 'personal' }) {
  const personal = scope === 'personal'
  const { addToast } = useUIStore()
  const [draft, setDraft] = useState<AgeDraft | null>(null)
  const [savedPolicy, setSavedPolicy] = useState<TicketAgePolicy | null>(null)
  const [enabled, setEnabled] = useState(true)
  const [usePersonal, setUsePersonal] = useState(false)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [reload, setReload] = useState(0)
  const generation = useRef(0)
  const savingRef = useRef(false)
  const editable = !personal || usePersonal

  function applyPolicy(policy: TicketAgePolicy & { use_personal?: boolean }) {
    setSavedPolicy(policy)
    setDraft(toDraft(policy.rules))
    setEnabled(policy.enabled)
    setUsePersonal(policy.use_personal === true)
  }

  useEffect(() => {
    const current = ++generation.current
    setLoading(true)
    setError('')
    setDraft(null)
    void (personal ? getPersonalTicketAgePolicy() : getTicketAgePolicy()).then(result => {
      if (current !== generation.current) return
      if (!result.success || !result.data) throw new Error(result.message || '加载身份信息设置失败')
      applyPolicy(result.data)
    }).catch(reason => {
      if (current === generation.current) setError(getApiErrorMessage(reason, '加载身份信息设置失败'))
    }).finally(() => {
      if (current === generation.current) setLoading(false)
    })
    return () => { generation.current += 1 }
  }, [reload, personal])

  async function persist(followSystem = false) {
    if (!draft || !savedPolicy || savingRef.current || (!editable && !followSystem)) return
    // 关闭或恢复继承时使用最后保存的合法范围，隐藏的编辑草稿不阻止操作。
    let rules = savedPolicy.rules
    if (enabled && !followSystem) {
      rules = {} as TicketAgeRules
      for (const category of CATEGORIES) {
        const { min, max } = draft[category]
        if (!/^\d+$/.test(min.trim()) || (max.trim() !== '' && !/^\d+$/.test(max.trim()))) {
          setError(`${TICKET_CATEGORY_LABELS[category]}票最低年龄必填，年龄需为整数；最高年龄留空表示不限制`)
          return
        }
        rules[category] = { min_age: Number(min), max_age: max.trim() === '' ? null : Number(max) }
      }
      const validation = validateTicketAgeRules(rules)
      if (validation) { setError(validation); return }
    }
    const current = generation.current
    savingRef.current = true
    setSaving(true)
    setError('')
    try {
      const result = personal
        ? await savePersonalTicketAgePolicy(followSystem ? { use_personal: false } : { use_personal: true, enabled, rules })
        : await saveTicketAgePolicy({ enabled, rules })
      if (current !== generation.current) return
      if (!result.success || !result.data) throw new Error(result.message || '保存身份信息设置失败')
      applyPolicy(result.data)
      addToast({ type: 'success', message: followSystem ? '已恢复跟随系统设置' : '身份信息设置已保存' })
    } catch (reason) {
      if (current === generation.current) setError(getApiErrorMessage(reason, '保存身份信息设置失败'))
    } finally {
      savingRef.current = false
      if (current === generation.current) setSaving(false)
    }
  }

  return (
    <div className="space-y-4">
      <div className="vben-card">
        <div className="vben-card-header"><h2 className="vben-card-title"><Users className="h-4 w-4" />门票身份信息</h2></div>
        <div className="vben-card-body">
          {personal && <p className="text-sm leading-6 text-slate-500 dark:text-slate-400">这里的设置仅作用于您所属订单的身份信息收集。使用个人设置后，开关和三类票的年龄范围均以个人设置为准。</p>}
          {loading ? <div role="status" className="flex items-center gap-2 py-10 text-sm text-slate-500"><Loader2 className="h-4 w-4 animate-spin" />正在读取身份信息设置…</div> : (
            <form onSubmit={event => { event.preventDefault(); void persist() }} noValidate className="space-y-5">
              {draft && <>
                {personal && <div className="mt-5 flex flex-wrap items-center justify-between gap-3 rounded-xl bg-slate-50 p-4 dark:bg-slate-800/60">
                  <div><p className="text-sm font-medium">{usePersonal ? '使用个人设置' : '跟随系统设置'}</p><p className="mt-1 text-xs text-slate-500">{usePersonal ? '保存后优先使用您的个人配置，可随时恢复跟随系统。' : '自动使用系统当前配置，系统更新后会同步生效。'}</p></div>
                  {usePersonal
                    ? <button type="button" disabled={saving} onClick={() => void persist(true)} className="btn-ios-secondary disabled:opacity-50">恢复跟随系统</button>
                    : <button type="button" disabled={saving} onClick={() => { setUsePersonal(true); setError('') }} className="btn-ios-secondary">使用个人设置</button>}
                </div>}
                <fieldset disabled={saving || !editable} className="space-y-5">
                  <div className="flex items-center justify-between gap-4 rounded-xl border border-slate-200 p-4 dark:border-slate-700">
                    <label htmlFor={`ticket-age-validation-${scope}`} className="cursor-pointer text-sm font-medium">开启年龄校验</label>
                    <button id={`ticket-age-validation-${scope}`} type="button" role="switch" aria-label="开启年龄校验" aria-checked={enabled} onClick={() => { setEnabled(value => !value); setError('') }} className={`relative h-6 w-11 shrink-0 rounded-full transition-colors disabled:cursor-not-allowed disabled:opacity-60 ${enabled ? 'bg-blue-600' : 'bg-slate-300 dark:bg-slate-600'}`}><span className={`absolute left-0.5 top-0.5 h-5 w-5 rounded-full bg-white shadow transition-transform ${enabled ? 'translate-x-5' : ''}`} /></button>
                  </div>
                  {enabled && <>
                    <p className="text-sm leading-6 text-slate-500 dark:text-slate-400">根据身份证出生日期，计算出行人在游玩日期当天的周岁。提交信息时将校验是否符合对应票种的年龄要求。</p>
                    <div className="grid gap-4 lg:grid-cols-3">
                      {CATEGORIES.map(category => (
                        <section key={category} aria-label={`${TICKET_CATEGORY_LABELS[category]}票年龄设置`} className="rounded-xl border border-slate-200 p-4 dark:border-slate-700">
                          <h3 className="font-medium text-slate-800 dark:text-slate-100">{TICKET_CATEGORY_LABELS[category]}票</h3>
                          {savedPolicy && <p className="mt-1 text-xs text-slate-500">已保存：{formatTicketAgeRange(savedPolicy.rules[category])}</p>}
                          <div className="mt-4 grid grid-cols-2 gap-3">
                            <label className="input-group"><span className="input-label">最低年龄</span><input type="number" min={0} max={150} step={1} inputMode="numeric" aria-label={`${TICKET_CATEGORY_LABELS[category]}票最低年龄`} value={draft[category].min} onChange={event => { setDraft(previous => previous ? { ...previous, [category]: { ...previous[category], min: event.target.value } } : null); setError('') }} className="input-ios w-full" /></label>
                            <label className="input-group"><span className="input-label">最高年龄</span><input type="number" min={0} max={150} step={1} inputMode="numeric" aria-label={`${TICKET_CATEGORY_LABELS[category]}票最高年龄`} value={draft[category].max} placeholder="不限" onChange={event => { setDraft(previous => previous ? { ...previous, [category]: { ...previous[category], max: event.target.value } } : null); setError('') }} className="input-ios w-full" /></label>
                          </div>
                        </section>
                      ))}
                    </div>
                    <p className="text-xs leading-5 text-slate-500 dark:text-slate-400">年龄范围包含最低和最高年龄，最高年龄留空表示不限制。三类票种的年龄范围不能重叠。保存后适用于后续提交，已经提交的信息保持不变。</p>
                  </>}
                </fieldset>
              </>}
              {error && <p role="alert" className="flex items-start gap-2 rounded-lg bg-red-50 p-3 text-sm text-red-700 dark:bg-red-950/30 dark:text-red-300"><AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />{error}</p>}
              <div className="flex justify-end gap-3 border-t border-slate-100 pt-4 dark:border-slate-700">
                {!draft && <button type="button" onClick={() => setReload(value => value + 1)} className="btn-ios-secondary"><RefreshCw className="h-4 w-4" />重新加载</button>}
                <button type="submit" disabled={!draft || saving || !editable} className="btn-ios-primary disabled:opacity-50">{saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}{saving ? '保存中…' : personal ? '保存个人设置' : '保存身份信息设置'}</button>
              </div>
            </form>
          )}
        </div>
      </div>
      <TicketMessageTemplateSettingsCard key={scope} scope={scope} />
    </div>
  )
}
