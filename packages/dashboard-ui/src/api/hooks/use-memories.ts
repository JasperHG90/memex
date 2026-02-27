import { useMutation, useQuery } from '@tanstack/react-query';
import { api } from '../client.ts';
import type {
  MemoryUnitDTO,
  RetrievalRequest,
  LineageResponse,
} from '../generated.ts';

export function useMemory(unitId: string | undefined) {
  return useQuery({
    queryKey: ['memories', unitId],
    queryFn: () => api.get<MemoryUnitDTO>(`/memories/${unitId}`),
    enabled: !!unitId,
  });
}

export function useMemorySearch() {
  return useMutation({
    mutationFn: (request: RetrievalRequest) =>
      api.post<MemoryUnitDTO[]>('/memories/search', request),
  });
}

export function useMemoryLineage(
  unitId: string | undefined,
  options?: { direction?: string; depth?: number; limit?: number },
) {
  const params = new URLSearchParams();
  if (options?.direction) params.set('direction', options.direction);
  if (options?.depth != null) params.set('depth', String(options.depth));
  if (options?.limit != null) params.set('limit', String(options.limit));
  const qs = params.toString();

  return useQuery({
    queryKey: ['memories', unitId, 'lineage', options],
    queryFn: () =>
      api.get<LineageResponse>(`/notes/${unitId}/lineage${qs ? `?${qs}` : ''}`),
    enabled: !!unitId,
  });
}
