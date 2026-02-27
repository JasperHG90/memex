import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from '@/components/ui/dialog'
import { ScrollArea } from '@/components/ui/scroll-area'

interface DetailModalProps {
  title: string
  data: Record<string, unknown>
  open: boolean
  onOpenChange: (open: boolean) => void
}

function formatValue(value: unknown): string {
  if (value === null || value === undefined) return '-'
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  if (Array.isArray(value)) return value.map(String).join(', ')
  if (typeof value === 'object') return JSON.stringify(value, null, 2)
  return String(value)
}

export function DetailModal({ title, data, open, onOpenChange }: DetailModalProps) {
  const entries = Object.entries(data)

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>Properties and metadata</DialogDescription>
        </DialogHeader>
        <ScrollArea className="max-h-[60vh]">
          <table className="w-full text-sm">
            <tbody>
              {entries.map(([key, value]) => (
                <tr key={key} className="border-b border-border">
                  <td className="py-2 pr-4 align-top font-medium text-muted-foreground whitespace-nowrap">
                    {key}
                  </td>
                  <td className="py-2 text-foreground break-all">
                    {typeof value === 'object' && value !== null && !Array.isArray(value) ? (
                      <pre className="text-xs whitespace-pre-wrap font-mono">
                        {formatValue(value)}
                      </pre>
                    ) : (
                      formatValue(value)
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </ScrollArea>
      </DialogContent>
    </Dialog>
  )
}
