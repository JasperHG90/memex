import { useState, useMemo } from 'react'
import ReactMarkdown from 'react-markdown'
import { Brain, FileText, Users, RefreshCw } from 'lucide-react'
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  CartesianGrid,
} from 'recharts'
import { PageHeader } from '@/components/layout/page-header'
import { MetricCard } from '@/components/shared/metric-card'
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from '@/components/ui/dialog'
import { ScrollArea } from '@/components/ui/scroll-area'
import { useNote } from '@/api/hooks/use-notes'
import { Card, CardContent } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { Badge } from '@/components/ui/badge'
import { useSystemStats, useTokenUsage, useMetrics } from '@/api/hooks/use-stats'
import { useNotes } from '@/api/hooks/use-notes'

function parseMetrics(metricsText: string | undefined) {
  if (!metricsText) {
    return { cpuUsage: '-', memoryUsage: '-', requests: '-', status: 'Unknown' }
  }

  let reqTotal = 0
  let memBytes = 0
  let cpuSeconds = 0

  for (const line of metricsText.split('\n')) {
    if (line.startsWith('#')) continue
    const parts = line.split(' ')
    if (parts.length < 2) continue

    if (line.includes('http_requests_total')) {
      reqTotal += parseFloat(parts[parts.length - 1])
    } else if (line.startsWith('process_resident_memory_bytes')) {
      memBytes = parseFloat(parts[parts.length - 1])
    } else if (line.startsWith('process_cpu_seconds_total')) {
      cpuSeconds = parseFloat(parts[parts.length - 1])
    }
  }

  return {
    cpuUsage: `${cpuSeconds.toFixed(2)}s`,
    memoryUsage: `${(memBytes / 1024 / 1024).toFixed(0)} MB`,
    requests: String(Math.floor(reqTotal)),
    status: 'Healthy',
  }
}

interface NoteItem {
  id: string
  title: string
  preview: string
  date: string
  metadata: Record<string, unknown>
}

