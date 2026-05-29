import { useEffect, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { MemoryCard, memoryRowKey, type MemoryRow } from '../components/MemoryCard';
import { api } from '../lib/api';
import { useContainers } from '../lib/queries';
import { formatNumber } from '../lib/format';

/**
 * Memory browser.
 *
 * Two behaviours that the original page got wrong and which made memories look
 * "missing": (1) it defaulted the container to the literal string 'default',
 * which does not exist in any real deployment, and (2) it never browsed — the
 * page sat on "no results yet" until you typed something. Here the container
 * is a dropdown sourced from /containers (defaulting to the largest one and
 * persisted), and an empty query auto-browses the latest memories. A blank
 * query is sent to the server as a single space because the search endpoint
 * 422s on an empty string but treats " " as "return the top-k".
 */
const CONTAINER_KEY = 'tm-admin-memory-container';

interface SearchResponse {
  status?: string;
  message?: string;
  results?: MemoryRow[];
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

  const search = useQuery<SearchResponse>({
    queryKey: ['memory-search', container, submittedQuery, topk],
    queryFn: () => api.post<SearchResponse>('/search', { query: submittedQuery || ' ', topk, container }),
    enabled: !!container,
    staleTime: 10_000,
  });

  const resp = search.data;
  const serverError = resp?.status === 'error' ? resp?.message || t('common.error') : null;
  const rows = serverError ? [] : resp?.results ?? [];
  const isBrowse = submittedQuery === '';
  const noContainers = !containersQ.isLoading && containerList.length === 0;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between gap-3">
        <h1 className="text-lg font-semibold">{t('memory.title')}</h1>
        {!search.isLoading && !serverError && container ? (
          <span className="text-dim mono text-xs">
            {isBrowse
              ? t('memory.browsingHint', { container })
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

      {serverError ? (
        <div
          className="panel fade-in p-3 text-sm"
          style={{ borderColor: 'var(--red)', color: 'var(--red)' }}
        >
          {t('memory.searchError', { message: serverError })}
        </div>
      ) : null}

      {search.isLoading ? (
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
      ) : (
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
          {rows.map((r, i) => (
            <MemoryCard key={memoryRowKey(r, i)} row={r} />
          ))}
        </div>
      )}

      {!search.isLoading && !serverError && rows.length === 0 && container ? (
        <div className="panel text-dim p-8 text-center text-sm">{t('memory.emptyContainer')}</div>
      ) : null}
      {noContainers ? (
        <div className="panel text-dim p-8 text-center text-sm">{t('memory.noContainers')}</div>
      ) : null}
    </div>
  );
}
