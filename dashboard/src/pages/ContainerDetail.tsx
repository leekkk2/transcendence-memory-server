import { Link, useParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { api } from '../lib/api';
import { formatNumber, formatRelativeTs } from '../lib/format';

/**
 * Container detail view — metadata card + memory count + a "view memories"
 * jump to /memory?container=<name>. Server endpoint shape is loose; we read
 * the fields we know about and surface anything else under "raw" so the
 * page survives schema growth.
 */
export default function ContainerDetail() {
  const { name = '' } = useParams();
  const meta = useQuery({
    queryKey: ['container', name, 'metadata'],
    queryFn: () => api.get<Record<string, unknown>>(`/containers/${encodeURIComponent(name)}/metadata`),
    enabled: !!name,
  });
  const index = useQuery({
    queryKey: ['container', name, 'index-status'],
    queryFn: () => api.get<Record<string, unknown>>(`/containers/${encodeURIComponent(name)}/index-status`),
    enabled: !!name,
  });

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2 text-sm">
        <Link to="/containers" className="accent">
          ← containers
        </Link>
        <span className="text-dim">/</span>
        <span className="mono">{name}</span>
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <section className="panel p-4">
          <div className="text-dim mb-3 text-xs uppercase tracking-wider mono">metadata</div>
          <pre
            className="mono overflow-auto whitespace-pre-wrap text-xs"
            style={{ color: 'var(--text-dim)' }}
          >
            {meta.data ? JSON.stringify(meta.data, null, 2) : meta.isLoading ? 'loading…' : '(no metadata)'}
          </pre>
        </section>

        <section className="panel p-4">
          <div className="text-dim mb-3 text-xs uppercase tracking-wider mono">index status</div>
          <div className="text-sm">
            state: <span className="mono">{String(readField(index.data, 'state') ?? 'unknown')}</span>
          </div>
          <div className="text-sm">
            memory_count: <span className="mono">{formatNumber(Number(readField(index.data, 'memory_count')))}</span>
          </div>
          <div className="text-sm">
            backlog: <span className="mono">{formatNumber(Number(readField(index.data, 'backlog_count')))}</span>
          </div>
          <div className="text-sm">
            last_indexed:{' '}
            <span className="mono text-dim">
              {formatRelativeTs(Number(readField(index.data, 'last_indexed_at')) || undefined)}
            </span>
          </div>
        </section>
      </div>

      <Link
        to={`/memory?container=${encodeURIComponent(name)}`}
        className="inline-block rounded px-3 py-2 text-sm"
        style={{ background: 'var(--accent)', color: '#000' }}
      >
        view memories →
      </Link>
    </div>
  );
}

function readField(obj: unknown, key: string): unknown {
  if (obj && typeof obj === 'object') return (obj as Record<string, unknown>)[key];
  return undefined;
}
