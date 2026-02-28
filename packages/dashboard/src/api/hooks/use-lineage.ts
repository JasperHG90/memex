import { useQuery } from '@tanstack/react-query';
import { z } from 'zod';
import { api } from '../client.ts';
import { EntityDTO, LineageResponse } from '../generated.ts';
import { validateResponse } from '../validate.ts';
export type { LineageResponse };
export type { EntityDTO };

const EntityArraySchema = z.array(EntityDTO);

export function useLineage(
  id: string | null,
  type: string = 'entity',
  depth: number = 4,
) {
  return useQuery({
    queryKey: ['lineage', type, id, depth],
    queryFn: async () => {
      const data = await api.get<z.infer<typeof LineageResponse>>(
        `/lineage/${type}/${id}?direction=upstream&depth=${depth}&limit=10`,
      );
      return validateResponse(LineageResponse, data);
    },
    enabled: !!id,
  });
}

export function useEntitySearch(query: string, enabled: boolean = true) {
  return useQuery({
    queryKey: ['entities', 'search', query],
    queryFn: async (): Promise<z.infer<typeof EntityDTO>[]> => {
      const params = new URLSearchParams({ limit: '10' });
      if (query && query.length >= 2) {
        params.set('q', query);
        params.set('limit', '100');
      } else {
        params.set('sort', '-mentions');
      }

      const raw = await api.get<z.infer<typeof EntityDTO>[]>(`/entities?${params}`);
      const items = validateResponse(EntityArraySchema, raw);

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
