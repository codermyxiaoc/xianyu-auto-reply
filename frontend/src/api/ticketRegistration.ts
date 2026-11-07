import { get, post, put } from '@/utils/request'
import type { ApiResponse } from '@/types'

const PREFIX = '/api/v1/ticket-registration'

export type TicketVisitorCategory = 'adult' | 'child' | 'senior'

export interface TicketAgeRange {
  min_age: number
  max_age: number | null
}

export type TicketAgeRules = Record<TicketVisitorCategory, TicketAgeRange>

export interface TicketAgePolicy {
  enabled: boolean
  rules: TicketAgeRules
}

export interface PersonalTicketAgePolicy extends TicketAgePolicy {
  use_personal: boolean
}

export interface TicketMessageTemplate {
  message_template: string
}

export interface PersonalTicketMessageTemplate extends TicketMessageTemplate {
  use_personal: boolean
}

export interface TicketTypeOption {
  id: string
  name: string
  category: TicketVisitorCategory | null
}

export interface TicketVisitorConfiguration {
  id: string
  category: TicketVisitorCategory
  amount: string
  ticket_type_id: string
  ticket_type_name: string
  name: string | null
  id_number: string | null
}

export interface TicketConfiguration {
  id: string
  version: number
  visit_date: string
  status: 'draft' | 'submitted'
  visitors: TicketVisitorConfiguration[]
  link: string
  public_url_ready: boolean
  submitted_at: string | null
  sent_at: string | null
  phone: string | null
  exported_at: string | null
}

export interface TicketOrderConfiguration {
  order_id: string
  order_no: string
  order_status: string
  configurable: boolean
  configuration: TicketConfiguration | null
  age_rules?: TicketAgeRules
  age_validation_enabled?: boolean
}

export interface SaveTicketConfigurationInput {
  version: number
  visit_date: string
  visitors: Array<{
    id?: string
    category: TicketVisitorCategory
    amount: string
    ticket_type_id: string
  }>
}

export interface PublicTicketVisitor {
  id: string
  category: TicketVisitorCategory
  amount: string
  ticket_type_name: string
  name: string | null
  id_number_masked: string | null
}

export interface PublicTicketForm {
  order_no: string
  visit_date: string
  version: number
  status: 'draft' | 'submitted'
  submitted_at: string | null
  visitors: PublicTicketVisitor[]
  phone_masked: string | null
  age_rules?: TicketAgeRules
  age_validation_enabled?: boolean
}

export interface SubmitTicketFormInput {
  version: number
  phone: string
  visitors: Array<{ id: string; name: string; id_number: string }>
}

export interface CompleteTicketPhoneInput {
  version: number
  phone: string
}

export async function exportTicketRegistrations(orderIds: string[]): Promise<Blob> {
  const selectedIds = [...new Set(orderIds)]
  if (selectedIds.length === 0) throw new Error('请先勾选需要导出的订单')
  if (selectedIds.length > 1000) throw new Error('每次最多导出 1000 个选中订单')
  const file = await post<Blob>(`${PREFIX}/export.xlsx`, { order_ids: selectedIds }, { responseType: 'blob', timeout: 150000 })
  // 项目业务错误也可能以 HTTP 200 返回，不能把 JSON 错误保存为 Excel。
  const signature = new Uint8Array(await file.slice(0, 4).arrayBuffer())
  if (file.type.includes('json') || signature[0] !== 0x50 || signature[1] !== 0x4b) {
    let message = '导出失败，服务未返回有效的 Excel 文件'
    try {
      const result = JSON.parse(await file.text()) as ApiResponse
      message = result.message || result.detail || message
    } catch { /* 非 JSON 的错误页使用统一提示。 */ }
    throw new Error(message)
  }
  return file
}

export const getTicketTypes = (): Promise<ApiResponse<TicketTypeOption[]>> => get(`${PREFIX}/types`)

export const getTicketAgeRules = (): Promise<ApiResponse<TicketAgeRules>> => get(`${PREFIX}/age-rules`)

export const saveTicketAgeRules = (rules: TicketAgeRules): Promise<ApiResponse<TicketAgeRules>> =>
  put(`${PREFIX}/age-rules`, rules)

export const getTicketAgePolicy = (): Promise<ApiResponse<TicketAgePolicy>> => get(`${PREFIX}/age-policy`)

