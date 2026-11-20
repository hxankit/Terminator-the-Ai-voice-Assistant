import { useEffect, useRef, useState } from 'react'
import { FileText, Loader2, Search, Trash2, Upload } from 'lucide-react'
import { api, type Doc } from '../api'
import { Badge, Button, Card, ErrorNote, Input, PageHeader, fmtDate } from '../components/ui'

export default function Knowledge() {
  const [docs, setDocs] = useState<Doc[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [q, setQ] = useState('')
  const [hits, setHits] = useState<any[] | null>(null)
  const input = useRef<HTMLInputElement>(null)
  const load = () => api.get<Doc[]>('/documents').then(setDocs).catch(e => setError(e.message))
  useEffect(() => { load() }, [])

  async function upload(files: FileList | null) {
    setBusy(true); setError(null)
    for (const f of Array.from(files || [])) {
      const fd = new FormData(); fd.append('file', f)
      try { const d = await api.post<Doc>('/documents', fd); if (d.status === 'failed') setError(`${f.name}: ${d.error}`) }
      catch (e: any) { setError(`${f.name}: ${e.message}`) }
    }
    setBusy(false); if (input.current) input.current.value = ''; load()
  }

  async function search() {
    if (!q.trim()) return
    try { setHits(await api.post('/kb/search', { query: q })) } catch (e: any) { setError(e.message) }
  }

  return (
    <div className="mx-auto max-w-4xl p-6">
      <PageHeader title="Knowledge base" desc="Upload documents so the assistant can answer questions grounded in them, with source references.">
        <input ref={input} type="file" multiple hidden accept=".pdf,.docx,.txt,.md,.csv,.xlsx,.xls,.json" onChange={e => upload(e.target.files)} />
        <Button onClick={() => input.current?.click()} disabled={busy}>{busy ? <Loader2 size={15} className="animate-spin" /> : <Upload size={15} />}Upload</Button>
      </PageHeader>
      <ErrorNote error={error} />
      <form onSubmit={e => { e.preventDefault(); search() }} className="flex gap-2 mb-4">
        <Input placeholder="Test retrieval: search your documents…" value={q} onChange={e => setQ(e.target.value)} />
        <Button variant="outline" type="submit"><Search size={15} />Search</Button>
      </form>
      {hits && (
        <Card className="mb-6 p-4 space-y-3">
          {!hits.length && <p className="text-sm text-zinc-500">No matching passages.</p>}
          {hits.map((h, i) => (
            <div key={i} className="text-sm"><div className="flex gap-2 items-center mb-1"><Badge>{h.filename}</Badge><span className="text-xs text-zinc-400">{h.location} · score {h.score}</span></div>
              <p className="text-zinc-600 dark:text-zinc-400 line-clamp-3 whitespace-pre-wrap">{h.text}</p></div>))}
        </Card>)}
      <Card>
        {!docs.length && <p className="p-8 text-center text-sm text-zinc-500">No documents yet. Supported: PDF, DOCX, TXT, Markdown, CSV, Excel, JSON.</p>}
        <ul className="divide-y divide-zinc-200 dark:divide-zinc-800">
          {docs.map(d => (
            <li key={d.id} className="flex items-center gap-3 px-4 py-3">
              <FileText size={18} className="text-zinc-400 shrink-0" />
              <div className="min-w-0 flex-1">
                <div className="truncate text-sm font-medium">{d.filename}</div>
                <div className="text-xs text-zinc-500">{(d.size / 1024).toFixed(1)} KB · {d.chunks ?? 0} chunks · {fmtDate(d.created_at)}{d.error && <span className="text-red-600"> · {d.error}</span>}</div>
              </div>
              <Badge tone={d.status === 'ready' ? 'green' : d.status === 'failed' ? 'red' : 'amber'}>{d.status}</Badge>
              {d.retrieval && <Badge tone={d.retrieval === 'vector' ? 'blue' : 'zinc'}>{d.retrieval}</Badge>}
              {d.status === 'ready' && d.retrieval !== 'vector' && (
                <button title="Index again with the embedding model (better search)" onClick={async () => { try { await api.post(`/documents/${d.id}/reindex`); load() } catch (e: any) { setError(e.message) } }}
                  className="rounded px-2 py-1 text-xs text-accent hover:bg-accent/10">Re-index</button>)}
              <button aria-label={`Delete ${d.filename}`} onClick={async () => { if (confirm(`Delete ${d.filename}?`)) { await api.del(`/documents/${d.id}`); load() } }} className="p-1.5 rounded text-zinc-400 hover:text-red-600 hover:bg-red-50 dark:hover:bg-red-950/30"><Trash2 size={15} /></button>
            </li>))}
        </ul>
      </Card>
      <p className="mt-3 text-xs text-zinc-500">“vector” = semantic search via your provider’s embedding model. “keyword” = full-text search (set an embedding model in Settings → Models for better recall).</p>
    </div>
  )
}
