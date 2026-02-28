import { useMemo } from 'react';
import { useNavigate } from 'react-router-dom';
import { FileText, Brain, Users, Eye, ArrowRight, Clock } from 'lucide-react';
import { PageHeader } from '@/components/layout/page-header';
import { Card, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Skeleton } from '@/components/ui/skeleton';
import { ScrollArea } from '@/components/ui/scroll-area';
import { useSystemStats } from '@/api/hooks/use-stats';
import { useNotes } from '@/api/hooks/use-notes';
import { useEntities } from '@/api/hooks/use-entities';
import { useReflectionQueue } from '@/api/hooks/use-reflections';
import { useVaultStore } from '@/stores/vault-store';
import { VaultBadge } from '@/components/shared/vault-badge';
import { formatLabel } from '@/components/shared/format-label';
import type { NoteDTO, EntityDTO, ReflectionQueueDTO } from '@/api/generated';

interface PipelineStage {
  icon: React.ComponentType<{ className?: string }>;
  label: string;
  count: number | null;
  color: string;
}

export default function KnowledgeFlow() {
  const navigate = useNavigate();
  const writerVaultId = useVaultStore((s) => s.writerVaultId);
  const attachedVaults = useVaultStore((s) => s.attachedVaults);
  const vaultIds = useMemo(() => {
    const ids = new Set<string>();
    if (writerVaultId) ids.add(writerVaultId);
    for (const v of attachedVaults) ids.add(v.id);
    return [...ids];
  }, [writerVaultId, attachedVaults]);

  const stats = useSystemStats(vaultIds);
  const { data: recentNotes, isLoading: notesLoading } = useNotes({ limit: 10, sort: '-created_at', vaultIds });
  const { data: topEntities, isLoading: entitiesLoading } = useEntities({ limit: 10, sort: '-mentions', vaultIds });
  const { data: reflectionQueue, isLoading: reflectionsLoading } = useReflectionQueue(vaultIds);

  const stages: PipelineStage[] = useMemo(() => [
    { icon: FileText, label: 'Notes', count: stats.data?.notes ?? null, color: '#3B82F6' },
    { icon: Brain, label: 'Memories', count: stats.data?.memories ?? null, color: '#A855F7' },
    { icon: Users, label: 'Entities', count: stats.data?.entities ?? null, color: '#22C55E' },
    { icon: Eye, label: 'Reflections', count: stats.data?.reflection_queue ?? null, color: '#F59E0B' },
  ], [stats.data]);

  return (
    <div className="space-y-6">
      <PageHeader
        title="Knowledge Flow"
        description="Live ingestion activity across the knowledge pipeline"
      />

      {/* Pipeline summary bar */}
      <Card className="bg-card border-border">
        <CardContent className="py-4 px-6">
          <div className="flex items-center justify-center gap-2 flex-wrap">
            {stages.map((stage, i) => (
              <div key={stage.label} className="flex items-center gap-2">
                <div className="flex items-center gap-2">
                  <div
                    className="flex h-8 w-8 items-center justify-center rounded-lg"
                    style={{ backgroundColor: stage.color + '15' }}
                  >
                    <div style={{ color: stage.color }}>
                      <stage.icon className="h-4 w-4" />
                    </div>
                  </div>
                  <span className="text-sm font-medium text-foreground">{stage.label}</span>
                  {stats.isLoading ? (
                    <Skeleton className="h-5 w-8" />
                  ) : stage.count != null ? (
                    <Badge variant="secondary" className="text-xs font-bold" style={{ color: stage.color }}>
                      {stage.count.toLocaleString()}
                    </Badge>
                  ) : (
                    <Badge variant="secondary" className="text-xs">-</Badge>
                  )}
                </div>
                {i < stages.length - 1 && (
                  <ArrowRight className="h-4 w-4 text-muted-foreground mx-1 shrink-0" />
                )}
              </div>
            ))}
          </div>
        </CardContent>
      </Card>

      {/* 4-column activity feed */}
      <div className="grid gap-4 grid-cols-1 md:grid-cols-2 xl:grid-cols-4">
        {/* Recent Notes */}
        <ActivityColumn
          title="Recent Notes"
          icon={FileText}
          color="#3B82F6"
          count={recentNotes?.length}
          isLoading={notesLoading}
        >
          {recentNotes?.map((note) => (
            <NoteItem
              key={note.id}
              note={note}
              onClick={() => navigate(`/lineage?id=${note.id}&type=note`)}
            />
          ))}
          {!notesLoading && (!recentNotes || recentNotes.length === 0) && (
            <p className="text-xs text-muted-foreground text-center py-4">No notes yet</p>
          )}
        </ActivityColumn>

        {/* Recent Memories — show stats count, no individual items fetched here */}
        <ActivityColumn
          title="Memories"
          icon={Brain}
          color="#A855F7"
          count={stats.data?.memories}
          isLoading={stats.isLoading}
        >
          <div className="flex flex-col items-center gap-3 py-6">
            <div className="text-3xl font-bold" style={{ color: '#A855F7' }}>
              {stats.isLoading ? (
                <Skeleton className="h-9 w-16" />
              ) : (
                (stats.data?.memories ?? 0).toLocaleString()
              )}
            </div>
            <p className="text-xs text-muted-foreground text-center">
              Atomic facts extracted via LLM
            </p>
            <button
              className="text-xs text-primary hover:underline"
              onClick={() => navigate('/search')}
            >
              Search memories
            </button>
          </div>
        </ActivityColumn>

        {/* Active Entities */}
        <ActivityColumn
          title="Active Entities"
          icon={Users}
          color="#22C55E"
          count={topEntities?.length}
          isLoading={entitiesLoading}
        >
          {topEntities?.map((entity) => (
            <EntityItem
              key={entity.id}
              entity={entity}
              onClick={() => navigate(`/entity?entity=${entity.id}`)}
            />
          ))}
          {!entitiesLoading && (!topEntities || topEntities.length === 0) && (
            <p className="text-xs text-muted-foreground text-center py-4">No entities yet</p>
          )}
        </ActivityColumn>

        {/* Reflection Queue */}
        <ActivityColumn
          title="Reflection Queue"
          icon={Eye}
          color="#F59E0B"
          count={reflectionQueue?.length}
          isLoading={reflectionsLoading}
        >
          {reflectionQueue?.slice(0, 10).map((item) => (
            <ReflectionItem key={item.entity_id} item={item} />
          ))}
          {!reflectionsLoading && (!reflectionQueue || reflectionQueue.length === 0) && (
            <p className="text-xs text-muted-foreground text-center py-4">Queue empty</p>
          )}
        </ActivityColumn>
      </div>
    </div>
  );
}

