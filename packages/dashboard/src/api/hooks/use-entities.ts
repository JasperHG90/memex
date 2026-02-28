import { useQuery } from '@tanstack/react-query';
import { api } from '../client.ts';
import type { EntityDTO, EntityMention, CooccurrenceRecord } from '../generated.ts';

interface UseEntitiesOptions {
  limit?: number;
  q?: string;
  sort?: '-mentions';
  vaultIds?: string[];
}

export function useEntities(options: UseEntitiesOptions = {}) {
  const params = new URLSearchParams();
  if (options.limit != null) params.set('limit', String(options.limit));
  if (options.q) params.set('q', options.q);
  if (options.sort) params.set('sort', options.sort);
  if (options.vaultIds?.length) {
    for (const id of options.vaultIds) params.append('vault_id', id);
  }
  const qs = params.toString();

  return useQuery({
    queryKey: ['entities', options],
    queryFn: () => api.get<EntityDTO[]>(`/entities${qs ? `?${qs}` : ''}`),
  });
}

export function useEntity(entityId: string | undefined) {
  return useQuery({
    queryKey: ['entities', entityId],
    queryFn: () => api.get<EntityDTO>(`/entities/${entityId}`),
    enabled: !!entityId,
  });
}

export function useEntityMentions(entityId: string | undefined, limit = 20) {
  return useQuery({
    queryKey: ['entities', entityId, 'mentions', { limit }],
    queryFn: () =>
      api.get<EntityMention[]>(`/entities/${entityId}/mentions?limit=${limit}`),
    enabled: !!entityId,
  });
}

export function useEntityCooccurrences(entityId: string | undefined) {
  return useQuery({
    queryKey: ['entities', entityId, 'cooccurrences'],
    queryFn: () =>
      api.get<CooccurrenceRecord[]>(`/entities/${entityId}/cooccurrences`),
    enabled: !!entityId,
  });
}

export function useBulkCooccurrences(entityIds: string[], vaultIds?: string[]) {
  const ids = entityIds.join(',');
  return useQuery({
    queryKey: ['cooccurrences', 'bulk', ids, vaultIds],
    queryFn: () => {
      const params = new URLSearchParams();
      params.set('ids', ids);
      if (vaultIds?.length) {
        for (const vid of vaultIds) params.append('vault_id', vid);
      }
      return api.get<CooccurrenceRecord[]>(`/cooccurrences?${params}`);
    },
    enabled: entityIds.length > 0,
  });
}

export function useEntityLineage(
  entityId: string | undefined,
  options?: { direction?: string; depth?: number; limit?: number },
) {
  const params = new URLSearchParams();
  if (options?.direction) params.set('direction', options.direction);
  if (options?.depth != null) params.set('depth', String(options.depth));
  if (options?.limit != null) params.set('limit', String(options.limit));
  const qs = params.toString();

  return useQuery({
    queryKey: ['entities', entityId, 'lineage', options],
    queryFn: () =>
      api.get(`/entities/${entityId}/lineage${qs ? `?${qs}` : ''}`),
    enabled: !!entityId,
  });
}
