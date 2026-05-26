import { useState } from 'react';
import { EndpointTable } from '../components/EndpointTable';
import { TimeseriesChart } from '../components/TimeseriesChart';
import { useUsageEndpoints, useUsageSummary, useUsageTimeseries } from '../lib/queries';
import { formatMs, formatNumber } from '../lib/format';

/**
 * Usage analytics — top endpoints, drill-down timeseries, cold endpoints.
 * The page never writes anything; it's pure consumption of Lane B's
 * /admin/usage/* surface.
 */
export default function Usage() {
  const [sort, setSort] = useState<'calls' | 'errors' | 'p95'>('calls');
  const [selected, setSelected] = useState<string>('');
  const summary = useUsageSummary('7d');
  const endpoints = useUsageEndpoints('7d', sort);
  const ts = useUsageTimeseries(selected, '7d', '1h');

  const data = endpoints.data as
    | { rows?: Row[]; cold_endpoints?: { path: string; calls?: number }[] }
    | undefined;

  return (
    <div className="space-y-6">
      <h1 className="text-lg font-semibold">Usage analytics (7d)</h1>

      <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
        <KPI title="Calls (7d)" value={formatNumber(summary.data?.total_calls ?? 0)} />
        <KPI title="Errors" value={formatNumber(summary.data?.total_errors ?? 0)} />
        <KPI title="p95" value={formatMs(summary.data?.p95_latency_ms ?? 0)} />
      </div>

      <EndpointTable
        rows={(data?.rows ?? []).map((r) => ({ ...r }))}
        sort={sort}
        onSortChange={setSort}
      />

      <section className="panel p-4 space-y-3">
        <div className="flex items-center justify-between">
          <div className="text-dim text-xs uppercase tracking-wider mono">
            timeseries · {selected || '(pick an endpoint)'}
          </div>
          <select
            value={selected}
            onChange={(e) => setSelected(e.target.value)}
            className="rounded border px-2 py-1 mono text-xs"
            style={{ background: 'var(--bg-elev)', borderColor: 'var(--border)' }}
          >
            <option value="">—</option>
            {(data?.rows ?? []).slice(0, 20).map((r) => (
              <option key={r.path} value={r.path}>
                {r.path}
              </option>
            ))}
          </select>
        </div>
        {selected ? <TimeseriesChart points={readPoints(ts.data)} /> : null}
      </section>

      {(data?.cold_endpoints ?? []).length > 0 ? (
        <section className="panel p-4">
          <div className="text-dim mb-2 text-xs uppercase tracking-wider mono">
            cold endpoints (low call volume — review for deletion)
          </div>
          <ul className="text-xs mono space-y-1">
            {data?.cold_endpoints?.map((c) => (
              <li key={c.path}>
                {c.path}{' '}
                <span className="text-dim">— {formatNumber(c.calls)} calls</span>
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  );
}

interface Row {
  path: string;
  calls?: number;
  errors?: number;
  p50_latency_ms?: number;
  p95_latency_ms?: number;
  distinct_containers?: number;
}

function KPI({ title, value }: { title: string; value: string }) {
  return (
    <div className="panel p-4">
      <div className="text-dim text-xs uppercase tracking-wider mono">{title}</div>
      <div className="mt-2 text-xl font-semibold">{value}</div>
    </div>
  );
}

function readPoints(data: unknown): { ts: number; calls?: number; p95?: number }[] {
  if (!data || typeof data !== 'object') return [];
  const p = (data as { points?: unknown }).points;
  if (Array.isArray(p)) return p as { ts: number; calls?: number; p95?: number }[];
  return [];
}
