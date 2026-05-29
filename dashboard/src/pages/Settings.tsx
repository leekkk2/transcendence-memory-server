import { useEffect, useState } from 'react';
import { useMutation } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { ProfileList } from '../components/ProfileList';
import { api } from '../lib/api';
import { useMe } from '../lib/auth';
import { useHealth, useProfiles } from '../lib/queries';

/**
 * Settings — read-only profile / login / about info plus a theme toggle.
 * Anything that requires a server restart (session TTL, lockout) is shown
 * only; we don't pretend a UI button can hot-swap env vars.
 */
const THEME_KEY = 'tm-admin-theme';
type Theme = 'dark' | 'light' | 'system';

export default function Settings() {
  const { t } = useTranslation();
  const me = useMe();
  const health = useHealth();
  const profiles = useProfiles();
  const [theme, setTheme] = useState<Theme>('dark');

  useEffect(() => {
    const stored = (localStorage.getItem(THEME_KEY) as Theme | null) ?? 'dark';
    setTheme(stored);
    applyTheme(stored);
  }, []);

  const probe = useMutation({
    mutationFn: () => api.post('/admin/probe-embedding', {}),
  });

  return (
    <div className="space-y-6">
      <h1 className="text-lg font-semibold">{t('settings.title')}</h1>

      <Section title={t('settings.profileStatus')}>
        <ProfileList data={profiles.data} />
        <div className="mt-4 flex items-center gap-3">
          <button onClick={() => probe.mutate()} disabled={probe.isPending} className="btn btn-accent text-sm">
            {probe.isPending ? t('settings.probing') : t('settings.probeReset')}
          </button>
          {probe.isSuccess ? (
            <span className="badge badge-green">
              <span className="dot" />
              {t('settings.probeOk')}
            </span>
          ) : null}
          {probe.isError ? (
            <span className="badge badge-red">
              <span className="dot" />
              {t('settings.probeFailed')}
            </span>
          ) : null}
        </div>
      </Section>

      <Section title={t('settings.loginSecurity')}>
        <Row label={t('settings.apiKeyHash')} value={me.data?.api_key_hash ?? '—'} />
        <Row label={t('settings.env')} value={me.data?.env ?? 'dev'} />
        <Row label={t('settings.sessionExpires')} value={String(me.data?.expires_at ?? '—')} />
        <p className="text-dim mt-3 text-xs">{t('settings.sessionHint')}</p>
      </Section>

      <Section title={t('settings.theme')}>
        <div className="flex gap-2">
          {(['dark', 'light', 'system'] as const).map((tk) => (
            <button
              key={tk}
              onClick={() => {
                setTheme(tk);
                localStorage.setItem(THEME_KEY, tk);
                applyTheme(tk);
              }}
              className={theme === tk ? 'btn btn-accent text-sm' : 'btn btn-ghost text-sm'}
            >
              {t(`settings.theme${tk[0].toUpperCase()}${tk.slice(1)}`)}
            </button>
          ))}
        </div>
      </Section>

      <Section title={t('settings.about')}>
        <Row label={t('settings.dashboard')} value={`v${__APP_VERSION__}`} />
        <Row
          label={t('settings.server')}
          value={
            health.data
              ? `${health.data.architecture ?? '—'} · ${health.data.build_flavor ?? '—'}`
              : '—'
          }
          hint={t('settings.serverHint')}
        />
      </Section>
    </div>
  );
}

function applyTheme(theme: Theme) {
  const root = document.documentElement;
  let effective: 'dark' | 'light' = theme === 'system' ? 'dark' : theme;
  if (theme === 'system') {
    effective = window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
  }
  root.classList.remove('light', 'dark');
  root.classList.add(effective);
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="panel p-4">
      <div className="text-dim mono mb-3 text-xs uppercase tracking-wider">{title}</div>
      {children}
    </section>
  );
}

function Row({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="flex flex-wrap items-baseline justify-between gap-x-3 py-0.5 text-sm">
      <span className="text-dim mono text-xs">{label}</span>
      <span className="mono text-xs">{value}</span>
      {hint ? <span className="text-dim w-full text-right text-[11px]">{hint}</span> : null}
    </div>
  );
}
