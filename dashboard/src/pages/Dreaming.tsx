import { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ConfigField, type ConfigDraft } from '../components/ConfigField';
import {
  useConfig,
  useUpdateConfig,
  useDreamingStatus,
  useTriggerDream,
  type ConfigItem,
  type DreamReport,
} from '../lib/queries';
import { formatRelative } from '../lib/format';

/**
 * Dreaming control page — additive surface over the P6 dreaming endpoints.
 *
 * The global switches (global_enabled / scheduler_enabled / trigger_cron /
 * batch_model) are registered config keys, so we reuse ConfigField + the same
 * PUT /admin/config write path as ConfigSettings rather than inventing a write
 * bypass. Per-container overrides write dynamic config:dreaming:container:{c}:*
 * keys through the same mutation. The manual trigger is report-only by default
 * (dry_run checked) so a kick can never delete data.
 */

const GLOBAL_KEYS = [
  'config:dreaming:global_enabled',
  'config:dreaming:scheduler_enabled',
  'config:dreaming:trigger_cron',
  'config:dreaming:batch_model',
];

export default function Dreaming() {
  const { t } = useTranslation();
  const config = useConfig();
  const update = useUpdateConfig();
  const status = useDreamingStatus();
  const trigger = useTriggerDream();

  const [drafts, setDrafts] = useState<Record<string, ConfigDraft>>({});
  const [scope, setScope] = useState<string>('');
  const [dryRun, setDryRun] = useState(true);
  const [report, setReport] = useState<DreamReport | null>(null);

  const items = config.data?.items ?? [];
  const globalItems = useMemo(
    () => GLOBAL_KEYS.map((k) => items.find((it) => it.key === k)).filter((x): x is ConfigItem => !!x),
    [items],
  );

  const dirtyKeys = useMemo(
    () => Object.keys(drafts).filter((k) => keyDirty(k, items, drafts)),
    [drafts, items],
  );

  function setDraft(key: string, value: ConfigDraft) {
    setDrafts((prev) => ({ ...prev, [key]: value }));
  }

  async function save() {
    const updates = dirtyKeys.map((key) => ({ key, value: drafts[key] }));
    if (updates.length === 0) return;
    await update.mutateAsync(updates);
    setDrafts({});
  }

  async function runTrigger() {
    const r = await trigger.mutateAsync({
      container: scope.trim() === '' ? null : scope.trim(),
      dry_run: dryRun,
    });
    setReport(r);
  }

  const data = status.data;

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-lg font-semibold">{t('dreaming.title')}</h1>
        <SchedulerBadge running={!!data?.scheduler_running} enabled={!!data?.scheduler_enabled} />
      </div>
      <p className="text-dim text-[11px]">{t('dreaming.intro')}</p>

      {/* Global switches via reused ConfigField + PUT /admin/config */}
      <section className="panel p-4">
        <div className="text-dim mono mb-1 text-xs uppercase tracking-wider">
          {t('dreaming.globalSection')}
        </div>
        <p className="text-dim mb-3 text-[11px]">{t('dreaming.globalHint')}</p>
        {config.isLoading ? (
          <div className="text-dim text-sm">{t('common.loading')}</div>
        ) : (
          <div className="divide-y" style={{ borderColor: 'var(--border-soft)' }}>
            {globalItems.map((it) => (
              <ConfigField
                key={it.key}
                item={it}
                draft={it.key in drafts ? drafts[it.key] : undefined}
                dirty={keyDirty(it.key, items, drafts)}
                onChange={(v) => setDraft(it.key, v)}
                onReset={() => setDraft(it.key, it.type === 'bool' ? it.default : null)}
              />
            ))}
          </div>
        )}
      </section>

      {/* Per-container dreaming overrides */}
      <section className="panel overflow-x-auto">
        <div className="text-dim mono px-4 pt-4 text-xs uppercase tracking-wider">
          {t('dreaming.perContainer')}
        </div>
        <table className="tbl mt-2">
          <thead>
            <tr>
              <th>{t('dreaming.colContainer')}</th>
              <th>{t('dreaming.colEnabled')}</th>
              <th>{t('dreaming.colCron')}</th>
              <th>{t('dreaming.colModel')}</th>
            </tr>
          </thead>
          <tbody>
            {(data?.containers ?? []).length === 0 ? (
              <tr>
                <td colSpan={4} className="text-dim py-8 text-center text-sm">
                  {t('dreaming.noContainers')}
                </td>
              </tr>
            ) : null}
            {(data?.containers ?? []).map((c) => (
              <ContainerRow
                key={c.container}
                container={c.container}
                enabled={c.enabled}
                cron={c.cron}
                model={c.model}
                drafts={drafts}
                onDraft={setDraft}
              />
            ))}
          </tbody>
        </table>
      </section>

      <div className="flex items-center justify-end gap-3">
        {update.isError ? (
          <span className="badge badge-red">
            <span className="dot" />
            {t('config.saveError')}
          </span>
        ) : null}
        <span className="text-dim mono text-xs">
          {t('config.dirtyCount', { count: dirtyKeys.length })}
        </span>
        <button
          type="button"
          onClick={save}
          disabled={dirtyKeys.length === 0 || update.isPending}
          className="btn btn-accent text-sm"
        >
          {update.isPending ? t('config.saving') : t('config.save')}
        </button>
      </div>

      {/* Manual trigger */}
      <section className="panel p-4 space-y-3">
        <div className="text-dim mono text-xs uppercase tracking-wider">{t('dreaming.triggerTitle')}</div>
        <p className="text-dim text-[11px]">{t('dreaming.triggerHint')}</p>
        <div className="flex flex-wrap items-center gap-3">
          <input
            type="text"
            value={scope}
            placeholder={t('dreaming.scopePlaceholder')}
            onChange={(e) => setScope(e.target.value)}
            className="input mono w-full max-w-xs text-xs"
          />
          <label className="flex items-center gap-2 text-xs">
            <input type="checkbox" checked={dryRun} onChange={(e) => setDryRun(e.target.checked)} />
            <span>{t('dreaming.dryRun')}</span>
          </label>
          <button
            type="button"
            onClick={runTrigger}
            disabled={trigger.isPending}
            className="btn btn-accent ml-auto text-sm"
          >
            {trigger.isPending ? t('dreaming.triggering') : t('dreaming.triggerBtn')}
          </button>
        </div>
        {trigger.isError ? (
          <div className="text-[11px]" style={{ color: 'var(--red)' }}>
            {t('dreaming.triggerError')}
          </div>
        ) : null}
        {report ? <ReportView report={report} label={t('dreaming.runResult')} /> : null}
      </section>

      {/* Last scheduled / prior report */}
      {data?.last_report ? (
        <ReportView report={data.last_report} label={t('dreaming.lastReport')} />
      ) : null}
    </div>
  );
}

