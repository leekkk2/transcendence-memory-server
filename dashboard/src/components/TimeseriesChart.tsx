import { Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';

interface Point {
  ts: number;
  calls?: number;
  errors?: number;
  p95?: number;
}

/**
 * Calls + p95 dual-line chart. Pure SVG via recharts so the page doesn't
 * pull a canvas dep. The y-axes are independent — calls/sec and p95/ms live
 * on different scales so a tiny spike in p95 is still visible against the
 * dominant calls line.
 */
export function TimeseriesChart({ points }: { points: Point[] }) {
  const data = points.map((p) => ({
    ts: new Date(p.ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
    calls: p.calls ?? 0,
    p95: p.p95 ?? 0,
  }));
  return (
    <div className="panel h-64 p-3">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ top: 8, right: 8, left: 8, bottom: 8 }}>
          <XAxis dataKey="ts" stroke="var(--text-dim)" fontSize={11} />
          <YAxis yAxisId="left" stroke="var(--accent)" fontSize={11} />
          <YAxis yAxisId="right" orientation="right" stroke="var(--text-dim)" fontSize={11} />
          <Tooltip
            contentStyle={{ background: 'var(--bg-elev)', border: '1px solid var(--border)' }}
            labelStyle={{ color: 'var(--text-dim)' }}
          />
          <Line yAxisId="left" type="monotone" dataKey="calls" stroke="var(--accent)" strokeWidth={1.5} dot={false} />
          <Line yAxisId="right" type="monotone" dataKey="p95" stroke="var(--text-dim)" strokeWidth={1.5} dot={false} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
