import type { ReactNode } from 'react';

/**
 * Single KPI card. Title + big number + optional sublabel underneath.
 * Background uses the elevated surface so it sits a step above the page bg
 * without needing a shadow.
 */
export function MetricCard({
  title,
  value,
  sub,
  trend,
}: {
  title: string;
  value: ReactNode;
  sub?: ReactNode;
  trend?: 'up' | 'down' | 'flat';
}) {
  const trendColour =
    trend === 'up' ? 'var(--green)' : trend === 'down' ? 'var(--red)' : 'var(--text-dim)';
  return (
    <div className="panel p-4">
      <div className="text-dim text-xs uppercase tracking-wider mono">{title}</div>
      <div className="mt-2 text-2xl font-semibold">{value}</div>
      {sub ? (
        <div className="mt-1 text-xs mono" style={{ color: trendColour }}>
          {sub}
        </div>
      ) : null}
    </div>
  );
}
