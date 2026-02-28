import { useState, useCallback, useEffect } from 'react';
import { useSearchParams } from 'react-router-dom';
import { GitBranch, AlertCircle } from 'lucide-react';
import { useLineage } from '@/api/hooks/use-lineage';
import type { EntityDTO } from '@/api/generated';
import { LineageGraph } from './lineage/lineage-graph';
import { EntitySearch } from './lineage/entity-search';

function flattenDict(
  obj: Record<string, unknown>,
  parentKey: string = '',
  sep: string = '.',
): Record<string, string> {
  const result: Record<string, string> = {};
  for (const [k, v] of Object.entries(obj)) {
    const newKey = parentKey ? `${parentKey}${sep}${k}` : k;
    if (v && typeof v === 'object' && !Array.isArray(v)) {
      Object.assign(result, flattenDict(v as Record<string, unknown>, newKey, sep));
    } else if (Array.isArray(v)) {
      result[newKey] = JSON.stringify(v);
    } else {
      result[newKey] = String(v ?? '');
    }
  }
  return result;
}

interface NodeDetail {
  id: string;
  raw: Record<string, string>;
}

export default function LineagePage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [selectedId, setSelectedId] = useState<string | null>(
    searchParams.get('unit') ?? searchParams.get('id') ?? null,
  );
  const [entityType, setEntityType] = useState<string>(
    searchParams.get('type') ?? 'entity',
  );
  const [detail, setDetail] = useState<NodeDetail | null>(null);

  const { data: lineage, isLoading, error } = useLineage(selectedId, entityType);

  // Sync URL params with selection
  useEffect(() => {
    if (selectedId) {
      setSearchParams({ id: selectedId, type: entityType }, { replace: true });
    }
  }, [selectedId, entityType, setSearchParams]);

  const handleEntitySelect = useCallback((entity: EntityDTO) => {
    setSelectedId(entity.id);
    setEntityType('entity');
    setDetail(null);
  }, []);

  const handleNodeClick = useCallback((_nodeId: string, data: Record<string, unknown>) => {
    const raw = { ...data };
    delete raw.embedding;

    const flat = flattenDict(raw);
    const truncated: Record<string, string> = {};
    for (const [k, v] of Object.entries(flat)) {
      truncated[k] = v.length > 500 ? v.slice(0, 500) + '...' : v;
    }

    setDetail({ id: _nodeId, raw: truncated });
  }, []);

  return (
    <div className="flex flex-col gap-4 h-full">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <GitBranch size={24} className="text-primary" />
          <h1 className="text-2xl font-bold text-foreground">Lineage</h1>
        </div>
        <EntitySearch onSelect={handleEntitySelect} />
      </div>

      <div className="flex-1 min-h-0 relative" style={{ minHeight: 500 }}>
        {!selectedId && (
          <div className="absolute inset-0 flex items-center justify-center rounded-lg border border-border bg-card">
            <div className="text-center space-y-2">
              <GitBranch size={40} className="mx-auto text-muted-foreground" />
              <p className="text-muted-foreground">
                Search for an entity to explore its lineage
              </p>
            </div>
          </div>
        )}

        {isLoading && selectedId && (
          <div className="absolute inset-0 flex items-center justify-center rounded-lg border border-border bg-card">
            <div className="text-center space-y-2">
              <div className="w-8 h-8 border-2 border-primary border-t-transparent rounded-full animate-spin mx-auto" />
              <p className="text-muted-foreground text-sm">Loading lineage...</p>
            </div>
          </div>
        )}

        {error && (
          <div className="absolute inset-0 flex items-center justify-center rounded-lg border border-border bg-card">
            <div className="text-center space-y-2">
              <AlertCircle size={40} className="mx-auto text-destructive" />
              <p className="text-muted-foreground">Failed to load lineage data</p>
              <p className="text-xs text-muted-foreground">{String(error)}</p>
            </div>
          </div>
        )}

        {lineage && !isLoading && <LineageGraph lineage={lineage} onNodeClick={handleNodeClick} />}
      </div>

      {detail && (
        <div className="border border-border rounded-lg bg-card p-4 max-h-64 overflow-y-auto">
          <div className="flex items-center justify-between mb-3">
            <h3 className="text-sm font-semibold text-foreground">Node Details</h3>
            <button
              onClick={() => setDetail(null)}
              className="text-xs text-muted-foreground hover:text-foreground cursor-pointer"
            >
              Close
            </button>
          </div>
          <p className="text-xs text-muted-foreground mb-2 font-mono">{detail.id}</p>
          <table className="w-full text-xs">
            <tbody>
              {Object.entries(detail.raw)
                .sort(([a], [b]) => a.localeCompare(b))
                .map(([key, value]) => (
                  <tr key={key} className="border-b border-border">
                    <td className="py-1 pr-3 font-medium text-muted-foreground whitespace-nowrap align-top">
                      {key}
                    </td>
                    <td className="py-1 text-foreground break-all">{value}</td>
                  </tr>
                ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
