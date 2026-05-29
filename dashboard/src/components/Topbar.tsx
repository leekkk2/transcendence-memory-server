import { LogOut, Menu } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useMe, useLogout } from '../lib/auth';
import { LanguageSwitcher } from './LanguageSwitcher';

/**
 * Topbar — mobile nav trigger + brand + env badge + identity + language +
 * logout. Only the first 12 chars of the api-key hash are surfaced; the full
 * key never reaches the browser past login.
 */
const ENV_COLOURS: Record<string, { bg: string; text: string; key: string }> = {
  prod: { bg: 'var(--red)', text: '#ffffff', key: 'env.prod' },
  production: { bg: 'var(--red)', text: '#ffffff', key: 'env.prod' },
  staging: { bg: 'var(--yellow)', text: '#000000', key: 'env.staging' },
  dev: { bg: 'var(--green)', text: '#000000', key: 'env.dev' },
};

export function Topbar({ onOpenNav }: { onOpenNav?: () => void }) {
  const { t } = useTranslation();
  const { data } = useMe();
  const logout = useLogout();
  const envKey = (data?.env || 'dev').toLowerCase();
  const env = ENV_COLOURS[envKey] ?? ENV_COLOURS.dev;

  return (
    <header
      className="flex h-14 shrink-0 items-center justify-between gap-3 border-b px-4 sm:px-6"
      style={{ borderColor: 'var(--border)', background: 'var(--bg-elev)' }}
    >
      <div className="flex items-center gap-3 text-sm">
        <button
          onClick={onOpenNav}
          aria-label={t('topbar.openMenu')}
          className="btn btn-ghost flex p-1.5 lg:hidden"
        >
          <Menu size={16} />
        </button>
        <span className="hidden font-mono text-dim sm:inline">transcendence-memory</span>
        <span
          className="mono rounded px-2 py-0.5 text-[11px] uppercase"
          style={{ background: env.bg, color: env.text }}
        >
          {t(env.key)}
        </span>
      </div>
      <div className="flex items-center gap-3 text-sm sm:gap-4">
        <LanguageSwitcher />
        <span className="hidden font-mono text-dim md:inline">
          {t('topbar.key')}&nbsp;{data?.api_key_hash ? `${data.api_key_hash}…` : '—'}
        </span>
        <button
          onClick={() => logout.mutate()}
          className="flex items-center gap-1 rounded px-2 py-1 text-sm text-dim hover:text-text"
        >
          <LogOut size={14} />
          <span className="hidden sm:inline">{t('topbar.logout')}</span>
        </button>
      </div>
    </header>
  );
}
