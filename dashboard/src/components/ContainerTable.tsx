import { Link } from 'react-router-dom';
import { formatNumber, formatRelativeTs } from '../lib/format';
import type { ContainerListItem } from '../lib/queries';

/**
 * Containers > main table. Click name → ContainerDetail. The shape is
 * deliberately tolerant — Lane B's /admin/usage/containers may enrich rows
 * later, but the dashboard works even if only `name` is present.
 */
export function ContainerTable({ rows }: { rows: ContainerListItem[] }) {
  return (
    <div className="panel overflow-hidden">
      <table className="w-full text-sm">
        <thead style={{ background: 'var(--bg)' }}>
          <tr>
            <th className="mono text-xs text-dim uppercase tracking-wider px-3 py-2 text-left">Name</th>
            <th className="mono text-xs text-dim uppercase tracking-wider px-3 py-2 text-right">Memory</th>
            <th className="mono text-xs text-dim uppercase tracking-wider px-3 py-2 text-right">Last active</th>
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 ? (
            <tr>
              <td colSpan={3} className="px-3 py-6 text-center text-dim">
                no containers
              </td>
            </tr>
          ) : null}
          {rows.map((c) => (
            <tr key={c.name} className="border-t" style={{ borderColor: 'var(--border)' }}>
              <td className="px-3 py-2 font-mono text-xs">
                <Link to={`/containers/${encodeURIComponent(c.name)}`} className="accent">
                  {c.name}
                </Link>
              </td>
              <td className="px-3 py-2 text-right mono">{formatNumber(c.memory_count)}</td>
              <td className="px-3 py-2 text-right mono text-dim">{formatRelativeTs(c.last_active)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
