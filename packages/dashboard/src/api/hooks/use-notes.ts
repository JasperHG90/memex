import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { z } from 'zod';
import { api } from '../client.ts';
import {
  NoteDTO,
  NoteSearchResult,
  IngestResponse,
  type NoteSearchRequest,
  type NoteCreateDTO,
} from '../generated.ts';
import { validateResponse } from '../validate.ts';

const NoteArraySchema = z.array(NoteDTO);
const NoteSearchResultArraySchema = z.array(NoteSearchResult);

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
    queryFn: async () => {
      const data = await api.get<NoteDTO[]>(`/notes${qs ? `?${qs}` : ''}`);
      return validateResponse(NoteArraySchema, data);
    },
  });
}

export function useNote(noteId: string | undefined) {
  return useQuery({
    queryKey: ['notes', noteId],
    queryFn: async () => {
      const data = await api.get<NoteDTO>(`/notes/${noteId}`);
      return validateResponse(NoteDTO, data);
    },
    enabled: !!noteId,
  });
}

export function useNotePageIndex(noteId: string | undefined) {
  return useQuery({
    queryKey: ['notes', noteId, 'page-index'],
    queryFn: () =>
      api.get<{ note_id: string; page_index: { metadata?: Record<string, unknown>; toc?: unknown[] } | unknown[] | null }>(`/notes/${noteId}/page-index`),
    enabled: !!noteId,
  });
}

export function useNoteSearch() {
  return useMutation({
    mutationFn: async (request: NoteSearchRequest) => {
      const data = await api.post<NoteSearchResult[]>('/notes/search', request);
      return validateResponse(NoteSearchResultArraySchema, data);
    },
  });
}

export function useIngestNote() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (request: NoteCreateDTO) => {
      const data = await api.post<IngestResponse>('/ingestions', request);
      return validateResponse(IngestResponse, data);
    },
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
