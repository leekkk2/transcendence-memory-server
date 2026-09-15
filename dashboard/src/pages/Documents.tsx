import { FormEvent, useEffect, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Link, useSearchParams } from 'react-router-dom';
import { FileText, Image, Upload, RefreshCw, Search } from 'lucide-react';
import { api, ApiError } from '../lib/api';
import { useContainers, useHealth, JobsResponse } from '../lib/queries';
import { documentStatusClass, IngestReceipt, useDocumentConfig, useDocuments } from '../lib/documents';
import { DocumentViewer } from '../components/DocumentViewer';

const STATES = ['pending', 'parsing', 'analyzing', 'processing', 'handling', 'processed', 'failed', 'unknown'];

export default function Documents() {
  const { t } = useTranslation();
  const cache = useQueryClient();
  const containers = useContainers();
  const health = useHealth();
  const config = useDocumentConfig();
  const [params, setParams] = useSearchParams();
  const container = params.get('container') || '';
  const [mode, setMode] = useState<'text' | 'file'>('text');
  const [text, setText] = useState('');
  const [description, setDescription] = useState('');
  const [file, setFile] = useState<File | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const [status, setStatus] = useState('');
  const [kind, setKind] = useState('');
  const [search, setSearch] = useState('');
  const [query, setQuery] = useState('');
  const [offset, setOffset] = useState(0);
  const [duplicates, setDuplicates] = useState(false);
  const [selected, setSelected] = useState('');
  const [receipt, setReceipt] = useState<{ container: string; id: number } | null>(null);
  const documents = useDocuments(container, status, kind, query, offset, duplicates);
  const jobs = useQuery<JobsResponse>({
    queryKey: ['document-jobs', container],
    queryFn: () => api.get(`/jobs?container=${encodeURIComponent(container)}&limit=50`),
    enabled: !!container, refetchInterval: 5_000,
  });
  useEffect(() => {
    if (!container && containers.data?.containers.length) {
      const names = containers.data.containers.map(item => item.id || item.name);
      setParams({ container: names.includes('main') ? 'main' : names[0] }, { replace: true });
    }
  }, [container, containers.data, setParams]);
  const upload = useMutation({
    mutationFn: async (input: { target: string; mode: 'text' | 'file'; text: string; description: string; file: File | null }) => {
      if (input.mode === 'text') return api.post<IngestReceipt>('/documents/text', { container: input.target, text: input.text, description: input.description || undefined });
      const body = new FormData();
      body.set('container', input.target);
      body.set('file', input.file!);
      if (input.description) body.set('description', input.description);
      return api.upload<IngestReceipt>('/documents/upload', body);
    },
    retry: false,
    onSuccess: (data, input) => {
      setReceipt({ container: input.target, id: data.pid });
      setText(''); setFile(null); setDescription('');
      if (fileInput.current) fileInput.current.value = '';
      void cache.invalidateQueries({ queryKey: ['documents', input.target] });
      void cache.invalidateQueries({ queryKey: ['document-jobs', input.target] });
    },
  });
  const maxBytes = config.data?.max_upload_bytes;
  const tooLarge = !!file && !!maxBytes && file.size > maxBytes;
  const ready = health.data?.accepting_ingest && health.data?.worker_running
    && health.data?.runtime_ready?.[mode === 'text' ? 'documents_text' : 'documents_file'] === true;
  const canSubmit = !!container && ready && !upload.isPending && !tooLarge
    && (mode === 'text' ? !!text.trim() : !!file && !!maxBytes);
  const currentJobs = (jobs.data?.jobs || []).filter(job => job.op.startsWith('ingest-document-')).slice(0, 6);
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (canSubmit) upload.mutate({ target: container, mode, text, description, file });
  };
  const changeContainer = (value: string) => {
    setParams({ container: value }); setOffset(0); setSelected(''); setReceipt(null); upload.reset();
  };
  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div><h1 className="text-xl font-semibold">{t('documents.title')}</h1><p className="text-dim mt-1 max-w-2xl text-sm leading-6">{t('documents.subtitle')}</p></div>
        <label className="flex min-w-48 flex-col gap-1 text-xs text-dim">{t('documents.container')}
          <select className="input text-sm text-text" value={container} onChange={event => changeContainer(event.target.value)} disabled={upload.isPending} data-testid="document-container">
            {!container && <option value="">{t('documents.chooseContainer')}</option>}
            {containers.data?.containers.map(item => <option key={item.id || item.name} value={item.id || item.name}>{item.name}</option>)}
          </select>
        </label>
      </div>
      {containers.isError && <p role="alert" className="text-sm text-red">{containers.error.message}</p>}
      {containers.data?.containers.length === 0 && <p className="panel p-4 text-sm">{t('documents.noContainers')} <Link className="accent underline" to="/containers">{t('nav.containers')}</Link></p>}
      <section className="panel p-4 sm:p-5" aria-labelledby="ingest-title">
        <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
          <h2 id="ingest-title" className="font-semibold">{t('documents.ingestTitle')}</h2>
          <div className="flex gap-2" role="group" aria-label={t('documents.inputMode')}>
            <button type="button" className={mode === 'text' ? 'btn btn-accent' : 'btn'} aria-pressed={mode === 'text'} disabled={upload.isPending} onClick={() => setMode('text')}><FileText size={14} className="mr-1 inline" />{t('documents.textMode')}</button>
            <button type="button" className={mode === 'file' ? 'btn btn-accent' : 'btn'} aria-pressed={mode === 'file'} disabled={upload.isPending} onClick={() => setMode('file')}><Image size={14} className="mr-1 inline" />{t('documents.fileMode')}</button>
          </div>
        </div>
        <form onSubmit={submit} className="space-y-3" aria-label={t('documents.ingestTitle')}>
          {mode === 'text' ? <label className="block text-sm">{t('documents.fullText')}
            <textarea className="input mt-2 min-h-40 w-full resize-y leading-6" required value={text} onChange={event => setText(event.target.value)} placeholder={t('documents.textPlaceholder')} disabled={upload.isPending} data-testid="ingest-text" />
          </label> : <div className="rounded-lg border border-dashed border-border bg-bg p-5">
            <label className="block text-sm">{t('documents.fileLabel')}
              <input ref={fileInput} className="mt-3 block w-full text-sm file:mr-3 file:rounded file:border-0 file:bg-bg-elev file:px-4 file:py-2 file:text-text" type="file" accept=".pdf,.png,.jpg,.jpeg,.webp,.bmp,.tif,.tiff,.gif,.txt,.md,.doc,.docx,.ppt,.pptx,.html,.htm" onChange={event => setFile(event.target.files?.[0] || null)} disabled={upload.isPending} data-testid="ingest-file" />
            </label>
            <p className="text-dim mt-3 text-xs leading-5">{t('documents.fileHint', { size: maxBytes ? Math.round(maxBytes / 1024 / 1024) : '—' })}</p>
            {file && <p className="mt-2 break-all text-xs">{file.name} · {(file.size / 1024 / 1024).toFixed(2)} MB</p>}
            {tooLarge && <p role="alert" className="mt-2 text-sm text-red">{t('documents.tooLarge')}</p>}
            {config.isError && <p role="alert" className="mt-2 text-sm text-red">{config.error.message}</p>}
          </div>}
          <label className="block text-sm">{t('documents.description')}<input className="input mt-2 w-full" value={description} maxLength={500} onChange={event => setDescription(event.target.value)} placeholder={t('documents.descriptionHint')} disabled={upload.isPending} /></label>
          <div className="flex flex-wrap items-center justify-between gap-3">
            <p className="text-dim max-w-2xl text-xs leading-5">{ready ? t('documents.queueHint') : t('documents.notReady')}</p>
            <button className="btn btn-accent flex items-center gap-2" type="submit" disabled={!canSubmit} data-testid="ingest-submit"><Upload size={14} />{t(upload.isPending ? 'documents.submitting' : 'documents.submit')}</button>
          </div>
        </form>
        {receipt?.container === container && <div role="status" className="mt-4 rounded-lg border border-accent bg-bg p-3 text-sm" data-testid="ingest-receipt">{t('documents.enqueued', { id: receipt.id })} <Link className="accent underline" to="/jobs">{t('documents.viewJobs')}</Link></div>}
        {upload.isError && <div role="alert" className="mt-4 space-y-1 break-words text-sm text-red"><p>{upload.error.message}</p>{(!(upload.error instanceof ApiError) || upload.error.status >= 500) && <p>{t('documents.unknownReceipt')}</p>}</div>}
      </section>
      {currentJobs.length > 0 && <section className="panel p-4" aria-labelledby="document-jobs-title">
        <div className="mb-3 flex justify-between gap-3"><h2 id="document-jobs-title" className="text-sm font-semibold">{t('documents.recentJobs')}</h2><Link to="/jobs" className="accent text-xs">{t('documents.viewJobs')}</Link></div>
        <div className="space-y-2">{currentJobs.map(job => <div key={job.id} className="rounded border border-border p-3 text-xs">
          <div className="flex flex-wrap items-center justify-between gap-2"><span className="mono">#{job.id} · {t(job.op === 'ingest-document-text' ? 'documents.textMode' : 'documents.fileMode')}</span><span className={documentStatusClass(job.status)}>{t(`jobs.${job.status}`, job.status)}</span></div>
          {job.last_error && <p className="mt-2 whitespace-pre-wrap break-words text-red">{job.last_error}</p>}
        </div>)}</div><p className="text-dim mt-3 text-xs">{t('documents.jobsHint')}</p>
      </section>}
      <section className="space-y-4" aria-labelledby="documents-title">
        <div className="flex flex-wrap items-center justify-between gap-3"><h2 id="documents-title" className="font-semibold">{t('documents.library')}</h2><button className="btn flex items-center gap-2" onClick={() => { void documents.refetch(); void jobs.refetch(); }} disabled={documents.isFetching}><RefreshCw size={14} />{t('documents.refresh')}</button></div>
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">{['processed', 'processing', 'pending', 'failed'].map(state => <div key={state} className="panel p-3"><p className="text-dim text-xs">{t(`documents.states.${state}`)}</p><p className="mono mt-2 text-xl">{documents.data ? (state === 'processing' ? ['processing', 'parsing', 'analyzing', 'handling'].reduce((sum, value) => sum + (documents.data.counts[value] || 0), 0) : documents.data.counts[state] || 0) : '—'}</p></div>)}</div>
        <div className="flex flex-wrap gap-3">
          <form className="flex min-w-0 flex-1 gap-2" onSubmit={event => { event.preventDefault(); setQuery(search); setOffset(0); }}><input aria-label={t('documents.search')} className="input min-w-0 flex-1" value={search} onChange={event => setSearch(event.target.value)} placeholder={t('documents.search')} /><button className="btn" type="submit" aria-label={t('documents.search')}><Search size={16} /></button></form>
          <select className="input" aria-label={t('documents.statusFilter')} value={status} onChange={event => { setStatus(event.target.value); setOffset(0); }}><option value="">{t('documents.allStatuses')}</option>{STATES.map(state => <option key={state} value={state}>{t(`documents.states.${state}`)}</option>)}</select>
          <select className="input" aria-label={t('documents.kindFilter')} value={kind} onChange={event => { setKind(event.target.value); setOffset(0); }}><option value="">{t('documents.allKinds')}</option>{['text', 'pdf', 'image', 'file'].map(value => <option key={value} value={value}>{t(`documents.kinds.${value}`)}</option>)}</select>
        </div>
        <label className="flex items-center gap-2 text-xs text-dim"><input type="checkbox" checked={duplicates} onChange={event => { setDuplicates(event.target.checked); setOffset(0); }} />{t('documents.includeDuplicates', { count: documents.data?.duplicate_count || 0 })}</label>
        {documents.isError && <p role="alert" className="text-sm text-red">{documents.error.message}</p>}
        <div className="panel overflow-x-auto">
          <table className="tbl"><thead><tr><th>{t('documents.source')}</th><th>{t('documents.statusFilter')}</th><th>{t('documents.chunksTitle')}</th><th>{t('documents.updated')}</th><th><span className="sr-only">{t('documents.detail')}</span></th></tr></thead>
            <tbody>{documents.isPending && container ? <tr><td colSpan={5} className="text-dim py-8 text-center">{t('common.loading')}</td></tr> : !documents.data?.documents.length ? <tr><td colSpan={5} className="text-dim py-8 text-center">{t('documents.empty')}</td></tr> : documents.data.documents.map(doc => <tr key={doc.id}>
              <td className="max-w-xs"><button className="text-left" onClick={() => setSelected(doc.id)}><div className="break-all text-sm">{doc.source || t('documents.untitled')}</div><div className="text-dim mt-1 line-clamp-2 text-xs">{doc.summary || doc.id}</div></button><span className="text-dim text-xs">{t(`documents.kinds.${doc.kind}`)}</span></td>
              <td><span className={documentStatusClass(doc.status)}>{t(`documents.states.${doc.status}`, doc.status)}</span>{doc.duplicate && <span className="text-dim ml-1 text-xs">{t('documents.duplicate')}</span>}</td><td className="mono text-xs">{doc.chunks}</td><td className="text-dim whitespace-nowrap text-xs">{doc.updated_at ? doc.updated_at.slice(0, 19).replace('T', ' ') : '—'}</td><td><button className="btn whitespace-nowrap" onClick={() => setSelected(doc.id)}>{t('documents.detail')}</button></td>
            </tr>)}</tbody>
          </table>
        </div>
        <div className="flex items-center justify-between gap-3 text-xs"><span className="text-dim">{t('documents.total', { count: documents.data?.total || 0 })}</span><div className="flex gap-2"><button className="btn" disabled={!offset} onClick={() => setOffset(Math.max(0, offset - 20))}>{t('documents.previous')}</button><button className="btn" disabled={offset + 20 >= (documents.data?.total || 0)} onClick={() => setOffset(offset + 20)}>{t('documents.next')}</button></div></div>
      </section>
      {selected && <DocumentViewer key={container + selected} container={container} id={selected} onClose={() => setSelected('')} />}
    </div>
  );
}
