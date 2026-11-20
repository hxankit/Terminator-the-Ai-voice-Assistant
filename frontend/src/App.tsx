import { useCallback, useEffect, useRef, useState } from 'react'
import {
  Activity as ActivityIcon, Bell, BookOpen, Bot, Brain, CheckSquare, FolderKanban, LogOut, Menu, MoreHorizontal,
  Moon, PanelLeftClose, Users, Pin, Plus, Search, Settings as SettingsIcon, Sun, Trash2, Pencil, X, Wifi, WifiOff, Monitor,
} from 'lucide-react'
import { api, type Conversation, type Notification, type Project, type Provider, type User } from './api'
import { rt, type RtStatus } from './lib/realtime'
import { Button, Input, Label, Modal, Textarea, cx, fmtDate } from './components/ui'
import Auth from './views/Auth'
import Chat from './views/Chat'
import Knowledge from './views/Knowledge'
import Memory from './views/Memory'
import Tasks from './views/Tasks'
import Contacts from './views/Contacts'
import Settings from './views/Settings'
import Activity from './views/Activity'

type View = 'chat' | 'knowledge' | 'memory' | 'tasks' | 'contacts' | 'settings' | 'activity'
const NAV: { view: View; label: string; icon: any }[] = [
  { view: 'knowledge', label: 'Knowledge base', icon: BookOpen },
  { view: 'memory', label: 'Memory', icon: Brain },
  { view: 'tasks', label: 'Tasks', icon: CheckSquare },
  { view: 'contacts', label: 'Contacts', icon: Users },
  { view: 'activity', label: 'Agents & activity', icon: ActivityIcon },
  { view: 'settings', label: 'Models & settings', icon: SettingsIcon },
]

function parseHash(): { view: View; cid: string | null } {
  const [, a, b] = location.hash.split('/')
  if (a === 'c' && b) return { view: 'chat', cid: b }
  if (NAV.some(n => n.view === a)) return { view: a as View, cid: null }
  return { view: 'chat', cid: null }
}

function applyTheme(theme?: string) {
  const dark = theme === 'dark' || ((!theme || theme === 'system') && matchMedia('(prefers-color-scheme: dark)').matches)
  document.documentElement.classList.toggle('dark', dark)
}

export default function App() {
  const [user, setUser] = useState<User | null | false>(null)
  useEffect(() => { api.get<User>('/auth/me').then(setUser).catch(() => setUser(false)) }, [])
  useEffect(() => { applyTheme(user ? user.prefs?.theme : undefined) }, [user])
  if (user === null) return <div className="flex h-full items-center justify-center text-sm text-zinc-500">Loading…</div>
  if (user === false) return <Auth onAuthed={setUser} />
  return <Shell user={user} setUser={setUser} />
}