function SchedulerBadge({ running, enabled }: { running: boolean; enabled: boolean }) {
  const { t } = useTranslation();
  if (running) {
    return (
      <span className="badge badge-green">
        <span className="dot" />
        {t('dreaming.schedulerRunning')}
      </span>
    );
  }
  return (
    <span className={enabled ? 'badge badge-yellow' : 'badge'}>
      <span className="dot" />
      {enabled ? t('dreaming.schedulerEnabledIdle') : t('dreaming.schedulerOff')}
    </span>
  );
}

function ContainerRow({
  container,
  enabled,
  cron,
  model,
  drafts,
  onDraft,
}: {
  container: string;
  enabled: boolean;
  cron: string | null;
  model: string | null;
  drafts: Record<string, ConfigDraft>;
  onDraft: (key: string, value: ConfigDraft) => void;
}) {
  const enabledKey = `config:dreaming:container:${container}:enabled`;
  const cronKey = `config:dreaming:container:${container}:cron`;
  const modelKey = `config:dreaming:container:${container}:model`;
  const enabledVal = enabledKey in drafts ? drafts[enabledKey] === true : enabled;
  const cronVal = cronKey in drafts ? (drafts[cronKey] as string | null) ?? '' : cron ?? '';
  const modelVal = modelKey in drafts ? (drafts[modelKey] as string | null) ?? '' : model ?? '';
  return (
    <tr>
      <td className="mono text-xs">{container}</td>
      <td>
        <input
          type="checkbox"
          checked={enabledVal}
          onChange={(e) => onDraft(enabledKey, e.target.checked)}
        />
      </td>
      <td>
        <input
          type="text"
          value={cronVal}
          onChange={(e) => onDraft(cronKey, e.target.value === '' ? null : e.target.value)}
          className="input mono w-32 text-xs"
        />
      </td>
      <td>
        <input
          type="text"
          value={modelVal}
          onChange={(e) => onDraft(modelKey, e.target.value === '' ? null : e.target.value)}
          className="input mono w-40 text-xs"
        />
      </td>
    </tr>
  );
}

