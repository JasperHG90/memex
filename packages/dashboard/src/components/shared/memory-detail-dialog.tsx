import { Fragment, useState } from 'react';
import { Trash2 } from 'lucide-react';
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
import { useDeleteMemory } from '@/api/hooks/use-memories';
import type { MemoryUnitDTO } from '@/api/generated';

function cleanFactType(raw: string): string {
  if (raw.includes('.')) {
    return raw.split('.').pop()?.toLowerCase() ?? raw;
  }
  return raw.toLowerCase();
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
          <MemoryActions key={unit.id} unitId={unit.id} onDeleted={() => onOpenChange(false)} />
        </ScrollArea>
      </DialogContent>
    </Dialog>
  );
}

function MemoryActions({ unitId, onDeleted }: { unitId: string; onDeleted: () => void }) {
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);
  const deleteMemory = useDeleteMemory();

  return (
    <div className="flex items-center gap-2">
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
