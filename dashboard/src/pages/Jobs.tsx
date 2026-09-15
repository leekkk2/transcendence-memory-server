import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { api } from '../lib/api';
import { useTranslation } from 'react-i18next';
import { useJobs } from '../lib/queries';
import { formatNumber, formatRelative } from '../lib/format';

/**
 * Jobs / queue view. Reads the real /jobs shape (id / op / enqueued_at) — the
 * earlier code read job_id / type / created_at, leaving the Job + Type columns
 * blank and Created at "—". Counts come from the server's `stats` block so the
 * totals reflect the whole queue, not just the rows on this page.
 */
const STATUSES = ['pending', 'running', 'done', 'failed', 'cancelled'] as const;

function statusBadgeClass(status: string): string {
  switch (status) {
    case 'done':
      return 'badge badge-green';
    case 'failed':
      return 'badge badge-red';
    case 'running':
      return 'badge badge-cyan';
    case 'pending':
      return 'badge badge-yellow';
    default:
      return 'badge badge-dim';
  }
}

export default function Jobs() {
  const { t } = useTranslation();
  const { data, isLoading, error } = useJobs();
  const cache = useQueryClient();
  const [confirmId, setConfirmId] = useState<number | null>(null);
  const cancel = useMutation({
    mutationFn: (id: number) => api.del('/jobs/' + id),
    onSuccess: () => { setConfirmId(null); void cache.invalidateQueries({ queryKey: ['jobs'] }); },
  });

  const rows = data?.jobs ?? [];
  // Prefer server-wide stats; fall back to counting the visible page.
  const counts: Record<string, number> =
    data?.stats ??
    rows.reduce<Record<string, number>>((acc, j) => {
      acc[j.status] = (acc[j.status] ?? 0) + 1;
      return acc;
    }, {});

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between gap-3">
        <h1 className="text-lg font-semibold">{t('jobs.title')}</h1>
        <span className={data?.worker_running ? 'badge badge-green' : 'badge badge-dim'}>
          <span className="dot" />
          {data?.worker_running ? t('jobs.workerRunning') : t('jobs.workerStopped')}
        </span>
      </div>

      <div className="flex flex-wrap gap-2.5">
        {STATUSES.map((status) => (
          <div key={status} className="panel mono flex items-center gap-2 px-3 py-2 text-xs">
            <span className="text-dim">{t(`jobs.${status}`)}</span>
            <span className="font-semibold">{formatNumber(counts[status] ?? 0)}</span>
          </div>
        ))}
      </div>

      {error && <p role="alert" className="text-sm text-red">{error.message}</p>}
      {cancel.isError && <p role="alert" className="text-sm text-red">{cancel.error.message}</p>}
      <p className="text-dim text-xs">{t('jobs.cancelHint')}</p>
      <div className="panel overflow-x-auto">
        <table className="tbl">
          <thead>
            <tr>
              <th>{t('jobs.colJob')}</th>
              <th>{t('jobs.colType')}</th>
              <th>{t('jobs.colContainer')}</th>
              <th>{t('jobs.colStatus')}</th>
              <th className="text-right">{t('jobs.colAttempts')}</th>
              <th className="text-right">{t('jobs.colCreated')}</th>
              <th>{t('jobs.actions')}</th>
            </tr>
          </thead>
          <tbody>
            {isLoading ? (
              <tr>
                <td colSpan={7} className="text-dim py-8 text-center text-sm">
                  {t('common.loading')}
                </td>
              </tr>
            ) : rows.length === 0 ? (
              <tr>
                <td colSpan={7} className="text-dim py-8 text-center text-sm">
                  {t('jobs.noJobs')}
                </td>
              </tr>
            ) : (
              rows.map((j) => (
                <tr key={j.id}>
                  <td className="mono text-xs">{j.id}</td>
                  <td className="text-xs">{j.label || j.op}{j.last_error && <details className="mt-2 max-w-md"><summary className="cursor-pointer text-red">{t('jobs.errorDetail')}</summary><pre className="mt-2 whitespace-pre-wrap break-words font-sans text-xs text-red">{j.last_error}</pre></details>}</td>
                  <td className="mono text-xs">{j.container}</td>
                  <td>
                    <span className={statusBadgeClass(j.status)}>
                      <span className="dot" />
                      {t(`jobs.${j.status}`, { defaultValue: j.status })}
                    </span>
                  </td>
                  <td className="mono text-dim text-right text-xs">
                    {j.attempts ?? 0}/{j.max_attempts ?? '—'}
                  </td>
                  <td className="mono text-dim text-right text-xs">{formatRelative(j.enqueued_at)}</td>
                  <td>{j.status === 'pending' && (confirmId === j.id ? <div className="flex gap-2"><button className="btn text-red" disabled={cancel.isPending} onClick={() => cancel.mutate(j.id)}>{t('jobs.confirmCancel')}</button><button className="btn" disabled={cancel.isPending} onClick={() => setConfirmId(null)}>{t('jobs.keepJob')}</button></div> : <button className="btn whitespace-nowrap" onClick={() => setConfirmId(j.id)}>{t('jobs.cancelJob')}</button>)}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
