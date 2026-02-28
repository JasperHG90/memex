import { useMemo, useState } from 'react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  useEntityMentions,
  useEntityCooccurrences,
  useEntity,
  useEntities,
} from '@/api/hooks/use-entities';
import { X, ExternalLink, Loader2, RefreshCw } from 'lucide-react';
import { VaultBadge } from '@/components/shared/vault-badge';
import { formatLabel } from '@/components/shared/format-label';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from '@/components/ui/dialog';
import type { EntityMention, CooccurrenceRecord } from '@/api/generated';
import { useTriggerReflection } from '@/api/hooks/use-reflections';
import { toast } from 'sonner';

const FACT_TYPE_COLORS: Record<string, string> = {
  world: '#3B82F6',
  experience: '#A855F7',
  opinion: '#F59E0B',
  observation: '#22C55E',
};

interface EntitySidePanelProps {
  entityId: string;
  onClose: () => void;
  onOpenDetail: (entityId: string) => void;
}

export function EntitySidePanel({ entityId, onClose, onOpenDetail }: EntitySidePanelProps) {
  const { data: entity } = useEntity(entityId);
  const { data: mentions, isLoading: mentionsLoading } = useEntityMentions(entityId);
  const { data: cooccurrences, isLoading: cooccurrencesLoading } = useEntityCooccurrences(entityId);
  const { data: allEntities } = useEntities({ limit: 100, sort: '-mentions' });
  const [selectedMention, setSelectedMention] = useState<EntityMention | null>(null);
  const triggerReflection = useTriggerReflection();

  // Build a name lookup for co-occurring entity IDs
  const entityNameMap = useMemo(() => {
    const map = new Map<string, string>();
    if (allEntities) {
      for (const e of allEntities) {
        map.set(e.id, e.name);
      }
    }
    return map;
  }, [allEntities]);

  return (
    <div className="flex h-full w-72 flex-col border-l border-border bg-card min-h-0">
      <div className="flex items-center justify-between border-b border-border px-4 py-3 shrink-0">
        <div className="flex items-center gap-2 min-w-0">
          <h3 className="truncate text-sm font-semibold text-foreground">
            {entity?.name ?? 'Loading...'}
          </h3>
        </div>
        <button onClick={onClose} className="text-muted-foreground hover:text-foreground">
          <X className="h-4 w-4" />
        </button>
      </div>

      <div className="flex-1 min-h-0 overflow-y-auto">
        <div className="flex flex-col gap-4 p-4">
          {entity && (
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant="outline" className="text-xs">
                {entity.mention_count} mentions
              </Badge>
              <VaultBadge vaultId={entity.vault_id} />
              <Button
                variant="outline"
                size="sm"
                className="text-xs"
                onClick={() => onOpenDetail(entityId)}
              >
                <ExternalLink className="mr-1 h-3 w-3" />
                Details
              </Button>
              <Button
                variant="outline"
                size="sm"
                className="text-xs"
                disabled={triggerReflection.isPending}
                onClick={async () => {
                  try {
                    await triggerReflection.mutateAsync(entityId);
                    toast.success('Reflection triggered successfully');
                  } catch (err) {
                    toast.error(`Failed to trigger reflection: ${err instanceof Error ? err.message : String(err)}`);
                  }
                }}
              >
                <RefreshCw className={`mr-1 h-3 w-3 ${triggerReflection.isPending ? 'animate-spin' : ''}`} />
                Reflect
              </Button>
            </div>
          )}

          {/* Mentions Timeline */}
          <div>
            <h4 className="mb-2 text-xs font-medium text-muted-foreground">Mentions</h4>
            {mentionsLoading ? (
              <div className="flex items-center gap-2 py-4 text-xs text-muted-foreground">
                <Loader2 className="h-3 w-3 animate-spin" />
                Loading mentions...
              </div>
            ) : mentions && mentions.length > 0 ? (
              <MentionsTimeline mentions={mentions} onMentionClick={setSelectedMention} />
            ) : (
              <p className="text-xs text-muted-foreground">No mentions found.</p>
            )}
          </div>

          {/* Co-occurrences */}
          <div>
            <h4 className="mb-2 text-xs font-medium text-muted-foreground">Co-occurs with</h4>
            {cooccurrencesLoading ? (
              <div className="flex items-center gap-2 py-4 text-xs text-muted-foreground">
                <Loader2 className="h-3 w-3 animate-spin" />
                Loading co-occurrences...
              </div>
            ) : cooccurrences && cooccurrences.length > 0 ? (
              <div className="flex flex-wrap gap-1.5">
                {cooccurrences.map((c: CooccurrenceRecord) => {
                  const otherId =
                    c.entity_id_1 === entityId ? c.entity_id_2 : c.entity_id_1;
                  const otherName = entityNameMap.get(otherId) ?? otherId.slice(0, 8) + '...';
                  return (
                    <Badge key={otherId} variant="secondary" className="text-xs cursor-pointer hover:bg-hover" onClick={() => onOpenDetail(otherId)}>
                      {otherName} ({c.cooccurrence_count})
                    </Badge>
                  );
                })}
              </div>
            ) : (
              <p className="text-xs text-muted-foreground">No co-occurrences found.</p>
            )}
          </div>
        </div>
      </div>

      {/* Mention detail dialog */}
      <Dialog open={!!selectedMention} onOpenChange={(open) => !open && setSelectedMention(null)}>
        <DialogContent className="max-w-lg">
          <DialogHeader>
            <DialogTitle>Memory Detail</DialogTitle>
            <DialogDescription>
              {selectedMention?.document?.name ?? selectedMention?.document?.title ?? 'Memory unit'}
            </DialogDescription>
          </DialogHeader>
          {selectedMention && (
            <div className="space-y-3">
              <div className="flex flex-wrap items-center gap-2">
                <Badge
                  variant="secondary"
                  className="text-xs"
                  style={{
                    backgroundColor: (FACT_TYPE_COLORS[selectedMention.unit.fact_type] ?? '#71717A') + '22',
                    color: FACT_TYPE_COLORS[selectedMention.unit.fact_type] ?? '#71717A',
                  }}
                >
                  {formatLabel(selectedMention.unit.fact_type)}
                </Badge>
                {selectedMention.unit.note_id && (
                  <a
                    href={`/lineage?id=${selectedMention.unit.note_id}&type=note`}
                    className="inline-flex items-center gap-1 text-xs text-primary hover:underline"
                  >
                    <ExternalLink className="h-3 w-3" />
                    View note
                  </a>
                )}
                <VaultBadge vaultId={selectedMention.unit.vault_id} />
              </div>
              <p className="text-sm text-foreground leading-relaxed">
                {selectedMention.unit.text}
              </p>
              <div className="flex flex-col gap-1 text-xs text-muted-foreground">
                {selectedMention.unit.mentioned_at && (
                  <span>Mentioned: {new Date(selectedMention.unit.mentioned_at).toLocaleString()}</span>
                )}
                {selectedMention.unit.occurred_start && (
                  <span>
                    Occurred: {new Date(selectedMention.unit.occurred_start).toLocaleDateString()}
                    {selectedMention.unit.occurred_end &&
                      ` - ${new Date(selectedMention.unit.occurred_end).toLocaleDateString()}`}
                  </span>
                )}
              </div>
            </div>
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}

function MentionsTimeline({
  mentions,
  onMentionClick,
}: {
  mentions: EntityMention[];
  onMentionClick: (m: EntityMention) => void;
}) {
  // Group by date
  const grouped = useMemo(() => {
    const groups = new Map<string, EntityMention[]>();
    for (const m of mentions) {
      const date = m.unit.mentioned_at
        ? new Date(m.unit.mentioned_at).toLocaleDateString()
        : 'Unknown date';
      const existing = groups.get(date) ?? [];
      existing.push(m);
      groups.set(date, existing);
    }
    return [...groups.entries()].sort(([a], [b]) => {
      if (a === 'Unknown date') return 1;
      if (b === 'Unknown date') return -1;
      return new Date(b).getTime() - new Date(a).getTime();
    });
  }, [mentions]);

  return (
    <div className="flex flex-col gap-3">
      {grouped.map(([date, items]) => (
        <div key={date}>
          <p className="text-[10px] font-semibold text-muted-foreground mb-1.5 uppercase tracking-wider">
            {date}
          </p>
          <div className="relative ml-2 border-l border-border pl-3 flex flex-col gap-2">
            {items.map((m) => {
              const color = FACT_TYPE_COLORS[m.unit.fact_type] ?? '#71717A';
              return (
                <div key={m.unit.id} className="relative">
                  {/* Timeline dot */}
                  <div
                    className="absolute -left-[17px] top-2 h-2.5 w-2.5 rounded-full border-2 border-card"
                    style={{ backgroundColor: color }}
                  />
                  <MentionCard mention={m} onClick={() => onMentionClick(m)} />
                </div>
              );
            })}
          </div>
        </div>
      ))}
    </div>
  );
}

function MentionCard({ mention, onClick }: { mention: EntityMention; onClick: () => void }) {
  const factType = mention.unit.fact_type;
  const color = FACT_TYPE_COLORS[factType] ?? '#71717A';

  return (
    <button type="button" onClick={onClick} className="w-full text-left rounded-md border border-border p-2 hover:bg-hover transition-colors cursor-pointer">
      <div className="mb-1 flex items-center gap-1.5">
        <Badge
          variant="secondary"
          className="text-[10px]"
          style={{ backgroundColor: color + '22', color }}
        >
          {formatLabel(factType)}
        </Badge>
        <span className="text-[10px] text-muted-foreground">
          {mention.document?.name ?? mention.document?.title ?? 'Untitled'}
        </span>
      </div>
      <p className="line-clamp-2 text-xs text-foreground">{mention.unit.text}</p>
    </button>
  );
}
