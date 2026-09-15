import { useQuery } from '@tanstack/react-query';
import { api } from './api';

export interface DocumentItem {
  id: string;
  status: string;
  source: string;
  kind: string;
  summary: string;
  chars: number;
  chunks: number;
  created_at: string;
  updated_at: string;
  error: string;
  duplicate: boolean;
}
export interface DocumentList {
  documents: DocumentItem[];
  total: number;
  counts: Record<string, number>;
  duplicate_count: number;
  storage_state: string;
  offset: number;
  limit: number;
}
export interface DocumentDetail extends Omit<DocumentItem, 'chunks'> {
  text: string;
  text_length: number;
  text_offset: number;
  text_truncated: boolean;
  chunks: { id: string; content: string; order: number; tokens: number }[];
  chunks_total: number;
  chunk_offset: number;
}
export interface GraphNode { id: string; label: string; type: string; description: string }
export interface GraphEdge { id: string; source: string; target: string; description: string; keywords: string }
export interface GraphSnapshot {
  nodes: GraphNode[];
  edges: GraphEdge[];
  node_count: number;
  edge_count: number;
  entity_types: Record<string, number>;
  sampled: boolean;
  storage_state: string;
}
export interface QueryAnswer {
  status: string;
  answer: string;
  citations?: unknown[];
  mode: string;
  container: string;
}
export interface IngestReceipt { pid: number; status: string; background: boolean }

const base = (container: string) => `/admin/containers/${encodeURIComponent(container)}`;

export function useDocuments(container: string, status: string, kind: string, q: string, offset: number, duplicates: boolean) {
  const params = new URLSearchParams({ status, kind, q, offset: String(offset), limit: '20', include_duplicates: String(duplicates) });
  return useQuery<DocumentList>({
    queryKey: ['documents', container, status, kind, q, offset, duplicates],
    queryFn: () => api.get(`${base(container)}/documents?${params}`),
    enabled: !!container, refetchInterval: 10_000, retry: 1,
  });
}
export function useDocument(container: string, id: string, textOffset: number, chunkOffset: number) {
  return useQuery<DocumentDetail>({
    queryKey: ['document', container, id, textOffset, chunkOffset],
    queryFn: () => api.get(`${base(container)}/documents/${encodeURIComponent(id)}?text_offset=${textOffset}&chunk_offset=${chunkOffset}`),
    enabled: !!container && !!id, retry: 1, refetchInterval: 15_000,
  });
}
export function useGraph(container: string, q: string) {
  return useQuery<GraphSnapshot>({
    queryKey: ['graph', container, q],
    queryFn: () => api.get(`${base(container)}/graph?q=${encodeURIComponent(q)}`),
    enabled: !!container, retry: 1, staleTime: 30_000,
  });
}
export function useDocumentConfig() {
  return useQuery<{ max_upload_bytes: number; text_page_chars: number }>({
    queryKey: ['document-config'], queryFn: () => api.get('/admin/documents/config'), staleTime: 60_000,
  });
}
export function documentStatusClass(status: string) {
  if (status === 'processed' || status === 'done') return 'badge badge-green';
  if (status === 'failed') return 'badge badge-red';
  if (status === 'pending') return 'badge badge-yellow';
  return 'badge badge-cyan';
}
