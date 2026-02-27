import { useState, useRef, useEffect, useCallback } from 'react';
import { Search, Tag } from 'lucide-react';
import { useEntitySearch, type EntityDTO } from '@/api/hooks/use-lineage';

interface EntitySearchProps {
  onSelect: (entity: EntityDTO) => void;
}

export function EntitySearch({ onSelect }: EntitySearchProps) {
  const [query, setQuery] = useState('');
  const [open, setOpen] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  const { data: results = [], isLoading } = useEntitySearch(query, open);

  const handleSelect = useCallback(
    (entity: EntityDTO) => {
      setQuery(entity.name);
      setOpen(false);
      onSelect(entity);
    },
    [onSelect],
  );

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === 'Enter' && results.length > 0) {
        e.preventDefault();
        handleSelect(results[0]);
      }
      if (e.key === 'Escape') {
        setOpen(false);
      }
    },
    [results, handleSelect],
  );

  // Close on outside click
  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (containerRef.current && !containerRef.current.contains(e.target as HTMLElement)) {
        setOpen(false);
      }
    }
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  return (
    <div ref={containerRef} className="relative w-80">
      <div className="relative">
        <Search
          className="absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground"
          size={16}
        />
        <input
          ref={inputRef}
          type="text"
          placeholder="Search entities..."
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
            setOpen(true);
          }}
          onFocus={() => setOpen(true)}
          onKeyDown={handleKeyDown}
          className="w-full h-9 pl-9 pr-3 rounded-md border border-border bg-card text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring"
        />
      </div>
      {open && results.length > 0 && (
        <div className="absolute top-full mt-1 w-full max-h-72 overflow-y-auto rounded-md border border-border bg-card shadow-lg z-50">
          {isLoading && (
            <div className="px-3 py-2 text-sm text-muted-foreground">Searching...</div>
          )}
          {results.map((entity) => (
            <button
              key={entity.id}
              onMouseDown={(e) => {
                e.preventDefault();
                handleSelect(entity);
              }}
              className="flex items-center gap-2 w-full px-3 py-2 text-left text-sm hover:bg-hover transition-colors cursor-pointer"
            >
              <Tag size={14} className="text-cyan-400 shrink-0" />
              <span className="text-foreground truncate">{entity.name}</span>
              {entity.mention_count > 0 && (
                <span className="ml-auto text-xs text-muted-foreground shrink-0">
                  {entity.mention_count} mentions
                </span>
              )}
            </button>
          ))}
        </div>
      )}
      {open && !isLoading && results.length === 0 && query.length >= 2 && (
        <div className="absolute top-full mt-1 w-full rounded-md border border-border bg-card shadow-lg z-50 px-3 py-4 text-center text-sm text-muted-foreground">
          No entities found
        </div>
      )}
    </div>
  );
}
