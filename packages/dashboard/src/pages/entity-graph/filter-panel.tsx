import { Slider } from '@/components/ui/slider';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { ChevronDown, ChevronUp, RotateCcw } from 'lucide-react';
import { useState } from 'react';
import { ENTITY_TYPES } from './entity-node';

const RECENCY_OPTIONS = [
  { label: 'All time', value: 'all' },
  { label: 'Last 7 days', value: '7d' },
  { label: 'Last 30 days', value: '30d' },
  { label: 'Last 90 days', value: '90d' },
] as const;

export interface GraphFilters {
  minConnectionStrength: number;
  minImportance: number;
  recency: string;
  entityTypes: string[];
}

interface FilterPanelProps {
  filters: GraphFilters;
  onFiltersChange: (filters: GraphFilters) => void;
}

export function FilterPanel({ filters, onFiltersChange }: FilterPanelProps) {
  const [isOpen, setIsOpen] = useState(true);

  function updateFilter<K extends keyof GraphFilters>(key: K, value: GraphFilters[K]) {
    onFiltersChange({ ...filters, [key]: value });
  }

  function resetFilters() {
    onFiltersChange({
      minConnectionStrength: 1,
      minImportance: 1,
      recency: 'all',
      entityTypes: [...ENTITY_TYPES],
    });
  }

  return (
    <div className="absolute top-4 left-4 z-10 w-56">
      <div className="rounded-lg border border-border bg-card p-3 shadow-lg">
        <button
          onClick={() => setIsOpen(!isOpen)}
          className="flex w-full items-center justify-between text-sm font-medium text-foreground"
        >
          Graph Filters
          {isOpen ? <ChevronUp className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
        </button>

        {isOpen && (
          <div className="mt-3 flex flex-col gap-3">
            <div>
              <label className="text-xs text-muted-foreground">
                Connection Strength: {filters.minConnectionStrength}
              </label>
              <Slider
                min={1}
                max={10}
                step={1}
                value={[filters.minConnectionStrength]}
                onValueChange={([v]) => updateFilter('minConnectionStrength', v)}
                className="mt-1"
              />
            </div>

            <div>
              <label className="text-xs text-muted-foreground">
                Importance: {filters.minImportance}
              </label>
              <Slider
                min={1}
                max={20}
                step={1}
                value={[filters.minImportance]}
                onValueChange={([v]) => updateFilter('minImportance', v)}
                className="mt-1"
              />
            </div>

            <div>
              <label className="text-xs text-muted-foreground">Recency</label>
              <div className="mt-1 flex flex-wrap gap-1">
                {RECENCY_OPTIONS.map((opt) => (
                  <button
                    key={opt.value}
                    onClick={() => updateFilter('recency', opt.value)}
                    className={`rounded px-2 py-0.5 text-xs transition-colors ${
                      filters.recency === opt.value
                        ? 'bg-primary text-white'
                        : 'bg-muted text-muted-foreground hover:text-foreground'
                    }`}
                  >
                    {opt.label}
                  </button>
                ))}
              </div>
            </div>

            <div>
              <label className="text-xs text-muted-foreground">Entity Types</label>
              <div className="mt-1 flex flex-col gap-1">
                {[...ENTITY_TYPES].map((type) => (
                  <label key={type} className="flex items-center gap-2 text-xs text-foreground cursor-pointer">
                    <Checkbox
                      checked={filters.entityTypes.includes(type)}
                      onCheckedChange={(checked) => {
                        const newTypes = checked
                          ? [...filters.entityTypes, type]
                          : filters.entityTypes.filter((t) => t !== type);
                        updateFilter('entityTypes', newTypes);
                      }}
                    />
                    {type}
                  </label>
                ))}
              </div>
            </div>

            <Button variant="outline" size="sm" onClick={resetFilters} className="mt-1 w-full">
              <RotateCcw className="mr-1.5 h-3 w-3" />
              Reset Filters
            </Button>
          </div>
        )}
      </div>
    </div>
  );
}
