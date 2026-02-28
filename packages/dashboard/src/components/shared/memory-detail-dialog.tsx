import { Fragment, useState } from 'react';
import { ThumbsUp, ThumbsDown, Trash2 } from 'lucide-react';
import { toast } from 'sonner';

import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Separator } from '@/components/ui/separator';
import { ScrollArea } from '@/components/ui/scroll-area';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from '@/components/ui/dialog';
import { TypeBadge } from '@/components/shared/type-badge';
import { VaultBadge } from '@/components/shared/vault-badge';
import { useAdjustBelief, useDeleteMemory } from '@/api/hooks/use-memories';
import type { MemoryUnitDTO } from '@/api/generated';

function cleanFactType(raw: string): string {
  if (raw.includes('.')) {
    return raw.split('.').pop()?.toLowerCase() ?? raw;
  }
  return raw.toLowerCase();
}

function getConfidenceInfo(alpha: number | null | undefined, beta: number | null | undefined) {
  if (alpha == null || beta == null || alpha + beta === 0) return null;
  const mean = alpha / (alpha + beta);
  if (mean > 0.7) return { mean, color: 'bg-emerald-500', textColor: 'text-emerald-500', label: 'High' };
  if (mean > 0.4) return { mean, color: 'bg-amber-500', textColor: 'text-amber-500', label: 'Medium' };
  return { mean, color: 'bg-red-500', textColor: 'text-red-500', label: 'Low' };
}

interface MemoryDetailDialogProps {
  unit: MemoryUnitDTO | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function MemoryDetailDialog({ unit, open, onOpenChange }: MemoryDetailDialogProps) {
  if (!unit) return null;

  const factType = cleanFactType(unit.fact_type);
  const metadata = unit.metadata ?? {};
  const metaEntries = Object.entries(metadata);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl max-h-[85vh]">
        <DialogHeader>
          <DialogTitle>Memory Details</DialogTitle>
          <DialogDescription>Full memory unit with metadata</DialogDescription>
        </DialogHeader>

        <ScrollArea className="max-h-[70vh]">
          {/* Type badges */}
          <div className="flex flex-wrap items-center gap-2">
            <TypeBadge type="memory_unit" />
            <TypeBadge type={factType} />
            {unit.status && (
              <Badge variant="outline" className="text-xs">
                {unit.status}
              </Badge>
            )}
          </div>

          {/* Full text */}
          <p className="mt-4 text-sm leading-relaxed text-foreground whitespace-pre-wrap">
            {unit.text}
          </p>

          <Separator className="my-4" />

          {/* Metadata section */}
          <div className="space-y-3">
            <p className="text-xs font-medium text-muted-foreground uppercase tracking-wide">
              Metadata
            </p>
            <div className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
              {unit.note_id && (
                <>
                  <span className="text-muted-foreground">Source Note</span>
                  <span className="text-foreground truncate" title={unit.note_id}>
                    {unit.note_id}
                  </span>
                </>
              )}
              {unit.source_note_ids && unit.source_note_ids.length > 0 && (
                <>
                  <span className="text-muted-foreground">Source Notes</span>
                  <span className="text-foreground break-all">
                    {unit.source_note_ids.join(', ')}
                  </span>
                </>
              )}
              {unit.score != null && (
                <>
                  <span className="text-muted-foreground">Score</span>
                  <span className="text-foreground">{unit.score.toFixed(4)}</span>
                </>
              )}
              {(() => {
                const conf = getConfidenceInfo(unit.confidence_alpha, unit.confidence_beta);
                if (!conf) return null;
                return (
                  <>
                    <span className="text-muted-foreground">Confidence</span>
                    <div className="flex items-center gap-2">
                      <div className="h-2 w-24 overflow-hidden rounded-full bg-muted">
                        <div className={`h-full rounded-full ${conf.color} transition-all`} style={{ width: `${conf.mean * 100}%` }} />
                      </div>
                      <span className={`text-xs ${conf.textColor}`}>{conf.label} ({(conf.mean * 100).toFixed(0)}%)</span>
                    </div>
                  </>
                );
              })()}
              {unit.mentioned_at && (
                <>
                  <span className="text-muted-foreground">Mentioned At</span>
                  <span className="text-foreground">
                    {new Date(unit.mentioned_at).toLocaleString()}
                  </span>
                </>
              )}
              {unit.occurred_start && (
                <>
                  <span className="text-muted-foreground">Occurred Start</span>
                  <span className="text-foreground">
                    {new Date(unit.occurred_start).toLocaleString()}
                  </span>
                </>
              )}
              {unit.occurred_end && (
                <>
                  <span className="text-muted-foreground">Occurred End</span>
                  <span className="text-foreground">
                    {new Date(unit.occurred_end).toLocaleString()}
                  </span>
                </>
              )}
              {unit.vault_id && (
                <>
                  <span className="text-muted-foreground">Vault</span>
                  <VaultBadge vaultId={unit.vault_id} />
                </>
              )}
            </div>

            {/* Extra metadata from the metadata field */}
            {metaEntries.length > 0 && (
              <>
                <Separator />
                <div className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
                  {metaEntries.map(([key, value]) => (
                    <Fragment key={key}>
                      <span className="text-muted-foreground">{key}</span>
                      <span className="text-foreground break-all">
                        {typeof value === 'object' ? JSON.stringify(value) : String(value ?? '-')}
                      </span>
                    </Fragment>
                  ))}
                </div>
              </>
            )}
          </div>

          <Separator className="my-4" />

          {/* Belief adjustment and delete actions — keyed on unit.id to auto-reset state */}
          <MemoryActions key={unit.id} unitId={unit.id} factType={factType} onDeleted={() => onOpenChange(false)} />
        </ScrollArea>
      </DialogContent>
    </Dialog>
  );
}

