import { useState, useCallback, useEffect, type KeyboardEvent } from 'react';
import ReactMarkdown from 'react-markdown';
import { Search, Loader2, FileText, ChevronDown, ChevronUp } from 'lucide-react';

import { Input } from '@/components/ui/input';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { Switch } from '@/components/ui/switch';
import { Separator } from '@/components/ui/separator';
import { ScrollArea } from '@/components/ui/scroll-area';
import { Skeleton } from '@/components/ui/skeleton';
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
import { ResultCardSkeleton } from '@/components/shared/result-card-skeleton';
import { EmptyState } from '@/components/shared/empty-state';
import { ErrorState } from '@/components/shared/error-state';
import { AdvancedSearchPanel, type AdvancedSearchParams } from '@/components/shared/advanced-search-panel';
import { useNoteSearch } from '@/api/hooks/use-notes';
import { useNote, useNotePageIndex } from '@/api/hooks/use-notes';
import { useSummary } from '@/api/hooks/use-summary';
import { useVaultStore } from '@/stores/vault-store';
import { usePreferencesStore } from '@/stores/preferences-store';
import type { NoteSearchResult, NoteSnippet } from '@/api/generated';

const DOC_STRATEGIES = ['semantic', 'keyword', 'graph', 'temporal'];

function extractTitle(metadata: Record<string, unknown>): string {
  return String(metadata?.title ?? metadata?.name ?? 'Untitled');
}

interface PageIndexNode {
  title: string;
  level: number;
  depth: number;
  prefix: string;
  summary: string;
}

function flattenPageIndex(
  nodes: unknown,
  depth = 0,
  ancestorIsLast: boolean[] = [],
): PageIndexNode[] {
  const result: PageIndexNode[] = [];

  if (Array.isArray(nodes)) {
    const valid = nodes.filter(
      (n): n is Record<string, unknown> =>
        typeof n === 'object' && n !== null && 'title' in n,
    );
    for (let i = 0; i < valid.length; i++) {
      result.push(
        ...flattenPageIndex(valid[i], depth, [
          ...ancestorIsLast,
          i === valid.length - 1,
        ]),
      );
    }
  } else if (typeof nodes === 'object' && nodes !== null) {
    const node = nodes as Record<string, unknown>;
    const title = String(node.title ?? node.name ?? '');
    const level = typeof node.level === 'number' ? node.level : depth;

    let summary = '';
    const rawSummary = node.summary;
    if (typeof rawSummary === 'object' && rawSummary !== null) {
      const s = rawSummary as Record<string, unknown>;
      const parts = ['who', 'what', 'how', 'when', 'where']
        .map((k) => s[k])
        .filter(Boolean)
        .map(String);
      summary = parts.join(' | ');
    }

    let prefix = '';
    if (depth > 0) {
      const parts: string[] = [];
      for (const isLast of ancestorIsLast.slice(1, -1)) {
        parts.push(isLast ? '    ' : '\u2502   ');
      }
      const isLast = ancestorIsLast[ancestorIsLast.length - 1] ?? false;
      parts.push(isLast ? '\u2514\u2500\u2500 ' : '\u251C\u2500\u2500 ');
      prefix = parts.join('');
    }

    if (title) {
      result.push({ title, level, depth, prefix, summary });
    }

    const children = node.children;
    if (Array.isArray(children)) {
      const validChildren = children.filter(
        (c): c is Record<string, unknown> =>
          typeof c === 'object' && c !== null && 'title' in c,
      );
      for (let i = 0; i < validChildren.length; i++) {
        result.push(
          ...flattenPageIndex(validChildren[i], depth + 1, [
            ...ancestorIsLast,
            i === validChildren.length - 1,
          ]),
        );
      }
    }
  }

  return result;
}

