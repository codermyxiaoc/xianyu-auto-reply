import { forwardRef, useEffect, useId, useImperativeHandle, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import type { ChangeEvent, CSSProperties, KeyboardEvent, SelectHTMLAttributes } from 'react'
import { Check, ChevronDown } from 'lucide-react'
import { cn } from '@/utils/cn'
import './NativeSelect.css'

export interface NativeSelectProps extends SelectHTMLAttributes<HTMLSelectElement> { placeholder?: string }
interface Choice { index: number; value: string; label: string; disabled: boolean; selected: boolean; group: string; hidden: boolean }

/** Retain native form/change semantics, while drawing the dropdown independently of the operating system. */
export const NativeSelect = forwardRef<HTMLSelectElement, NativeSelectProps>(function NativeSelect({
  children, className, style, placeholder = '请选择', disabled, onChange, onFocus, onInvalid,
  tabIndex, autoFocus, ...props
}, forwardedRef) {
  const nativeRef = useRef<HTMLSelectElement>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  const wrapperRef = useRef<HTMLSpanElement>(null)
  const menuId = useId()
  const errorId = `${menuId}-error`
  const [choices, setChoices] = useState<Choice[]>([])
  const [isOpen, setIsOpen] = useState(false)
  const [activeIndex, setActiveIndex] = useState(-1)
  const [isDisabled, setIsDisabled] = useState(Boolean(disabled))
  const [label, setLabel] = useState('')
  const [invalidMessage, setInvalidMessage] = useState('')
  const [position, setPosition] = useState<CSSProperties>({ visibility: 'hidden' })
  const search = useRef({ value: '', time: 0 })
  const signatureRef = useRef('')
  useImperativeHandle(forwardedRef, () => nativeRef.current!, [])

  function syncNative() {
    const native = nativeRef.current
    if (!native) return
    const next = [...native.options].map((option, index): Choice => ({
      index, value: option.value, label: option.label, selected: option.selected, hidden: option.hidden,
      disabled: option.disabled || (option.parentElement instanceof HTMLOptGroupElement && option.parentElement.disabled),
      group: option.parentElement instanceof HTMLOptGroupElement ? option.parentElement.label : '',
    }))
    const signature = JSON.stringify(next)
    if (signature !== signatureRef.current) { signatureRef.current = signature; setChoices(next) }
    const unavailable = native.matches(':disabled')
    setIsDisabled(previous => previous === unavailable ? previous : unavailable)
    if (unavailable) setIsOpen(false)
    if (native.validity.valid) setInvalidMessage('')
    const labels = [...(native.labels || [])]
    const sibling = wrapperRef.current?.previousElementSibling
    if (labels.length === 0 && sibling?.tagName === 'LABEL') labels.push(sibling as HTMLLabelElement)
    const text = labels.map(element => {
      const copy = element.cloneNode(true) as HTMLElement
      copy.querySelectorAll('[data-native-select], select, button, input').forEach(control => control.remove())
      return copy.textContent?.trim() || ''
    }).filter(Boolean).join(' ')
    setLabel(previous => previous === text ? previous : text)
  }
  // The native DOM resolves fragments, conditional options, maps and optgroups consistently.
  useLayoutEffect(syncNative)
  useEffect(() => {
    const native = nativeRef.current
    if (!native) return
    const fieldsets: Element[] = []
    for (let parent = native.parentElement; parent; parent = parent.parentElement) {
      if (parent instanceof HTMLFieldSetElement) fieldsets.push(parent)
    }
    const observer = new MutationObserver(syncNative)
    fieldsets.forEach(fieldset => observer.observe(fieldset, { attributes: true, attributeFilter: ['disabled'] }))
    const reset = () => { setIsOpen(false); window.setTimeout(syncNative, 0) }
    native.form?.addEventListener('reset', reset)
    if (autoFocus) triggerRef.current?.focus()
    return () => { observer.disconnect(); native.form?.removeEventListener('reset', reset) }
  }, [autoFocus])

  function open(last = false) {
    if (isDisabled || nativeRef.current?.matches(':disabled')) return
    const enabled = choices.filter(choice => !choice.disabled && !choice.hidden)
    const selected = enabled.find(choice => choice.selected)
    setActiveIndex((selected || (last ? enabled[enabled.length - 1] : enabled[0]))?.index ?? -1)
    setPosition({ visibility: 'hidden' }); setIsOpen(true)
  }
  function choose(index: number) {
    const native = nativeRef.current
    const choice = choices[index]
    if (!native || native.matches(':disabled') || !choice || choice.disabled || choice.hidden) return
    if (native.multiple) native.options[index].selected = !native.options[index].selected
    else native.selectedIndex = index
    native.dispatchEvent(new Event('change', { bubbles: true }))
    if (!native.multiple) setIsOpen(false)
    triggerRef.current?.focus({ preventScroll: true })
  }
  function move(direction: number) {
    const enabled = choices.filter(choice => !choice.disabled && !choice.hidden)
    if (!enabled.length) return
    const current = enabled.findIndex(choice => choice.index === activeIndex)
    setActiveIndex(enabled[Math.max(0, Math.min(enabled.length - 1, current + direction))].index)
  }
  function keyDown(event: KeyboardEvent<HTMLButtonElement>) {
    if (isDisabled) return
    if (event.key === 'Escape') {
      if (isOpen) { event.preventDefault(); event.stopPropagation(); setIsOpen(false) }
      return
    }
    if (event.key === 'Tab') { setIsOpen(false); return }
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault(); event.stopPropagation()
      if (!isOpen) open(event.key === 'ArrowUp'); else move(event.key === 'ArrowDown' ? 1 : -1)
      return
    }
    if (event.key === 'Home' || event.key === 'End') {
      event.preventDefault(); event.stopPropagation()
      if (!isOpen) open()
      const enabled = choices.filter(choice => !choice.disabled && !choice.hidden)
      setActiveIndex((event.key === 'Home' ? enabled[0] : enabled[enabled.length - 1])?.index ?? -1)
      return
    }
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault(); event.stopPropagation()
      if (isOpen) choose(activeIndex); else open()
      return
    }
    if (event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey) {
      event.preventDefault()
      const now = Date.now()
      const prefix = now - search.current.time > 700 ? event.key : search.current.value + event.key
      search.current = { value: prefix.toLocaleLowerCase(), time: now }
      const match = choices.find(choice => !choice.disabled && !choice.hidden && choice.label.toLocaleLowerCase().startsWith(search.current.value))
      if (!isOpen) open()
      if (match) setActiveIndex(match.index)
    }
  }

  useLayoutEffect(() => {
    if (!isOpen) return
    const trigger = triggerRef.current, menu = menuRef.current
    if (!trigger || !menu) return
    function place() {
      if (!trigger || !menu) return
      const rect = trigger.getBoundingClientRect()
      const margin = 8, gap = 5
      const width = Math.min(Math.max(rect.width, 140), window.innerWidth - margin * 2)
      const below = Math.max(0, window.innerHeight - rect.bottom - margin - gap)
      const above = Math.max(0, rect.top - margin - gap)
      const desired = Math.min(menu.scrollHeight || 280, 300)
      const upwards = below < desired && above > below
      const maxHeight = Math.min(300, upwards ? above : below)
      const height = Math.min(desired, maxHeight)
      const font = window.getComputedStyle(trigger)
      setPosition({ position: 'fixed', zIndex: 10000, width, maxHeight,
        left: Math.max(margin, Math.min(rect.left, window.innerWidth - width - margin)),
        top: upwards ? Math.max(margin, rect.top - height - gap) : rect.bottom + gap,
        fontSize: font.fontSize, fontFamily: font.fontFamily, visibility: 'visible' })
    }
    place()
    const resize = new ResizeObserver(place)
    resize.observe(trigger)
    const scroll = (event: Event) => { if (!(event.target instanceof Node && menu.contains(event.target))) place() }
    const outside = (event: PointerEvent) => {
      if (event.target instanceof Node && !wrapperRef.current?.contains(event.target) && !menu.contains(event.target)) setIsOpen(false)
    }
    window.addEventListener('resize', place)
    window.addEventListener('scroll', scroll, true)
    document.addEventListener('pointerdown', outside, true)
    return () => { resize.disconnect(); window.removeEventListener('resize', place); window.removeEventListener('scroll', scroll, true); document.removeEventListener('pointerdown', outside, true) }
  }, [isOpen, choices, invalidMessage])
  useEffect(() => {
    if (isOpen && activeIndex >= 0) document.getElementById(`${menuId}-${activeIndex}`)?.scrollIntoView({ block: 'nearest' })
  }, [isOpen, activeIndex, menuId])
  function change(event: ChangeEvent<HTMLSelectElement>) { syncNative(); onChange?.(event) }
  const display = choices.filter(choice => choice.selected).map(choice => choice.label).join('、') || placeholder
  const accessibleLabel = props['aria-label'] || label || props.name || placeholder

  return (
    <span ref={wrapperRef} data-native-select="" className="contents">
      <select {...props} ref={nativeRef} disabled={disabled} tabIndex={-1} aria-hidden="true" className="native-select-control" onChange={change}
        onFocus={event => { onFocus?.(event); triggerRef.current?.focus({ preventScroll: true }) }}
        onInvalid={event => { onInvalid?.(event); if (event.defaultPrevented) return; event.preventDefault(); setInvalidMessage(event.currentTarget.validationMessage); triggerRef.current?.focus({ preventScroll: true }); open() }}
      >{children}</select>
      <button ref={triggerRef} type="button" role="combobox" aria-haspopup="listbox"
        aria-label={props['aria-labelledby'] ? undefined : accessibleLabel} aria-labelledby={props['aria-labelledby']}
        aria-describedby={invalidMessage ? `${props['aria-describedby'] || ''} ${errorId}`.trim() : props['aria-describedby']}
        aria-required={props.required || undefined} aria-invalid={invalidMessage ? true : props['aria-invalid']}
        aria-expanded={isOpen} aria-controls={isOpen ? menuId : undefined} aria-activedescendant={isOpen && activeIndex >= 0 ? `${menuId}-${activeIndex}` : undefined}
        disabled={isDisabled} tabIndex={tabIndex} title={props.title} style={style} data-open={isOpen || undefined}
        className={cn('native-select-trigger rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-100 disabled:cursor-not-allowed disabled:bg-slate-100 disabled:text-slate-400 dark:disabled:bg-slate-800', className)}
        onClick={event => { event.stopPropagation(); if (isOpen) setIsOpen(false); else open() }} onKeyDown={keyDown}
        onBlur={event => { if (!(event.relatedTarget instanceof Node && menuRef.current?.contains(event.relatedTarget))) setIsOpen(false) }}>
        <span className="min-w-0 truncate">{display}</span>
        <ChevronDown aria-hidden="true" className={cn('h-4 w-4 shrink-0 text-slate-400 transition-transform duration-150', isOpen && 'rotate-180')} />
      </button>
      {isOpen && createPortal(
        <div ref={menuRef} id={menuId} role="listbox" aria-label={accessibleLabel} aria-multiselectable={props.multiple || undefined} style={position} className="native-select-menu"
          onPointerDown={event => event.preventDefault()} onClick={event => event.stopPropagation()}>
          {invalidMessage && <div id={errorId} role="alert" className="px-3 py-2 text-xs text-red-600 dark:text-red-400">{invalidMessage}</div>}
          {choices.filter(choice => !choice.hidden).length === 0 ? <div className="px-3 py-3 text-center text-slate-400">暂无选项</div> : choices.filter(choice => !choice.hidden).map((choice, index, visible) => (
            <div key={choice.index}>
              {choice.group && choice.group !== visible[index - 1]?.group && <div className="px-3 pb-1 pt-2 text-xs font-medium text-slate-400">{choice.group}</div>}
              <div id={`${menuId}-${choice.index}`} role="option" aria-selected={choice.selected} aria-disabled={choice.disabled || undefined}
                data-selected={choice.selected || undefined} data-active={activeIndex === choice.index || undefined} className="native-select-option"
                onPointerMove={() => { if (!choice.disabled) setActiveIndex(choice.index) }} onClick={event => { event.stopPropagation(); choose(choice.index) }}>
                <span className="min-w-0 break-words">{choice.label}</span>
                {choice.selected ? <Check aria-hidden="true" className="h-4 w-4 shrink-0" /> : <span className="w-4 shrink-0" />}
              </div>
            </div>
          ))}
        </div>, document.body,
      )}
    </span>
  )
})