function MemoryActions({ unitId, factType, onDeleted }: { unitId: string; factType: string; onDeleted: () => void }) {
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);
  const adjustBelief = useAdjustBelief();
  const deleteMemory = useDeleteMemory();
  const isOpinion = factType === 'opinion';

  return (
    <div className="flex items-center gap-2">
      {isOpinion && (
        <>
          <span className="text-xs text-muted-foreground">Belief:</span>
          <Button
            variant="outline"
            size="sm"
            disabled={adjustBelief.isPending}
            onClick={async () => {
              try {
                await adjustBelief.mutateAsync({ unitId, adjustment: 'confirm' });
                toast.success('Memory confirmed');
              } catch (err) {
                toast.error(`Failed: ${err instanceof Error ? err.message : String(err)}`);
              }
            }}
          >
            <ThumbsUp className="mr-1 h-3 w-3" />
            Confirm
          </Button>
          <Button
            variant="outline"
            size="sm"
            disabled={adjustBelief.isPending}
            onClick={async () => {
              try {
                await adjustBelief.mutateAsync({ unitId, adjustment: 'contradict' });
                toast.success('Memory contradicted');
              } catch (err) {
                toast.error(`Failed: ${err instanceof Error ? err.message : String(err)}`);
              }
            }}
          >
            <ThumbsDown className="mr-1 h-3 w-3" />
            Contradict
          </Button>
        </>
      )}
      {!showDeleteConfirm ? (
        <Button
          variant="outline"
          size="sm"
          className="ml-auto text-destructive hover:bg-destructive/10"
          onClick={() => setShowDeleteConfirm(true)}
        >
          <Trash2 className="mr-1 h-3 w-3" />
          Delete
        </Button>
      ) : (
        <div className="ml-auto flex items-center gap-2">
          <span className="text-xs text-destructive">Are you sure?</span>
          <Button
            variant="destructive"
            size="sm"
            disabled={deleteMemory.isPending}
            onClick={async () => {
              try {
                await deleteMemory.mutateAsync(unitId);
                toast.success('Memory deleted');
                onDeleted();
              } catch (err) {
                toast.error(`Failed: ${err instanceof Error ? err.message : String(err)}`);
              }
            }}
          >
            {deleteMemory.isPending ? 'Deleting...' : 'Yes, delete'}
          </Button>
          <Button variant="outline" size="sm" onClick={() => setShowDeleteConfirm(false)}>
            Cancel
          </Button>
        </div>
      )}
    </div>
  );
}
