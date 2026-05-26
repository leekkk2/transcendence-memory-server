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
  const headerCls = 'mono text-xs text-dim uppercase tracking-wider px-3 py-2 cursor-pointer select-none';
  return (
    <div className="panel overflow-hidden">
      <table className="w-full text-sm">
        <thead style={{ background: 'var(--bg)' }}>
          <tr>
            <th className={headerCls} style={{ textAlign: 'left' }}>Path</th>
            <th
              className={headerCls + ' text-right'}
              onClick={() => onSortChange('calls')}
            >
              Calls{sort === 'calls' ? ' ▾' : ''}
            </th>
            <th
              className={headerCls + ' text-right'}
              onClick={() => onSortChange('errors')}
            >
              Errors{sort === 'errors' ? ' ▾' : ''}
            </th>
            <th
              className={headerCls + ' text-right'}
              onClick={() => onSortChange('p95')}
            >
              p95{sort === 'p95' ? ' ▾' : ''}
            </th>
            <th className={headerCls + ' text-right'}>Containers</th>
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 ? (
            <tr>
              <td colSpan={5} className="px-3 py-6 text-center text-dim text-sm">
                no data
              </td>
            </tr>
          ) : null}
          {rows.map((r) => (
            <tr key={r.path} className="border-t" style={{ borderColor: 'var(--border)' }}>
              <td className="px-3 py-2 font-mono text-xs">{r.path}</td>
              <td className="px-3 py-2 text-right mono">{formatNumber(r.calls)}</td>
              <td
                className="px-3 py-2 text-right mono"
                style={{ color: (r.errors ?? 0) > 0 ? 'var(--red)' : 'var(--text-dim)' }}
              >
                {formatNumber(r.errors)}
              </td>
              <td className="px-3 py-2 text-right mono">{formatMs(r.p95_latency_ms)}</td>
              <td className="px-3 py-2 text-right mono">{formatNumber(r.distinct_containers)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
