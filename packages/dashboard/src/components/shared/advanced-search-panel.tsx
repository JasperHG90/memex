import { useState } from 'react';
import { ChevronDown, ChevronUp } from 'lucide-react';
import { Slider } from '@/components/ui/slider';
import { Switch } from '@/components/ui/switch';
import { Input } from '@/components/ui/input';

interface AdvancedSearchParams {
  minScore: number | null;
  tokenBudget: number | null;
  expandQuery: boolean;
}

interface AdvancedSearchPanelProps {
  params: AdvancedSearchParams;
  onChange: (params: AdvancedSearchParams) => void;
}

export type { AdvancedSearchParams };

export function AdvancedSearchPanel({ params, onChange }: AdvancedSearchPanelProps) {
  const [isOpen, setIsOpen] = useState(false);

  return (
    <div className="rounded-lg border border-border bg-card">
      <button
        type="button"
        onClick={() => setIsOpen(!isOpen)}
        className="flex w-full items-center justify-between px-4 py-2.5 text-xs font-medium text-muted-foreground hover:text-foreground transition-colors"
      >
        Advanced Options
        {isOpen ? <ChevronUp className="h-3.5 w-3.5" /> : <ChevronDown className="h-3.5 w-3.5" />}
      </button>

      {isOpen && (
        <div className="border-t border-border px-4 py-3 space-y-4">
          {/* Min Score */}
          <div>
            <label className="text-xs text-muted-foreground">
              Min Score: {params.minScore != null ? params.minScore.toFixed(2) : 'None'}
            </label>
            <Slider
              min={0}
              max={1}
              step={0.05}
              value={[params.minScore ?? 0]}
              onValueChange={([v]) => onChange({ ...params, minScore: v > 0 ? v : null })}
              className="mt-1"
            />
          </div>

          {/* Token Budget */}
          <div>
            <label className="text-xs text-muted-foreground">Token Budget</label>
            <Input
              type="number"
              placeholder="No limit"
              value={params.tokenBudget ?? ''}
              onChange={(e) => onChange({
                ...params,
                tokenBudget: e.target.value ? parseInt(e.target.value, 10) : null,
              })}
              className="mt-1 h-8 text-xs"
            />
          </div>

          {/* Expand Query */}
          <div className="flex items-center justify-between">
            <label className="text-xs text-muted-foreground">Expand Query (LLM)</label>
            <Switch
              checked={params.expandQuery}
              onCheckedChange={(checked) => onChange({ ...params, expandQuery: checked })}
            />
          </div>
        </div>
      )}
    </div>
  );
}
