import { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ContainerTable } from '../components/ContainerTable';
import { useContainers } from '../lib/queries';

/**
 * Containers list view. Toolbar = search box; clicking a row navigates to
 * ContainerDetail. Server lists are small (typically <50 containers), so
 * filtering is purely client-side.
 */
export default function Containers() {
  const { t } = useTranslation();
  const { data, isLoading } = useContainers();
  const [filter, setFilter] = useState('');

  const rows = useMemo(() => {
    const list = data?.containers ?? [];
    const f = filter.trim().toLowerCase();
    const filtered = f ? list.filter((c) => c.name.toLowerCase().includes(f)) : list;
    return [...filtered].sort((a, b) => (b.objects ?? 0) - (a.objects ?? 0));
  }, [data, filter]);

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between gap-3">
        <h1 className="text-lg font-semibold">{t('containers.title')}</h1>
        <input
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          placeholder={t('containers.filterPlaceholder')}
          className="input mono w-48 text-xs"
        />
      </div>
      {isLoading ? (
        <div className="panel space-y-2 p-4">
          {Array.from({ length: 6 }).map((_, i) => (
            <div key={i} className="skeleton h-7 w-full" />
          ))}
        </div>
      ) : (
        <ContainerTable rows={rows} />
      )}
    </div>
  );
}
