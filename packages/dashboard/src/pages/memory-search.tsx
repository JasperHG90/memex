import { useState, useCallback, useEffect, type KeyboardEvent } from 'react';
import { useNavigate } from 'react-router-dom';
import { Search, Loader2 } from 'lucide-react';

import { Input } from '@/components/ui/input';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { Switch } from '@/components/ui/switch';
import { PageHeader } from '@/components/layout/page-header';
import { StrategyFilter } from '@/components/shared/strategy-filter';
import { SummaryCard } from '@/components/shared/summary-card';
import { TypeBadge } from '@/components/shared/type-badge';
import { ResultCardSkeleton } from '@/components/shared/result-card-skeleton';
import { EmptyState } from '@/components/shared/empty-state';
import { ErrorState } from '@/components/shared/error-state';
import { AdvancedSearchPanel, type AdvancedSearchParams } from '@/components/shared/advanced-search-panel';
import { MemoryDetailDialog } from '@/components/shared/memory-detail-dialog';
import { VaultBadge } from '@/components/shared/vault-badge';
import { useMemorySearch } from '@/api/hooks/use-memories';
import { useSummary } from '@/api/hooks/use-summary';
import { useVaultStore } from '@/stores/vault-store';
import { usePreferencesStore } from '@/stores/preferences-store';
import { useDebounce } from '@/lib/use-debounce';
import type { MemoryUnitDTO } from '@/api/generated';

const ALL_STRATEGIES = ['semantic', 'keyword', 'graph', 'temporal', 'mental_model'];

export default function MemorySearch() {
  const navigate = useNavigate();
  const allSelectedVaultIds = useVaultStore((s) => s.allSelectedVaultIds);
  const defaultSearchLimit = usePreferencesStore((s) => s.defaultSearchLimit);
  const defaultStrategies = usePreferencesStore((s) => s.defaultStrategies);

  // Search state
  const [query, setQuery] = useState('');
  const [activeStrategies, setActiveStrategies] = useState<string[]>(defaultStrategies);
  const [results, setResults] = useState<MemoryUnitDTO[]>([]);
  const [offset, setOffset] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [hasSearched, setHasSearched] = useState(false);

  // Advanced search params
  const [advancedParams, setAdvancedParams] = useState<AdvancedSearchParams>({ minScore: null, tokenBudget: null, expandQuery: false });

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
          limit: defaultSearchLimit,
          offset: searchOffset,
          rerank: true,
          include_vectors: false,
          include_stale: false,
          vault_ids: vaultIds.length > 0 ? vaultIds : undefined,
          strategies: strategies ?? undefined,
          skip_opinion_formation: true,
          min_score: advancedParams.minScore ?? undefined,
          token_budget: advancedParams.tokenBudget ?? undefined,
        },
        {
          onSuccess: (data) => {
            const newResults = data;
            if (append) {
              setResults((prev) => [...prev, ...newResults]);
            } else {
              setResults(newResults);
            }
            setHasMore(newResults.length >= defaultSearchLimit);
            setHasSearched(true);
          },
        },
      );
    },
    [activeStrategies, advancedParams, allSelectedVaultIds, defaultSearchLimit, searchMutation],
  );

  const handleSearch = useCallback(() => {
    if (!query.trim()) return;
    setOffset(0);
    setResults([]);
    summaryMutation.reset();
    executeSearch(query, 0, false);
  }, [query, executeSearch, summaryMutation]);

  const handleSearchWithQuery = useCallback((q: string) => {
    setOffset(0);
    setResults([]);
    summaryMutation.reset();
    executeSearch(q, 0, false);
  }, [executeSearch, summaryMutation]);

  const handleLoadMore = useCallback(() => {
    const nextOffset = offset + defaultSearchLimit;
    setOffset(nextOffset);
    executeSearch(query, nextOffset, true);
  }, [query, offset, defaultSearchLimit, executeSearch]);

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

      {/* Advanced search options */}
      <AdvancedSearchPanel params={advancedParams} onChange={setAdvancedParams} />

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
        <EmptyState
          icon={Search}
          title="Search Memories"
          description="Enter a query to search across all memory units."
          suggestions={[
            { label: 'Recent conversations', onClick: () => { setQuery('recent conversations'); handleSearchWithQuery('recent conversations'); } },
            { label: 'Key decisions', onClick: () => { setQuery('key decisions'); handleSearchWithQuery('key decisions'); } },
            { label: 'Technical insights', onClick: () => { setQuery('technical insights'); handleSearchWithQuery('technical insights'); } },
            { label: 'All observations', onClick: () => { setQuery('observations'); handleSearchWithQuery('observations'); } },
          ]}
        />
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
        unit={selectedResult}
        open={!!selectedResult}
        onOpenChange={(open) => { if (!open) setSelectedResult(null); }}
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
          <VaultBadge vaultId={result.vault_id} />
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
              {(() => {
                const conf = getConfidenceInfo(result.confidence_alpha, result.confidence_beta);
                if (!conf) return null;
                return (
                  <div className="flex items-center gap-1.5 ml-2">
                    <div className={`h-2 w-2 rounded-full ${conf.color}`} />
                    <span className={`text-xs ${conf.textColor}`}>{(conf.mean * 100).toFixed(0)}%</span>
                  </div>
                );
              })()}
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

function getConfidenceInfo(alpha: number | null | undefined, beta: number | null | undefined) {
  if (alpha == null || beta == null || alpha + beta === 0) return null;
  const mean = alpha / (alpha + beta);
  if (mean > 0.7) return { mean, color: 'bg-emerald-500', textColor: 'text-emerald-500', label: 'High' };
  if (mean > 0.4) return { mean, color: 'bg-amber-500', textColor: 'text-amber-500', label: 'Medium' };
  return { mean, color: 'bg-red-500', textColor: 'text-red-500', label: 'Low' };
}
