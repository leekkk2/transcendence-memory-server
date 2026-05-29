import { Navigate } from 'react-router-dom';
import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { useMe } from '../lib/auth';

/**
 * Router-level auth gate. Reads `useMe()` — if the cookie session is valid
 * the children render; otherwise the user is bounced to /login. Loading
 * renders a minimal skeleton so the chrome doesn't flicker on cold-loads.
 */
export function ProtectedRoute({ children }: { children: ReactNode }) {
  const { t } = useTranslation();
  const { data, isLoading, error } = useMe();

  if (isLoading) {
    return (
      <div className="flex h-screen w-screen items-center justify-center">
        <div className="text-dim font-mono text-sm">{t('common.loading')}</div>
      </div>
    );
  }

  if (error || !data) {
    return <Navigate to="/login" replace />;
  }

  return <>{children}</>;
}
