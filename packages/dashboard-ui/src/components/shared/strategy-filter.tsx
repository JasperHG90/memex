import { cn } from '@/lib/utils'

const STRATEGIES = [
  { key: 'semantic', label: 'Semantic' },
  { key: 'keyword', label: 'Keyword' },
  { key: 'graph', label: 'Graph' },
  { key: 'temporal', label: 'Temporal' },
  { key: 'mental_model', label: 'Mental Model' },
] as const

interface StrategyFilterProps {
  selected: string[]
  onChange: (strategies: string[]) => void
  className?: string
}

export function StrategyFilter({ selected, onChange, className }: StrategyFilterProps) {
  function toggle(key: string) {
    if (selected.includes(key)) {
      onChange(selected.filter((s) => s !== key))
    } else {
      onChange([...selected, key])
    }
  }

  return (
    <div className={cn('flex flex-wrap gap-2', className)}>
      {STRATEGIES.map(({ key, label }) => {
        const isActive = selected.includes(key)
        return (
          <button
            key={key}
            type="button"
            onClick={() => toggle(key)}
            className={cn(
              'rounded-md px-3 py-1.5 text-xs font-medium transition-colors cursor-pointer',
              isActive
                ? 'bg-primary text-white'
                : 'bg-card text-muted-foreground hover:bg-hover hover:text-foreground border border-border',
            )}
          >
            {label}
          </button>
        )
      })}
    </div>
  )
}
