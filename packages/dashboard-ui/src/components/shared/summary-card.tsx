import ReactMarkdown from 'react-markdown'
import { Card, CardContent } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { Sparkles } from 'lucide-react'

interface SummaryCardProps {
  summary: string | undefined
  isLoading: boolean
  onCitationClick?: (index: number) => void
}

/**
 * Renders summary text with clickable citation markers like [1], [2], etc.
 */
function renderSummaryWithCitations(
  text: string,
  onCitationClick?: (index: number) => void,
) {
  const parts = text.split(/(\[\d+\])/g)

  return parts.map((part, i) => {
    const match = part.match(/^\[(\d+)\]$/)
    if (match) {
      const index = parseInt(match[1], 10)
      return (
        <button
          key={i}
          type="button"
          onClick={() => onCitationClick?.(index)}
          className="mx-0.5 inline-flex items-center justify-center rounded bg-primary/20 px-1.5 py-0.5 text-xs font-semibold text-primary hover:bg-primary/30 transition-colors cursor-pointer"
        >
          {part}
        </button>
      )
    }
    return <span key={i} className="[&>p]:inline [&>p]:m-0"><ReactMarkdown>{part}</ReactMarkdown></span>
  })
}

export function SummaryCard({ summary, isLoading, onCitationClick }: SummaryCardProps) {
  if (isLoading) {
    return (
      <Card className="bg-card border-border">
        <CardContent className="p-6">
          <div className="flex items-center gap-2 mb-3">
            <Sparkles className="h-4 w-4 text-primary animate-pulse" />
            <span className="text-sm font-medium text-muted-foreground">
              Generating summary...
            </span>
          </div>
          <div className="space-y-2">
            <Skeleton className="h-4 w-full" />
            <Skeleton className="h-4 w-4/5" />
            <Skeleton className="h-4 w-3/5" />
          </div>
        </CardContent>
      </Card>
    )
  }

  if (!summary) return null

  return (
    <Card className="bg-card border-border">
      <CardContent className="p-6">
        <div className="flex items-center gap-2 mb-3">
          <Sparkles className="h-4 w-4 text-primary" />
          <span className="text-sm font-medium text-foreground">AI Summary</span>
        </div>
        <div className="text-sm leading-relaxed text-foreground/90">
          {renderSummaryWithCitations(summary, onCitationClick)}
        </div>
      </CardContent>
    </Card>
  )
}
