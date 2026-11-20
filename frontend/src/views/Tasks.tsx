import { useEffect, useState, type FormEvent } from 'react'
import { Calendar, Trash2 } from 'lucide-react'
import { api, type Task } from '../api'
import { Badge, Button, Card, ErrorNote, Input, PageHeader, cx, fmtDate } from '../components/ui'

export default function Tasks() {
  const [tasks, setTasks] = useState<Task[]>([])
  const [status, setStatus] = useState('open')
  const [title, setTitle] = useState(''); const [due, setDue] = useState(''); const [priority, setPriority] = useState('normal')
  const [error, setError] = useState<string | null>(null)
  const load = () => api.get<Task[]>(`/tasks?status=${status}`).then(setTasks).catch(e => setError(e.message))
  useEffect(() => { load() }, [status]) // eslint-disable-line

  async function add(e: FormEvent) {
    e.preventDefault()
    try { await api.post('/tasks', { title, priority, due_at: due ? new Date(due).toISOString() : null }); setTitle(''); setDue(''); load() }
    catch (err: any) { setError(err.message) }
  }
  const toggle = async (t: Task) => { await api.put(`/tasks/${t.id}`, { ...t, status: t.status === 'open' ? 'done' : 'open' }); load() }
  const overdue = (t: Task) => t.status === 'open' && t.due_at && new Date(t.due_at) < new Date()

  return (
    <div className="mx-auto max-w-3xl p-6">
      <PageHeader title="Tasks & reminders" desc="You can also ask in chat: “remind me to renew the SSL cert on Friday at 10am”." />
      <form onSubmit={add} className="flex flex-wrap gap-2 mb-4">
        <Input required placeholder="New task…" value={title} onChange={e => setTitle(e.target.value)} className="flex-1 min-w-[200px]" />
        <Input type="datetime-local" aria-label="Due date" value={due} onChange={e => setDue(e.target.value)} className="w-auto" />
        <select aria-label="Priority" value={priority} onChange={e => setPriority(e.target.value)} className="rounded-lg border border-zinc-300 dark:border-zinc-700 bg-transparent px-2 text-sm"><option>low</option><option>normal</option><option>high</option></select>
        <Button type="submit">Add</Button>
      </form>
      <div className="flex gap-2 mb-4">{['open', 'done', 'all'].map(s => <Button key={s} variant={status === s ? 'primary' : 'outline'} onClick={() => setStatus(s)} className="capitalize">{s}</Button>)}</div>
      <ErrorNote error={error} />
      <Card>
        {!tasks.length && <p className="p-8 text-center text-sm text-zinc-500">Nothing here.</p>}
        <ul className="divide-y divide-zinc-200 dark:divide-zinc-800">
          {tasks.map(t => (
            <li key={t.id} className="flex items-center gap-3 px-4 py-3">
              <input type="checkbox" aria-label={`Mark ${t.title} ${t.status === 'open' ? 'done' : 'open'}`} checked={t.status === 'done'} onChange={() => toggle(t)} className="h-4 w-4 accent-[var(--color-accent)]" />
              <div className="flex-1 min-w-0">
                <div className={cx('text-sm', t.status === 'done' && 'line-through text-zinc-400')}>{t.title}</div>
                {t.due_at && <div className={cx('text-xs flex items-center gap-1 mt-0.5', overdue(t) ? 'text-red-600' : 'text-zinc-500')}><Calendar size={11} />{fmtDate(t.due_at)}{overdue(t) && ' · overdue'}</div>}
              </div>
              {t.priority !== 'normal' && <Badge tone={t.priority === 'high' ? 'red' : 'zinc'}>{t.priority}</Badge>}
              <button aria-label="Delete task" onClick={async () => { await api.del(`/tasks/${t.id}`); load() }} className="p-1.5 rounded text-zinc-400 hover:text-red-600"><Trash2 size={14} /></button>
            </li>))}
        </ul>
      </Card>
    </div>
  )
}