export const saveTicketAgePolicy = (policy: TicketAgePolicy): Promise<ApiResponse<TicketAgePolicy>> =>
  put(`${PREFIX}/age-policy`, policy)

export const getPersonalTicketAgePolicy = (): Promise<ApiResponse<PersonalTicketAgePolicy>> => get(`${PREFIX}/personal-age-policy`)

export const savePersonalTicketAgePolicy = (policy: PersonalTicketAgePolicy | { use_personal: false }): Promise<ApiResponse<PersonalTicketAgePolicy>> =>
  put(`${PREFIX}/personal-age-policy`, policy)

export const getTicketMessageTemplate = (): Promise<ApiResponse<TicketMessageTemplate>> =>
  get(`${PREFIX}/message-template`)

export const saveTicketMessageTemplate = (template: TicketMessageTemplate): Promise<ApiResponse<TicketMessageTemplate>> =>
  put(`${PREFIX}/message-template`, template)

export const getPersonalTicketMessageTemplate = (): Promise<ApiResponse<PersonalTicketMessageTemplate>> =>
  get(`${PREFIX}/personal-message-template`)

export const savePersonalTicketMessageTemplate = (
  template: PersonalTicketMessageTemplate | { use_personal: false },
): Promise<ApiResponse<PersonalTicketMessageTemplate>> => put(`${PREFIX}/personal-message-template`, template)

export const getTicketOrderConfiguration = (orderId: string): Promise<ApiResponse<TicketOrderConfiguration>> =>
  get(`${PREFIX}/orders/${encodeURIComponent(orderId)}`, { headers: { 'X-Frontend-Origin': window.location.origin } })

export const saveTicketOrderConfiguration = (
  orderId: string,
  data: SaveTicketConfigurationInput,
): Promise<ApiResponse<TicketOrderConfiguration>> => put(`${PREFIX}/orders/${encodeURIComponent(orderId)}`, data, {
  headers: { 'X-Frontend-Origin': window.location.origin },
})

export const recollectTicketOrderInformation = (
  orderId: string,
  version: number,
): Promise<ApiResponse<TicketOrderConfiguration>> => post(`${PREFIX}/orders/${encodeURIComponent(orderId)}/recollect`, { version }, {
  headers: { 'X-Frontend-Origin': window.location.origin },
})

export const sendTicketRegistrationLink = (
  orderId: string,
  version: number,
): Promise<ApiResponse<TicketOrderConfiguration>> => post(`${PREFIX}/orders/${encodeURIComponent(orderId)}/send`, { version }, {
  timeout: 150000,
  headers: { 'X-Frontend-Origin': window.location.origin },
})

// 买家无需登录后台，公开请求不携带后台令牌或 Cookie，也不经过登录拦截器。
async function publicRequest(
  path: string,
  token: string,
  data?: SubmitTicketFormInput | CompleteTicketPhoneInput,
): Promise<ApiResponse<PublicTicketForm>> {
  const controller = new AbortController()
  const timeout = window.setTimeout(() => controller.abort(), 30000)
  try {
    const response = await fetch(`${PREFIX}/public/${path}`, {
      method: data ? 'POST' : 'GET',
      headers: { 'Content-Type': 'application/json', 'X-Ticket-Token': token },
      credentials: 'omit',
      cache: 'no-store',
      referrerPolicy: 'no-referrer',
      signal: controller.signal,
      ...(data ? { body: JSON.stringify(data) } : {}),
    })
    const result = await response.json() as ApiResponse<PublicTicketForm>
    if (typeof result?.success !== 'boolean') throw new Error('服务返回异常，请稍后重试')
    return result
  } catch (error) {
    if (error instanceof Error && error.name === 'AbortError') throw new Error('请求超时，请稍后重试')
    throw error
  } finally {
    window.clearTimeout(timeout)
  }
}

export const getPublicTicketForm = (token: string): Promise<ApiResponse<PublicTicketForm>> =>
  publicRequest('form', token)

export const submitPublicTicketForm = (
  token: string,
  data: SubmitTicketFormInput,
): Promise<ApiResponse<PublicTicketForm>> => publicRequest('submit', token, data)

export const completePublicTicketPhone = (
  token: string,
  data: CompleteTicketPhoneInput,
): Promise<ApiResponse<PublicTicketForm>> => publicRequest('phone', token, data)
