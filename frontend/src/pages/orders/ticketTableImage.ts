/** 图片生成只接收可展示字段，不接收完整订单配置或登记链接。 */
export interface TicketTableImageInput {
  orderNo: string
  visitDate: string
  phone: string
  visitors: Array<{ name: string; idNumber: string; ticketType: string; amount: string }>
}

interface ImageOptions { signal?: AbortSignal }
const WIDTH = 1280
const MARGIN = 40
const COLUMN_WIDTHS = [220, 340, 410, 230]
const CELL_PADDING = 18
const LINE_HEIGHT = 34
const FONT = '"Microsoft YaHei", "PingFang SC", "Segoe UI", sans-serif'
const MONO_FONT = 'Consolas, "Courier New", monospace'
const IMAGE_FAILURE = '无法生成门票图片，请刷新页面后重试'

function checkAborted(signal?: AbortSignal) {
  if (signal?.aborted) throw new DOMException('已取消保存图片', 'AbortError')
}

function text(value: string): string {
  if (typeof value !== 'string' || value.length > 1000) throw new Error('表格文字不完整或过长，请重新读取门票信息')
  return value.trim() || '—'
}

function cents(value: string): number {
  const match = typeof value === 'string' ? /^(\d{1,8})(?:\.(\d{1,2}))?$/.exec(value) : null
  if (!match) throw new Error('门票金额格式不正确，请返回并检查金额')
  return Number(match[1]) * 100 + Number((match[2] || '').padEnd(2, '0'))
}

function amountText(value: number): string {
  return `${Math.floor(value / 100)}.${String(value % 100).padStart(2, '0')}`
}

/** 按实际字体宽度换行，绝不使用省略号或把身份证转为数字。 */
function wrap(context: CanvasRenderingContext2D, value: string, width: number): string[] {
  const lines: string[] = []
  for (const paragraph of value.replace(/\r\n?/g, '\n').split('\n')) {
    let line = ''
    for (const character of Array.from(paragraph)) {
      if (line && context.measureText(line + character).width > width) {
        lines.push(line)
        line = character
      } else line += character
    }
    lines.push(line)
  }
  return lines
}

function cellFont(column: number) {
  return `24px ${column === 1 || column === 3 ? MONO_FONT : FONT}`
}

function encodePng(canvas: HTMLCanvasElement, signal?: AbortSignal): Promise<Blob> {
  return new Promise((resolve, reject) => {
    let settled = false
    const finish = (blob: Blob | null, error?: unknown) => {
      if (settled) return
      settled = true
      signal?.removeEventListener('abort', abort)
      if (error) reject(error)
      else if (blob) resolve(blob)
      else reject(new Error(IMAGE_FAILURE))
    }
    const abort = () => finish(null, new DOMException('已取消保存图片', 'AbortError'))
    signal?.addEventListener('abort', abort, { once: true })
    if (signal?.aborted) { abort(); return }
    try { canvas.toBlob(blob => finish(blob), 'image/png') } catch { finish(null, new Error(IMAGE_FAILURE)) }
  })
}

