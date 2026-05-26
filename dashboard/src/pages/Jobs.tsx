import { formatNumber, formatRelativeTs } from '../lib/format';
import { useJobs } from '../lib/queries';

/**
 * Jobs / queue view. Single table fed by /jobs; columns are deliberately
 * narrow so a typical workload (5-30 rows) fits a single screen without
 * scrolling.
 */
export default function Jobs() {
  const { data, isLoading } = useJobs();

  const rows = data?.jobs ?? [];
  const counts = rows.reduce<Record<string, number>>((acc, j) => {
    acc[j.status] = (acc[j.status] ?? 0) + 1;
    return acc;
  }, {});

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-semibold">Jobs</h1>

      <div className="flex flex-wrap gap-3">
        {['pending', 'running', 'done', 'failed', 'cancelled'].map((status) => (
          <div key={status} className="panel px-3 py-2 text-xs mono">
            <span className="text-dim">{status}</span>
            <span className="ml-2 font-semibold">{formatNumber(counts[status] ?? 0)}</span>
          </div>
        ))}
      </div>

      <div className="panel overflow-hidden">
        <table className="w-full text-sm">
          <thead style={{ background: 'var(--bg)' }}>
            <tr>
              <th className="mono text-xs text-dim uppercase tracking-wider px-3 py-2 text-left">Job</th>
              <th className="mono text-xs text-dim uppercase tracking-wider px-3 py-2 text-left">Type</th>
              <th className="mono text-xs text-dim uppercase tracking-wider px-3 py-2 text-left">Container</th>
              <th className="mono text-xs text-dim uppercase tracking-wider px-3 py-2 text-left">Status</th>
              <th className="mono text-xs text-dim uppercase tracking-wider px-3 py-2 text-right">Created</th>
            </tr>
          </thead>
          <tbody>
            {isLoading ? (
              <tr>
                <td colSpan={5} className="px-3 py-6 text-center text-dim text-sm">
                  loading…
                </td>
              </tr>
            ) : rows.length === 0 ? (
              <tr>
                <td colSpan={5} className="px-3 py-6 text-center text-dim text-sm">
                  no jobs
                </td>
              </tr>
            ) : (
              rows.map((j) => (
                <tr key={j.job_id} className="border-t" style={{ borderColor: 'var(--border)' }}>
                  <td className="px-3 py-2 mono text-xs">{j.job_id}</td>
                  <td className="px-3 py-2 mono text-xs">{j.type}</td>
                  <td className="px-3 py-2 mono text-xs">{j.container}</td>
                  <td className="px-3 py-2 mono text-xs">{j.status}</td>
                  <td className="px-3 py-2 text-right mono text-xs text-dim">
                    {formatRelativeTs(j.created_at)}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
