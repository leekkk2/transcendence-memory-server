import { useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useMutation } from '@tanstack/react-query';
import { MemoryCard, type MemoryRow } from '../components/MemoryCard';
import { api } from '../lib/api';

/**
 * Memory browser — runs /search against the selected container. Querying is
 * explicit (no auto-fire on keystroke) to keep load predictable; results are
 * shown as cards so the user can scan before drilling into one row.
 */
export default function Memory() {
  const [params, setParams] = useSearchParams();
  const container = params.get('container') ?? '';
  const [query, setQuery] = useState('');
  const [topk, setTopk] = useState(20);
  const [rows, setRows] = useState<MemoryRow[]>([]);

  const search = useMutation({
    mutationFn: () =>
      api.post<{ results: MemoryRow[] }>('/search', {
        query: query || ' ',
        topk,
        container: container || 'default',
      }),
    onSuccess: (data) => setRows(data.results ?? []),
  });

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-semibold">Memory browser</h1>

      <div className="panel p-4 space-y-3">
        <div className="flex items-center gap-2">
          <label className="text-dim mono text-xs uppercase">container</label>
          <input
            value={container}
            onChange={(e) => setParams({ container: e.target.value })}
            placeholder="default"
            className="rounded border px-2 py-1 font-mono text-xs flex-1"
            style={{ background: 'var(--bg-elev)', borderColor: 'var(--border)' }}
          />
        </div>
        <div className="flex items-center gap-2">
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="search query…"
            className="flex-1 rounded border px-3 py-1.5 text-sm"
            style={{ background: 'var(--bg-elev)', borderColor: 'var(--border)' }}
          />
          <input
            type="number"
            value={topk}
            onChange={(e) => setTopk(Math.max(1, Math.min(100, Number(e.target.value) || 20)))}
            className="w-20 rounded border px-2 py-1.5 mono text-xs"
            style={{ background: 'var(--bg-elev)', borderColor: 'var(--border)' }}
          />
          <button
            onClick={() => search.mutate()}
            disabled={search.isPending}
            className="rounded px-3 py-1.5 text-sm disabled:opacity-50"
            style={{ background: 'var(--accent)', color: '#000' }}
          >
            {search.isPending ? 'searching…' : 'search'}
          </button>
        </div>
      </div>

      <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
        {rows.map((r) => (
          <MemoryCard key={r.id} row={r} />
        ))}
        {rows.length === 0 ? (
          <div className="text-dim text-sm">no results yet — enter a query and press search</div>
        ) : null}
      </div>
    </div>
  );
}