function ReportView({ report, label }: { report: DreamReport; label: string }) {
  const { t } = useTranslation();
  return (
    <section className="panel overflow-x-auto">
      <div className="flex flex-wrap items-center gap-2 px-4 pt-4">
        <span className="text-dim mono text-xs uppercase tracking-wider">{label}</span>
        <StatusBadge status={report.status} />
        {report.dry_run ? <span className="badge badge-cyan">{t('dreaming.dryRunTag')}</span> : null}
        {report.excluded_from_rag ? (
          <span className="badge badge-green">{t('dreaming.excludedFromRag')}</span>
        ) : null}
        <span className="text-dim mono ml-auto text-[11px]">
          {formatRelative(report.finished_at) || '—'}
        </span>
      </div>
      <table className="tbl mt-2">
        <thead>
          <tr>
            <th>{t('dreaming.colTool')}</th>
            <th>{t('dreaming.colContainer')}</th>
            <th>{t('dreaming.colSummary')}</th>
            <th className="text-right">{t('dreaming.colCandidates')}</th>
            <th>{t('dreaming.colApplied')}</th>
          </tr>
        </thead>
        <tbody>
          {report.actions.length === 0 ? (
            <tr>
              <td colSpan={5} className="text-dim py-6 text-center text-sm">
                {t('dreaming.noActions')}
              </td>
            </tr>
          ) : null}
          {report.actions.map((a, i) => (
            <tr key={`${a.tool}-${i}`}>
              <td className="mono text-xs">{a.tool || '—'}</td>
              <td className="mono text-xs">{a.container ?? '—'}</td>
              <td className="text-xs">{a.summary || '—'}</td>
              <td className="mono text-right">{a.candidates}</td>
              <td>
                {a.applied ? (
                  <span className="badge badge-yellow">{t('common.yes', { defaultValue: 'yes' })}</span>
                ) : (
                  <span className="badge">{t('dreaming.reportOnly')}</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {report.notes ? (
        <p className="text-dim px-4 py-3 text-[11px]">{report.notes}</p>
      ) : null}
    </section>
  );
}

function StatusBadge({ status }: { status: string }) {
  const cls =
    status === 'ok' ? 'badge badge-green' : status === 'skipped_global_disabled' ? 'badge badge-yellow' : 'badge';
  return (
    <span className={cls}>
      <span className="dot" />
      {status}
    </span>
  );
}

/** A draft key is dirty when it differs from the live config value (or, for
 *  dynamic per-container keys not in the config list, whenever it's present). */
function keyDirty(key: string, items: ConfigItem[], drafts: Record<string, ConfigDraft>): boolean {
  if (!(key in drafts)) return false;
  const draft = drafts[key];
  const it = items.find((x) => x.key === key);
  if (!it) return true; // dynamic per-container key — any staged value is a change
  return draft !== it.value;
}
