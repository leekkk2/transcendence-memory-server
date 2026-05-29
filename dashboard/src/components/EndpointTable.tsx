import { useTranslation } from 'react-i18next';
import { formatMs, formatNumber } from '../lib/format';

interface Row {
  path: string;
  calls?: number;
  errors?: number;
  p50_latency_ms?: number;
  p95_latency_ms?: number;
  distinct_containers?: number;
  last_called_at?: number | null;
}

/**
 * Usage > Top endpoints table. Stateless — sorting is driven by the parent
 * which re-issues the underlying query with `sort=` swapped.
 */
export function EndpointTable({
  rows,
  onSortChange,
  sort,
}: {
  rows: Row[];
  sort: 'calls' | 'errors' | 'p95';
  onSortChange: (s: 'calls' | 'errors' | 'p95') => void;
}) {
  const { t } = useTranslation();
  const arrow = (key: 'calls' | 'errors' | 'p95') => (sort === key ? ' ▾' : '');

  return (
    <div className="panel overflow-x-auto">
      <table className="tbl">
        <thead>
          <tr>
            <th>{t('usage.colPath')}</th>
            <th className="cursor-pointer select-none text-right" onClick={() => onSortChange('calls')}>
              {t('usage.colCalls')}
              {arrow('calls')}
            </th>
            <th className="cursor-pointer select-none text-right" onClick={() => onSortChange('errors')}>
              {t('usage.colErrors')}
              {arrow('errors')}
            </th>
            <th className="cursor-pointer select-none text-right" onClick={() => onSortChange('p95')}>
              {t('usage.colP95')}
              {arrow('p95')}
            </th>
            <th className="text-right">{t('usage.colContainers')}</th>
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 ? (
            <tr>
              <td colSpan={5} className="text-dim py-8 text-center text-sm">
                {t('usage.noData')}
              </td>
            </tr>
          ) : null}
          {rows.map((r) => (
            <tr key={r.path}>
              <td className="mono text-xs">{r.path}</td>
              <td className="mono text-right">{formatNumber(r.calls)}</td>
              <td
                className="mono text-right"
                style={{ color: (r.errors ?? 0) > 0 ? 'var(--red)' : 'var(--text-dim)' }}
              >
                {formatNumber(r.errors)}
              </td>
              <td className="mono text-right">{formatMs(r.p95_latency_ms)}</td>
              <td className="mono text-right">{formatNumber(r.distinct_containers)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
