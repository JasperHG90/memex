import { useState, useRef, useEffect, useCallback, useMemo } from 'react';
import { Search, X } from 'lucide-react';
import { Input } from '@/components/ui/input';
import { TYPE_COLORS } from './entity-types';
import type { EntityDTO } from '@/api/generated';

interface EntitySearchProps {
  entities: EntityDTO[];
  onSelect: (entityId: string) => void;
}

export function EntitySearch({ entities, onSelect }: EntitySearchProps) {
  const [query, setQuery] = useState('');
  const [isOpen, setIsOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const [prevMatchCount, setPrevMatchCount] = useState(0);

  const matches = useMemo(
    () =>
      query.length > 0
        ? entities
            .filter((e) => e.name.toLowerCase().includes(query.toLowerCase()))
            .slice(0, 8)
        : [],
    [query, entities],
  );

  // Reset active index when matches change (React-recommended derived state pattern)
  if (matches.length !== prevMatchCount) {
    setPrevMatchCount(matches.length);
    setActiveIndex(0);
  }

  const showDropdown = isOpen && matches.length > 0;

  const selectEntity = useCallback(
    (entityId: string) => {
      onSelect(entityId);
      setQuery('');
      setIsOpen(false);
      inputRef.current?.blur();
    },
    [onSelect],
  );

  // Keyboard navigation
  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (!showDropdown) return;

      if (e.key === 'ArrowDown') {
        e.preventDefault();
        setActiveIndex((i) => Math.min(i + 1, matches.length - 1));
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        setActiveIndex((i) => Math.max(i - 1, 0));
      } else if (e.key === 'Enter') {
        e.preventDefault();
        if (matches[activeIndex]) selectEntity(matches[activeIndex].id);
      } else if (e.key === 'Escape') {
        setIsOpen(false);
      }
    },
    [showDropdown, matches, activeIndex, selectEntity],
  );

  // Scroll active item into view
  useEffect(() => {
    if (!listRef.current) return;
    const active = listRef.current.children[activeIndex] as HTMLElement | undefined;
    active?.scrollIntoView({ block: 'nearest' });
  }, [activeIndex]);

  // Close dropdown on outside click
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (inputRef.current && !inputRef.current.closest('.entity-search-root')?.contains(e.target as Node)) {
        setIsOpen(false);
      }
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, []);

  return (
    <div className="entity-search-root relative w-64">
      <div className="relative">
        <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-muted-foreground pointer-events-none" />
        <Input
          ref={inputRef}
          placeholder="Search entities..."
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
            setIsOpen(true);
          }}
          onFocus={() => setIsOpen(true)}
          onKeyDown={handleKeyDown}
          className="h-8 pl-8 pr-8 text-sm bg-background border-border"
        />
        {query && (
          <button
            onClick={() => { setQuery(''); setIsOpen(false); }}
            className="absolute right-2 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        )}
      </div>

      {showDropdown && (
        <div
          ref={listRef}
          className="absolute top-full left-0 right-0 mt-1 z-50 max-h-64 overflow-y-auto rounded-lg border border-border bg-card shadow-xl"
        >
          {matches.map((entity, i) => {
            const color = TYPE_COLORS[entity.entity_type ?? ''] ?? '#71717A';
            return (
              <button
                key={entity.id}
                type="button"
                className={`flex w-full items-center gap-2.5 px-3 py-2 text-left transition-colors ${
                  i === activeIndex ? 'bg-primary/10' : 'hover:bg-hover'
                }`}
                onMouseEnter={() => setActiveIndex(i)}
                onClick={() => selectEntity(entity.id)}
              >
                <div
                  className="h-2.5 w-2.5 shrink-0 rounded-full"
                  style={{ backgroundColor: color }}
                />
                <span className="text-sm font-medium text-foreground truncate">
                  {entity.name}
                </span>
                <span className="ml-auto shrink-0 text-[10px] text-muted-foreground">
                  {entity.mention_count ?? 0} mentions
                </span>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
