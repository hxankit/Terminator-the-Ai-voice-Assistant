import { useEffect, useRef, useState } from 'react'
import { Pencil, Phone, Plus, Trash2, Upload, Search } from 'lucide-react'
import { api } from '../api'
import { Badge, Button, Card, ErrorNote, Input, Label, Modal, PageHeader, Textarea } from '../components/ui'

interface Contact { id: string; name: string; phones: string[]; emails: string[]; aliases: string[]; notes: string }
type Draft = { id?: string; name: string; phones: string; emails: string; aliases: string; notes: string }
const split = (s: string) => s.split(/[,\n]/).map(x => x.trim()).filter(Boolean)

export default function Contacts({ countryCode }: { countryCode?: string }) {
  const [items, setItems] = useState<Contact[]>([])
  const [q, setQ] = useState('')
  const [edit, setEdit] = useState<Draft | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const file = useRef<HTMLInputElement>(null)
  const load = (query = q) => api.get<Contact[]>(`/contacts${query ? `?q=${encodeURIComponent(query)}` : ''}`).then(setItems).catch(e => setError(e.message))
  useEffect(() => { const t = setTimeout(() => load(q), q ? 250 : 0); return () => clearTimeout(t) }, [q]) // eslint-disable-line

  async function save() {
    if (!edit) return
    const body = { name: edit.name, phones: split(edit.phones), emails: split(edit.emails), aliases: split(edit.aliases), notes: edit.notes }
    try {
      if (edit.id) await api.put(`/contacts/${edit.id}`, body); else await api.post('/contacts', body)
      setEdit(null); setError(null); load()
    } catch (e: any) { setError(e.message) }
  }

  async function importVcf(files: FileList | null) {
    const f = files?.[0]; if (!f) return
    const fd = new FormData(); fd.append('file', f)
    try {
      const r = await api.post<{ imported: number; merged: number; skipped: number }>('/contacts/import', fd)
      setNotice(`Imported ${r.imported} new, updated ${r.merged}, skipped ${r.skipped} without a usable number.`); setError(null); load()
    } catch (e: any) { setError(e.message) }
    if (file.current) file.current.value = ''
  }

  return (
    <div className="mx-auto max-w-3xl p-6">
      <PageHeader title="Contacts" desc="Used for “send a WhatsApp to Mom…”. Add nicknames so the names you say match.">
        <input ref={file} type="file" hidden accept=".vcf,text/vcard" onChange={e => importVcf(e.target.files)} />
        <Button variant="outline" onClick={() => file.current?.click()}><Upload size={15} />Import .vcf</Button>
        <Button onClick={() => setEdit({ name: '', phones: '', emails: '', aliases: '', notes: '' })}><Plus size={15} />Add</Button>
      </PageHeader>
      {!countryCode && <p className="mb-3 rounded-lg bg-amber-50 dark:bg-amber-950/30 px-3 py-2 text-xs text-amber-800 dark:text-amber-300">
        Tip: set your default country code in Settings so numbers saved without “+” work.</p>}
      <ErrorNote error={error} />
      {notice && <p className="mb-3 rounded-lg bg-emerald-50 dark:bg-emerald-950/30 px-3 py-2 text-sm text-emerald-800 dark:text-emerald-300">{notice}</p>}
      <div className="relative mb-4"><Search size={14} className="absolute left-3 top-3 text-zinc-400" />
        <Input aria-label="Search contacts" placeholder="Search by name or nickname" value={q} onChange={e => setQ(e.target.value)} className="pl-8" /></div>
      <Card>
        {!items.length && <p className="p-8 text-center text-sm text-zinc-500">{q ? 'No matches.' : 'No contacts yet. Import a .vcf export from your phone (Contacts → Export) or add people manually.'}</p>}
        <ul className="divide-y divide-zinc-200 dark:divide-zinc-800">
          {items.map(c => (
            <li key={c.id} className="flex items-center gap-3 px-4 py-3">
              <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-accent/10 text-sm font-semibold text-accent">{c.name[0]?.toUpperCase()}</div>
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-1.5 text-sm font-medium">{c.name}{c.aliases.map(a => <Badge key={a}>{a}</Badge>)}</div>
                <div className="flex items-center gap-1 text-xs text-zinc-500"><Phone size={11} />{c.phones.join(', ') || 'no phone'}</div>
              </div>
              <button aria-label={`Edit ${c.name}`} onClick={() => setEdit({ id: c.id, name: c.name, phones: c.phones.join(', '), emails: c.emails.join(', '), aliases: c.aliases.join(', '), notes: c.notes || '' })} className="rounded p-1.5 text-zinc-400 hover:bg-zinc-100 dark:hover:bg-zinc-800"><Pencil size={14} /></button>
              <button aria-label={`Delete ${c.name}`} onClick={async () => { if (confirm(`Delete ${c.name}?`)) { await api.del(`/contacts/${c.id}`); load() } }} className="rounded p-1.5 text-zinc-400 hover:text-red-600"><Trash2 size={14} /></button>
            </li>))}
        </ul>
      </Card>
      {edit && (
        <Modal title={edit.id ? 'Edit contact' : 'Add contact'} onClose={() => setEdit(null)}>
          <div className="space-y-3">
            <div><Label>Name</Label><Input autoFocus value={edit.name} onChange={e => setEdit({ ...edit, name: e.target.value })} /></div>
            <div><Label hint="what you call them when talking, comma-separated">Nicknames</Label><Input placeholder="Mom, Mummy" value={edit.aliases} onChange={e => setEdit({ ...edit, aliases: e.target.value })} /></div>
            <div><Label hint="with country code, e.g. +91 98765 43210">Phone</Label><Input value={edit.phones} onChange={e => setEdit({ ...edit, phones: e.target.value })} /></div>
            <div><Label>Email</Label><Input value={edit.emails} onChange={e => setEdit({ ...edit, emails: e.target.value })} /></div>
            <div><Label>Notes</Label><Textarea rows={2} value={edit.notes} onChange={e => setEdit({ ...edit, notes: e.target.value })} /></div>
            <ErrorNote error={error} />
            <div className="flex justify-end gap-2"><Button variant="outline" onClick={() => setEdit(null)}>Cancel</Button><Button onClick={save} disabled={!edit.name.trim()}>Save</Button></div>
          </div>
        </Modal>)}
    </div>
  )
}
