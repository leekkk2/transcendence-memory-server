import { useTranslation } from 'react-i18next';
import type { ProfilesResponse } from '../lib/queries';

/**
 * Renders the model profiles from /admin/profiles. The endpoint returns plural
 * buckets (`embeddings` / `rerankers`) and carries no circuit-breaker state —
 * the earlier code read singular keys + a `breaker` field that never existed,
 * so this panel was permanently "no profiles configured". We now surface what
 * the server actually reports: provider / model / dim, whether an API key is
 * configured, and which profile the default route uses.
 */
interface ProfileItem {
  kind: string;
  name: string;
  sub: string;
  configured: boolean;
  isDefault: boolean;
}

export function ProfileList({ data }: { data?: ProfilesResponse }) {
  const { t } = useTranslation();
  const defEmbedding = data?.default_route?.embedding;
  const defReranker = data?.default_route?.reranker;

  const items: ProfileItem[] = [
    ...(data?.embeddings ?? []).map((e) => ({
      kind: t('profile.embedding'),
      name: e.name,
      sub: `${e.model} · ${t('profile.dim')} ${e.dim}`,
      configured: e.api_key_configured ?? false,
      isDefault: e.name === defEmbedding,
    })),
    ...(data?.rerankers ?? []).map((r) => ({
      kind: t('profile.reranker'),
      name: r.name,
      sub: r.model,
      configured: r.api_key_configured ?? false,
      isDefault: r.name === defReranker,
    })),
  ];

  if (items.length === 0) {
    return <div className="text-dim text-xs">{t('overview.noProfiles')}</div>;
  }

  return (
    <ul className="space-y-2.5">
      {items.map((p) => (
        <li key={`${p.kind}/${p.name}`} className="flex items-center gap-2.5 text-sm">
          <span
            className="inline-block h-2 w-2 shrink-0 rounded-full"
            style={{ background: p.configured ? 'var(--green)' : 'var(--text-dim)' }}
            title={p.configured ? t('profile.configured') : t('profile.notConfigured')}
          />
          <span className="text-dim mono text-[10px] uppercase">{p.kind}</span>
          <span className="mono truncate text-xs">{p.name}</span>
          {p.isDefault ? <span className="badge badge-cyan">{t('profile.default')}</span> : null}
          <span className="text-dim mono ml-auto truncate text-[11px]">{p.sub}</span>
        </li>
      ))}
    </ul>
  );
}
