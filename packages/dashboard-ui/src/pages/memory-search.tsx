import { Fragment, useState, useCallback, useEffect, type KeyboardEvent } from 'react';
import { useNavigate } from 'react-router-dom';
import { Search, Loader2 } from 'lucide-react';

import { Input } from '@/components/ui/input';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Switch } from '@/components/ui/switch';
import { Separator } from '@/components/ui/separator';
import { ScrollArea } from '@/components/ui/scroll-area';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from '@/components/ui/dialog';
import { PageHeader } from '@/components/layout/page-header';
import { StrategyFilter } from '@/components/shared/strategy-filter';
import { SummaryCard } from '@/components/shared/summary-card';
import { TypeBadge } from '@/components/shared/type-badge';
import { ResultCardSkeleton } from '@/components/shared/result-card-skeleton';
import { EmptyState } from '@/components/shared/empty-state';
import { ErrorState } from '@/components/shared/error-state';
import { useMemorySearch } from '@/api/hooks/use-memories';
import { useSummary } from '@/api/hooks/use-summary';
import { useVaultStore } from '@/stores/vault-store';
import { useDebounce } from '@/lib/use-debounce';
import type { MemoryUnitDTO } from '@/api/generated';

const ALL_STRATEGIES = ['semantic', 'keyword', 'graph', 'temporal', 'mental_model'];
const SEARCH_LIMIT = 10;

