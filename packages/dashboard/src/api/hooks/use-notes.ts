import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../client.ts';
import type {
  NoteDTO,
  NoteSearchResult,
  NoteSearchRequest,
  NoteCreateDTO,
  IngestResponse,
} from '../generated.ts';

interface UseNotesOptions {
  limit?: number;
  offset?: number;
  sort?: '-created_at';
  vaultIds?: string[];
}

export function useNotes(options: UseNotesOptions = {}) {
  const params = new URLSearchParams();
  if (options.limit != null) params.set('limit', String(options.limit));
  if (options.offset != null) params.set('offset', String(options.offset));
  if (options.sort) params.set('sort', options.sort);
  if (options.vaultIds?.length) {
    for (const id of options.vaultIds) params.append('vault_id', id);
  }
  const qs = params.toString();

  return useQuery({
    queryKey: ['notes', options],
    queryFn: () => api.get<NoteDTO[]>(`/notes${qs ? `?${qs}` : ''}`),
  });
}

export function useNote(noteId: string | undefined) {
  return useQuery({
    queryKey: ['notes', noteId],
    queryFn: () => api.get<NoteDTO>(`/notes/${noteId}`),
    enabled: !!noteId,
  });
}

export function useNotePageIndex(noteId: string | undefined) {
  return useQuery({
    queryKey: ['notes', noteId, 'page-index'],
    queryFn: () =>
      api.get<{ note_id: string; page_index: unknown[] }>(`/notes/${noteId}/page-index`),
    enabled: !!noteId,
  });
}

export function useNoteSearch() {
  return useMutation({
    mutationFn: (request: NoteSearchRequest) =>
      api.post<NoteSearchResult[]>('/notes/search', request),
  });
}

export function useIngestNote() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (request: NoteCreateDTO) =>
      api.post<IngestResponse>('/ingestions', request),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['notes'] });
      void queryClient.invalidateQueries({ queryKey: ['stats'] });
    },
  });
}

export function useDeleteNote() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (noteId: string) =>
      api.delete<{ status: string }>(`/notes/${noteId}`),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['notes'] });
      void queryClient.invalidateQueries({ queryKey: ['stats'] });
    },
  });
}