function Shell({ user, setUser }: { user: User; setUser: (u: User | false) => void }) {
  const [{ view, cid }, setRoute] = useState(parseHash)
  const [convs, setConvs] = useState<Conversation[]>([])
  const [providers, setProviders] = useState<Provider[]>([])
  const [projects, setProjects] = useState<Project[]>([])
  const [projectId, setProjectId] = useState<string | null>(null)
  const [q, setQ] = useState('')
  const [sidebar, setSidebar] = useState(() => innerWidth >= 768)
  const [notifs, setNotifs] = useState<Notification[]>([])
  const [toasts, setToasts] = useState<Notification[]>([])
  const [showNotifs, setShowNotifs] = useState(false)
  const [rtStatus, setRtStatus] = useState<RtStatus>('closed')
  const [projectModal, setProjectModal] = useState<Partial<Project> | null>(null)

  useEffect(() => { const f = () => setRoute(parseHash()); addEventListener('hashchange', f); return () => removeEventListener('hashchange', f) }, [])
  const go = (h: string) => { location.hash = h; if (innerWidth < 768) setSidebar(false) }

  const loadConvs = useCallback(() => {
    const p = new URLSearchParams({ limit: '50', ...(q && { q }), ...(projectId && { project_id: projectId }) })
    api.get<{ items: Conversation[] }>(`/conversations?${p}`).then(r => setConvs(r.items)).catch(() => {})
  }, [q, projectId])
  useEffect(() => { const t = setTimeout(loadConvs, q ? 250 : 0); return () => clearTimeout(t) }, [loadConvs, q])
  const loadProviders = useCallback(() => api.get<Provider[]>('/providers').then(setProviders).catch(() => {}), [])
  const loadProjects = useCallback(() => api.get<Project[]>('/projects').then(setProjects).catch(() => {}), [])
  useEffect(() => { loadProviders(); loadProjects() }, [loadProviders, loadProjects])

  // realtime channel: assistant-initiated notifications (reminders, memory saves, ...)
  useEffect(() => {
    rt.connect()
    api.get<Notification[]>('/notifications').then(setNotifs).catch(() => {})
    const offS = rt.onStatus(setRtStatus)
    const off = rt.on(ev => {
      if (ev.type !== 'notification') return
      const n: Notification = ev.item
      setNotifs(x => [n, ...x])
      setToasts(x => [...x, n])
      setTimeout(() => setToasts(x => x.filter(t => t.id !== n.id)), n.kind === 'reminder' ? 15000 : 6000)
      if (n.kind === 'reminder' && 'Notification' in window && window.Notification.permission === 'granted' && document.hidden)
        new window.Notification(n.title, { body: n.body })
    })
    return () => { off(); offS(); rt.disconnect() }
  }, [])
  const unread = notifs.filter(n => !n.read).length

  useEffect(() => {
    const m = matchMedia('(prefers-color-scheme: dark)'); const f = () => applyTheme(user.prefs?.theme)
    m.addEventListener('change', f); return () => m.removeEventListener('change', f)
  }, [user.prefs?.theme])

  const setPrefs = async (p: Record<string, any>) => setUser(await api.patch<User>('/auth/prefs', p))
  const logout = async () => { rt.disconnect(); await api.post('/auth/logout'); setUser(false) }
  const cycleTheme = () => setPrefs({ theme: ({ system: 'light', light: 'dark', dark: 'system' } as any)[user.prefs?.theme || 'system'] })
  const ThemeIcon = { light: Sun, dark: Moon, system: Monitor }[(user.prefs?.theme || 'system') as 'light'] || Monitor

  async function convAction(c: Conversation, action: 'rename' | 'pin' | 'delete') {
    if (action === 'delete') { if (!confirm(`Delete “${c.title}”?`)) return; await api.del(`/conversations/${c.id}`); if (cid === c.id) go('#/') }
    if (action === 'pin') await api.patch(`/conversations/${c.id}`, { pinned: !c.pinned })
    if (action === 'rename') { const t = prompt('Rename conversation', c.title); if (!t?.trim()) return; await api.patch(`/conversations/${c.id}`, { title: t.trim() }) }
    loadConvs()
  }

  async function saveProject() {
    if (!projectModal?.name?.trim()) return
    if (projectModal.id) await api.put(`/projects/${projectModal.id}`, { name: projectModal.name, description: projectModal.description || '' })
    else { const p = await api.post<Project>('/projects', { name: projectModal.name, description: projectModal.description || '' }); setProjectId(p.id) }
    setProjectModal(null); loadProjects()
  }

  return (
    <div className="flex h-full">
      {sidebar && <div className="fixed inset-0 z-30 bg-black/30 md:hidden" onClick={() => setSidebar(false)} />}
      <aside className={cx('fixed z-40 h-full w-72 shrink-0 flex-col border-r border-zinc-200 dark:border-zinc-800 bg-zinc-50 dark:bg-zinc-900 md:static', sidebar ? 'flex' : 'hidden')}>
        <div className="flex items-center gap-2 px-3 pt-3 pb-2">
          <div className="rounded-lg bg-accent p-1 text-white"><Bot size={18} /></div><span className="font-semibold">Terminator</span>
          <span className="ml-1" title={rtStatus === 'open' ? 'Live connection' : rtStatus === 'connecting' ? 'Connecting…' : 'Offline. Reconnecting'}>
            {rtStatus === 'open' ? <Wifi size={13} className="text-emerald-500" /> : <WifiOff size={13} className="text-amber-500" />}</span>
          <button aria-label="Notifications" onClick={() => setShowNotifs(true)} className="relative ml-auto rounded p-1.5 hover:bg-zinc-200 dark:hover:bg-zinc-800">
            <Bell size={16} />{unread > 0 && <span className="absolute -top-0.5 -right-0.5 min-w-4 rounded-full bg-red-500 px-1 text-[10px] font-semibold leading-4 text-white">{unread}</span>}</button>
          <button aria-label="Close sidebar" onClick={() => setSidebar(false)} className="rounded p-1.5 hover:bg-zinc-200 dark:hover:bg-zinc-800"><PanelLeftClose size={16} /></button>
        </div>
        <div className="px-3 space-y-2">
          <Button className="w-full justify-start" onClick={() => go('#/')}><Plus size={16} />New chat</Button>
          <div className="relative"><Search size={14} className="absolute left-2.5 top-2.5 text-zinc-400" />
            <Input aria-label="Search conversations" placeholder="Search conversations" value={q} onChange={e => setQ(e.target.value)} className="pl-8 py-1.5" /></div>
        </div>
        <div className="mt-3 px-3">
          <div className="flex items-center justify-between text-[11px] font-semibold uppercase tracking-wide text-zinc-500"><span>Projects</span>
            <button aria-label="New project" onClick={() => setProjectModal({})} className="rounded p-0.5 hover:bg-zinc-200 dark:hover:bg-zinc-800"><Plus size={13} /></button></div>
          <div className="mt-1 flex flex-wrap gap-1">
            <button onClick={() => setProjectId(null)} className={cx('rounded-md px-2 py-0.5 text-xs', !projectId ? 'bg-zinc-200 dark:bg-zinc-800 font-medium' : 'text-zinc-500 hover:bg-zinc-200 dark:hover:bg-zinc-800')}>All</button>
            {projects.map(p => (
              <span key={p.id} className="group inline-flex">
                <button onClick={() => setProjectId(p.id === projectId ? null : p.id)} title={p.description} className={cx('inline-flex items-center gap-1 rounded-md px-2 py-0.5 text-xs', projectId === p.id ? 'bg-accent/15 text-accent font-medium' : 'text-zinc-600 dark:text-zinc-400 hover:bg-zinc-200 dark:hover:bg-zinc-800')}>
                  <FolderKanban size={11} />{p.name}</button>
                {projectId === p.id && <button aria-label={`Edit project ${p.name}`} onClick={() => setProjectModal(p)} className="px-0.5 text-zinc-400 hover:text-zinc-700"><Pencil size={11} /></button>}
              </span>))}
          </div>
        </div>
        <nav aria-label="Conversations" className="mt-3 flex-1 overflow-y-auto scroll-thin px-2">
          {!convs.length && <p className="px-2 py-4 text-xs text-zinc-500">{q ? 'No matches.' : 'No conversations yet.'}</p>}
          {convs.map(c => <ConvItem key={c.id} c={c} active={c.id === cid} onOpen={() => go(`#/c/${c.id}`)} onAction={a => convAction(c, a)} />)}
        </nav>
        <div className="border-t border-zinc-200 dark:border-zinc-800 p-2 space-y-0.5">
          {NAV.map(n => (
            <button key={n.view} onClick={() => go(`#/${n.view}`)} className={cx('flex w-full items-center gap-2.5 rounded-lg px-2.5 py-1.5 text-sm', view === n.view ? 'bg-zinc-200 dark:bg-zinc-800 font-medium' : 'text-zinc-600 dark:text-zinc-400 hover:bg-zinc-200/60 dark:hover:bg-zinc-800/60')}>
              <n.icon size={16} />{n.label}</button>))}
          <div className="flex items-center gap-2 px-2.5 pt-2 text-sm">
            <div className="flex h-7 w-7 items-center justify-center rounded-full bg-accent/15 text-xs font-semibold text-accent">{(user.name || user.email)[0].toUpperCase()}</div>
            <span className="truncate flex-1 text-zinc-600 dark:text-zinc-400">{user.name || user.email}</span>
            <button aria-label={`Theme: ${user.prefs?.theme || 'system'}`} title={`Theme: ${user.prefs?.theme || 'system'}`} onClick={cycleTheme} className="rounded p-1.5 hover:bg-zinc-200 dark:hover:bg-zinc-800"><ThemeIcon size={15} /></button>
            <button aria-label="Sign out" title="Sign out" onClick={logout} className="rounded p-1.5 hover:bg-zinc-200 dark:hover:bg-zinc-800"><LogOut size={15} /></button>
          </div>
        </div>
      </aside>

      <main className="relative flex min-w-0 flex-1 flex-col">
        {!sidebar && <button aria-label="Open sidebar" onClick={() => setSidebar(true)} className="absolute left-3 top-3 z-20 rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-1.5 shadow-sm"><Menu size={18} /></button>}
        <div className={cx('flex-1 min-h-0', view !== 'chat' && 'overflow-y-auto scroll-thin', !sidebar && view !== 'chat' && 'pt-10')}>
          {view === 'chat' && <Chat cid={cid} projectId={projectId} providers={providers} defaultModel={user.prefs || {}}
            onCreated={id => { history.replaceState(null, '', `#/c/${id}`); setRoute({ view: 'chat', cid: id }); loadConvs() }} onActivity={loadConvs} />}
          {view === 'knowledge' && <Knowledge />}
          {view === 'memory' && <Memory autoMemory={user.prefs?.auto_memory !== false} onToggleAuto={v => setPrefs({ auto_memory: v })} />}
          {view === 'tasks' && <Tasks />}
          {view === 'contacts' && <Contacts countryCode={user.prefs?.default_country_code} />}
          {view === 'settings' && <Settings providers={providers} reload={loadProviders} user={user} setPrefs={setPrefs} />}
          {view === 'activity' && <Activity />}
        </div>
      </main>

      {/* live toasts for assistant-initiated messages */}
      <div className="fixed right-4 top-4 z-50 flex w-80 flex-col gap-2" aria-live="polite">
        {toasts.map(t => (
          <div key={t.id} className="rounded-xl border border-zinc-200 dark:border-zinc-700 bg-white dark:bg-zinc-900 p-3 shadow-lg">
            <div className="flex items-start gap-2">
              {t.kind === 'reminder' ? <Bell size={16} className="mt-0.5 text-accent" /> : <Brain size={16} className="mt-0.5 text-accent" />}
              <div className="min-w-0 flex-1"><div className="text-sm font-medium">{t.title}</div>{t.body && <div className="text-xs text-zinc-500 mt-0.5">{t.body}</div>}</div>
              <button aria-label="Dismiss" onClick={() => setToasts(x => x.filter(y => y.id !== t.id))}><X size={14} /></button>
            </div>
          </div>))}
      </div>

      {showNotifs && (
        <Modal title="Notifications" onClose={async () => { setShowNotifs(false); if (unread) { await api.post('/notifications/read'); setNotifs(n => n.map(x => ({ ...x, read: true }))) } }}>
          {'Notification' in window && window.Notification.permission === 'default' &&
            <Button variant="outline" className="mb-3" onClick={() => window.Notification.requestPermission()}>Enable desktop notifications for reminders</Button>}
          {!notifs.length && <p className="text-sm text-zinc-500">Nothing yet. Reminders and other updates from your assistant show up here.</p>}
          <ul className="space-y-2">
            {notifs.map(n => (
              <li key={n.id} className={cx('rounded-lg border p-3', n.read ? 'border-zinc-200 dark:border-zinc-800' : 'border-accent/40 bg-accent/5')}>
                <div className="text-sm font-medium">{n.title}</div>{n.body && <div className="text-xs text-zinc-500">{n.body}</div>}
                <div className="mt-1 text-[11px] text-zinc-400">{fmtDate(n.created_at)}</div>
              </li>))}
          </ul>
        </Modal>)}

      {projectModal && (
        <Modal title={projectModal.id ? 'Edit project' : 'New project'} onClose={() => setProjectModal(null)}>
          <div className="space-y-3">
            <div><Label>Name</Label><Input autoFocus value={projectModal.name || ''} onChange={e => setProjectModal({ ...projectModal, name: e.target.value })} /></div>
            <div><Label hint="given to the assistant as context in this project's chats">Description</Label><Textarea rows={3} value={projectModal.description || ''} onChange={e => setProjectModal({ ...projectModal, description: e.target.value })} /></div>
            <div className="flex justify-between gap-2">
              {projectModal.id ? <Button variant="ghost" className="text-red-600" onClick={async () => { if (confirm('Delete project? Its chats are kept; its project memories are removed.')) { await api.del(`/projects/${projectModal.id}`); setProjectId(null); setProjectModal(null); loadProjects(); loadConvs() } }}><Trash2 size={14} />Delete</Button> : <span />}
              <div className="flex gap-2"><Button variant="outline" onClick={() => setProjectModal(null)}>Cancel</Button><Button onClick={saveProject}>Save</Button></div>
            </div>
          </div>
        </Modal>)}
    </div>
  )
}