// --- Column wrapper ---

function ActivityColumn({
  title,
  icon: Icon,
  color,
  count,
  isLoading,
  children,
}: {
  title: string;
  icon: React.ComponentType<{ className?: string }>;
  color: string;
  count?: number | null;
  isLoading: boolean;
  children: React.ReactNode;
}) {
  return (
    <Card className="bg-card border-border">
      <CardContent className="p-0">
        <div className="flex items-center gap-2 px-4 py-3 border-b border-border">
          <div style={{ color }}><Icon className="h-4 w-4" /></div>
          <span className="text-sm font-semibold text-foreground">{title}</span>
          {count != null && (
            <Badge variant="secondary" className="ml-auto text-[10px]">
              {count}
            </Badge>
          )}
        </div>
        <ScrollArea className="h-[320px]">
          <div className="p-3 space-y-2">
            {isLoading ? (
              <>
                <Skeleton className="h-12 w-full" />
                <Skeleton className="h-12 w-full" />
                <Skeleton className="h-12 w-full" />
              </>
            ) : (
              children
            )}
          </div>
        </ScrollArea>
      </CardContent>
    </Card>
  );
}

// --- Item components ---

function NoteItem({ note, onClick }: { note: NoteDTO; onClick: () => void }) {
  const title = note.title ?? note.name ?? 'Untitled';
  return (
    <button
      className="w-full text-left rounded-md border border-border p-2 hover:bg-hover transition-colors"
      onClick={onClick}
    >
      <p className="text-sm font-medium text-foreground truncate">{title}</p>
      <div className="flex items-center gap-1 mt-1">
        <Clock className="h-3 w-3 text-muted-foreground" />
        <span className="text-[10px] text-muted-foreground">
          {new Date(note.created_at).toLocaleDateString()}
        </span>
        <VaultBadge vaultId={note.vault_id} />
      </div>
    </button>
  );
}

function EntityItem({ entity, onClick }: { entity: EntityDTO; onClick: () => void }) {
  return (
    <button
      className="w-full text-left rounded-md border border-border p-2 hover:bg-hover transition-colors"
      onClick={onClick}
    >
      <div className="flex items-center justify-between">
        <p className="text-sm font-medium text-foreground truncate">{entity.name}</p>
        <Badge variant="secondary" className="text-[10px] shrink-0">
          {entity.mention_count} mentions
        </Badge>
      </div>
      <div className="flex items-center gap-1">
        {entity.entity_type && (
          <span className="text-[10px] text-muted-foreground">{formatLabel(entity.entity_type)}</span>
        )}
        <VaultBadge vaultId={entity.vault_id} />
      </div>
    </button>
  );
}

function ReflectionItem({ item }: { item: ReflectionQueueDTO }) {
  return (
    <div className="rounded-md border border-border p-2">
      <div className="flex items-center justify-between">
        <p className="text-xs text-foreground truncate" title={item.entity_id}>
          {item.entity_id.slice(0, 8)}...
        </p>
        {item.priority_score > 0 && (
          <Badge variant="secondary" className="text-[10px]">
            priority: {item.priority_score.toFixed(1)}
          </Badge>
        )}
      </div>
    </div>
  );
}
