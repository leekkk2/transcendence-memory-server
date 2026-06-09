import { NavLink } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import {
  Activity,
  Boxes,
  Brain,
  Coins,
  LineChart,
  ListChecks,
  Settings as SettingsIcon,
  SlidersHorizontal,
  type LucideIcon,
} from 'lucide-react';
import { cn } from '../lib/format';

interface NavItem {
  to: string;
  key: string;
  icon: LucideIcon;
  badge?: string;
}

/**
 * Sidebar nav. Items are flat (no submenu) by design; ContainerDetail lives at
 * /containers/:name and gets a "back to list" crumb instead of a second-level
 * nav. Labels are i18n keys resolved at render. `onNavigate` lets the mobile
 * drawer close itself when a link is tapped.
 */
const ITEMS: NavItem[] = [
  { to: '/overview', key: 'nav.overview', icon: Activity },
  { to: '/containers', key: 'nav.containers', icon: Boxes },
  { to: '/memory', key: 'nav.memory', icon: Brain },
  { to: '/usage', key: 'nav.usage', icon: LineChart, badge: '7d' },
  { to: '/tokens', key: 'nav.tokens', icon: Coins, badge: '7d' },
  { to: '/jobs', key: 'nav.jobs', icon: ListChecks },
  { to: '/config', key: 'nav.config', icon: SlidersHorizontal },
  { to: '/settings', key: 'nav.settings', icon: SettingsIcon },
];

export function Sidebar({ onNavigate }: { onNavigate?: () => void }) {
  const { t } = useTranslation();
  return (
    <aside
      className="flex h-full w-[220px] shrink-0 flex-col border-r"
      style={{ borderColor: 'var(--border)', background: 'var(--bg-elev)' }}
    >
      <div className="flex h-14 items-center px-5 font-mono text-sm tracking-tight">
        <span className="text-dim mr-1">tm</span>
        <span className="accent">admin</span>
      </div>
      <nav className="flex flex-col gap-0.5 px-3">
        {ITEMS.map(({ to, key, icon: Icon, badge }) => (
          <NavLink
            key={to}
            to={to}
            onClick={onNavigate}
            className={({ isActive }) =>
              cn(
                'nav-item group relative flex items-center gap-2.5 rounded-md px-3 py-2 text-sm',
                isActive
                  ? 'bg-[color:var(--bg)] text-text accent'
                  : 'text-dim hover:text-text hover:bg-[color:var(--bg)]',
              )
            }
            end
          >
            {({ isActive }) => (
              <>
                {isActive ? (
                  <span
                    className="absolute left-0 top-1/2 h-5 w-0.5 -translate-y-1/2 rounded-full"
                    style={{ background: 'var(--accent)' }}
                  />
                ) : null}
                <Icon size={16} />
                <span className="flex-1">{t(key)}</span>
                {badge ? (
                  <span
                    className="mono rounded px-1.5 py-0.5 text-[10px]"
                    style={{ background: 'var(--bg)', color: 'var(--text-dim)' }}
                  >
                    {badge}
                  </span>
                ) : null}
              </>
            )}
          </NavLink>
        ))}
      </nav>
    </aside>
  );
}
