import { useEffect, useState } from 'react';
import { useMutation } from '@tanstack/react-query';
import { ProfileBreakerBadge, type BreakerState } from '../components/ProfileBreakerBadge';
import { api } from '../lib/api';
import { useMe } from '../lib/auth';
import { useProfiles } from '../lib/queries';

/**
 * Settings — read-only profile / login / about info plus a theme toggle.
 * Anything that requires a server restart (session TTL, lockout) is shown
 * only; we don't pretend a UI button can hot-swap env vars.
 */
const THEME_KEY = 'tm-admin-theme';

export default function Settings() {
  const me = useMe();
  const profiles = useProfiles();
  const [theme, setTheme] = useState<'dark' | 'light' | 'system'>('dark');

  useEffect(() => {
    const stored = (localStorage.getItem(THEME_KEY) as 'dark' | 'light' | 'system' | null) ?? 'dark';
    setTheme(stored);
    applyTheme(stored);
  }, []);

  const probe = useMutation({
    mutationFn: () => api.post('/admin/probe-embedding', {}),
  });

  const profileList = readProfiles(profiles.data);

  return (
    <div className="space-y-6">
      <h1 className="text-lg font-semibold">Settings</h1>

      <Section title="Profile status">
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
        <button
          onClick={() => probe.mutate()}
          disabled={probe.isPending}
          className="mt-4 rounded px-3 py-1.5 text-sm disabled:opacity-50"
          style={{ background: 'var(--accent)', color: '#000' }}
        >
          {probe.isPending ? 'probing…' : 'probe & reset embedding'}
        </button>
      </Section>

      <Section title="Login security">
        <Row label="api_key_hash" value={me.data?.api_key_hash ?? '—'} />
        <Row label="env" value={me.data?.env ?? 'dev'} />
        <Row label="session expires_at" value={String(me.data?.expires_at ?? '—')} />
        <p className="text-dim mt-3 text-xs">
          session TTL and lockout policy are set via env vars (<span className="mono">TM_UI_SESSION_TTL</span>,{' '}
          <span className="mono">TM_UI_LOGIN_LOCKOUT_*</span>). Restart the server to change them.
        </p>
      </Section>

      <Section title="Theme">
        <div className="flex gap-2">
          {(['dark', 'light', 'system'] as const).map((t) => (
            <button
              key={t}
              onClick={() => {
                setTheme(t);
                localStorage.setItem(THEME_KEY, t);
                applyTheme(t);
              }}
              className="rounded px-3 py-1.5 text-sm"
              style={{
                background: theme === t ? 'var(--accent)' : 'var(--bg)',
                color: theme === t ? '#000' : 'var(--text)',
              }}
            >
              {t}
            </button>
          ))}
        </div>
      </Section>

      <Section title="About">
        <Row label="dashboard" value="v0.17.0" />
        <Row
          label="server"
          value="see /health"
          hint="full version surface in /health JSON response"
        />
      </Section>
    </div>
  );
}

function applyTheme(theme: 'dark' | 'light' | 'system') {
  const root = document.documentElement;
  let effective = theme;
  if (theme === 'system') {
    effective = window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
  }
  root.classList.remove('light', 'dark');
  root.classList.add(effective);
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="panel p-4">
      <div className="text-dim mb-3 text-xs uppercase tracking-wider mono">{title}</div>
      {children}
    </section>
  );
}

function Row({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="flex items-baseline justify-between text-sm">
      <span className="text-dim mono text-xs">{label}</span>
      <span className="mono text-xs">{value}</span>
      {hint ? <span className="text-dim text-xs">{hint}</span> : null}
    </div>
  );
}

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
