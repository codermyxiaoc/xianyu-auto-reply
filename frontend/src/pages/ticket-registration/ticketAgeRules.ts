import type { TicketAgeRange, TicketAgeRules, TicketVisitorCategory } from '@/api/ticketRegistration'

export const TICKET_CATEGORIES: TicketVisitorCategory[] = ['adult', 'child', 'senior']
export const TICKET_CATEGORY_LABELS: Record<TicketVisitorCategory, string> = {
  adult: '成人', child: '儿童', senior: '老人',
}

export function formatTicketAgeRange(range: TicketAgeRange): string {
  if (range.max_age === null) return `${range.min_age} 周岁及以上`
  if (range.min_age === range.max_age) return `${range.min_age} 周岁`
  return `${range.min_age}–${range.max_age} 周岁（含）`
}

/** 按游玩当天的完整周岁计算，不使用浏览器时区或当前年份。 */
export function ageOnVisitDate(idNumber: string, visitDate: string): number | null {
  if (!/^[1-9]\d{16}[\dXx]$/.test(idNumber) || !/^\d{4}-\d{2}-\d{2}$/.test(visitDate)) return null
  const birthYear = Number(idNumber.slice(6, 10))
  const birthMonth = Number(idNumber.slice(10, 12))
  const birthDay = Number(idNumber.slice(12, 14))
  const [year, month, day] = visitDate.split('-').map(Number)
  const validDate = (y: number, m: number, d: number) => {
    const value = new Date(Date.UTC(y, m - 1, d))
    return value.getUTCFullYear() === y && value.getUTCMonth() === m - 1 && value.getUTCDate() === d
  }
  if (!validDate(birthYear, birthMonth, birthDay) || !validDate(year, month, day)) return null
  const age = year - birthYear - (month < birthMonth || (month === birthMonth && day < birthDay) ? 1 : 0)
  return age < 0 ? null : age
}

export function ticketAgeError(idNumber: string, visitDate: string, category: TicketVisitorCategory, rules?: TicketAgeRules): string | null {
  if (!rules) return null
  const age = ageOnVisitDate(idNumber, visitDate)
  if (age === null) return '身份证出生日期晚于游玩日期或日期不正确，请核对'
  const range = rules[category]
  if (age < range.min_age || (range.max_age !== null && age > range.max_age)) {
    return `游玩当天为 ${age} 周岁，不符合${TICKET_CATEGORY_LABELS[category]}票要求（${formatTicketAgeRange(range)}），请核对或联系卖家`
  }
  return null
}

export function validateTicketAgeRules(rules: TicketAgeRules): string | null {
  for (const category of TICKET_CATEGORIES) {
    const { min_age: min, max_age: max } = rules[category]
    if (!Number.isInteger(min) || min < 0 || min > 150 || (max !== null && (!Number.isInteger(max) || max < 0 || max > 150))) {
      return `${TICKET_CATEGORY_LABELS[category]}票年龄必须为 0 至 150 的整数，最高年龄可留空`
    }
    if (max !== null && min > max) return `${TICKET_CATEGORY_LABELS[category]}票最低年龄不能超过最高年龄`
  }
  for (let index = 0; index < TICKET_CATEGORIES.length; index += 1) {
    const category = TICKET_CATEGORIES[index]
    const range = rules[category]
    for (const other of TICKET_CATEGORIES.slice(index + 1)) {
      const next = rules[other]
      if (range.min_age <= (next.max_age ?? Infinity) && next.min_age <= (range.max_age ?? Infinity)) {
        return `${TICKET_CATEGORY_LABELS[category]}票与${TICKET_CATEGORY_LABELS[other]}票的年龄范围不能重叠`
      }
    }
  }
  return null
}
