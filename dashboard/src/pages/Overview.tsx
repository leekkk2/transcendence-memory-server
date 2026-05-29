import { useTranslation } from 'react-i18next';
import { MetricCard } from '../components/MetricCard';
import { ProfileList } from '../components/ProfileList';
import { formatNumber, formatUptime } from '../lib/format';
import { useContainers, useHealth, useProfiles, useUsageSummary } from '../lib/queries';

/**
 * Overview — four headline KPIs, a top-endpoints snapshot, the model-profile
 * panel, and any active warnings. Every metric is a straight read from a
 * backend endpoint; this page does no math beyond formatting.
 */
export default function Overview() {
  const { t } = useTranslation();
  const health = useHealth();
  const summary = useUsageSummary('24h');
  const containers = useContainers();
  const profiles = useProfiles();

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4">
        <MetricCard
          title={t('overview.uptime')}
          value={formatUptime(health.data?.uptime_seconds ?? 0)}
          sub={health.data?.worker_running ? t('overview.workerOnline') : t('overview.workerIdle')}
          trend={health.data?.worker_running ? 'up' : 'flat'}
        />
        <MetricCard
          title={t('overview.containers')}
          value={formatNumber(containers.data?.containers?.length ?? 0)}
          sub={t('overview.activeContainers', { count: summary.data?.active_containers ?? 0 })}
        />
        <MetricCard
          title={t('overview.todayCalls')}
          value={formatNumber(summary.data?.total_calls ?? 0)}
          sub={t('overview.errorsSub', { count: summary.data?.total_errors ?? 0 })}
          trend={(summary.data?.total_errors ?? 0) > 0 ? 'down' : 'up'}
        />
        <MetricCard
          title={t('overview.p95Latency')}
          value={`${formatNumber(summary.data?.p95_latency_ms ?? 0)} ms`}
          sub={t('overview.p50Sub', { value: formatNumber(summary.data?.p50_latency_ms ?? 0) })}
        />
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <section className="panel p-4">
          <div className="text-dim mono mb-3 text-xs uppercase tracking-wider">
            {t('overview.topEndpoints')}
          </div>
          <ul className="space-y-2 text-sm">
            {(summary.data?.top_endpoints ?? []).slice(0, 6).map((e) => (
              <li key={e.path} className="flex items-center justify-between gap-2">
                <span className="mono truncate text-xs">{e.path}</span>
                <span className="mono text-dim">{formatNumber(e.calls)}</span>
              </li>
            ))}
            {(summary.data?.top_endpoints?.length ?? 0) === 0 ? (
              <li className="text-dim text-xs">{t('overview.noTraffic')}</li>
            ) : null}
          </ul>
        </section>

        <section className="panel p-4">
          <div className="text-dim mono mb-3 text-xs uppercase tracking-wider">
            {t('overview.modelProfiles')}
          </div>
          <ProfileList data={profiles.data} />
        </section>
      </div>

      {(health.data?.warnings?.length ?? 0) > 0 ? (
        <section className="panel p-4" style={{ borderColor: 'var(--yellow)' }}>
          <div className="text-dim mono mb-2 text-xs uppercase tracking-wider">
            {t('overview.warnings')}
          </div>
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
