import { NavLink } from 'react-router-dom';
import {
  Activity,
  Boxes,
  Brain,
  LineChart,
  ListChecks,
  Settings as SettingsIcon,
} from 'lucide-react';
import { cn } from '../lib/format';

/**
 * Sidebar — six nav items + brand. Items are flat (no submenu) by design;
 * Container detail lives at /containers/:name and gets a "back to list"
 * crumb on the detail page instead of mounting a second-level nav.
 */
const ITEMS = [
  { to: '/overview', label: 'Overview', icon: Activity },
  { to: '/containers', label: 'Containers', icon: Boxes },
  { to: '/memory', label: 'Memory', icon: Brain },
  { to: '/usage', label: 'Usage', icon: LineChart, badge: '7d' },
  { to: '/jobs', label: 'Jobs', icon: ListChecks },
  { to: '/settings', label: 'Settings', icon: SettingsIcon },
];

export function Sidebar() {
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
        {ITEMS.map(({ to, label, icon: Icon, badge }) => (
          <NavLink
            key={to}
            to={to}
            className={({ isActive }) =>
              cn(
                'group flex items-center gap-2 rounded px-3 py-2 text-sm transition-colors',
                isActive
                  ? 'bg-[color:var(--bg)] text-text accent'
                  : 'text-dim hover:text-text hover:bg-[color:var(--bg)]',
              )
            }
            end
          >
            <Icon size={16} />
            <span className="flex-1">{label}</span>
            {badge ? (
              <span
                className="rounded px-1.5 py-0.5 text-[10px] font-mono"
                style={{ background: 'var(--bg)', color: 'var(--text-dim)' }}
              >
                {badge}
              </span>
            ) : null}
          </NavLink>
        ))}
      </nav>
    </aside>
  );
}
