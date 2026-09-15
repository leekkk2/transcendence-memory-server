import { CheckCircle2, CircleAlert, FileText, Network } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router-dom';
import { useHealth } from '../lib/queries';

export function FullCapabilities() {
  const { t } = useTranslation();
  const health = useHealth();
  const data = health.data;
  const keys = ['search', 'documents_text', 'documents_file', 'query', 'embed'];
  const ready = !!data && data.build_flavor === 'full' && keys.every(key => data.runtime_ready?.[key] === true)
    && !data.degraded_reasons?.length && !data.warnings?.length && data.accepting_ingest && data.worker_running;
  return (
    <section className="panel p-4 sm:p-5" aria-labelledby="full-capabilities">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 id="full-capabilities" className="font-semibold">{t('full.title')}</h2>
          <p className="text-dim mt-1 text-xs">{t('full.hint')}</p>
        </div>
        <span className={ready ? 'badge badge-green' : 'badge badge-yellow'} data-testid="full-status">
          {ready ? <CheckCircle2 size={13} /> : <CircleAlert size={13} />}
          {health.isLoading ? t('common.loading') : ready ? t('full.ready') : t('full.attention')}
          {data && <span className="mono ml-1">{data.build_flavor.toUpperCase()}</span>}
        </span>
      </div>
      {health.isError && <p role="alert" className="mb-3 text-sm text-red">{t('full.unavailable')}</p>}
      <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-5">
        {keys.map(key => {
          const active = data?.runtime_ready?.[key] === true;
          return <div key={key} className="flex items-center gap-2 rounded-lg border border-border p-3 text-sm">
            {active ? <CheckCircle2 size={16} className="shrink-0 text-green" /> : <CircleAlert size={16} className="shrink-0 text-yellow" />}
            <div><div>{t(`full.${key}`)}</div><div className="text-dim mt-1 text-xs">{data ? t(active ? 'full.available' : 'full.notReady') : '—'}</div></div>
          </div>;
        })}
      </div>
      {!!data?.degraded_reasons?.length && <ul role="alert" className="mt-3 space-y-1 text-xs text-yellow">{data.degraded_reasons.map(reason => <li key={reason}>{reason}</li>)}</ul>}
      <div className="mt-4 flex flex-wrap gap-3">
        <Link className="btn flex items-center gap-2" to="/documents"><FileText size={14} />{t('nav.documents')}</Link>
        <Link className="btn flex items-center gap-2" to="/knowledge"><Network size={14} />{t('nav.knowledge')}</Link>
      </div>
    </section>
  );
}