export default function MemorySearch() {
  const navigate = useNavigate();
  const allSelectedVaultIds = useVaultStore((s) => s.allSelectedVaultIds);

  // Search state
  const [query, setQuery] = useState('');
  const [activeStrategies, setActiveStrategies] = useState<string[]>(ALL_STRATEGIES);
  const [results, setResults] = useState<MemoryUnitDTO[]>([]);
  const [offset, setOffset] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [hasSearched, setHasSearched] = useState(false);

  // Summary state
  const [showSummary, setShowSummary] = useState(false);

  // Detail modal state
  const [selectedResult, setSelectedResult] = useState<MemoryUnitDTO | null>(null);

  // Debounce query for auto-search
  const debouncedQuery = useDebounce(query, 300);

  // Hooks
  const searchMutation = useMemorySearch();
  const summaryMutation = useSummary();

  const executeSearch = useCallback(
    (searchQuery: string, searchOffset: number, append: boolean) => {
      if (!searchQuery.trim()) return;

      const vaultIds = allSelectedVaultIds();
      const strategies =
        activeStrategies.length < ALL_STRATEGIES.length ? activeStrategies : undefined;

      searchMutation.mutate(
        {
          query: searchQuery,
          limit: SEARCH_LIMIT,
          offset: searchOffset,
          rerank: true,
          include_vectors: false,
          include_stale: false,
          vault_ids: vaultIds.length > 0 ? vaultIds : undefined,
          strategies: strategies ?? undefined,
          skip_opinion_formation: true,
        },
        {
          onSuccess: (data) => {
            const newResults = data as unknown as MemoryUnitDTO[];
            if (append) {
              setResults((prev) => [...prev, ...newResults]);
            } else {
              setResults(newResults);
            }
            setHasMore(newResults.length >= SEARCH_LIMIT);
            setHasSearched(true);
          },
        },
      );
    },
    [activeStrategies, allSelectedVaultIds, searchMutation],
  );

  const handleSearch = useCallback(() => {
    if (!query.trim()) return;
    setOffset(0);
    setResults([]);
    summaryMutation.reset();
    executeSearch(query, 0, false);
  }, [query, executeSearch, summaryMutation]);

  const handleLoadMore = useCallback(() => {
    const nextOffset = offset + SEARCH_LIMIT;
    setOffset(nextOffset);
    executeSearch(query, nextOffset, true);
  }, [query, offset, executeSearch]);

  const handleKeyDown = useCallback(
    (e: KeyboardEvent<HTMLInputElement>) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        handleSearch();
      }
    },
    [handleSearch],
  );

  // Re-trigger search when strategies change (if we already have a query)
  useEffect(() => {
    if (debouncedQuery.trim() && hasSearched) {
      setOffset(0);
      setResults([]);
      summaryMutation.reset();
      executeSearch(debouncedQuery, 0, false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeStrategies]);

  // Auto-trigger summary when toggled on and results exist
  useEffect(() => {
    if (showSummary && results.length > 0 && !summaryMutation.data && !summaryMutation.isPending) {
      summaryMutation.mutate({
        query,
        texts: results.slice(0, 50).map((r) => r.text),
      });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showSummary, results]);

  const handleToggleStrategy = useCallback(
    (strategies: string[]) => {
      // Keep at least one strategy active
      if (strategies.length === 0) return;
      setActiveStrategies(strategies);
    },
    [],
  );

  const handleCitationClick = useCallback((index: number) => {
    // Open the detail modal for the cited result (1-indexed from summary)
    const resultIndex = index - 1;
    if (resultIndex >= 0 && resultIndex < results.length) {
      setSelectedResult(results[resultIndex]);
    }
  }, [results]);

  const handleToggleSummary = useCallback(
    (checked: boolean) => {
      setShowSummary(checked);
      if (checked && results.length > 0 && !summaryMutation.data) {
        summaryMutation.mutate({
          query,
          texts: results.slice(0, 50).map((r) => r.text),
        });
      }
    },
    [query, results, summaryMutation],
  );

  const isLoading = searchMutation.isPending;
  const isError = searchMutation.isError;

  return (
    <div className="space-y-6">
      <PageHeader
        title="Memory Search"
        description="Search memories, entities, and documents"
      />

      {/* Search bar */}
      <div className="flex gap-2">
        <div className="relative flex-1">
          <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            placeholder="Search memories, entities, and documents..."
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={handleKeyDown}
            className="pl-10"
          />
        </div>
        <Button
          onClick={handleSearch}
          disabled={isLoading || !query.trim()}
        >
          {isLoading ? (
            <Loader2 className="h-4 w-4 animate-spin" />
          ) : (
            <Search className="h-4 w-4" />
          )}
          Search
        </Button>
      </div>

      {/* Strategy filter */}
      <StrategyFilter
        selected={activeStrategies}
        onChange={handleToggleStrategy}
      />

      {/* Summary toggle */}
      <div className="flex items-center gap-2">
        <span className="text-sm text-muted-foreground">AI Summary</span>
        <Switch
          checked={showSummary}
          onCheckedChange={handleToggleSummary}
          aria-label="Toggle AI Summary"
        />
      </div>

      {/* Summary card */}
      {showSummary && (results.length > 0 || summaryMutation.isPending) && (
        <SummaryCard
          summary={summaryMutation.data?.summary}
          isLoading={summaryMutation.isPending}
          onCitationClick={handleCitationClick}
        />
      )}

      {/* Results area */}
      {isLoading && results.length === 0 ? (
        <div className="space-y-3">
          {Array.from({ length: 3 }).map((_, i) => (
            <ResultCardSkeleton key={i} />
          ))}
        </div>
      ) : isError ? (
        <ErrorState
          message="Search failed. Please try again."
          onRetry={handleSearch}
        />
      ) : hasSearched && results.length === 0 ? (
        <EmptyState
          icon={Search}
          title="No results found"
          description="Try adjusting your search query or strategy filters."
        />
      ) : !hasSearched ? (
        <div className="rounded-xl border border-border bg-card p-10 text-center">
          <p className="text-muted-foreground">
            Enter a search query to find memories.
          </p>
        </div>
      ) : (
        <div className="space-y-3">
          {results.map((result, index) => (
            <ResultCard
              key={result.id}
              result={result}
              index={index}
              onViewDetails={() => setSelectedResult(result)}
              onViewLineage={() =>
                navigate(`/lineage?id=${result.id}&type=memory_unit`)
              }
            />
          ))}

          {/* Load more */}
          {hasMore && (
            <div className="flex justify-center pt-2">
              <Button
                variant="outline"
                onClick={handleLoadMore}
                disabled={isLoading}
              >
                {isLoading ? (
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                ) : null}
                Load More
              </Button>
            </div>
          )}
        </div>
      )}

      {/* Detail modal */}
      <MemoryDetailDialog
        result={selectedResult}
        onClose={() => setSelectedResult(null)}
      />
    </div>
  );
}

// --- Helper components ---

interface ResultCardProps {
  result: MemoryUnitDTO;
  index: number;
  onViewDetails: () => void;
  onViewLineage: () => void;
}

function ResultCard({ result, index, onViewDetails, onViewLineage }: ResultCardProps) {
  const factType = cleanFactType(result.fact_type);
  const score = result.score ?? 0;
  const scorePercent = Math.min(score * 100, 100);
  const truncatedText =
    result.text.length > 200 ? result.text.slice(0, 200) + '...' : result.text;

  return (
    <Card
      id={`result-${index + 1}`}
      className="bg-card border-border transition-colors hover:border-accent/50"
    >
      <CardContent className="p-4">
        {/* Badges row */}
        <div className="mb-2 flex flex-wrap items-center gap-2">
          <TypeBadge type="memory_unit" />
          <TypeBadge type={factType} />
          {score > 0 && (
            <div className="ml-auto flex items-center gap-2">
              <div className="h-1.5 w-20 overflow-hidden rounded-full bg-muted">
                <div
                  className="h-full rounded-full bg-accent transition-all"
                  style={{ width: `${scorePercent}%` }}
                />
              </div>
              <span className="text-xs text-muted-foreground">
                {score.toFixed(2)}
              </span>
            </div>
          )}
        </div>

        {/* Content */}
        <p className="mb-3 text-sm text-foreground leading-relaxed">
          {truncatedText}
        </p>

        {/* Timestamp */}
        {result.mentioned_at && (
          <p className="mb-3 text-xs text-muted-foreground">
            {new Date(result.mentioned_at).toLocaleDateString()}
          </p>
        )}

        {/* Actions */}
        <div className="flex gap-2 border-t border-border pt-3">
          <Button variant="secondary" size="sm" onClick={onViewDetails}>
            Details
          </Button>
          <Button variant="outline" size="sm" onClick={onViewLineage}>
            Lineage
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

function cleanFactType(raw: string): string {
  if (raw.includes('.')) {
    return raw.split('.').pop()?.toLowerCase() ?? raw;
  }
  return raw.toLowerCase();
}

function MemoryDetailDialog({
  result,
  onClose,
}: {
  result: MemoryUnitDTO | null;
  onClose: () => void;
}) {
  if (!result) return null;

  const factType = cleanFactType(result.fact_type);
  const metadata = result.metadata ?? {};
  const metaEntries = Object.entries(metadata);

  return (
    <Dialog open={!!result} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-w-2xl max-h-[85vh]">
        <DialogHeader>
          <DialogTitle>Memory Details</DialogTitle>
          <DialogDescription>Full memory unit with metadata</DialogDescription>
        </DialogHeader>

        {/* Type badges */}
        <div className="flex flex-wrap items-center gap-2">
          <TypeBadge type="memory_unit" />
          <TypeBadge type={factType} />
          {result.status && (
            <Badge variant="outline" className="text-xs">
              {result.status}
            </Badge>
          )}
        </div>

        {/* Full text */}
        <ScrollArea className="max-h-[40vh]">
          <p className="text-sm leading-relaxed text-foreground whitespace-pre-wrap">
            {result.text}
          </p>
        </ScrollArea>

        <Separator />

        {/* Metadata section */}
        <div className="space-y-3">
          <p className="text-xs font-medium text-muted-foreground uppercase tracking-wide">
            Metadata
          </p>
          <div className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
            {result.note_id && (
              <>
                <span className="text-muted-foreground">Source Note</span>
                <span className="text-foreground truncate" title={result.note_id}>
                  {result.note_id}
                </span>
              </>
            )}
            {result.source_note_ids && result.source_note_ids.length > 0 && (
              <>
                <span className="text-muted-foreground">Source Notes</span>
                <span className="text-foreground break-all">
                  {result.source_note_ids.join(', ')}
                </span>
              </>
            )}
            {result.score != null && (
              <>
                <span className="text-muted-foreground">Score</span>
                <span className="text-foreground">{result.score.toFixed(4)}</span>
              </>
            )}
            {result.confidence_alpha != null && (
              <>
                <span className="text-muted-foreground">Confidence (alpha)</span>
                <span className="text-foreground">{result.confidence_alpha.toFixed(4)}</span>
              </>
            )}
            {result.confidence_beta != null && (
              <>
                <span className="text-muted-foreground">Confidence (beta)</span>
                <span className="text-foreground">{result.confidence_beta.toFixed(4)}</span>
              </>
            )}
            {result.mentioned_at && (
              <>
                <span className="text-muted-foreground">Mentioned At</span>
                <span className="text-foreground">
                  {new Date(result.mentioned_at).toLocaleString()}
                </span>
              </>
            )}
            {result.occurred_start && (
              <>
                <span className="text-muted-foreground">Occurred Start</span>
                <span className="text-foreground">
                  {new Date(result.occurred_start).toLocaleString()}
                </span>
              </>
            )}
            {result.occurred_end && (
              <>
                <span className="text-muted-foreground">Occurred End</span>
                <span className="text-foreground">
                  {new Date(result.occurred_end).toLocaleString()}
                </span>
              </>
            )}
            {result.vault_id && (
              <>
                <span className="text-muted-foreground">Vault</span>
                <span className="text-foreground truncate" title={result.vault_id}>
                  {result.vault_id}
                </span>
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
      </DialogContent>
    </Dialog>
  );
}