export default function NoteSearch() {
  const allSelectedVaultIds = useVaultStore((s) => s.allSelectedVaultIds);
  const defaultSearchLimit = usePreferencesStore((s) => s.defaultSearchLimit);

  // Search state
  const [query, setQuery] = useState('');
  const [activeStrategies, setActiveStrategies] = useState<string[]>(DOC_STRATEGIES);
  const [results, setResults] = useState<NoteSearchResult[]>([]);
  const [hasSearched, setHasSearched] = useState(false);

  // Advanced search params
  const [advancedParams, setAdvancedParams] = useState<AdvancedSearchParams>({ minScore: null, tokenBudget: null, expandQuery: false });

  // Summary state
  const [showSummary, setShowSummary] = useState(false);

  // Detail modal
  const [selectedNoteId, setSelectedNoteId] = useState<string | null>(null);

  // Hooks
  const searchMutation = useNoteSearch();
  const summaryMutation = useSummary();

  const executeSearch = useCallback(
    (searchQuery: string) => {
      if (!searchQuery.trim()) return;

      const vaultIds = allSelectedVaultIds();
      const strategies =
        activeStrategies.length < DOC_STRATEGIES.length ? activeStrategies : undefined;

      searchMutation.mutate(
        {
          query: searchQuery,
          limit: defaultSearchLimit,
          vault_ids: vaultIds.length > 0 ? vaultIds : undefined,
          strategies: strategies ?? ['semantic', 'keyword', 'graph', 'temporal'],
          expand_query: advancedParams.expandQuery,
          fusion_strategy: 'rrf',
          reason: false,
          summarize: false,
        },
        {
          onSuccess: (data) => {
            setResults(data);
            setHasSearched(true);
          },
        },
      );
    },
    [activeStrategies, advancedParams, allSelectedVaultIds, defaultSearchLimit, searchMutation],
  );

  const handleSearch = useCallback(() => {
    if (!query.trim()) return;
    setResults([]);
    summaryMutation.reset();
    executeSearch(query);
  }, [query, executeSearch, summaryMutation]);

  const handleSearchWithQuery = useCallback((q: string) => {
    setResults([]);
    summaryMutation.reset();
    executeSearch(q);
  }, [executeSearch, summaryMutation]);

  const handleKeyDown = useCallback(
    (e: KeyboardEvent<HTMLInputElement>) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        handleSearch();
      }
    },
    [handleSearch],
  );

  // Re-trigger search when strategies change
  useEffect(() => {
    if (query.trim() && hasSearched) {
      setResults([]);
      summaryMutation.reset();
      executeSearch(query);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeStrategies]);

  // Auto-trigger summary when toggled on
  useEffect(() => {
    if (showSummary && results.length > 0 && !summaryMutation.data && !summaryMutation.isPending) {
      const texts = results
        .slice(0, 20)
        .flatMap((r) => r.snippets.slice(0, 2).map((s) => s.text));
      summaryMutation.mutate({ query, texts });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showSummary, results]);

  const handleToggleStrategy = useCallback((strategies: string[]) => {
    if (strategies.length === 0) return;
    setActiveStrategies(strategies);
  }, []);

  const handleCitationClick = useCallback(
    (index: number) => {
      if (index >= 0 && index < results.length) {
        setSelectedNoteId(results[index].note_id);
      }
    },
    [results],
  );

  const handleToggleSummary = useCallback(
    (checked: boolean) => {
      setShowSummary(checked);
      if (checked && results.length > 0 && !summaryMutation.data) {
        const texts = results
          .slice(0, 20)
          .flatMap((r) => r.snippets.slice(0, 2).map((s) => s.text));
        summaryMutation.mutate({ query, texts });
      }
    },
    [query, results, summaryMutation],
  );

  const isLoading = searchMutation.isPending;
  const isError = searchMutation.isError;

  return (
    <div className="space-y-6">
      <PageHeader
        title="Note Search"
        description="Search notes using multi-strategy retrieval"
      />

      {/* Search bar */}
      <div className="flex gap-2">
        <div className="relative flex-1">
          <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            placeholder="Search notes..."
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={handleKeyDown}
            className="pl-10"
          />
        </div>
        <Button onClick={handleSearch} disabled={isLoading || !query.trim()}>
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
        available={DOC_STRATEGIES}
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

      {/* Results */}
      {isLoading && results.length === 0 ? (
        <div className="space-y-3">
          {Array.from({ length: 3 }).map((_, i) => (
            <ResultCardSkeleton key={i} />
          ))}
        </div>
      ) : isError ? (
        <ErrorState
          message="Note search failed. Please try again."
          onRetry={handleSearch}
        />
      ) : hasSearched && results.length === 0 ? (
        <EmptyState
          icon={Search}
          title="No notes found"
          description="Try adjusting your search query or strategy filters."
        />
      ) : !hasSearched ? (
        <EmptyState
          icon={Search}
          title="Search Notes"
          description="Enter a query to search across all ingested notes."
          suggestions={[
            { label: 'Architecture decisions', onClick: () => { setQuery('architecture decisions'); handleSearchWithQuery('architecture decisions'); } },
            { label: 'Meeting notes', onClick: () => { setQuery('meeting notes'); handleSearchWithQuery('meeting notes'); } },
            { label: 'Research findings', onClick: () => { setQuery('research findings'); handleSearchWithQuery('research findings'); } },
            { label: 'Project updates', onClick: () => { setQuery('project updates'); handleSearchWithQuery('project updates'); } },
          ]}
        />
      ) : (
        <div className="space-y-3">
          {results.map((result) => (
            <NoteResultCard
              key={result.note_id}
              result={result}
              onOpenDetails={() => setSelectedNoteId(result.note_id)}
            />
          ))}
        </div>
      )}

      {/* Detail modal */}
      <NoteDetailDialog
        noteId={selectedNoteId}
        onClose={() => setSelectedNoteId(null)}
      />
    </div>
  );
}

// --- Result card ---

function NoteResultCard({
  result,
  onOpenDetails,
}: {
  result: NoteSearchResult;
  onOpenDetails: () => void;
}) {
  const title = extractTitle(result.metadata);
  const score = result.score ?? 0;
  const scorePercent = Math.min(score * 100, 100);

  return (
    <Card className="bg-card border-border transition-colors hover:border-accent/50">
      <CardContent className="p-4">
        {/* Header */}
        <div className="mb-3 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <FileText className="h-4 w-4 text-muted-foreground" />
            <span className="text-sm font-semibold text-foreground">{title}</span>
          </div>
          {score > 0 && (
            <div className="flex items-center gap-2">
              <div className="h-1.5 w-20 overflow-hidden rounded-full bg-muted">
                <div
                  className="h-full rounded-full bg-accent transition-all"
                  style={{ width: `${scorePercent}%` }}
                />
              </div>
              <span className="text-xs text-muted-foreground">{score.toFixed(2)}</span>
            </div>
          )}
        </div>

        {/* Snippets */}
        {result.snippets.length > 0 && (
          <div className="mb-3 space-y-2">
            {result.snippets.slice(0, 2).map((snippet, i) => (
              <SnippetPreview key={i} snippet={snippet} />
            ))}
          </div>
        )}

        {/* Answer (when summarize=true) */}
        {result.answer && (
          <div className="mb-3 rounded-md bg-blue-500/5 p-3 border border-blue-500/20">
            <p className="text-xs font-medium text-blue-400 mb-1">AI Answer</p>
            <p className="text-sm text-foreground/90 leading-relaxed">{result.answer}</p>
          </div>
        )}

        {/* Actions */}
        <div className="flex gap-2 border-t border-border pt-3">
          <Button variant="secondary" size="sm" onClick={onOpenDetails}>
            Details
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

function SnippetPreview({ snippet }: { snippet: NoteSnippet }) {
  const truncatedText =
    snippet.text.length > 300 ? snippet.text.slice(0, 300) + '...' : snippet.text;

  return (
    <div className="rounded-md bg-muted/30 border border-border p-2">
      {snippet.node_title && (
        <p className="text-[10px] font-bold text-primary mb-1">{snippet.node_title}</p>
      )}
      <div className="prose prose-invert prose-xs max-w-none line-clamp-2 text-xs text-muted-foreground [&>*]:m-0 [&>*]:text-muted-foreground">
        <ReactMarkdown>{truncatedText}</ReactMarkdown>
      </div>
    </div>
  );
}

// --- Detail modal with note content + page index ---

function NoteDetailDialog({
  noteId,
  onClose,
}: {
  noteId: string | null;
  onClose: () => void;
}) {
  const { data: note, isLoading: isNoteLoading } = useNote(noteId ?? undefined);
  const { data: pageIndexData, isLoading: isPageIndexLoading } = useNotePageIndex(
    noteId ?? undefined,
  );

  const [showContent, setShowContent] = useState(true);

  if (!noteId) return null;

  const pageIndexNodes = pageIndexData?.page_index
    ? flattenPageIndex(pageIndexData.page_index)
    : [];

  const noteTitle = note?.title ?? note?.name ?? 'Note Details';
  const metadata = note?.doc_metadata ?? {};
  const metadataEntries = Object.entries(metadata);

  return (
    <Dialog open={!!noteId} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-w-4xl max-h-[85vh]">
        <DialogHeader>
          <DialogTitle>{noteTitle}</DialogTitle>
          <DialogDescription>Note content, page index, and metadata</DialogDescription>
        </DialogHeader>

        <div className="flex gap-4 min-h-0 max-h-[65vh]">
          {/* Left: note content */}
          <div className="flex-[2] min-w-0 space-y-2">
            <button
              className="flex items-center gap-1 text-xs font-medium text-muted-foreground uppercase tracking-wide"
              onClick={() => setShowContent(!showContent)}
            >
              Content
              {showContent ? (
                <ChevronUp className="h-3 w-3" />
              ) : (
                <ChevronDown className="h-3 w-3" />
              )}
            </button>

            {showContent && (
              isNoteLoading ? (
                <div className="space-y-2 py-4">
                  <Skeleton className="h-4 w-full" />
                  <Skeleton className="h-4 w-4/5" />
                  <Skeleton className="h-4 w-3/5" />
                  <Skeleton className="h-4 w-full" />
                  <Skeleton className="h-4 w-2/3" />
                </div>
              ) : note?.original_text ? (
                <ScrollArea className="h-[480px]">
                  <div className="prose prose-invert prose-sm max-w-none pr-4">
                    <ReactMarkdown>{note.original_text}</ReactMarkdown>
                  </div>
                </ScrollArea>
              ) : (
                <p className="text-sm text-muted-foreground py-4">No content available.</p>
              )
            )}
          </div>

          {/* Right: page index + metadata */}
          <div className="flex-1 min-w-[240px] border-l border-border pl-4 space-y-4">
            {/* Page Index */}
            <div>
              <p className="text-xs font-medium text-muted-foreground uppercase tracking-wide mb-2">
                Page Index
              </p>
              {isPageIndexLoading ? (
                <div className="space-y-2">
                  <Skeleton className="h-3 w-full" />
                  <Skeleton className="h-3 w-4/5" />
                  <Skeleton className="h-3 w-3/5" />
                </div>
              ) : pageIndexNodes.length > 0 ? (
                <ScrollArea className="h-[250px]">
                  <div className="space-y-0">
                    {pageIndexNodes.map((node, i) => (
                      <PageIndexRow key={i} node={node} />
                    ))}
                  </div>
                </ScrollArea>
              ) : (
                <p className="text-xs text-muted-foreground">No page index available.</p>
              )}
            </div>

            <Separator />

            {/* Metadata */}
            <div>
              <p className="text-xs font-medium text-muted-foreground uppercase tracking-wide mb-2">
                Metadata
              </p>
              {metadataEntries.length > 0 ? (
                <ScrollArea className="h-[180px]">
                  <table className="w-full text-xs">
                    <tbody>
                      {metadataEntries.map(([key, value]) => (
                        <tr key={key} className="border-b border-border">
                          <td className="py-1 pr-2 font-medium text-muted-foreground align-top whitespace-nowrap">
                            {key}
                          </td>
                          <td className="py-1 text-foreground break-all">
                            {typeof value === 'object'
                              ? JSON.stringify(value)
                              : String(value ?? '-')}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </ScrollArea>
              ) : (
                <p className="text-xs text-muted-foreground">No metadata.</p>
              )}
            </div>

            {/* Note info */}
            {note && (
              <div className="space-y-1">
                <p className="text-xs text-muted-foreground">
                  Created: {new Date(note.created_at).toLocaleDateString()}
                </p>
                {note.assets && note.assets.length > 0 && (
                  <div>
                    <p className="text-xs font-medium text-muted-foreground uppercase tracking-wide mb-2">
                      Assets
                    </p>
                    <div className="flex flex-col gap-2">
                      {note.assets.map((asset) => {
                        const isImage = /\.(jpg|jpeg|png|gif|webp|svg)$/i.test(asset);
                        const fileName = asset.split('/').pop() ?? asset;
                        return (
                          <div key={asset} className="rounded-md border border-border p-2">
                            {isImage && (
                              <img
                                src={`${import.meta.env.VITE_API_BASE ?? '/api/v1'}/resources/${encodeURIComponent(asset)}`}
                                alt={fileName}
                                className="mb-2 max-h-32 rounded object-contain"
                                onError={(e) => { (e.target as HTMLImageElement).style.display = 'none'; }}
                              />
                            )}
                            <a
                              href={`${import.meta.env.VITE_API_BASE ?? '/api/v1'}/resources/${encodeURIComponent(asset)}`}
                              download={fileName}
                              target="_blank"
                              rel="noopener noreferrer"
                              className="inline-flex items-center gap-1 text-xs text-primary hover:underline"
                            >
                              <FileText className="h-3 w-3" />
                              {fileName}
                            </a>
                          </div>
                        );
                      })}
                    </div>
                  </div>
                )}
              </div>
            )}
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function PageIndexRow({ node }: { node: PageIndexNode }) {
  return (
    <div className="py-1 border-b border-border/30 hover:bg-muted/20 transition-colors">
      <div className="flex items-baseline">
        {node.prefix && (
          <span className="font-mono text-xs text-muted-foreground/40 whitespace-pre shrink-0">
            {node.prefix}
          </span>
        )}
        <span
          className={`text-xs leading-relaxed ${
            node.depth === 0 ? 'font-semibold text-foreground' : 'text-foreground/80'
          }`}
        >
          {node.title}
        </span>
      </div>
      {node.summary && (
        <p
          className="text-[11px] text-muted-foreground/70 leading-snug"
          style={{ paddingLeft: node.depth * 28 }}
        >
          {node.summary}
        </p>
      )}
    </div>
  );
}
