import { MetricCard } from '../components/MetricCard';
import { ProfileBreakerBadge, type BreakerState } from '../components/ProfileBreakerBadge';
import { formatNumber, formatUptime } from '../lib/format';
import { useContainers, useHealth, useProfiles, useUsageSummary } from '../lib/queries';

/**
 * Overview — one screen showing four headline KPIs, a queue/backlog summary,
 * breaker health, and the last 24h call-count snapshot. Every metric is a
 * straight read from a Lane B / Lane A endpoint; this page does no math
 * itself beyond formatting.
 */
export default function Overview() {
  const health = useHealth();
  const summary = useUsageSummary('24h');
  const containers = useContainers();
  const profiles = useProfiles();

  const profileList = readProfiles(profiles.data);

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4">
        <MetricCard
          title="Uptime"
          value={formatUptime(health.data?.uptime_seconds ?? 0)}
          sub={health.data?.worker_running ? 'worker online' : 'worker idle'}
        />
        <MetricCard
          title="Containers"
          value={formatNumber(containers.data?.containers?.length ?? 0)}
          sub={`${formatNumber(summary.data?.active_containers ?? 0)} active 24h`}
        />
        <MetricCard
          title="Today calls"
          value={formatNumber(summary.data?.total_calls ?? 0)}
          sub={`${formatNumber(summary.data?.total_errors ?? 0)} errors`}
          trend={(summary.data?.total_errors ?? 0) > 0 ? 'down' : 'up'}
        />
        <MetricCard
          title="p95 latency"
          value={`${formatNumber(summary.data?.p95_latency_ms ?? 0)} ms`}
          sub={`p50 ${formatNumber(summary.data?.p50_latency_ms ?? 0)} ms`}
        />
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <section className="panel p-4">
          <div className="text-dim mb-3 text-xs uppercase tracking-wider mono">
            top endpoints (24h)
          </div>
          <ul className="space-y-2 text-sm">
            {(summary.data?.top_endpoints ?? []).slice(0, 6).map((e) => (
              <li key={e.path} className="flex items-center justify-between">
                <span className="mono text-xs">{e.path}</span>
                <span className="mono text-dim">{formatNumber(e.calls)}</span>
              </li>
            ))}
            {(summary.data?.top_endpoints?.length ?? 0) === 0 ? (
              <li className="text-dim text-xs">no traffic recorded</li>
            ) : null}
          </ul>
        </section>

        <section className="panel p-4">
          <div className="text-dim mb-3 text-xs uppercase tracking-wider mono">
            breaker health
          </div>
          <ul className="space-y-2 text-sm">
            {profileList.length === 0 ? (
              <li className="text-dim text-xs">no profiles configured</li>
            ) : null}
            {profileList.map((p) => (
              <li key={p.name}>
                <ProfileBreakerBadge name={p.name} state={p.state} />
              </li>
            ))}
          </ul>
        </section>
      </div>

      {(health.data?.warnings?.length ?? 0) > 0 ? (
        <section className="panel p-4">
          <div className="text-dim mb-2 text-xs uppercase tracking-wider mono">warnings</div>
          <ul className="text-sm" style={{ color: 'var(--yellow)' }}>
            {health.data?.warnings.map((w) => (
              <li key={w} className="mono text-xs">
                · {w}
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  );
}

/**
 * Extract a flat list of `{ name, state }` from /admin/profiles' shape so
 * the page renders even when the server reports a slightly different schema
 * across embedding / reranker / llm / vlm.
 */
function readProfiles(data: unknown): { name: string; state: BreakerState }[] {
  if (!data || typeof data !== 'object') return [];
  const buckets = data as Record<string, unknown>;
  const out: { name: string; state: BreakerState }[] = [];
  for (const family of ['embedding', 'reranker', 'llm', 'vlm']) {
    const v = buckets[family];
    if (Array.isArray(v)) {
      for (const item of v) {
        if (item && typeof item === 'object') {
          const obj = item as { name?: string; breaker?: string; state?: string };
          out.push({
            name: `${family}/${obj.name ?? '?'}`,
            state: normaliseState(obj.breaker ?? obj.state),
          });
        }
      }
    }
  }
  return out;
}

function normaliseState(s: string | undefined): BreakerState {
  if (s === 'closed' || s === 'open' || s === 'half-open') return s;
  return 'unknown';
}
