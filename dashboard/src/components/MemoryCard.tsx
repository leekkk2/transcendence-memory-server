import { useTranslation } from 'react-i18next';

/**
 * Memory browser card. Fields mirror the real `/search` result item — an
 * earlier `MemoryRow` declared `id` / `created_at`, which the server never
 * returns (it sends taskId / chunkId / title), so cards rendered blank
 * headers with a dead React key. Title is now the headline, the score sits in
 * a badge, and the body preview / tags fill the rest.
 */
export interface MemoryRow {
  taskId?: string;
  chunkId?: string;
  container?: string;
  docType?: string;
  source?: string | null;
  title?: string | null;
  text?: string;
  tags?: string[];
  score?: number;
}

/** Stable key for a result row — taskId+chunkId is unique per chunk. */
export function memoryRowKey(row: MemoryRow, index: number): string {
  return row.chunkId || row.taskId || `row-${index}`;
}

export function MemoryCard({ row }: { row: MemoryRow }) {
  const { t } = useTranslation();
  const title = row.title?.trim() || row.taskId || t('memory.untitled');
  const hasScore = typeof row.score === 'number' && Number.isFinite(row.score);

  return (
    <div className="panel fade-in flex flex-col gap-2 p-3.5">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0 flex-1 text-sm font-semibold leading-snug line-clamp-2">{title}</div>
        {hasScore ? (
          <span className="badge badge-cyan shrink-0" title={t('memory.score')}>
            {row.score!.toFixed(2)}
          </span>
        ) : null}
      </div>

      <div className="text-dim line-clamp-4 text-xs leading-relaxed">{row.text ?? t('memory.noText')}</div>

      {row.tags && row.tags.length > 0 ? (
        <div className="flex flex-wrap gap-1">
          {row.tags.slice(0, 8).map((tag, i) => (
            <span
              key={`${tag}-${i}`}
              className="mono rounded px-1.5 py-0.5 text-[10px]"
              style={{ background: 'var(--bg)', color: 'var(--text-dim)' }}
            >
              {tag}
            </span>
          ))}
          {row.tags.length > 8 ? (
            <span className="text-dim mono text-[10px]">+{row.tags.length - 8}</span>
          ) : null}
        </div>
      ) : null}

      {row.docType || row.source ? (
        <div className="text-dim mono mt-auto flex items-center gap-2 truncate pt-1 text-[10px]">
          {row.docType ? <span className="badge badge-dim">{row.docType}</span> : null}
          {row.source ? <span className="truncate">{row.source}</span> : null}
        </div>
      ) : null}
    </div>
  );
}
