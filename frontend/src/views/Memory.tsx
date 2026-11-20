import { useEffect, useState } from 'react'
import { Pencil, Plus, Trash2 } from 'lucide-react'
import { api, type Memory as Mem } from '../api'
import { Badge, Button, Card, ErrorNote, Input, Label, Modal, PageHeader, Textarea, fmtDate, cx } from '../components/ui'

const KINDS = { preference: 'Preference', long_term: 'Long-term', project: 'Project' } as const

export default function Memory({ autoMemory, onToggleAuto }: { autoMemory: boolean; onToggleAuto: (v: boolean) => void }) {
  const [items, setItems] = useState<Mem[]>([])
  const [filter, setFilter] = useState<string>('')
  const [q, setQ] = useState('')
  const [edit, setEdit] = useState<Partial<Mem> | null>(null)
  const [error, setError] = useState<string | null>(null)
  const load = () => api.get<Mem[]>(`/memories?${new URLSearchParams({ ...(filter && { kind: filter }), ...(q && { q }) })}`).then(setItems).catch(e => setError(e.message))
  useEffect(() => { load() }, [filter]) // eslint-disable-line

  async function save() {
    try {
      if (edit?.id) await api.put(`/memories/${edit.id}`, { content: edit.content, kind: edit.kind })
      else await api.post('/memories', { content: edit?.content, kind: edit?.kind || 'long_term' })
      setEdit(null); load()
    } catch (e: any) { setError(e.message) }
  }

  return (
    <div className="mx-auto max-w-4xl p-6">
      <PageHeader title="Memory" desc="What the assistant knows about you across conversations. Relevant memories are added to each request.">
        <Button onClick={() => setEdit({ kind: 'long_term', content: '' })}><Plus size={15} />Add memory</Button>
      </PageHeader>
      <Card className="p-4 mb-4 flex items-center justify-between gap-4">
        <div><div className="text-sm font-medium">Automatic memory</div><div className="text-xs text-zinc-500">Saves only durable facts and preferences. Small talk and anything that looks like a secret are skipped.</div></div>
        <button role="switch" aria-checked={autoMemory} aria-label="Automatic memory" onClick={() => onToggleAuto(!autoMemory)}
          className={cx('relative h-6 w-11 shrink-0 rounded-full transition-colors', autoMemory ? 'bg-accent' : 'bg-zinc-300 dark:bg-zinc-700')}>
          <span className={cx('absolute top-0.5 h-5 w-5 rounded-full bg-white shadow transition-all', autoMemory ? 'left-[22px]' : 'left-0.5')} /></button>
      </Card>
      <div className="flex flex-wrap gap-2 mb-4">
        {[['', 'All'], ...Object.entries(KINDS)].map(([k, n]) => <Button key={k} variant={filter === k ? 'primary' : 'outline'} onClick={() => setFilter(k)}>{n}</Button>)}
        <form className="ml-auto" onSubmit={e => { e.preventDefault(); load() }}><Input placeholder="Search memories" value={q} onChange={e => setQ(e.target.value)} className="w-56" /></form>
      </div>
      <ErrorNote error={error} />
      <Card>
        {!items.length && <p className="p-8 text-center text-sm text-zinc-500">No memories yet.</p>}
        <ul className="divide-y divide-zinc-200 dark:divide-zinc-800">
          {items.map(m => (
            <li key={m.id} className="flex items-start gap-3 px-4 py-3 group">
              <div className="flex-1 min-w-0"><p className="text-sm">{m.content}</p>
                <div className="mt-1 flex gap-2 items-center"><Badge tone={m.kind === 'preference' ? 'blue' : 'zinc'}>{KINDS[m.kind]}</Badge><span className="text-xs text-zinc-400">{m.source === 'auto' ? 'auto-saved' : `added by ${m.source}`} · {fmtDate(m.updated_at)}</span></div></div>
              <button aria-label="Edit" onClick={() => setEdit(m)} className="p-1.5 rounded text-zinc-400 hover:bg-zinc-100 dark:hover:bg-zinc-800"><Pencil size={14} /></button>
              <button aria-label="Delete" onClick={async () => { await api.del(`/memories/${m.id}`); load() }} className="p-1.5 rounded text-zinc-400 hover:text-red-600 hover:bg-red-50 dark:hover:bg-red-950/30"><Trash2 size={14} /></button>
            </li>))}
        </ul>
      </Card>
      {edit && (
        <Modal title={edit.id ? 'Edit memory' : 'Add memory'} onClose={() => setEdit(null)}>
          <div className="space-y-3">
            <div><Label>Type</Label><select value={edit.kind} onChange={e => setEdit({ ...edit, kind: e.target.value as Mem['kind'] })} className="w-full rounded-lg border border-zinc-300 dark:border-zinc-700 bg-transparent px-3 py-2 text-sm">
              <option value="long_term">Long-term fact</option><option value="preference">Preference</option></select></div>
            <div><Label>Content</Label><Textarea rows={3} value={edit.content} onChange={e => setEdit({ ...edit, content: e.target.value })} placeholder="e.g. I deploy Node apps with PM2 behind Nginx on Ubuntu" /></div>
            <div className="flex justify-end gap-2"><Button variant="outline" onClick={() => setEdit(null)}>Cancel</Button><Button onClick={save} disabled={!edit.content?.trim()}>Save</Button></div>
          </div>
        </Modal>)}
    </div>
  )
}
