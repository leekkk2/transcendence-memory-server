import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { X } from 'lucide-react';
import { documentStatusClass, useDocument } from '../lib/documents';

export function DocumentViewer({ container, id, onClose }: { container: string; id: string; onClose: () => void }) {
  const { t } = useTranslation();
  const [textOffset, setTextOffset] = useState(0);
  const [chunkOffset, setChunkOffset] = useState(0);
  const result = useDocument(container, id, textOffset, chunkOffset);
  const doc = result.data;
  return (
    <section className="panel min-w-0 p-4 sm:p-5" aria-label={t('documents.detail')} data-testid="document-detail">
      <div className="mb-4 flex items-start justify-between gap-3">
        <div className="min-w-0"><h2 className="break-words font-semibold">{doc?.source || t('documents.untitled')}</h2><p className="text-dim mono mt-1 break-all text-xs">{id}</p></div>
        <button className="btn shrink-0" onClick={onClose} aria-label={t('documents.close')}><X size={16} /></button>
      </div>
      {result.isPending && <p className="text-dim">{t('common.loading')}</p>}
      {result.isError && <p role="alert" className="break-words text-sm text-red">{result.error.message}</p>}
      {doc && <>
        <div className="mb-3 flex flex-wrap gap-2"><span className={documentStatusClass(doc.status)}>{t(`documents.states.${doc.status}`, doc.status)}</span><span className="badge badge-dim">{t('documents.charCount', { count: doc.chars })}</span><span className="badge badge-dim">{t('documents.chunkCount', { count: doc.chunks_total })}</span></div>
        {doc.error && <p role="alert" className="mb-3 whitespace-pre-wrap break-words rounded border border-red p-3 text-sm text-red">{doc.error}</p>}
        <h3 className="mb-1 text-sm font-semibold">{t('documents.parsedText')}</h3>
        <p className="text-dim mb-3 text-xs">{t('documents.originalHint')}</p>
        <pre className="max-h-96 overflow-y-auto whitespace-pre-wrap break-words rounded-lg bg-bg-code p-4 font-sans text-sm leading-7" data-testid="document-text">{doc.text || t('documents.noText')}</pre>
        <div className="my-3 flex flex-wrap items-center justify-between gap-2 text-xs">
          <span className="text-dim">{t('documents.textRange', { from: doc.text_length ? textOffset + 1 : 0, to: Math.min(textOffset + doc.text.length, doc.text_length), total: doc.text_length })}</span>
          <div className="flex gap-2"><button className="btn" disabled={!textOffset} onClick={() => setTextOffset(Math.max(0, textOffset - 12000))}>{t('documents.previous')}</button><button className="btn" disabled={!doc.text_truncated} onClick={() => setTextOffset(textOffset + 12000)}>{t('documents.next')}</button></div>
        </div>
        <h3 className="mb-2 text-sm font-semibold">{t('documents.chunksTitle')}</h3>
        <div className="space-y-2">{doc.chunks.map(chunk => <details key={chunk.id} className="rounded border border-border p-3 text-sm"><summary className="cursor-pointer break-all">{t('documents.chunkNumber', { count: chunk.order + 1 })} · <span className="text-dim mono text-xs">{chunk.id}</span></summary><p className="mt-3 whitespace-pre-wrap break-words leading-6">{chunk.content}</p></details>)}</div>
        {doc.chunks_total > 10 && <div className="mt-3 flex justify-end gap-2"><button className="btn" disabled={!chunkOffset} onClick={() => setChunkOffset(Math.max(0, chunkOffset - 10))}>{t('documents.previous')}</button><button className="btn" disabled={chunkOffset + 10 >= doc.chunks_total} onClick={() => setChunkOffset(chunkOffset + 10)}>{t('documents.next')}</button></div>}
      </>}
    </section>
  );
}
