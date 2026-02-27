import type { ReactNode } from 'react'
import { Button } from '@/components/ui/button'
import { StickyNote } from 'lucide-react'
import { useUIStore } from '@/stores/ui-store'

interface PageHeaderProps {
  title: string
  description?: string
  actions?: ReactNode
}

export function PageHeader({ title, description, actions }: PageHeaderProps) {
  const toggleQuickNote = useUIStore((s) => s.toggleQuickNote)

  return (
    <div className="mb-6 flex items-start justify-between">
      <div>
        <h1 className="text-2xl font-bold text-foreground">{title}</h1>
        {description && (
          <p className="mt-1 text-sm text-muted-foreground">{description}</p>
        )}
      </div>
      <div className="flex items-center gap-2">
        {actions}
        <Button variant="outline" size="sm" onClick={toggleQuickNote}>
          <StickyNote className="h-4 w-4" />
          Quick Note
        </Button>
      </div>
    </div>
  )
}
