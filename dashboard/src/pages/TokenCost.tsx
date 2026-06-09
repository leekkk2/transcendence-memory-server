import { useTranslation } from 'react-i18next';
import { useTokenUsage, type TokenUsageRow } from '../lib/queries';
import { formatNumber } from '../lib/format';

/**
 * Token cost dashboard — pure consumption of P3's GET /admin/usage/tokens.
 * Totals as KPIs (incl. the live-counter total), then three breakdown tables
 * (by model / task type / agent). Read-only; no writes.
 */
export default function TokenCost() {
  const { t } = useTranslation();
  const tokens = useTokenUsage('7d');
  const data = tokens.data;

  return (
    <div className="space-y-6">
      <h1 className="text-lg font-semibold">{t('tokenCost.title')}</h1>

      <div className="grid grid-cols-1 gap-4 md:grid-cols-4">
        <KPI title={t('tokenCost.totalTokens')} value={formatNumber(data?.totals?.total_tokens ?? 0)} />
        <KPI title={t('tokenCost.promptTokens')} value={formatNumber(data?.totals?.prompt_tokens ?? 0)} />
        <KPI
          title={t('tokenCost.completionTokens')}
          value={formatNumber(data?.totals?.completion_tokens ?? 0)}
        />
        <KPI
          title={t('tokenCost.liveTotal')}
          value={formatNumber(data?.totals?.live_total_tokens ?? 0)}
        />
      </div>

      <BreakdownTable
        title={t('tokenCost.byModel')}
        keyLabel={t('tokenCost.colModel')}
        rows={data?.by_model ?? []}
      />
      <BreakdownTable
        title={t('tokenCost.byTaskType')}
        keyLabel={t('tokenCost.colTaskType')}
        rows={data?.by_task_type ?? []}
      />
      <BreakdownTable
        title={t('tokenCost.byAgent')}
        keyLabel={t('tokenCost.colAgent')}
        rows={data?.by_agent ?? []}
      />
    </div>
  );
}

function KPI({ title, value }: { title: string; value: string }) {
  return (
    <div className="panel p-4">
      <div className="text-dim text-xs uppercase tracking-wider mono">{title}</div>
      <div className="mt-2 text-xl font-semibold">{value}</div>
    </div>
  );
}

function BreakdownTable({
  title,
  keyLabel,
  rows,
}: {
  title: string;
  keyLabel: string;
  rows: TokenUsageRow[];
}) {
  const { t } = useTranslation();
  return (
    <section className="panel overflow-x-auto">
      <div className="text-dim mono px-4 pt-4 text-xs uppercase tracking-wider">{title}</div>
      <table className="tbl mt-2">
        <thead>
          <tr>
            <th>{keyLabel}</th>
            <th className="text-right">{t('tokenCost.colPrompt')}</th>
            <th className="text-right">{t('tokenCost.colCompletion')}</th>
            <th className="text-right">{t('tokenCost.colTotal')}</th>
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 ? (
            <tr>
              <td colSpan={4} className="text-dim py-8 text-center text-sm">
                {t('tokenCost.noData')}
              </td>
            </tr>
          ) : null}
          {rows.map((r) => (
            <tr key={r.key}>
              <td className="mono text-xs">{r.key || '—'}</td>
              <td className="mono text-right">{formatNumber(r.prompt_tokens)}</td>
              <td className="mono text-right">{formatNumber(r.completion_tokens)}</td>
              <td className="mono text-right">{formatNumber(r.total_tokens)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
