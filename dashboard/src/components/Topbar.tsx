import { LogOut } from 'lucide-react';
import { useMe, useLogout } from '../lib/auth';

/**
 * Topbar — env badge + logged-in identity + logout. We deliberately surface
 * only the first 12 chars of the api-key hash; the full key never reaches the
 * browser past login.
 */
const ENV_COLOURS: Record<string, { bg: string; text: string; label: string }> = {
  prod: { bg: 'var(--red)', text: '#ffffff', label: 'prod' },
  production: { bg: 'var(--red)', text: '#ffffff', label: 'prod' },
  staging: { bg: 'var(--yellow)', text: '#000000', label: 'staging' },
  dev: { bg: 'var(--green)', text: '#000000', label: 'dev' },
};

export function Topbar() {
  const { data } = useMe();
  const logout = useLogout();
  const envKey = (data?.env || 'dev').toLowerCase();
  const env = ENV_COLOURS[envKey] ?? ENV_COLOURS.dev;

  return (
    <header
      className="flex h-14 shrink-0 items-center justify-between border-b px-6"
      style={{ borderColor: 'var(--border)', background: 'var(--bg-elev)' }}
    >
      <div className="flex items-center gap-3 text-sm">
        <span className="font-mono text-dim">transcendence-memory</span>
        <span
          className="rounded px-2 py-0.5 text-[11px] font-mono uppercase"
          style={{ background: env.bg, color: env.text }}
        >
          {env.label}
        </span>
      </div>
      <div className="flex items-center gap-4 text-sm">
        <span className="font-mono text-dim">
          key&nbsp;{data?.api_key_hash ? `${data.api_key_hash}…` : '—'}
        </span>
        <button
          onClick={() => logout.mutate()}
          className="flex items-center gap-1 rounded px-2 py-1 text-sm text-dim hover:text-text"
          style={{ background: 'transparent' }}
        >
          <LogOut size={14} />
          <span>logout</span>
        </button>
      </div>
    </header>
  );
}
