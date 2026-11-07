import { NativeSelect } from './NativeSelect'
import { cn } from '@/utils/cn'

export interface SelectOption {
  value: string
  label: string
  disabled?: boolean
  key?: string
}

interface SelectProps {
  value: string
  onChange: (value: string) => void
  options: SelectOption[]
  placeholder?: string
  disabled?: boolean
  className?: string
}

export function Select({ value, onChange, options, placeholder = '请选择', disabled = false, className }: SelectProps) {
  return (
    <NativeSelect value={value} onChange={event => onChange(event.target.value)} disabled={disabled}
      placeholder={placeholder} aria-label={placeholder} className={cn('w-full', className)}>
      {!options.some(option => option.value === value) && <option value={value} disabled hidden>{placeholder}</option>}
      {options.map(option => <option key={option.key || option.value} value={option.value} disabled={option.disabled}>{option.label}</option>)}
    </NativeSelect>
  )
}
