import { useState } from 'react';
import { useTranslation } from 'react-i18next';
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
  const { t } = useTranslation();
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
      <h1 className="text-lg font-semibold">{t('usage.title')}</h1>

      <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
        <KPI title={t('usage.calls7d')} value={formatNumber(summary.data?.total_calls ?? 0)} />
        <KPI title={t('usage.errors')} value={formatNumber(summary.data?.total_errors ?? 0)} />
        <KPI title={t('usage.p95')} value={formatMs(summary.data?.p95_latency_ms ?? 0)} />
      </div>

      <EndpointTable rows={(data?.rows ?? []).map((r) => ({ ...r }))} sort={sort} onSortChange={setSort} />

      <section className="panel space-y-3 p-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="text-dim mono text-xs uppercase tracking-wider">
            {t('usage.timeseries')} · {selected || t('usage.pickEndpoint')}
          </div>
          <select
            value={selected}
            onChange={(e) => setSelected(e.target.value)}
            className="input mono text-xs"
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
          <div className="text-dim mono mb-2 text-xs uppercase tracking-wider">
            {t('usage.coldEndpoints')}
          </div>
          <ul className="mono space-y-1 text-xs">
            {data?.cold_endpoints?.map((c) => (
              <li key={c.path}>
                {c.path} <span className="text-dim">— {t('usage.callsUnit', { count: c.calls ?? 0 })}</span>
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
