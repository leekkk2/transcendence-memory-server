import { useMemo, useState } from 'react';
import { ContainerTable } from '../components/ContainerTable';
import { useContainers } from '../lib/queries';

/**
 * Containers list view. Toolbar = search box; clicking a row navigates to
 * ContainerDetail. Server lists are small (typically <50 containers), so
 * filtering is purely client-side.
 */
export default function Containers() {
  const { data, isLoading } = useContainers();
  const [filter, setFilter] = useState('');

  const rows = useMemo(() => {
    const list = data?.containers ?? [];
    const f = filter.trim().toLowerCase();
    return f ? list.filter((c) => c.name.toLowerCase().includes(f)) : list;
  }, [data, filter]);

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold">Containers</h1>
        <input
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          placeholder="filter by name…"
          className="rounded border px-3 py-1.5 font-mono text-xs"
          style={{ background: 'var(--bg-elev)', borderColor: 'var(--border)' }}
        />
      </div>
      {isLoading ? (
        <div className="text-dim text-sm">loading…</div>
      ) : (
        <ContainerTable rows={rows} />
      )}
    </div>
  );
}
