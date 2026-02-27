import { useQuery } from '@tanstack/react-query';
import { api } from '../client.ts';
import type { EntityDTO, LineageResponse } from '../generated.ts';
export type { LineageResponse, EntityDTO };

export function useLineage(
  id: string | null,
  type: string = 'entity',
  depth: number = 4,
) {
  return useQuery({
    queryKey: ['lineage', type, id, depth],
    queryFn: () =>
      api.get<LineageResponse>(
        `/lineage/${type}/${id}?direction=upstream&depth=${depth}&limit=10`,
      ),
    enabled: !!id,
  });
}

export function useEntitySearch(query: string, enabled: boolean = true) {
  return useQuery({
    queryKey: ['entities', 'search', query],
    queryFn: async (): Promise<EntityDTO[]> => {
      const params = new URLSearchParams({ limit: '10' });
      if (query && query.length >= 2) {
        params.set('q', query);
        params.set('limit', '100');
      } else {
        params.set('sort', '-mentions');
      }

      const items = await api.get<EntityDTO[]>(`/entities?${params}`);

      // Client-side filter if query provided (server may not filter precisely)
      if (query && query.length >= 2) {
        const q = query.toLowerCase();
        return items.filter((e) => e.name.toLowerCase().includes(q)).slice(0, 10);
      }

      return items.slice(0, 10);
    },
    enabled,
    staleTime: 30_000,
  });
}
