/**
 * Session hooks — wrap the three /admin/ui/* endpoints behind TanStack Query
 * so login state stays consistent across the dashboard. Logging in / out
 * invalidates the 'me' query so the ProtectedRoute hook recovers instantly
 * without a page reload.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import { api, ApiError } from './api';

export interface SessionInfo {
  api_key_hash: string;
  expires_at: number;
  env: string;
}

export function useMe() {
  return useQuery<SessionInfo, ApiError>({
    queryKey: ['me'],
    queryFn: () => api.get<SessionInfo>('/admin/ui/me'),
    retry: false,
    staleTime: 60_000,
  });
}

export function useLogin() {
  const navigate = useNavigate();
  const qc = useQueryClient();
  return useMutation<SessionInfo, ApiError, string>({
    mutationFn: (apiKey) => api.post<SessionInfo>('/admin/ui/login', { api_key: apiKey }),
    onSuccess: (data) => {
      qc.setQueryData(['me'], data);
      navigate('/overview', { replace: true });
    },
  });
}

export function useLogout() {
  const navigate = useNavigate();
  const qc = useQueryClient();
  return useMutation<void, ApiError, void>({
    mutationFn: async () => {
      await api.post('/admin/ui/logout', {});
    },
    onSettled: () => {
      qc.removeQueries({ queryKey: ['me'] });
      navigate('/login', { replace: true });
    },
  });
}