function ConvItem({ c, active, onOpen, onAction }: { c: Conversation; active: boolean; onOpen: () => void; onAction: (a: 'rename' | 'pin' | 'delete') => void }) {
  const [menu, setMenu] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!menu) return
    const f = (e: MouseEvent) => { if (!ref.current?.contains(e.target as Node)) setMenu(false) }
    addEventListener('mousedown', f); return () => removeEventListener('mousedown', f)
  }, [menu])
  return (
    <div ref={ref} className={cx('group relative flex items-center rounded-lg', active ? 'bg-zinc-200 dark:bg-zinc-800' : 'hover:bg-zinc-200/60 dark:hover:bg-zinc-800/60')}>
      <button onClick={onOpen} className="flex min-w-0 flex-1 items-center gap-1.5 px-2.5 py-1.5 text-left text-sm" aria-current={active ? 'page' : undefined}>
        {c.pinned && <Pin size={11} className="shrink-0 text-zinc-400" />}<span className="truncate">{c.title}</span></button>
      <button aria-label="Conversation options" onClick={() => setMenu(!menu)} className="mr-1 rounded p-1 text-zinc-400 opacity-0 group-hover:opacity-100 focus:opacity-100 hover:text-zinc-700"><MoreHorizontal size={15} /></button>
      {menu && (
        <div role="menu" className="absolute right-0 top-8 z-10 w-36 rounded-lg border border-zinc-200 dark:border-zinc-700 bg-white dark:bg-zinc-900 py-1 shadow-lg">
          {([['rename', 'Rename', Pencil], ['pin', c.pinned ? 'Unpin' : 'Pin', Pin], ['delete', 'Delete', Trash2]] as const).map(([a, l, I]) => (
            <button key={a} role="menuitem" onClick={() => { setMenu(false); onAction(a) }} className={cx('flex w-full items-center gap-2 px-3 py-1.5 text-sm hover:bg-zinc-100 dark:hover:bg-zinc-800', a === 'delete' && 'text-red-600')}><I size={13} />{l}</button>))}
        </div>)}
    </div>
  )
}
