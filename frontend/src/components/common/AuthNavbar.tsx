/** 公开页面顶栏：保留品牌入口、登录和主题切换。 */
import { Link, useLocation } from 'react-router-dom'
import { useState, useEffect } from 'react'
import { MessageSquare, Sun, Moon, LogIn } from 'lucide-react'
import { getDefaultLoginBrandingSettings, LOGIN_BRANDING_UPDATED_EVENT } from '@/api/settings'
import { getLoginBrandingSettings } from '@/api/auth'
import type { LoginBrandingSettings } from '@/types'
import { initializeThemeMode, toggleThemeMode } from '@/utils/theme'

interface AuthNavbarProps {
  systemName?: string
}

const DEFAULT_SYSTEM_NAME = getDefaultLoginBrandingSettings()['login.system_name']

export function usePublicSystemName(systemName?: string): string {
  const [resolvedSystemName, setResolvedSystemName] = useState(() => {
    if (typeof systemName === 'string' && systemName.trim()) {
      return systemName.trim()
    }
    return DEFAULT_SYSTEM_NAME
  })

  useEffect(() => {
    if (typeof systemName === 'string' && systemName.trim()) {
      setResolvedSystemName(systemName.trim())
      return
    }

    let cancelled = false

    const loadSystemName = async () => {
      try {
        const brandingSettings = await getLoginBrandingSettings()
        if (!cancelled) {
          setResolvedSystemName(brandingSettings['login.system_name'])
        }
      } catch {
        if (!cancelled) {
          setResolvedSystemName(DEFAULT_SYSTEM_NAME)
        }
      }
    }

    const handleBrandingUpdated = (event: Event) => {
      const customEvent = event as CustomEvent<LoginBrandingSettings>
      const nextSystemName = customEvent.detail?.['login.system_name']
      if (typeof nextSystemName === 'string' && nextSystemName.trim()) {
        setResolvedSystemName(nextSystemName.trim())
        return
      }
      setResolvedSystemName(DEFAULT_SYSTEM_NAME)
    }

    loadSystemName()
    window.addEventListener(LOGIN_BRANDING_UPDATED_EVENT, handleBrandingUpdated as EventListener)

    return () => {
      cancelled = true
      window.removeEventListener(LOGIN_BRANDING_UPDATED_EVENT, handleBrandingUpdated as EventListener)
    }
  }, [systemName])

  return resolvedSystemName
}

export function PublicPageFooter({ systemName }: AuthNavbarProps) {
  const resolvedSystemName = usePublicSystemName(systemName)

  return (
    <p className="text-center mt-6 text-slate-400 dark:text-slate-500 text-xs">
      &copy; {new Date().getFullYear()} {resolvedSystemName}
    </p>
  )
}

export function AuthNavbar({ systemName }: AuthNavbarProps) {
  const location = useLocation()
  const resolvedSystemName = usePublicSystemName(systemName)
  // 主题切换在所有公开页面共用。
  const [isDark, setIsDark] = useState(false)
  useEffect(() => {
    setIsDark(initializeThemeMode() === 'dark')
  }, [])
  const toggleTheme = () => {
    setIsDark(toggleThemeMode() === 'dark')
  }

  return (
    <nav aria-label="公共导航" className="fixed top-0 left-0 right-0 z-50 bg-white/95 dark:bg-slate-900/95 backdrop-blur-sm border-b border-slate-200 dark:border-slate-700">
      <div className="max-w-7xl mx-auto px-4 sm:px-6">
        <div className="flex items-center justify-between h-14">
          {/* Logo */}
          <Link to="/" aria-label={`${resolvedSystemName}首页`} className="flex items-center gap-2 flex-shrink-0">
            <div className="w-8 h-8 rounded-lg bg-[rgb(var(--color-primary))] flex items-center justify-center">
              <MessageSquare className="w-4 h-4 text-white" />
            </div>
            <span className="text-sm font-bold text-slate-900 dark:text-white hidden sm:inline">
              {resolvedSystemName}
            </span>
          </Link>

          {/* 登录和主题切换在桌面及手机端均直接显示。 */}
          <div className="flex items-center gap-1">
            <Link to="/login" aria-current={location.pathname === '/login' ? 'page' : undefined} className="btn-ios-primary btn-sm">
              <LogIn className="h-4 w-4" />登录
            </Link>
            <button
              type="button"
              onClick={toggleTheme}
              className="p-2 rounded-md text-slate-600 dark:text-slate-300
                         hover:bg-slate-100 dark:hover:bg-slate-800 transition-colors"
              title={isDark ? '切换到亮色模式' : '切换到暗色模式'}
            >
              {isDark ? <Sun className="w-4 h-4" /> : <Moon className="w-4 h-4" />}
            </button>
          </div>
        </div>
      </div>
    </nav>
  )
}
