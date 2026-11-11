import { Bell, BellOff } from 'lucide-react'
import { useAuthStore } from '@/store/authStore'
import { useChatNotificationSound } from '../chat-new/useChatNotificationSound'

export function ChatSoundSettingsCard() {
  const userId = useAuthStore(state => state.user?.user_id)
  const sound = useChatNotificationSound(userId)
  return (
    <section data-chat-sound-control className="card p-5 space-y-3">
      <h2 className="flex items-center gap-2 text-base font-semibold"><Bell className="h-5 w-5" />聊天消息提示音</h2>
      <p className="text-sm text-slate-500 dark:text-slate-400">开启后，在线聊天中所有已连接账号收到新消息时播放提示音。此偏好保存在当前浏览器，并按登录用户分别记忆。</p>
      <div className="flex flex-wrap items-center gap-3">
        <button type="button" disabled={!sound.supported} aria-pressed={sound.enabled && sound.ready}
          aria-label={sound.enabled && sound.ready ? '关闭消息提示音' : '开启消息提示音'}
          onClick={() => { if (sound.enabled && sound.ready) sound.disable(); else void sound.enable() }}
          className="btn-ios-secondary">
          {sound.enabled && sound.ready ? <Bell className="h-4 w-4" /> : <BellOff className="h-4 w-4" />}
          {!sound.supported ? '浏览器不支持提示音' : sound.enabled ? (sound.ready ? '提示音已开启' : '点击启用声音') : '开启提示音'}
        </button>
        {sound.enabled && !sound.ready && <button type="button" onClick={sound.disable} aria-label="关闭消息提示音" className="text-sm text-slate-500">关闭</button>}
        <span className="text-xs text-slate-500">点击开启会播放一次试听音</span>
      </div>
      {sound.error && <p role="status" className="text-sm text-amber-600">{sound.error}</p>}
    </section>
  )
}
