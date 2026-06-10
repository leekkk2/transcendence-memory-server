import { useEffect, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { MemoryCard, memoryRowKey, type MemoryRow } from '../components/MemoryCard';
import { api } from '../lib/api';
import { useContainers, useMemoriesInfinite, type MemoryListItem } from '../lib/queries';
import { formatNumber } from '../lib/format';

/**
 * Memory browser.
 *
 * Two data paths that must not fight each other:
 *   - Browse (empty query): paginated GET /containers/{name}/memories via
 *     useInfiniteQuery — first page only on mount, "load more" appends. This
 *     replaced the old "POST /search with a blank query" trick, which ran a
 *     query-embedding round-trip for every page view: it loaded the whole
 *     container slowly and hard-failed ("error: exit 1") whenever the
 *     embedding backend was unreachable, even though browsing needs no
 *     embedding at all.
 *   - Search (non-empty query): POST /search as before, only enabled once a
 *     query is submitted. Failures render a friendly message + retry button;
 *     the server now sends reasoned statuses instead of bare exit codes.
 */
const CONTAINER_KEY = 'tm-admin-memory-container';
const PAGE_SIZE = 50;

interface SearchResponse {
  status?: string;
  message?: string;
  results?: MemoryRow[];
  // Graceful-degradation metadata (Phase 1 §3): a search can partially succeed —
  // some containers return results while siblings are down/uninitialized. The
  // server keeps HTTP 200 + body flags rather than failing the whole request.
  degraded?: boolean;
  is_degraded?: boolean;
  per_container_status?: Record<string, string>;
  containers?: string[];
  fallback_source?: string | null;
  blocked_low_score?: number;
}

function listItemToRow(item: MemoryListItem): MemoryRow {
  return {
    taskId: item.id ?? undefined,
    title: item.title ?? undefined,
    text: item.text ?? undefined,
    source: item.source ?? undefined,
    tags: item.tags,
  };
}

function SkeletonGrid() {
  return (
    <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
      {Array.from({ length: 6 }).map((_, i) => (
        <div key={i} className="panel space-y-2 p-3.5">
          <div className="skeleton h-4 w-3/4" />
          <div className="skeleton h-3 w-full" />
          <div className="skeleton h-3 w-5/6" />
          <div className="skeleton h-3 w-2/3" />
        </div>
      ))}
    </div>
  );
}

export default function Memory() {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const containersQ = useContainers();

  // Largest container first — that's the one a human almost always wants.
  const containerList = useMemo(
    () => [...(containersQ.data?.containers ?? [])].sort((a, b) => (b.objects ?? 0) - (a.objects ?? 0)),
    [containersQ.data],
  );

  const [container, setContainerState] = useState<string>(
    () => params.get('container') || localStorage.getItem(CONTAINER_KEY) || '',
  );
  const [queryInput, setQueryInput] = useState('');
  const [submittedQuery, setSubmittedQuery] = useState(''); // '' → browse mode
  const [topk, setTopk] = useState(20);

  // Lock onto the largest container once the list arrives, if none is chosen.
  useEffect(() => {
    if (!container && containerList.length > 0) {
      const def = containerList[0].name;
      setContainerState(def);
      localStorage.setItem(CONTAINER_KEY, def);
      setParams({ container: def }, { replace: true });
    }
  }, [container, containerList, setParams]);

  const selectContainer = (name: string) => {
    setContainerState(name);
    setQueryInput('');
    setSubmittedQuery('');
    localStorage.setItem(CONTAINER_KEY, name);
    setParams({ container: name }, { replace: true });
  };

  const runSearch = () => setSubmittedQuery(queryInput.trim());
  const isBrowse = submittedQuery === '';

  // Browse path: async pagination, no embedding involved.
  const browse = useMemoriesInfinite(isBrowse ? container : '', PAGE_SIZE);
  const browseRows: MemoryRow[] = useMemo(
    () => (browse.data?.pages ?? []).flatMap((p) => p.items).map(listItemToRow),
    [browse.data],
  );
  const browseTotal = browse.data?.pages.length
    ? browse.data.pages[browse.data.pages.length - 1].total
    : 0;

  // Search path: only fires once a non-empty query is submitted.
  const search = useQuery<SearchResponse>({
    queryKey: ['memory-search', container, submittedQuery, topk],
    queryFn: () => api.post<SearchResponse>('/search', { query: submittedQuery, topk, container }),
    enabled: !!container && !isBrowse,
    staleTime: 10_000,
  });

  const resp = search.data;
  const searchServerError = !isBrowse && resp?.status === 'error' ? resp?.message || t('common.error') : null;
  const searchNetworkError = !isBrowse && search.isError;
  const searchError = searchServerError || (searchNetworkError ? t('common.error') : null);
  const browseError = isBrowse && browse.isError ? (browse.error as Error)?.message || t('common.error') : null;

  const searchRows = searchError ? [] : resp?.results ?? [];
  const rows = isBrowse ? browseRows : searchRows;
  const noContainers = !containersQ.isLoading && containerList.length === 0;

  // Partial success is NOT an error: render results as usual and surface a
  // non-fatal hint listing only the containers that did not return cleanly.
  const isDegraded = !isBrowse && !searchError && !!(resp?.degraded ?? resp?.is_degraded);
  const degradedContainers = isDegraded
    ? Object.entries(resp?.per_container_status ?? {}).filter(([, s]) => s !== 'ok')
    : [];
  const containerStatusLabel = (status: string) =>
    status === 'not_initialized' ? t('memory.notInitializedHint') : status;

  const isInitialLoading = isBrowse ? browse.isLoading : search.isLoading;
  const activeError = isBrowse ? browseError : searchError;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between gap-3">
        <h1 className="text-lg font-semibold">{t('memory.title')}</h1>
        {!isInitialLoading && !activeError && container ? (
          <span className="text-dim mono text-xs">
            {isBrowse
              ? t('memory.loadedCount', { loaded: formatNumber(browseRows.length), total: formatNumber(browseTotal) })
              : t('memory.resultsCount', { count: rows.length })}
          </span>
        ) : null}
      </div>

      <div className="panel space-y-3 p-4">
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
          <label className="text-dim mono w-20 shrink-0 text-xs uppercase">{t('memory.container')}</label>
          <select
            value={container}
            onChange={(e) => selectContainer(e.target.value)}
            disabled={noContainers}
            className="input mono w-full text-xs sm:max-w-xs"
          >
            {container === '' ? <option value="">{t('memory.selectContainer')}</option> : null}
            {containerList.map((c) => (
              <option key={c.name} value={c.name}>
                {c.name} · {formatNumber(c.objects ?? 0)}
              </option>
            ))}
          </select>
        </div>
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
          <input
            value={queryInput}
            onChange={(e) => setQueryInput(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && runSearch()}
            placeholder={t('memory.queryPlaceholder')}
            className="input flex-1 text-sm"
          />
          <input
            type="number"
            value={topk}
            aria-label={t('memory.topk')}
            onChange={(e) => setTopk(Math.max(1, Math.min(100, Number(e.target.value) || 20)))}
            className="input mono w-20 text-xs"
          />
          <button
            onClick={runSearch}
            disabled={search.isFetching || !container}
            className="btn btn-accent text-sm"
          >
            {search.isFetching ? t('memory.searching') : t('memory.search')}
          </button>
        </div>
      </div>

      {activeError ? (
        <div
          className="panel fade-in flex items-center justify-between gap-3 p-3 text-sm"
          style={{ borderColor: 'var(--red)', color: 'var(--red)' }}
        >
          <span>
            {isBrowse
              ? t('memory.listError', { message: activeError })
              : t('memory.searchError', { message: activeError })}
          </span>
          <button
            onClick={() => (isBrowse ? browse.refetch() : search.refetch())}
            className="btn shrink-0 text-xs"
          >
            {t('memory.retry')}
          </button>
        </div>
      ) : null}

      {isDegraded && degradedContainers.length > 0 ? (
        <div
          className="panel fade-in p-3 text-sm"
          style={{ borderColor: 'var(--yellow)', color: 'var(--yellow)' }}
        >
          <div>{t('memory.degradedHint')}</div>
          <ul className="mono mt-1 space-y-0.5 text-xs">
            {degradedContainers.map(([name, status]) => (
              <li key={name}>
                {name} · {containerStatusLabel(status)}
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {isInitialLoading ? (
        <SkeletonGrid />
      ) : (
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
          {rows.map((r, i) => (
            <MemoryCard key={memoryRowKey(r, i)} row={r} />
          ))}
        </div>
      )}

      {isBrowse && !isInitialLoading && !browseError && browse.hasNextPage ? (
        <div className="flex justify-center">
          <button
            onClick={() => browse.fetchNextPage()}
            disabled={browse.isFetchingNextPage}
            className="btn text-sm"
          >
            {browse.isFetchingNextPage
              ? t('memory.loadingMore')
              : t('memory.loadMore', { remaining: formatNumber(Math.max(0, browseTotal - browseRows.length)) })}
          </button>
        </div>
      ) : null}
      {isBrowse && !isInitialLoading && !browseError && browseRows.length > 0 && !browse.hasNextPage ? (
        <div className="text-dim mono text-center text-xs">{t('memory.allLoaded')}</div>
      ) : null}

      {!isInitialLoading && !activeError && rows.length === 0 && container ? (
        <div className="panel text-dim p-8 text-center text-sm">
          {isBrowse ? t('memory.emptyContainer') : t('memory.noSearchResults')}
        </div>
      ) : null}
      {noContainers ? (
        <div className="panel text-dim p-8 text-center text-sm">{t('memory.noContainers')}</div>
      ) : null}
    </div>
  );
}
