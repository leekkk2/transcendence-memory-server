import { formatRelativeTs } from '../lib/format';

/**
 * Memory browser row — id + truncated preview + tags + relative timestamp.
 * Click handler bubbles to the parent which opens the detail Sheet.
 */
export interface MemoryRow {
  id: string;
  text?: string;
  tags?: string[];
  created_at?: number;
  source?: string | null;
}

export function MemoryCard({ row, onOpen }: { row: MemoryRow; onOpen?: (id: string) => void }) {
  return (
    <div
      className="panel cursor-pointer p-3 hover:opacity-90"
      onClick={() => onOpen?.(row.id)}
      role="button"
      tabIndex={0}
    >
      <div className="flex items-center justify-between text-xs mono text-dim">
        <span>{row.id}</span>
        <span>{formatRelativeTs(row.created_at)}</span>
      </div>
      <div className="mt-2 text-sm line-clamp-3">{row.text ?? '(no text)'}</div>
      {row.tags && row.tags.length > 0 ? (
        <div className="mt-2 flex flex-wrap gap-1">
          {row.tags.map((t) => (
            <span
              key={t}
              className="rounded px-1.5 py-0.5 text-[10px] mono"
              style={{ background: 'var(--bg)', color: 'var(--text-dim)' }}
            >
              {t}
            </span>
          ))}
        </div>
      ) : null}
    </div>
  );
}
