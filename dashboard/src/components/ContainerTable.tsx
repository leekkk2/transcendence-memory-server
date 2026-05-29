import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { formatNumber, formatRelative } from '../lib/format';
import type { ContainerListItem } from '../lib/queries';

/** Maps the server's index_state to a semantic badge class. */
function stateBadgeClass(state?: string): string {
  if (state === 'fresh') return 'badge badge-green';
  if (state === 'stale') return 'badge badge-yellow';
  return 'badge badge-dim';
}

/**
 * Containers > main table. Reads the real /containers fields (objects /
 * index_state / last_modified) — the previous memory_count / last_active
 * guess rendered every row as "—".
 */
export function ContainerTable({ rows }: { rows: ContainerListItem[] }) {
  const { t } = useTranslation();
  return (
    <div className="panel overflow-x-auto">
      <table className="tbl">
        <thead>
          <tr>
            <th>{t('containers.colName')}</th>
            <th className="text-right">{t('containers.colObjects')}</th>
            <th>{t('containers.colState')}</th>
            <th className="text-right">{t('containers.colLastModified')}</th>
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 ? (
            <tr>
              <td colSpan={4} className="text-dim py-8 text-center">
                {t('containers.noContainers')}
              </td>
            </tr>
          ) : null}
          {rows.map((c) => (
            <tr key={c.name}>
              <td className="mono text-xs">
                <Link to={`/containers/${encodeURIComponent(c.name)}`} className="accent">
                  {c.name}
                </Link>
              </td>
              <td className="mono text-right">{formatNumber(c.objects)}</td>
              <td>
                <span className={stateBadgeClass(c.index_state)}>
                  <span className="dot" />
                  {t(`indexState.${c.index_state ?? 'unknown'}`, { defaultValue: c.index_state ?? 'unknown' })}
                </span>
              </td>
              <td className="mono text-dim text-right text-xs">{formatRelative(c.last_modified)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
