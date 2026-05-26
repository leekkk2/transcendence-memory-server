/**
 * TanStack Query key registry + thin fetchers for the protected endpoints.
 *
 * Centralising keys here means a write somewhere can invalidate the right
 * subset without each page re-deriving the cache shape. Polling intervals
 * (Overview 10s, Jobs 10s, Usage 5min cache) live with the hooks so it's
 * obvious which views fire requests at which cadence.
 */
import { useQuery } from '@tanstack/react-query';
import { api } from './api';

export interface HealthResponse {
  status: string;
  service: string;
  architecture: string;
  build_flavor: 'lite' | 'full';
  multimodal_capable: boolean;
  uptime_seconds: number;
  worker_running: boolean;
  accepting_ingest: boolean;
  warnings: string[];
}

export interface UsageSummary {
  window: string;
  total_calls: number;
  total_errors: number;
  error_rate: number;
  p50_latency_ms: number;
  p95_latency_ms: number;
  active_containers: number;
  active_api_keys: number;
  top_endpoints: { path: string; calls: number; p95: number }[];
}

export interface ContainerListItem {
  name: string;
  memory_count?: number;
  last_active?: number;
}

export interface JobsResponse {
  jobs: Array<{
    job_id: string;
    type: string;
    container: string;
    status: string;
    created_at: number;
    progress?: number;
  }>;
}

export function useHealth(refetchMs = 10_000) {
  return useQuery<HealthResponse>({
    queryKey: ['health'],
    queryFn: () => api.get('/health'),
    refetchInterval: refetchMs,
    staleTime: refetchMs / 2,
  });
}

export function useSystemHealth(refetchMs = 10_000) {
  return useQuery<Record<string, unknown>>({
    queryKey: ['system-health'],
    queryFn: () => api.get('/admin/system-health'),
    refetchInterval: refetchMs,
    staleTime: refetchMs / 2,
  });
}

export function useUsageSummary(window: string = '24h') {
  return useQuery<UsageSummary>({
    queryKey: ['usage', 'summary', window],
    queryFn: () => api.get(`/admin/usage/summary?window=${encodeURIComponent(window)}`),
    refetchInterval: 30_000,
  });
}

export function useUsageEndpoints(window: string = '7d', sort: 'calls' | 'errors' | 'p95' = 'calls') {
  return useQuery({
    queryKey: ['usage', 'endpoints', window, sort],
    queryFn: () => api.get(`/admin/usage/endpoints?window=${window}&sort=${sort}`),
    staleTime: 5 * 60_000,
  });
}

export function useUsageContainers(window: string = '7d') {
  return useQuery({
    queryKey: ['usage', 'containers', window],
    queryFn: () => api.get(`/admin/usage/containers?window=${window}`),
    staleTime: 5 * 60_000,
  });
}

export function useUsageTimeseries(path: string, window: string = '7d', bucket: '5m' | '1h' | '1d' = '1h') {
  return useQuery({
    queryKey: ['usage', 'timeseries', path, window, bucket],
    queryFn: () =>
      api.get(`/admin/usage/timeseries?path=${encodeURIComponent(path)}&window=${window}&bucket=${bucket}`),
    enabled: !!path,
    staleTime: 60_000,
  });
}

export function useContainers() {
  return useQuery<{ containers: ContainerListItem[] }>({
    queryKey: ['containers'],
    queryFn: () => api.get('/containers'),
    staleTime: 30_000,
  });
}

export function useJobs(refetchMs = 10_000) {
  return useQuery<JobsResponse>({
    queryKey: ['jobs'],
    queryFn: () => api.get('/jobs'),
    refetchInterval: refetchMs,
  });
}

export function useProfiles() {
  return useQuery({
    queryKey: ['profiles'],
    queryFn: () => api.get('/admin/profiles'),
    staleTime: 60_000,
  });
}