/** 生成整张表格 PNG，而非截取弹框当前可视区域。所有字体均来自本机。 */
export async function generateTicketTableImage(input: TicketTableImageInput, options: ImageOptions = {}): Promise<Blob> {
  const { signal } = options
  checkAborted(signal)
  if (!Array.isArray(input.visitors) || input.visitors.length < 1 || input.visitors.length > 50) {
    throw new Error('门票图片支持 1 至 50 位出行人')
  }
  const orderNo = text(input.orderNo)
  const visitDate = text(input.visitDate)
  const phone = text(input.phone)
  const visitors = input.visitors.map(visitor => ({
    name: text(visitor.name), idNumber: text(visitor.idNumber), ticketType: text(visitor.ticketType),
    cents: cents(visitor.amount),
  }))
  const total = visitors.reduce((sum, visitor) => sum + visitor.cents, 0)
  const canvas = document.createElement('canvas')
  try {
    const context = canvas.getContext('2d')
    if (!context) throw new Error(IMAGE_FAILURE)
    const tableWidth = WIDTH - MARGIN * 2
    context.font = `24px ${FONT}`
    const orderLines = wrap(context, `订单号：${orderNo}`, tableWidth)
    const dateLines = wrap(context, `游玩日期：${visitDate}    人数：${visitors.length} 位`, tableWidth)
    const tableTop = MARGIN + 64 + (orderLines.length + dateLines.length) * LINE_HEIGHT + 24
    const rows = visitors.map(visitor => {
      const values = [visitor.name, visitor.idNumber, visitor.ticketType, amountText(visitor.cents)]
      const cells = values.map((value, column) => {
        context.font = cellFont(column)
        return wrap(context, value, COLUMN_WIDTHS[column] - CELL_PADDING * 2)
      })
      return { cells, height: Math.max(68, Math.max(...cells.map(lines => lines.length)) * LINE_HEIGHT + 32) }
    })
    context.font = `24px ${FONT}`
    const phoneLines = wrap(context, `联系电话：${phone}`, 720)
    const footerHeight = Math.max(80, phoneLines.length * LINE_HEIGHT + 32)
    const headerHeight = 60
    const tableBottom = tableTop + headerHeight + rows.reduce((sum, row) => sum + row.height, 0)
    const height = tableBottom + 24 + footerHeight + MARGIN
    // 常规订单用 1.5 倍像素。极长文本整体等比缩放，保留全部行并避免移动端画布超限。
    const scale = Math.min(1.5, 16000 / height, Math.sqrt(12_000_000 / (WIDTH * height)))
    canvas.width = Math.floor(WIDTH * scale)
    canvas.height = Math.floor(height * scale)
    context.scale(scale, scale)
    context.fillStyle = '#ffffff'
    context.fillRect(0, 0, WIDTH, height)
    context.textBaseline = 'top'
    context.fillStyle = '#0f172a'
    context.font = `600 34px ${FONT}`
    context.fillText('门票身份信息', MARGIN, MARGIN)
    context.font = `24px ${FONT}`
    context.fillStyle = '#475569'
    let metadataY = MARGIN + 64
    for (const line of [...orderLines, ...dateLines]) {
      context.fillText(line, MARGIN, metadataY)
      metadataY += LINE_HEIGHT
    }

    context.fillStyle = '#eff6ff'
    context.fillRect(MARGIN, tableTop, tableWidth, headerHeight)
    const columnStarts = COLUMN_WIDTHS.map((_, column) => MARGIN + COLUMN_WIDTHS.slice(0, column).reduce((sum, width) => sum + width, 0))
    context.font = `600 24px ${FONT}`
    context.fillStyle = '#334155'
    ;['姓名', '身份证', '票种', '金额（元）'].forEach((label, column) => {
      context.textAlign = column === 3 ? 'right' : 'left'
      const x = columnStarts[column] + (column === 3 ? COLUMN_WIDTHS[column] - CELL_PADDING : CELL_PADDING)
      context.fillText(label, x, tableTop + 17)
    })

    let y = tableTop + headerHeight
    context.strokeStyle = '#dbe3ed'
    context.lineWidth = 1
    rows.forEach((row, index) => {
      if (index % 2 === 1) {
        context.fillStyle = '#f8fafc'
        context.fillRect(MARGIN, y, tableWidth, row.height)
      }
      row.cells.forEach((lines, column) => {
        context.font = cellFont(column)
        context.fillStyle = '#0f172a'
        context.textAlign = column === 3 ? 'right' : 'left'
        const x = columnStarts[column] + (column === 3 ? COLUMN_WIDTHS[column] - CELL_PADDING : CELL_PADDING)
        const lineTop = y + (row.height - lines.length * LINE_HEIGHT) / 2
        lines.forEach((line, lineIndex) => context.fillText(line, x, lineTop + lineIndex * LINE_HEIGHT))
      })
      y += row.height
      context.beginPath()
      context.moveTo(MARGIN, y)
      context.lineTo(WIDTH - MARGIN, y)
      context.stroke()
    })
    context.strokeRect(MARGIN, tableTop, tableWidth, tableBottom - tableTop)
    columnStarts.slice(1).forEach(x => {
      context.beginPath(); context.moveTo(x, tableTop); context.lineTo(x, tableBottom); context.stroke()
    })

    const footerTop = tableBottom + 24
    context.fillStyle = '#f1f5f9'
    context.fillRect(MARGIN, footerTop, tableWidth, footerHeight)
    context.font = `24px ${FONT}`
    context.fillStyle = '#334155'
    context.textAlign = 'left'
    phoneLines.forEach((line, index) => context.fillText(line, MARGIN + CELL_PADDING, footerTop + 16 + index * LINE_HEIGHT))
    context.font = `600 26px ${FONT}`
    context.fillStyle = '#0f172a'
    context.textAlign = 'right'
    context.fillText(`合计：${amountText(total)} 元`, WIDTH - MARGIN - CELL_PADDING, footerTop + 24)
    checkAborted(signal)
    const blob = await encodePng(canvas, signal)
    checkAborted(signal)
    return blob
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error
    throw new Error(IMAGE_FAILURE)
  } finally {
    // 编码后释放长画布像素，完整身份信息仅留在用户请求保存的图片 Blob 中。
    canvas.width = 1
    canvas.height = 1
  }
}

function filenamePart(value: string): string {
  return value.replace(/[^\p{L}\p{N}_-]/gu, '_').slice(0, 80) || '订单'
}

export async function saveTicketTableImage(input: TicketTableImageInput, options: ImageOptions = {}): Promise<void> {
  const blob = await generateTicketTableImage(input, options)
  checkAborted(options.signal)
  let url: string | null = null
  const anchor = document.createElement('a')
  let clicked = false
  try {
    url = URL.createObjectURL(blob)
    anchor.href = url
    anchor.download = `门票身份信息_${filenamePart(input.orderNo)}_${filenamePart(input.visitDate)}.png`
    anchor.style.display = 'none'
    document.body.appendChild(anchor)
    checkAborted(options.signal)
    anchor.click()
    clicked = true
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error
    throw new Error('图片下载失败，请检查浏览器下载权限后重试')
  } finally {
    anchor.remove()
    // 让浏览器先接收下载，再撤销 URL；失败或取消则立即释放。
    if (url) {
      const downloadUrl = url
      if (clicked) window.setTimeout(() => URL.revokeObjectURL(downloadUrl), 1000)
      else URL.revokeObjectURL(downloadUrl)
    }
  }
}
