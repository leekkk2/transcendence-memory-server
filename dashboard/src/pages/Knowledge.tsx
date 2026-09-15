import { FormEvent, lazy, Suspense, useEffect, useState } from 'react';
import { useMutation } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Link, useSearchParams } from 'react-router-dom';
import { Network, RefreshCw, Search, Send } from 'lucide-react';
import { api } from '../lib/api';
import { useContainers, useHealth } from '../lib/queries';
import { GraphNode, QueryAnswer, useGraph } from '../lib/documents';

const GraphCanvas = lazy(() => import('../components/GraphCanvas').then(module => ({ default: module.GraphCanvas })));
const MODES = ['hybrid', 'local', 'global', 'naive', 'mix'];

export default function Knowledge() {
  const { t } = useTranslation();
  const containers = useContainers();
  const health = useHealth();
  const [params, setParams] = useSearchParams();
  const container = params.get('container') || '';
  const [entityInput, setEntityInput] = useState('');
  const [entityQuery, setEntityQuery] = useState('');
  const [selected, setSelected] = useState<GraphNode | null>(null);
  const [question, setQuestion] = useState('');
  const [mode, setMode] = useState('hybrid');
  const [asked, setAsked] = useState('');
  const graph = useGraph(container, entityQuery);
  useEffect(() => {
    if (!container && containers.data?.containers.length) {
      const names = containers.data.containers.map(item => item.id || item.name);
      setParams({ container: names.includes('main') ? 'main' : names[0] }, { replace: true });
    }
  }, [container, containers.data, setParams]);
  const query = useMutation({
    mutationFn: (input: { container: string; query: string; mode: string }) => api.post<QueryAnswer>('/query', { ...input, top_k: 30, chunk_top_k: 6 }),
    retry: false,
  });
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!container || !question.trim() || query.isPending) return;
    setAsked(question);
    query.mutate({ container, query: question, mode });
  };
  const related = graph.data?.edges.filter(edge => edge.source === selected?.id || edge.target === selected?.id) || [];
  return <div className="space-y-6">
    <div className="flex flex-wrap items-start justify-between gap-4">
      <div><h1 className="text-xl font-semibold">{t('knowledge.title')}</h1><p className="text-dim mt-1 max-w-2xl text-sm leading-6">{t('knowledge.subtitle')}</p></div>
      <label className="flex min-w-48 flex-col gap-1 text-xs text-dim">{t('documents.container')}<select className="input text-sm text-text" value={container} disabled={query.isPending} data-testid="knowledge-container" onChange={event => { setParams({ container: event.target.value }); setSelected(null); query.reset(); setAsked(''); setEntityInput(''); setEntityQuery(''); }}>
        {!container && <option value="">{t('documents.chooseContainer')}</option>}{containers.data?.containers.map(item => <option key={item.id || item.name} value={item.id || item.name}>{item.name}</option>)}
      </select></label>
    </div>
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">{[['entities', graph.data?.node_count], ['relations', graph.data?.edge_count], ['types', graph.data ? Object.keys(graph.data.entity_types).length : undefined]].map(([key, count]) => <div className="panel p-4" key={key}><p className="text-dim text-xs">{t(`knowledge.${key}`)}</p><p className="mono mt-2 text-2xl">{count ?? '—'}</p></div>)}</div>
    <section className="panel min-w-0 space-y-4 p-4 sm:p-5" aria-labelledby="graph-title">
      <div className="flex flex-wrap items-center justify-between gap-3"><h2 id="graph-title" className="flex items-center gap-2 font-semibold"><Network size={17} />{t('knowledge.explore')}</h2><button className="btn flex items-center gap-2" disabled={graph.isFetching} onClick={() => { setSelected(null); void graph.refetch(); }}><RefreshCw size={14} />{t('documents.refresh')}</button></div>
      <form className="flex gap-2" onSubmit={event => { event.preventDefault(); setEntityQuery(entityInput); setSelected(null); }}><input className="input min-w-0 flex-1" aria-label={t('knowledge.entitySearch')} placeholder={t('knowledge.entitySearch')} value={entityInput} onChange={event => setEntityInput(event.target.value)} /><button className="btn" type="submit" aria-label={t('knowledge.entitySearch')}><Search size={16} /></button></form>
      <p className="text-dim text-xs leading-5">{t('knowledge.sampleHint', { nodes: graph.data?.nodes.length || 0, edges: graph.data?.edges.length || 0 })}</p>
      {graph.isPending && container && <div className="skeleton h-80" aria-label={t('common.loading')} />}
      {graph.isError && <p role="alert" className="text-sm text-red">{graph.error.message}</p>}
      {graph.data?.nodes.length ? <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_280px]">
        <Suspense fallback={<div className="skeleton h-96" />}><GraphCanvas graph={graph.data} onSelect={setSelected} /></Suspense>
        <div className="max-h-96 space-y-2 overflow-y-auto" aria-label={t('knowledge.entityList')}>
          {graph.data.nodes.map(node => <button key={node.id} className={`w-full rounded-lg border p-3 text-left ${selected?.id === node.id ? 'border-accent bg-bg' : 'border-border'}`} onClick={() => setSelected(node)}><div className="break-words text-sm">{node.label}</div><div className="text-dim mt-1 text-xs">{node.type}</div></button>)}
        </div>
      </div> : !graph.isPending && !graph.isError && <p className="text-dim py-8 text-center text-sm">{t('knowledge.empty')} <Link to={`/documents?container=${encodeURIComponent(container)}`} className="accent underline">{t('documents.ingestTitle')}</Link></p>}
      {selected && <div className="rounded-lg border border-border p-4">
        <h3 className="break-words font-semibold">{selected.label} <span className="badge badge-dim ml-2">{selected.type}</span></h3>
        <p className="mt-3 whitespace-pre-wrap break-words text-sm leading-7">{selected.description || t('knowledge.noDescription')}</p>
        <h4 className="text-dim mt-4 text-xs">{t('knowledge.visibleRelations')}</h4><ul className="mt-2 space-y-2">{related.map(edge => <li key={edge.id} className="rounded bg-bg p-3 text-xs"><div className="break-words font-semibold">{edge.source} → {edge.target}</div><p className="text-dim mt-1 whitespace-pre-wrap break-words">{edge.description || edge.keywords}</p></li>)}</ul>
      </div>}
    </section>
    <section className="panel p-4 sm:p-5" aria-labelledby="query-title">
      <h2 id="query-title" className="font-semibold">{t('knowledge.askTitle')}</h2><p className="text-dim mt-1 text-xs leading-5">{t('knowledge.askHint')}</p>
      <form onSubmit={submit} className="mt-4 space-y-3">
        <label className="block text-sm">{t('knowledge.question')}<textarea className="input mt-2 min-h-24 w-full" required value={question} onChange={event => setQuestion(event.target.value)} placeholder={t('knowledge.questionHint')} disabled={query.isPending} data-testid="graph-question" /></label>
        <div className="flex flex-wrap items-end justify-between gap-3"><label className="flex flex-col gap-1 text-xs text-dim">{t('knowledge.mode')}<select className="input text-sm text-text" value={mode} onChange={event => setMode(event.target.value)} disabled={query.isPending}>{MODES.map(value => <option key={value} value={value}>{t(`knowledge.modes.${value}`)}</option>)}</select></label><button className="btn btn-accent flex items-center gap-2" type="submit" disabled={!container || !question.trim() || query.isPending || health.data?.runtime_ready?.query !== true} data-testid="graph-submit"><Send size={14} />{t(query.isPending ? 'knowledge.querying' : 'knowledge.ask')}</button></div>
      </form>
      {query.isPending && <p role="status" className="text-dim mt-4 text-sm">{t('knowledge.waitHint')}</p>}
      {query.isError && <div role="alert" className="mt-4 space-y-2 break-words text-sm text-red"><p>{query.error.message}</p><p>{t('knowledge.errorHint')}</p></div>}
      {query.data && <div className="mt-5 space-y-3 rounded-lg border border-border bg-bg p-4" data-testid="query-result">
        <div className="flex flex-wrap items-center justify-between gap-2"><h3 className="text-sm font-semibold">{t('knowledge.answer')}</h3><span className={['ok', 'success'].includes(query.data.status) ? 'badge badge-green' : 'badge badge-yellow'}>{query.data.status}</span></div>
        <p className="text-dim whitespace-pre-wrap break-words text-xs">{asked}</p>
        <div className="whitespace-pre-wrap break-words text-sm leading-7">{query.data.answer || t('knowledge.noAnswer')}</div>
        <div className="border-t border-border pt-3"><h4 className="text-dim mb-2 text-xs">{t('knowledge.references')}</h4>{query.data.citations?.length ? <ol className="space-y-2">{query.data.citations.map((citation, index) => <li key={index} className="whitespace-pre-wrap break-words text-xs">{typeof citation === 'string' ? citation : JSON.stringify(citation, null, 2)}</li>)}</ol> : <p className="text-dim text-xs">{t('knowledge.referencesHint')}</p>}</div>
      </div>}
    </section>
  </div>;
}
