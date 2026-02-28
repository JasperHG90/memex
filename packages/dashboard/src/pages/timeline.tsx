import { useState, useMemo, useCallback, type KeyboardEvent } from 'react';
import { useNavigate } from 'react-router-dom';
import { Clock, Search, Loader2 } from 'lucide-react';
import { PageHeader } from '@/components/layout/page-header';
import { Card, CardContent } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { EmptyState } from '@/components/shared/empty-state';
import { ResultCardSkeleton } from '@/components/shared/result-card-skeleton';
import { MemoryDetailDialog } from '@/components/shared/memory-detail-dialog';
import { VaultBadge } from '@/components/shared/vault-badge';
import { formatLabel } from '@/components/shared/format-label';
import { useMemorySearch, useMemory } from '@/api/hooks/use-memories';
import { useVaultStore } from '@/stores/vault-store';
import type { MemoryUnitDTO } from '@/api/generated';

const FACT_TYPE_COLORS: Record<string, string> = {
  world: '#3B82F6',
  experience: '#A855F7',
  opinion: '#F59E0B',
  observation: '#22C55E',
};

export default function Timeline() {
  const navigate = useNavigate();
  const allSelectedVaultIds = useVaultStore((s) => s.allSelectedVaultIds);
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<MemoryUnitDTO[]>([]);
  const [hasSearched, setHasSearched] = useState(false);
  const [selectedUnitId, setSelectedUnitId] = useState<string | null>(null);
  const searchMutation = useMemorySearch();
  const { data: selectedUnit } = useMemory(selectedUnitId ?? undefined);

  const executeSearch = useCallback(
    (q: string) => {
      if (!q.trim()) return;
      const vaultIds = allSelectedVaultIds();
      searchMutation.mutate(
        {
          query: q,
          limit: 50,
          offset: 0,
          rerank: true,
          include_vectors: false,
          include_stale: false,
          skip_opinion_formation: true,
          vault_ids: vaultIds.length > 0 ? vaultIds : undefined,
        },
        {
          onSuccess: (data) => {
            setResults(data as unknown as MemoryUnitDTO[]);
            setHasSearched(true);
          },
        },
      );
    },
    [allSelectedVaultIds, searchMutation],
  );

  const handleSearch = useCallback(() => {
    if (!query.trim()) return;
    setResults([]);
    executeSearch(query);
  }, [query, executeSearch]);

  const handleKeyDown = useCallback(
    (e: KeyboardEvent<HTMLInputElement>) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        handleSearch();
      }
    },
    [handleSearch],
  );

  // Group results by date
  const grouped = useMemo(() => {
    const sorted = [...results].sort((a, b) => {
      const dateA = a.mentioned_at ? new Date(a.mentioned_at).getTime() : 0;
      const dateB = b.mentioned_at ? new Date(b.mentioned_at).getTime() : 0;
      return dateB - dateA;
    });

    const groups = new Map<string, MemoryUnitDTO[]>();
    for (const item of sorted) {
      const date = item.mentioned_at
        ? new Date(item.mentioned_at).toLocaleDateString(undefined, {
            weekday: 'long',
            year: 'numeric',
            month: 'long',
            day: 'numeric',
          })
        : 'Unknown Date';
      const existing = groups.get(date) ?? [];
      existing.push(item);
      groups.set(date, existing);
    }
    return [...groups.entries()];
  }, [results]);

  const isLoading = searchMutation.isPending;

  return (
    <div className="space-y-6">
      <PageHeader
        title="Memory Timeline"
        description="Explore memories chronologically"
      />

      {/* Search */}
      <div className="flex gap-2">
        <div className="relative flex-1">
          <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            placeholder="Search memories to view on timeline..."
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={handleKeyDown}
            className="pl-10"
          />
        </div>
        <Button onClick={handleSearch} disabled={isLoading || !query.trim()}>
          {isLoading ? <Loader2 className="h-4 w-4 animate-spin" /> : <Search className="h-4 w-4" />}
          Search
        </Button>
      </div>

      {/* Results */}
      {isLoading ? (
        <div className="space-y-3">
          {Array.from({ length: 3 }).map((_, i) => (
            <ResultCardSkeleton key={i} />
          ))}
        </div>
      ) : !hasSearched ? (
        <EmptyState
          icon={Clock}
          title="Memory Timeline"
          description="Search for memories to view them on a chronological timeline."
          suggestions={[
            { label: 'All memories', onClick: () => { setQuery('*'); executeSearch('*'); } },
            { label: 'Recent events', onClick: () => { setQuery('recent events'); executeSearch('recent events'); } },
            { label: 'Decisions', onClick: () => { setQuery('decisions'); executeSearch('decisions'); } },
          ]}
        />
      ) : grouped.length === 0 ? (
        <EmptyState
          icon={Search}
          title="No results"
          description="No memories found for this query."
        />
      ) : (
        <div className="space-y-8">
          {grouped.map(([date, items]) => (
            <div key={date}>
              <div className="sticky top-0 z-10 mb-3 flex items-center gap-2 bg-background py-2">
                <Clock className="h-4 w-4 text-muted-foreground" />
                <h3 className="text-sm font-semibold text-foreground">{date}</h3>
                <Badge variant="secondary" className="text-[10px]">{items.length}</Badge>
              </div>
              <div className="relative ml-4 border-l-2 border-border pl-6 space-y-3">
                {items.map((item) => {
                  const color = FACT_TYPE_COLORS[item.fact_type] ?? '#71717A';
                  const time = item.mentioned_at
                    ? new Date(item.mentioned_at).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
                    : null;
                  return (
                    <div key={item.id} className="relative">
                      <div
                        className="absolute -left-[31px] top-3 h-3 w-3 rounded-full border-2 border-background"
                        style={{ backgroundColor: color }}
                      />
                      <Card
                        className="bg-card border-border hover:border-accent/30 transition-colors cursor-pointer"
                        onClick={() => setSelectedUnitId(item.id)}
                      >
                        <CardContent className="p-3">
                          <div className="flex items-center gap-2 mb-1.5">
                            <Badge
                              variant="secondary"
                              className="text-[10px]"
                              style={{ backgroundColor: color + '22', color }}
                            >
                              {formatLabel(item.fact_type)}
                            </Badge>
                            <VaultBadge vaultId={item.vault_id} />
                            {time && (
                              <span className="text-[10px] text-muted-foreground">{time}</span>
                            )}
                            {item.score != null && item.score > 0 && (
                              <span className="ml-auto text-[10px] text-muted-foreground">
                                Score: {item.score.toFixed(2)}
                              </span>
                            )}
                          </div>
                          <p className="text-sm text-foreground leading-relaxed">
                            {item.text.length > 200 ? item.text.slice(0, 200) + '...' : item.text}
                          </p>
                          <div className="mt-2 flex gap-2">
                            <Button
                              variant="ghost"
                              size="sm"
                              className="h-6 text-xs"
                              onClick={() => navigate(`/lineage?id=${item.id}&type=memory_unit`)}
                            >
                              Lineage
                            </Button>
                          </div>
                        </CardContent>
                      </Card>
                    </div>
                  );
                })}
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Detail dialog */}
      <MemoryDetailDialog
        unit={(selectedUnit as MemoryUnitDTO | undefined) ?? results.find((r) => r.id === selectedUnitId) ?? null}
        open={!!selectedUnitId}
        onOpenChange={(open) => { if (!open) setSelectedUnitId(null); }}
      />
    </div>
  );
}
