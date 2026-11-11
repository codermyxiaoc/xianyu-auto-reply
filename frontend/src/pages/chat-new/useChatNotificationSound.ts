import { useEffect, useMemo, useSyncExternalStore } from 'react'
import { useAuthStore } from '@/store/authStore'
import type { ChatMessage } from '@/api/chatNew'

const PREFERENCE_PREFIX = 'chat-notification-sound:'
type AudioWindow = Window & { webkitAudioContext?: typeof AudioContext }
const audioConstructor = () => typeof window === 'undefined' ? undefined :
  window.AudioContext || (window as AudioWindow).webkitAudioContext
function savedPreference(scope: string) {
  try { return !!scope && localStorage.getItem(PREFERENCE_PREFIX + scope) === 'true' } catch { return false }
}

/** 同一登录用户共用音频上下文。设置页的手势解锁可以用于随后打开的聊天页。 */
class SoundController {
  state: { enabled: boolean; ready: boolean; supported: boolean; error: string }
  listeners = new Set<() => void>()
  context: AudioContext | null = null
  generation = 0
  unlocking: Promise<void> | null = null
  cancelResume: (() => void) | null = null
  tones = new Set<{ oscillator: OscillatorNode; gain: GainNode }>()
  seen = new Map<string, number>()
  lastSound = -Infinity

  constructor(readonly scope: string) {
    this.state = { enabled: savedPreference(scope), ready: false, supported: !!audioConstructor(), error: '' }
  }
  snapshot = () => this.state
  subscribe = (listener: () => void) => {
    this.listeners.add(listener)
    return () => { this.listeners.delete(listener) }
  }
  update(change: Partial<typeof this.state>) {
    this.state = { ...this.state, ...change }
    this.listeners.forEach(listener => listener())
  }
  close() {
    this.generation++
    this.unlocking = null
    this.cancelResume?.()
    this.cancelResume = null
    for (const { oscillator, gain } of this.tones) {
      oscillator.onended = null
      try { oscillator.stop(); oscillator.disconnect(); gain.disconnect() } catch { /* 已结束。 */ }
    }
    this.tones.clear()
    const context = this.context
    this.context = null
    if (context) {
      context.onstatechange = null
      try { void context.close().catch(() => {}) } catch { /* 浏览器关闭失败无需重试。 */ }
    }
    this.update({ ready: false })
  }
  play = () => {
    const context = this.context
    const now = Date.now()
    if (activeController !== this || !this.state.enabled || !context || context.state !== 'running' || now - this.lastSound < 1000) return
    try {
      this.lastSound = now
      for (const [offset, frequency] of [[0, 622.25], [0.13, 830.61]]) {
        const oscillator = context.createOscillator()
        const gain = context.createGain()
        const tone = { oscillator, gain }
        this.tones.add(tone)
        const start = context.currentTime + 0.01 + offset
        oscillator.type = 'sine'
        oscillator.frequency.setValueAtTime(frequency, start)
        gain.gain.setValueAtTime(0.0001, start)
        gain.gain.linearRampToValueAtTime(0.035, start + 0.012)
        gain.gain.exponentialRampToValueAtTime(0.0001, start + 0.09)
        oscillator.connect(gain)
        gain.connect(context.destination)
        oscillator.onended = () => {
          oscillator.disconnect(); gain.disconnect(); this.tones.delete(tone)
        }
        oscillator.start(start)
        oscillator.stop(start + 0.1)
      }
    } catch {
      this.close()
      this.update({ error: '声音播放失败，请在个人设置中重新启用声音' })
    }
  }
  unlock = (): Promise<void> => {
    if (!this.scope || activeController !== this || !this.state.enabled) return Promise.resolve()
    if (this.unlocking) return this.unlocking
    const generation = this.generation
    const current = () => activeController === this && generation === this.generation && this.state.enabled
    const work = (async () => {
      try {
        const Constructor = audioConstructor()
        if (!Constructor) throw new Error('unsupported')
        let context = this.context
        if (!context || context.state === 'closed') {
          context = new Constructor()
          this.context = context
          const created = context
          created.onstatechange = () => {
            if (current() && this.context === created) this.update({ ready: created.state === 'running',
              error: created.state === 'running' ? '' : '浏览器已暂停声音，请点击重新启用' })
          }
        }
        if (context.state !== 'running') {
          await new Promise<void>((resolve, reject) => {
            let settled = false
            const finish = (failure?: unknown) => {
              if (settled) return
              settled = true
              window.clearTimeout(timer)
              this.cancelResume = null
              if (failure) reject(failure); else resolve()
            }
            const timer = window.setTimeout(() => finish(new Error('Audio resume timed out')), 3000)
            this.cancelResume = () => finish()
            try { void context.resume().then(() => finish(), finish) } catch (failure) { finish(failure) }
          })
        }
        if (current()) this.update({ ready: context.state === 'running', error: context.state === 'running'
          ? '' : '浏览器尚未允许播放，请再次点击启用声音' })
      } catch {
        if (current()) this.update({ ready: false, error: audioConstructor()
          ? '无法启用声音，请再次点击或检查浏览器声音权限' : '当前浏览器不支持声音通知' })
      }
    })()
    this.unlocking = work
    void work.then(() => { if (this.unlocking === work) this.unlocking = null })
    return work
  }
  enable = async () => {
    if (!this.scope || activeController !== this) return
    this.update({ enabled: true })
    try { localStorage.setItem(PREFERENCE_PREFIX + this.scope, 'true') } catch { /* 本次会话仍可用。 */ }
    const generation = this.generation
    await this.unlock()
    if (generation === this.generation) this.play()
  }
  disable = () => {
    if (activeController !== this) return
    this.update({ enabled: false, error: '' })
    try { localStorage.setItem(PREFERENCE_PREFIX + this.scope, 'false') } catch { /* 关闭不依赖存储。 */ }
    this.close()
  }
  toggle = async () => { if (this.state.enabled) this.disable(); else await this.enable() }
  notify = (accountId: string, cid: string, message: ChatMessage) => {
    if (activeController !== this || !this.scope || message.isSelf !== false ||
      message.type === 'system' || message.type === 'card' || !message.senderId ||
      message.senderId.split('@')[0] === '2223046249624') return
    const now = Date.now()
    for (const [key, expires] of this.seen) if (expires <= now) this.seen.delete(key)
    const identity = message.messageId || message.clientMessageId
    const key = JSON.stringify([accountId, cid, identity || [message.senderId, message.time,
      message.type, message.text, message.images, message.itemCard?.itemId]])
    if (this.seen.has(key)) return
    this.seen.set(key, now + (identity ? 300_000 : 30_000))
    if (this.seen.size > 2000) this.seen.delete(this.seen.keys().next().value!)
    this.play()
  }
}

