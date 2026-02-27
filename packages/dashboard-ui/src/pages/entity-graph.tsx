import { useState, useCallback } from 'react';
import { useEntities, useBulkCooccurrences } from '@/api/hooks/use-entities';
import { GraphCanvas } from './entity-graph/graph-canvas';
import { FilterPanel, type GraphFilters } from './entity-graph/filter-panel';

const DEFAULT_FILTERS: GraphFilters = {
  minConnectionStrength: 1,
  minImportance: 1,
  recency: 'all',
};
import { EntitySidePanel } from './entity-graph/entity-side-panel';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from '@/components/ui/dialog';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { ScrollArea } from '@/components/ui/scroll-area';
import { Maximize2, Minimize2, Loader2, Share2 } from 'lucide-react';
import { useEntity } from '@/api/hooks/use-entities';

export default function EntityGraph() {

  const [filters, setFilters] = useState<GraphFilters>(DEFAULT_FILTERS);
  const [selectedEntityId, setSelectedEntityId] = useState<string | null>(null);
  const [detailEntityId, setDetailEntityId] = useState<string | null>(null);
  const [isFullscreen, setIsFullscreen] = useState(false);

  // Fetch entities sorted by mention count (top entities)
  const { data: entities, isLoading: entitiesLoading } = useEntities({
    limit: 100,
    sort: '-mentions',
  });

  // Fetch co-occurrences for all loaded entities
  const entityIds = entities?.map((e) => e.id) ?? [];
  const { data: cooccurrences, isLoading: cooccurrencesLoading } =
    useBulkCooccurrences(entityIds);

  const isLoading = entitiesLoading || cooccurrencesLoading;

  const handleNodeSelect = useCallback((entityId: string) => {
    setSelectedEntityId(entityId);
  }, []);

  const handleNodeDoubleClick = useCallback((entityId: string) => {
    setDetailEntityId(entityId);
  }, []);

  const handlePaneClick = useCallback(() => {
    setSelectedEntityId(null);
  }, []);

  const handleCloseSidePanel = useCallback(() => {
    setSelectedEntityId(null);
  }, []);

  return (
    <div className={`flex flex-col ${isFullscreen ? 'fixed inset-0 z-50 bg-background' : 'h-full'}`}>
      {/* Header */}
      <div className="flex items-center justify-between border-b border-border px-6 py-4">
        <div className="flex items-center gap-3">
          <Share2 className="h-5 w-5 text-primary" />
          <h1 className="text-xl font-semibold text-foreground">Entity Graph</h1>
          {entities && (
            <Badge variant="secondary" className="text-xs">
              {entities.length} entities
            </Badge>
          )}
        </div>
        <Button
          variant="ghost"
          size="sm"
          onClick={() => setIsFullscreen(!isFullscreen)}
        >
          {isFullscreen ? (
            <Minimize2 className="h-4 w-4" />
          ) : (
            <Maximize2 className="h-4 w-4" />
          )}
        </Button>
      </div>

      {/* Body */}
      <div className="relative flex flex-1 overflow-hidden">
        {/* Graph Canvas Area */}
        <div className="relative flex-1" style={{ backgroundColor: '#0D0D0D' }}>
          {isLoading ? (
            <div className="flex h-full items-center justify-center">
              <div className="flex flex-col items-center gap-3">
                <Loader2 className="h-8 w-8 animate-spin text-primary" />
                <p className="text-sm text-muted-foreground">Loading entity graph...</p>
              </div>
            </div>
          ) : entities && entities.length > 0 ? (
            <>
              <FilterPanel filters={filters} onFiltersChange={setFilters} />
              <GraphCanvas
                entities={entities}
                cooccurrences={cooccurrences ?? []}
                filters={filters}
                selectedEntityId={selectedEntityId}
                onNodeSelect={handleNodeSelect}
                onNodeDoubleClick={handleNodeDoubleClick}
                onPaneClick={handlePaneClick}
              />
            </>
          ) : (
            <div className="flex h-full items-center justify-center">
              <div className="flex flex-col items-center gap-2 text-center">
                <Share2 className="h-12 w-12 text-muted-foreground/40" />
                <p className="text-sm text-muted-foreground">No entities found.</p>
                <p className="text-xs text-muted-foreground/60">
                  Ingest documents to populate the entity graph.
                </p>
              </div>
            </div>
          )}
        </div>

        {/* Side Panel */}
        {selectedEntityId && (
          <EntitySidePanel
            entityId={selectedEntityId}
            onClose={handleCloseSidePanel}
            onOpenDetail={setDetailEntityId}
          />
        )}
      </div>

      {/* Detail Modal */}
      <EntityDetailModal
        entityId={detailEntityId}
        onClose={() => setDetailEntityId(null)}
      />
    </div>
  );
}

function EntityDetailModal({
  entityId,
  onClose,
}: {
  entityId: string | null;
  onClose: () => void;
}) {
  const { data: entity, isLoading } = useEntity(entityId ?? undefined);

  return (
    <Dialog open={!!entityId} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Entity Details</DialogTitle>
        </DialogHeader>
        {isLoading ? (
          <div className="flex items-center justify-center py-8">
            <Loader2 className="h-6 w-6 animate-spin text-primary" />
          </div>
        ) : entity ? (
          <ScrollArea className="max-h-80">
            <div className="flex flex-col gap-3">
              <div className="flex items-center gap-2">
                <Badge variant="default">{entity.name}</Badge>
                <Badge variant="secondary">{entity.mention_count} mentions</Badge>
              </div>
              <table className="w-full text-sm">
                <tbody>
                  {Object.entries(entity).map(([key, value]) => (
                    <tr key={key} className="border-b border-border">
                      <td className="py-1.5 pr-3 text-xs font-medium text-muted-foreground">
                        {key}
                      </td>
                      <td className="py-1.5 text-xs text-foreground">{String(value)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </ScrollArea>
        ) : (
          <p className="text-sm text-muted-foreground">Entity not found.</p>
        )}
        <DialogFooter showCloseButton />
      </DialogContent>
    </Dialog>
  );
}
