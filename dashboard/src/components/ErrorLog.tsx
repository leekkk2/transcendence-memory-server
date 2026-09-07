import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { api } from '../lib/api';

interface ErrorRow {
  id: number; ts: number; method: string; path: string; status: number;
  latency_ms: number; container: string | null; ua: string | null;
  error_type: string; error_detail: string | null; request_id: string | null;
}

export function ErrorLog() {
  const { t } = useTranslation();
  const [category, setCategory] = useState('other');
  const [window, setWindow] = useState('24h');
  const [offset, setOffset] = useState(0);
  const errors = useQuery<{ rows: ErrorRow[]; total: number }>({
    queryKey: ['usage-errors', window, category, offset],
    queryFn: () => api.get(`/admin/usage/errors?window=${window}&category=${category}&offset=${offset}&limit=25`),
    refetchInterval: 30000,
  });
  return <section id="errors" className="panel space-y-3 p-4">
    <div className="flex flex-wrap items-center gap-3">
      <h2>{t('usage.viewErrors')}</h2>
      <select aria-label={t('usage.errorCategory')} className="input text-xs" value={category} onChange={e => { setCategory(e.target.value); setOffset(0); }}>
        {['other', 'authenticated', 'unauthenticated_404', 'all'].map(k => <option key={k} value={k}>{t(`usage.categories.${k}`)}</option>)}
      </select>
      <select aria-label={t('usage.errorWindow')} className="input text-xs" value={window} onChange={e => { setWindow(e.target.value); setOffset(0); }}>
        {['1h', '24h', '7d', '30d'].map(k => <option key={k}>{k}</option>)}
      </select>
      <span className="text-dim text-xs">{errors.data?.total ?? 0}</span>
    </div>
    {errors.isError ? <p role="alert">{errors.error.message}</p> : null}
    {errors.isLoading ? <p>{t('common.loading')}</p> : null}
    {errors.data?.rows.length === 0 ? <p className="text-dim text-xs">{t('usage.noErrors')}</p> : null}
    {errors.data?.rows.map(row => <details key={row.id} className="border-b py-2 text-xs" style={{ borderColor: 'var(--border-soft)' }}>
      <summary className="cursor-pointer break-all font-mono">{new Date(row.ts).toLocaleString()} · {row.status} · {row.method} {row.path} · {row.container ?? '—'}</summary>
      <pre className="mt-2 whitespace-pre-wrap break-all">{row.error_detail ?? t('usage.historicalError', { status: row.status })}</pre>
      <p className="text-dim mt-2 break-all">{row.error_type} · {row.latency_ms} ms · {row.request_id ?? '—'} · {row.ua ?? '—'}</p>
    </details>)}
    <div className="flex gap-2">
      <button className="btn btn-ghost text-xs" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset-25))}>{t('usage.previous')}</button>
      <button className="btn btn-ghost text-xs" disabled={offset+25 >= (errors.data?.total ?? 0)} onClick={() => setOffset(offset+25)}>{t('usage.next')}</button>
    </div>
  </section>;
}
