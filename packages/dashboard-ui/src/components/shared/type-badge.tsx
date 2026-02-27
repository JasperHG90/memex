import { cn } from '@/lib/utils'

const TYPE_COLORS: Record<string, string> = {
  // Entity types
  Person: 'bg-blue-500/20 text-blue-400',
  Organization: 'bg-purple-500/20 text-purple-400',
  Location: 'bg-green-500/20 text-green-400',
  Concept: 'bg-amber-500/20 text-amber-400',
  Event: 'bg-red-500/20 text-red-400',
  Technology: 'bg-cyan-500/20 text-cyan-400',
  // Lineage/memory entity types
  note: 'bg-blue-500/20 text-blue-400',
  memory_unit: 'bg-green-500/20 text-green-400',
  observation: 'bg-amber-500/20 text-amber-400',
  mental_model: 'bg-purple-500/20 text-purple-400',
  entity: 'bg-cyan-500/20 text-cyan-400',
  asset: 'bg-violet-500/20 text-violet-400',
  // Fact types (memory search)
  world: 'bg-blue-500/20 text-blue-400',
  experience: 'bg-purple-500/20 text-purple-400',
  opinion: 'bg-amber-500/20 text-amber-400',
}

const DEFAULT_COLOR = 'bg-zinc-500/20 text-zinc-400'

interface TypeBadgeProps {
  type: string
  className?: string
}

export function TypeBadge({ type, className }: TypeBadgeProps) {
  const colorClass = TYPE_COLORS[type] ?? DEFAULT_COLOR

  return (
    <span
      className={cn(
        'inline-flex items-center rounded-md px-2 py-0.5 text-xs font-medium',
        colorClass,
        className,
      )}
    >
      {type}
    </span>
  )
}