let activeController: SoundController | null = null
const controllers = new Map<string, SoundController>()
function activate(controller: SoundController) {
  if (activeController === controller) return
  activeController?.close()
  activeController?.seen.clear()
  controllers.clear()
  controllers.set(controller.scope, controller)
  activeController = controller
}

// 路由卸载不关闭：个人设置先启用再导航到聊天，必须保留同一个已解锁的上下文。
// 退出或切换登录用户则立即关闭，清理所有消息指纹，不把声音授权串给另一个用户。
useAuthStore.subscribe((state, previous) => {
  if (state.user?.user_id !== previous.user?.user_id) {
    activeController?.close()
    activeController?.seen.clear()
    activeController = null
    controllers.clear()
  }
})
if (typeof window !== 'undefined') {
  const onGesture = (event: Event) => {
    if (event.target instanceof Element && event.target.closest('[data-chat-sound-control]')) return
    if (activeController?.state.enabled && !activeController.state.ready) void activeController.unlock()
  }
  window.addEventListener('pointerdown', onGesture, true)
  window.addEventListener('keydown', onGesture, true)
  window.addEventListener('storage', event => {
    const controller = activeController
    if (!controller || (event.key !== null && event.key !== PREFERENCE_PREFIX + controller.scope)) return
    const enabled = savedPreference(controller.scope)
    controller.update({ enabled, error: '' })
    if (!enabled) controller.close()
  })
  window.addEventListener('pagehide', () => activeController?.close())
}

/** 仅实时新消息调用 notify；历史加载、搜索和发送回显不播放。 */
export function useChatNotificationSound(userId: string | number | null | undefined) {
  const scope = userId == null ? '' : String(userId)
  const controller = useMemo(() => {
    const existing = controllers.get(scope)
    if (existing) return existing
    const created = new SoundController(scope)
    controllers.set(scope, created)
    return created
  }, [scope])
  useEffect(() => { activate(controller) }, [controller])
  const state = useSyncExternalStore(controller.subscribe, controller.snapshot, controller.snapshot)
  return { ...state, enable: controller.enable, disable: controller.disable,
    toggle: controller.toggle, notify: controller.notify }
}
