import { Link, useParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { api } from '../lib/api';
import { formatNumber, formatRelative } from '../lib/format';
import { useContainers } from '../lib/queries';

/**
 * Container detail — metadata (read from the /containers list item, since the
 * dedicated /metadata route is not exposed) + the live index-status surface.
 * Field names mirror the real /index-status payload (total_objects /
 * embedded_objects / backlog_active / last_embed_ok_at); the earlier guess at
 * memory_count / backlog_count / last_indexed_at rendered everything as "—".
 */
interface IndexStatus {
  state?: string;
  total_objects?: number;
  embedded_objects?: number;
  backlog_active?: number;
  dead_count?: number;
  job_running?: boolean;
  last_embed_ok_at?: number | null;
}

export default function ContainerDetail() {
  const { t } = useTranslation();
  const { name = '' } = useParams();
  const containers = useContainers();
  const item = containers.data?.containers?.find((c) => c.name === name);

  const index = useQuery<IndexStatus>({
    queryKey: ['container', name, 'index-status'],
    queryFn: () => api.get(`/containers/${encodeURIComponent(name)}/index-status`),
    enabled: !!name,
  });
  const idx = index.data;

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2 text-sm">
        <Link to="/containers" className="accent">
          ← {t('containerDetail.back')}
        </Link>
        <span className="text-dim">/</span>
        <span className="mono">{name}</span>
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <section className="panel p-4">
          <div className="text-dim mono mb-3 text-xs uppercase tracking-wider">metadata</div>
          <pre className="mono overflow-auto whitespace-pre-wrap text-xs" style={{ color: 'var(--text-dim)' }}>
            {item?.metadata
              ? JSON.stringify(item.metadata, null, 2)
              : containers.isLoading
                ? t('common.loading')
                : '(no metadata)'}
          </pre>
        </section>

        <section className="panel space-y-2.5 p-4">
          <div className="text-dim mono mb-1 text-xs uppercase tracking-wider">
            {t('containerDetail.indexStatus')}
          </div>
          {index.isLoading ? (
            <div className="skeleton h-24 w-full" />
          ) : idx ? (
            <>
              <DetailRow label={t('containerDetail.state')}>
                <span className={idx.state === 'fresh' ? 'badge badge-green' : idx.state === 'stale' ? 'badge badge-yellow' : 'badge badge-dim'}>
                  <span className="dot" />
                  {t(`indexState.${idx.state ?? 'unknown'}`, { defaultValue: idx.state ?? 'unknown' })}
                </span>
              </DetailRow>
              <DetailRow label={t('containerDetail.totalObjects')}>
                <span className="mono">{formatNumber(idx.total_objects)}</span>
              </DetailRow>
              <DetailRow label={t('containerDetail.embeddedObjects')}>
                <span className="mono">{formatNumber(idx.embedded_objects)}</span>
              </DetailRow>
              <DetailRow label={t('containerDetail.backlog')}>
                <span className="mono">{formatNumber(idx.backlog_active)}</span>
              </DetailRow>
              <DetailRow label={t('containerDetail.deadCount')}>
                <span className="mono" style={{ color: (idx.dead_count ?? 0) > 0 ? 'var(--red)' : undefined }}>
                  {formatNumber(idx.dead_count)}
                </span>
              </DetailRow>
              <DetailRow label={t('containerDetail.jobRunning')}>
                <span className="mono">{idx.job_running ? t('containerDetail.yes') : t('containerDetail.no')}</span>
              </DetailRow>
              <DetailRow label={t('containerDetail.lastEmbedOk')}>
                <span className="mono text-dim">{formatRelative(idx.last_embed_ok_at)}</span>
              </DetailRow>
            </>
          ) : (
            <div className="text-dim text-sm">{t('containerDetail.notAvailable')}</div>
          )}
        </section>
      </div>

      <Link to={`/memory?container=${encodeURIComponent(name)}`} className="btn btn-accent inline-block text-sm">
        {t('containerDetail.viewMemories')}
      </Link>
    </div>
  );
}

function DetailRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between text-sm">
      <span className="text-dim text-xs">{label}</span>
      {children}
    </div>
  );
}