export default function Overview() {
  const stats = useSystemStats()
  const tokenUsage = useTokenUsage()
  const metrics = useMetrics()
  const recentNotes = useNotes({ limit: 10, sort: '-created_at' })

  const [selectedNoteId, setSelectedNoteId] = useState<string | null>(null)

  const serverStats = useMemo(
    () => parseMetrics(metrics.data as string | undefined),
    [metrics.data],
  )

  const chartData = useMemo(() => {
    if (!tokenUsage.data?.usage) return []
    return tokenUsage.data.usage.map((entry) => ({
      date: entry.date.length > 5 ? entry.date.slice(5) : entry.date,
      tokens: entry.total_tokens,
    }))
  }, [tokenUsage.data])

  const noteItems: NoteItem[] = useMemo(() => {
    if (!recentNotes.data) return []
    return recentNotes.data.map((note) => {
      const meta = (note.doc_metadata ?? {}) as Record<string, unknown>
      const title =
        (meta.title as string) ??
        (meta.name as string) ??
        note.name ??
        note.title ??
        `Note ${String(note.id).slice(0, 8)}`
      const preview =
        (meta.description as string) ??
        (meta.summary as string) ??
        'Click to view details'
      const created = note.created_at
        ? new Date(note.created_at).toLocaleDateString()
        : '-'

      return {
        id: note.id,
        title: String(title),
        preview: String(preview),
        date: created,
        metadata: { id: note.id, vault_id: note.vault_id, ...meta },
      }
    })
  }, [recentNotes.data])

  function handleOpenDetails(item: NoteItem) {
    setSelectedNoteId(item.id)
  }

  return (
    <div>
      <PageHeader title="Overview" />

      {/* Top row: Metric Cards */}
      <div className="mb-6 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {stats.isLoading ? (
          <>
            <MetricCardSkeleton />
            <MetricCardSkeleton />
            <MetricCardSkeleton />
          </>
        ) : (
          <>
            <MetricCard
              icon={Brain}
              label="Total Memories"
              value={stats.data?.memories ?? 0}
            />
            <MetricCard
              icon={FileText}
              label="Total Notes"
              value={stats.data?.reflection_queue ?? 0}
              description="Pending reflections"
            />
            <MetricCard
              icon={Users}
              label="Total Entities"
              value={stats.data?.entities ?? 0}
            />
          </>
        )}
      </div>

      {/* Middle + Bottom: Chart + Health | Recent Memories */}
      <div className="flex flex-wrap gap-4 items-start">
        {/* Left column: Token Usage + Server Health */}
        <div className="flex min-w-[320px] flex-1 flex-col gap-4">
          {/* Token Usage Chart */}
          <Card className="bg-card border-border">
            <CardContent className="p-6">
              <h2 className="mb-4 text-lg font-semibold text-foreground">Token Usage</h2>
              {tokenUsage.isLoading ? (
                <Skeleton className="h-[300px] w-full" />
              ) : chartData.length === 0 ? (
                <div className="flex h-[300px] items-center justify-center text-muted-foreground text-sm">
                  No token usage data available
                </div>
              ) : (
                <ResponsiveContainer width="100%" height={300}>
                  <BarChart data={chartData}>
                    <CartesianGrid
                      strokeDasharray="3 3"
                      stroke="#262626"
                      vertical={false}
                    />
                    <XAxis
                      dataKey="date"
                      tick={{ fill: '#A1A1AA', fontSize: 12 }}
                      axisLine={{ stroke: '#262626' }}
                      tickLine={false}
                    />
                    <YAxis
                      tick={{ fill: '#A1A1AA', fontSize: 12 }}
                      axisLine={{ stroke: '#262626' }}
                      tickLine={false}
                    />
                    <Tooltip
                      contentStyle={{
                        backgroundColor: '#1A1A1A',
                        border: '1px solid #262626',
                        borderRadius: '8px',
                        color: '#EDEDED',
                      }}
                      labelStyle={{ color: '#A1A1AA' }}
                      cursor={{ fill: 'rgba(255,255,255,0.05)' }}
                    />
                    <Bar dataKey="tokens" fill="#3B82F6" radius={[4, 4, 0, 0]} />
                  </BarChart>
                </ResponsiveContainer>
              )}
            </CardContent>
          </Card>

          {/* Server Health Panel */}
          <Card className="bg-card border-border">
            <CardContent className="p-6">
              <div className="mb-4 flex items-center justify-between">
                <h2 className="text-lg font-semibold text-foreground">Server Health</h2>
                <Button
                  variant="ghost"
                  size="icon"
                  onClick={() => metrics.refetch()}
                >
                  <RefreshCw className="h-4 w-4" />
                </Button>
              </div>
              {metrics.isLoading ? (
                <div className="grid grid-cols-2 gap-4">
                  {Array.from({ length: 4 }).map((_, i) => (
                    <div key={i}>
                      <Skeleton className="mb-1 h-3 w-16" />
                      <Skeleton className="h-5 w-20" />
                    </div>
                  ))}
                </div>
              ) : (
                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <p className="text-xs text-muted-foreground">Status</p>
                    <Badge
                      variant={serverStats.status === 'Healthy' ? 'default' : 'destructive'}
                      className="mt-1"
                    >
                      {serverStats.status}
                    </Badge>
                  </div>
                  <div>
                    <p className="text-xs text-muted-foreground">Memory</p>
                    <p className="mt-1 font-semibold text-foreground">
                      {serverStats.memoryUsage}
                    </p>
                  </div>
                  <div>
                    <p className="text-xs text-muted-foreground">CPU Time</p>
                    <p className="mt-1 font-semibold text-foreground">
                      {serverStats.cpuUsage}
                    </p>
                  </div>
                  <div>
                    <p className="text-xs text-muted-foreground">Requests</p>
                    <p className="mt-1 font-semibold text-foreground">
                      {serverStats.requests}
                    </p>
                  </div>
                </div>
              )}
            </CardContent>
          </Card>
        </div>

        {/* Right column: Recent Memories Feed */}
        <div className="min-w-[280px] flex-1">
          <Card className="bg-card border-border">
            <CardContent className="p-6">
              <h2 className="mb-4 text-lg font-semibold text-foreground">
                Recent Memories
              </h2>
              {recentNotes.isLoading ? (
                <div className="space-y-3">
                  {Array.from({ length: 5 }).map((_, i) => (
                    <div key={i} className="flex items-center gap-3">
                      <Skeleton className="h-5 w-5 rounded" />
                      <div className="flex-1">
                        <Skeleton className="mb-1 h-4 w-3/4" />
                        <Skeleton className="h-3 w-1/2" />
                      </div>
                      <Skeleton className="h-3 w-16" />
                    </div>
                  ))}
                </div>
              ) : noteItems.length === 0 ? (
                <p className="py-8 text-center text-sm text-muted-foreground">
                  No recent memories found
                </p>
              ) : (
                <div className="divide-y divide-border">
                  {noteItems.map((item) => (
                    <button
                      key={item.id}
                      type="button"
                      onClick={() => handleOpenDetails(item)}
                      className="flex w-full items-center gap-3 py-3 text-left transition-colors hover:bg-hover rounded-md px-2 cursor-pointer"
                    >
                      <FileText className="h-4 w-4 shrink-0 text-muted-foreground" />
                      <div className="min-w-0 flex-1">
                        <p className="truncate text-sm font-medium text-foreground">
                          {item.title}
                        </p>
                        <p className="truncate text-xs text-muted-foreground">
                          {item.preview}
                        </p>
                      </div>
                      <span className="shrink-0 text-xs text-muted-foreground">
                        {item.date}
                      </span>
                    </button>
                  ))}
                </div>
              )}
            </CardContent>
          </Card>
        </div>
      </div>

      {/* Note Content Modal */}
      <NoteContentModal
        noteId={selectedNoteId}
        onClose={() => setSelectedNoteId(null)}
      />
    </div>
  )
}

function NoteContentModal({ noteId, onClose }: { noteId: string | null; onClose: () => void }) {
  const { data: note, isLoading } = useNote(noteId ?? undefined)

  const title = note?.title ?? note?.name ?? 'Note Details'

  return (
    <Dialog open={!!noteId} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-w-3xl max-h-[85vh]">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>Full note content</DialogDescription>
        </DialogHeader>
        <ScrollArea className="max-h-[65vh]">
          {isLoading ? (
            <div className="space-y-2 py-4">
              <Skeleton className="h-4 w-full" />
              <Skeleton className="h-4 w-4/5" />
              <Skeleton className="h-4 w-3/5" />
            </div>
          ) : note?.original_text ? (
            <div className="prose prose-invert prose-sm max-w-none pr-4">
              <ReactMarkdown>{note.original_text}</ReactMarkdown>
            </div>
          ) : (
            <p className="text-sm text-muted-foreground py-4">No content available.</p>
          )}
        </ScrollArea>
      </DialogContent>
    </Dialog>
  )
}

function MetricCardSkeleton() {
  return (
    <Card className="bg-card border-border">
      <CardContent className="p-6">
        <div className="flex items-center gap-3">
          <Skeleton className="h-5 w-5 rounded" />
          <Skeleton className="h-4 w-24" />
        </div>
        <Skeleton className="mt-2 h-7 w-16" />
      </CardContent>
    </Card>
  )
}
