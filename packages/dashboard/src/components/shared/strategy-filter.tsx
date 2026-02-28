import { cn } from '@/lib/utils'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'

const STRATEGIES = [
  { key: 'semantic', label: 'Semantic' },
  { key: 'keyword', label: 'Keyword' },
  { key: 'graph', label: 'Graph' },
  { key: 'temporal', label: 'Temporal' },
  { key: 'mental_model', label: 'Mental Model' },
] as const

const STRATEGY_DESCRIPTIONS: Record<string, string> = {
  semantic: 'Find results by meaning similarity using vector embeddings',
  keyword: 'Match exact words and phrases using full-text search',
  graph: 'Discover connections through the entity knowledge graph',
  temporal: 'Find results by time proximity and recency',
  mental_model: 'Search synthesized observations and mental models',
}

interface StrategyFilterProps {
  selected: string[]
  onChange: (strategies: string[]) => void
  available?: string[]
  className?: string
}

export function StrategyFilter({ selected, onChange, available, className }: StrategyFilterProps) {
  function toggle(key: string) {
    if (selected.includes(key)) {
      onChange(selected.filter((s) => s !== key))
    } else {
      onChange([...selected, key])
    }
  }

  return (
    <div className={cn('flex flex-wrap gap-2', className)}>
      {STRATEGIES.filter(({ key }) => !available || available.includes(key)).map(({ key, label }) => {
        const isActive = selected.includes(key)
        return (
          <Tooltip key={key}>
            <TooltipTrigger asChild>
              <button
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
            </TooltipTrigger>
            <TooltipContent side="bottom" className="max-w-xs">
              <p className="text-xs">{STRATEGY_DESCRIPTIONS[key] ?? key}</p>
            </TooltipContent>
          </Tooltip>
        )
      })}
    </div>
  )
}
