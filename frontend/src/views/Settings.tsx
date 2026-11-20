import { useEffect, useState } from 'react'
import QRCode from 'qrcode'
import { CheckCircle2, Loader2, Plus, RefreshCw, Trash2, XCircle, Pencil } from 'lucide-react'
import { api, type ModelCfg, type Provider, type User } from '../api'
import { Badge, Button, Card, ErrorNote, Input, Label, Modal, PageHeader, Toggle, fmtDate } from '../components/ui'

const PRESETS = [
  { name: 'Ollama (local)', base_url: 'http://localhost:11434/v1', model: 'llama3.1:8b', embed: 'nomic-embed-text' },
  { name: 'Cloudflare Workers AI', base_url: 'https://api.cloudflare.com/client/v4/accounts/ACCOUNT_ID/ai/v1', model: '@cf/meta/llama-3.3-70b-instruct-fp8-fast', embed: '@cf/baai/bge-m3', ctx: 16384 },
  { name: 'OpenAI', base_url: 'https://api.openai.com/v1', model: '', embed: '' },
  { name: 'OpenRouter', base_url: 'https://openrouter.ai/api/v1', model: '', embed: '' },
  { name: 'LM Studio', base_url: 'http://localhost:1234/v1', model: '', embed: '' },
  { name: 'Custom', base_url: '', model: '', embed: '' },
]

type Draft = { id?: string; name: string; base_url: string; api_key: string; embedding_model: string; models: ModelCfg[] }
type Preset = { name: string; base_url: string; model: string; embed: string; ctx?: number }
const presetModels = (p: Preset): ModelCfg[] => [{ ...newModel(p.model), context_window: p.ctx ?? 8192 }]
function newModel(name = ''): ModelCfg { return { name, temperature: 0.7, context_window: 8192, max_tokens: null, supports_tools: null } }

export default function Settings({ providers, reload, user, setPrefs }: { providers: Provider[]; reload: () => void; user: User; setPrefs: (p: Record<string, any>) => void }) {
  const [draft, setDraft] = useState<Draft | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [discovered, setDiscovered] = useState<string[]>([])
  const [verify, setVerify] = useState<Record<string, { busy?: boolean; ok?: boolean; msg?: string }>>({})
  const [cfAccounts, setCfAccounts] = useState<{ id: string; name: string; base_url: string }[]>([])
  const [cfBusy, setCfBusy] = useState(false)

  async function detectCloudflare() {
    if (!draft?.api_key) return
    setCfBusy(true); setError(null)
    try {
      const accts = await api.post<{ id: string; name: string; base_url: string }[]>('/providers/cloudflare/accounts', { api_key: draft.api_key })
      if (!accts.length) setError('This token has no Cloudflare account access. Give it the Workers AI permission.')
      else { setCfAccounts(accts); setDraft(d => d && { ...d, base_url: accts[0].base_url }) }
    } catch (e: any) { setError(e.message) } finally { setCfBusy(false) }
  }

  async function save() {
    if (!draft) return
    setError(null)
    const body = { ...draft, api_key: draft.api_key || (draft.id ? undefined : ''), models: draft.models.filter(m => m.name.trim()) }
    try {
      if (draft.id) await api.put(`/providers/${draft.id}`, body); else await api.post('/providers', body)
      setDraft(null); reload()
    } catch (e: any) { setError(e.message) }
  }

  async function discover(p: Provider) {
    try { const r = await api.get<{ models: string[] }>(`/providers/${p.id}/discover`); setDiscovered(r.models); edit(p) }
    catch (e: any) { setError(e.message) }
  }

  async function test(p: Provider, m: string) {
    const k = `${p.id}::${m}`
    setVerify(v => ({ ...v, [k]: { busy: true } }))
    try {
      const r = await api.post<{ supports_tools: boolean; reply: string }>(`/providers/${p.id}/models/${encodeURIComponent(m)}/verify`)
      setVerify(v => ({ ...v, [k]: { ok: true, msg: `Responded. Tool calling ${r.supports_tools ? 'verified ✓' : 'not supported, so this model chats without tools'}` } }))
      reload()
    } catch (e: any) { setVerify(v => ({ ...v, [k]: { ok: false, msg: e.message } })) }
  }

  const edit = (p: Provider) => setDraft({ id: p.id, name: p.name, base_url: p.base_url, api_key: '', embedding_model: p.embedding_model, models: p.models.map(m => ({ ...newModel(), ...m })) })

  return (
    <div className="mx-auto max-w-4xl p-6">
      <PageHeader title="Models & providers" desc="Any OpenAI-compatible endpoint works: Ollama, OpenAI, OpenRouter, LM Studio, vLLM, Groq…">
        <Button onClick={() => { setDiscovered([]); setCfAccounts([]); setDraft({ name: PRESETS[0].name, base_url: PRESETS[0].base_url, api_key: '', embedding_model: PRESETS[0].embed, models: presetModels(PRESETS[0]) }) }}><Plus size={15} />Add provider</Button>
      </PageHeader>
      <ErrorNote error={error} />
      <Preferences user={user} setPrefs={setPrefs} providers={providers} />
      {!providers.length && <Card className="p-8 text-center text-sm text-zinc-500">No providers yet. Add one to start chatting. For a free local setup install <a className="text-accent underline" href="https://ollama.com" target="_blank" rel="noreferrer">Ollama</a> and run <code>ollama pull llama3.1:8b</code>.</Card>}
      <div className="space-y-4">
        {providers.map(p => (
          <Card key={p.id} className="p-4">
            <div className="flex flex-wrap items-center gap-2">
              <div className="flex-1 min-w-0"><div className="font-medium">{p.name}</div><div className="text-xs text-zinc-500 truncate">{p.base_url}</div></div>
              {p.readonly ? <Badge>from .env</Badge> : <Badge tone={p.has_api_key ? 'green' : 'zinc'}>{p.has_api_key ? 'API key set' : 'no API key'}</Badge>}
              {p.embedding_model && <Badge tone="blue">embeddings: {p.embedding_model}</Badge>}
              <Button variant="ghost" onClick={() => discover(p)} title="List models from the provider"><RefreshCw size={14} />Discover</Button>
              {!p.readonly && <><Button variant="ghost" onClick={() => { setDiscovered([]); edit(p) }}><Pencil size={14} />Edit</Button>
                <Button variant="ghost" aria-label="Delete provider" onClick={async () => { if (confirm(`Delete ${p.name}?`)) { await api.del(`/providers/${p.id}`); reload() } }}><Trash2 size={14} /></Button></>}
            </div>
            <ul className="mt-3 space-y-2">
              {p.models.map(m => {
                const v = verify[`${p.id}::${m.name}`]
                return (
                  <li key={m.name} className="flex flex-wrap items-center gap-2 rounded-lg bg-zinc-50 dark:bg-zinc-900 px-3 py-2 text-sm">
                    <span className="font-mono">{m.name}</span>
                    <span className="text-xs text-zinc-500">temp {m.temperature} · ctx {m.context_window}{m.max_tokens ? ` · max ${m.max_tokens}` : ''}</span>
                    <Badge tone={m.supports_tools === true ? 'green' : m.supports_tools === false ? 'amber' : 'zinc'}>tools: {m.supports_tools === true ? 'verified' : m.supports_tools === false ? 'no' : 'unverified'}</Badge>
                    {user.prefs?.model === m.name && user.prefs?.provider_id === p.id ? <Badge tone="blue">default</Badge>
                      : <button className="text-xs text-accent hover:underline" onClick={() => setPrefs({ provider_id: p.id, model: m.name })}>Set default</button>}
                    <Button variant="outline" className="ml-auto" onClick={() => test(p, m.name)} disabled={v?.busy}>{v?.busy ? <Loader2 size={14} className="animate-spin" /> : null}Test</Button>
                    {v?.msg && <div className={`w-full text-xs flex items-center gap-1 ${v.ok ? 'text-emerald-700 dark:text-emerald-400' : 'text-red-600'}`}>{v.ok ? <CheckCircle2 size={13} /> : <XCircle size={13} />}{v.msg}</div>}
                  </li>)
              })}
            </ul>
          </Card>))}
      </div>

      <Microsoft365Card />
      <WhatsAppCard />
      <GmailCard />
      <InstagramCard />
      <Devices />
      {draft && (
        <Modal title={draft.id ? 'Edit provider' : 'Add provider'} onClose={() => setDraft(null)}>
          <div className="space-y-3">
            {!draft.id && <div><Label>Preset</Label><div className="flex flex-wrap gap-1.5">{PRESETS.map(p => (
              <Button key={p.name} variant={draft.name === p.name ? 'primary' : 'outline'} onClick={() => { setCfAccounts([]); setDraft({ ...draft, name: p.name, base_url: p.base_url, embedding_model: p.embed, models: presetModels(p) }) }}>{p.name}</Button>))}</div></div>}
            <div><Label>Name</Label><Input value={draft.name} onChange={e => setDraft({ ...draft, name: e.target.value })} /></div>
            <div><Label hint="OpenAI-compatible, usually ends in /v1">Base URL</Label><Input value={draft.base_url} onChange={e => setDraft({ ...draft, base_url: e.target.value })} placeholder="https://…/v1" /></div>
            <div><Label hint={draft.id ? 'leave blank to keep current' : 'not needed for Ollama'}>API key</Label><Input type="password" autoComplete="off" value={draft.api_key} onChange={e => setDraft({ ...draft, api_key: e.target.value })} /></div>
            {draft.base_url.includes('api.cloudflare.com') && (
              <div className="space-y-2 rounded-lg bg-zinc-50 dark:bg-zinc-800/50 p-3 text-xs">
                <p>Create a token at <a className="text-accent underline" href="https://dash.cloudflare.com/profile/api-tokens" target="_blank" rel="noreferrer">dash.cloudflare.com → API Tokens</a> with the <b>Workers AI</b> permission and paste it above. Your account is detected automatically when you save (or click Detect account to choose one).</p>
                <div className="flex flex-wrap items-center gap-2">
                  <Button variant="outline" disabled={!draft.api_key || cfBusy} onClick={detectCloudflare}>{cfBusy ? <Loader2 size={14} className="animate-spin" /> : null}Detect account</Button>
                  {cfAccounts.length > 1 && <select aria-label="Cloudflare account" className="rounded-lg border border-zinc-300 dark:border-zinc-700 bg-transparent px-2 py-1" value={draft.base_url} onChange={e => setDraft({ ...draft, base_url: e.target.value })}>
                    {cfAccounts.map(a => <option key={a.id} value={a.base_url}>{a.name}</option>)}</select>}
                  {cfAccounts.length > 0 && <span className="text-emerald-700 dark:text-emerald-400">✓ {cfAccounts.find(a => a.base_url === draft.base_url)?.name}</span>}
                </div>
                <p className="text-zinc-500">Much faster than a CPU-only local model. Your prompts are processed by Cloudflare. Free daily allowance, then paid.</p>
              </div>)}
            <div><Label hint="optional; enables semantic search for documents">Embedding model</Label><Input value={draft.embedding_model} onChange={e => setDraft({ ...draft, embedding_model: e.target.value })} placeholder="nomic-embed-text / text-embedding-3-small" /></div>
            <div>
              <Label>Chat models</Label>
              {discovered.length > 0 && <div className="mb-2 flex flex-wrap gap-1">{discovered.filter(d => !draft.models.some(m => m.name === d)).slice(0, 30).map(d => (
                <button key={d} onClick={() => setDraft({ ...draft, models: [...draft.models.filter(m => m.name), newModel(d)] })} className="rounded border border-dashed border-zinc-300 dark:border-zinc-700 px-1.5 py-0.5 text-xs hover:border-accent">+ {d}</button>))}</div>}
              <div className="space-y-2">
                {draft.models.map((m, i) => {
                  const set = (patch: Partial<ModelCfg>) => setDraft({ ...draft, models: draft.models.map((x, j) => j === i ? { ...x, ...patch } : x) })
                  return (
                    <div key={i} className="grid grid-cols-12 gap-1.5 items-center">
                      <Input className="col-span-5" placeholder="model name" value={m.name} onChange={e => set({ name: e.target.value })} aria-label="Model name" />
                      <Input className="col-span-2" type="number" step="0.1" min={0} max={2} value={m.temperature} onChange={e => set({ temperature: +e.target.value })} aria-label="Temperature" title="Temperature" />
                      <Input className="col-span-2" type="number" min={512} value={m.context_window} onChange={e => set({ context_window: +e.target.value })} aria-label="Context window" title="Context window (tokens)" />
                      <Input className="col-span-2" type="number" min={1} placeholder="max" value={m.max_tokens ?? ''} onChange={e => set({ max_tokens: e.target.value ? +e.target.value : null })} aria-label="Max tokens" title="Max output tokens" />
                      <button aria-label="Remove model" className="col-span-1 text-zinc-400 hover:text-red-600" onClick={() => setDraft({ ...draft, models: draft.models.filter((_, j) => j !== i) })}><Trash2 size={14} /></button>
                    </div>)
                })}
              </div>
              <p className="mt-1 text-[11px] text-zinc-400">name · temperature · context window · max output tokens</p>
              <Button variant="ghost" className="mt-1" onClick={() => setDraft({ ...draft, models: [...draft.models, newModel()] })}><Plus size={14} />Add model</Button>
            </div>
            <ErrorNote error={error} />
            <div className="flex justify-end gap-2"><Button variant="outline" onClick={() => setDraft(null)}>Cancel</Button><Button onClick={save}>Save</Button></div>
          </div>
        </Modal>)}
    </div>
  )
}

function Row({ title, desc, children }: { title: string; desc: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-4 py-3">
      <div><div className="text-sm font-medium">{title}</div><div className="text-xs text-zinc-500">{desc}</div></div>{children}
    </div>
  )
}

function Preferences({ user, setPrefs, providers }: { user: User; setPrefs: (p: Record<string, any>) => void; providers: Provider[] }) {
  const [cc, setCc] = useState(user.prefs?.default_country_code || '')
  const fbKey = user.prefs?.fallback_provider_id && user.prefs?.fallback_model ? `${user.prefs.fallback_provider_id}::${user.prefs.fallback_model}` : ''
  return (
    <Card className="mb-6 px-4 divide-y divide-zinc-200 dark:divide-zinc-800">
      <Row title="Quick actions" desc="Handle everyday commands instantly (volume, apps, media, timers, reminders, WhatsApp) without waiting for the AI model.">
        <Toggle label="Quick actions" checked={user.prefs?.fast_path !== false} onChange={v => setPrefs({ fast_path: v })} /></Row>
      <Row title="Smart routing" desc="Let the model pick the specialist when keywords are unclear. Costs an extra model call; slow on CPU-only machines.">
        <Toggle label="Smart routing" checked={!!user.prefs?.llm_routing} onChange={v => setPrefs({ llm_routing: v })} /></Row>
      <Row title="Approvals" desc="When Terminator must ask before acting on your PC. Catastrophic commands stay blocked in every mode.">
        <select aria-label="Approvals" value={user.prefs?.approval_mode || 'risky'} onChange={e => setPrefs({ approval_mode: e.target.value })}
          className="max-w-[220px] rounded-lg border border-zinc-300 dark:border-zinc-700 bg-transparent px-2 py-1.5 text-sm">
          <option value="risky">Only for risky actions</option>
          <option value="ask">Every time</option>
          <option value="never">Never ask</option>
        </select></Row>
      <Row title="Fallback model" desc="Used automatically when the main model fails (quota, network, outage). Automatic picks another provider, local first.">
        <select aria-label="Fallback model" value={fbKey} onChange={e => { const [pid, ...m] = e.target.value.split('::'); setPrefs({ fallback_provider_id: pid, fallback_model: m.join('::') }) }}
          className="max-w-[220px] rounded-lg border border-zinc-300 dark:border-zinc-700 bg-transparent px-2 py-1.5 text-sm">
          <option value="">Automatic</option>
          {providers.flatMap(p => p.models.map(m => <option key={p.id + m.name} value={`${p.id}::${m.name}`}>{m.name} · {p.name}</option>))}
        </select></Row>
      <Row title="Default country code" desc="Used for phone numbers saved without +country code (e.g. 91 for India, 1 for US).">
        <form className="flex items-center gap-2" onSubmit={e => { e.preventDefault(); setPrefs({ default_country_code: cc.replace(/\D/g, '') }) }}>
          <span className="text-sm text-zinc-500">+</span>
          <Input aria-label="Default country code" value={cc} onChange={e => setCc(e.target.value.replace(/\D/g, '').slice(0, 4))} className="w-20" placeholder="91" />
          <Button variant="outline" type="submit" disabled={cc === (user.prefs?.default_country_code || '')}>Save</Button>
        </form></Row>
    </Card>
  )
}

interface Device { id: string; name: string; created_at: string; last_used_at: string | null }

function Devices() {
  const [items, setItems] = useState<Device[]>([])
  const load = () => api.get<Device[]>('/auth/device-tokens').then(setItems).catch(() => {})
  useEffect(() => { load() }, [])
  return (
    <div className="mt-8">
      <h2 className="font-medium mb-1">Voice devices</h2>
      <p className="text-xs text-zinc-500 mb-3">The always-on voice service (“Hey Jarvis”) signs in with its own revocable key. Set it up from a terminal with <code>voice/terminator-voice login</code>.</p>
      <Card>
        {!items.length && <p className="p-6 text-center text-sm text-zinc-500">No voice devices linked yet.</p>}
        <ul className="divide-y divide-zinc-200 dark:divide-zinc-800">
          {items.map(d => (
            <li key={d.id} className="flex items-center gap-3 px-4 py-3 text-sm">
              <div className="flex-1"><div className="font-medium">{d.name}</div>
                <div className="text-xs text-zinc-500">added {fmtDate(d.created_at)} · {d.last_used_at ? `last used ${fmtDate(d.last_used_at)}` : 'never used'}</div></div>
              <Button variant="outline" onClick={async () => { if (confirm(`Revoke ${d.name}? It will stop working immediately.`)) { await api.del(`/auth/device-tokens/${d.id}`); load() } }}>Revoke</Button>
            </li>))}
        </ul>
      </Card>
    </div>
  )
}

function WhatsAppCard() {
  const [st, setSt] = useState<{ state: string; qr?: string; chats?: number } | null>(null)
  const [img, setImg] = useState<string | null>(null)
  useEffect(() => {
    let alive = true
    const tick = async () => {
      try {
        const s = await api.get<{ state: string; qr?: string; chats?: number }>('/integrations/whatsapp')
        if (!alive) return
        setSt(s)
        setImg(s.qr ? await QRCode.toDataURL(s.qr, { margin: 1, width: 240 }) : null)
      } catch { /* retry next tick */ }
    }
    tick()
    const t = window.setInterval(tick, 3000)
    return () => { alive = false; window.clearInterval(t) }
  }, [])
  const state = st?.state
  return (
    <div className="mt-8">
      <h2 className="font-medium mb-1">WhatsApp</h2>
      <p className="text-xs text-zinc-500 mb-3">Linked like WhatsApp Web, through the open-source whatsapp-mcp bridge on this PC. When you say “send …”, messages go out directly. Unofficial, so WhatsApp could restrict the number.</p>
      <Card className="p-4 text-sm">
        {!st && <p className="text-zinc-500">Checking…</p>}
        {state === 'connected' && <p className="flex items-center gap-2 text-emerald-700 dark:text-emerald-400"><CheckCircle2 size={16} />Linked and connected{st?.chats ? ` · ${st.chats} chats synced` : ''}. To unlink: WhatsApp on your phone → Linked devices.</p>}
        {state === 'waiting_for_scan' && (
          <div className="flex flex-wrap items-center gap-5">
            {img && <img src={img} alt="WhatsApp link QR code" className="h-48 w-48 rounded-lg bg-white p-1" />}
            <ol className="list-decimal space-y-1 pl-5 text-zinc-600 dark:text-zinc-300">
              <li>Open WhatsApp on your phone</li><li>Settings → <b>Linked devices</b> → <b>Link a device</b></li><li>Scan this code (it refreshes automatically)</li>
            </ol>
          </div>)}
        {state === 'offline' && <p className="text-amber-700 dark:text-amber-400">Linked, but the bridge isn't running. Start it with <code>systemctl --user start terminator-whatsapp</code>.</p>}
        {state === 'starting' && <p className="flex items-center gap-2 text-zinc-600 dark:text-zinc-300"><Loader2 size={14} className="animate-spin" />Not linked yet. Preparing a fresh QR code… (a few seconds)</p>}
        {state === 'not_set_up' && <p className="text-zinc-600 dark:text-zinc-300">The WhatsApp bridge isn't running. Start it with <code>systemctl --user start terminator-whatsapp</code>; a QR code appears here to scan.</p>}
      </Card>
    </div>
  )
}

type MsStatus = { state: string; user_code?: string; verification_uri?: string; account?: { name?: string; email?: string }; error?: string; client_id?: string; tenant_id?: string }

function Microsoft365Card() {
  const [st, setSt] = useState<MsStatus | null>(null)
  const [clientId, setClientId] = useState(''); const [tenantId, setTenantId] = useState('')
  const [err, setErr] = useState<string | null>(null); const [busy, setBusy] = useState(false)
  const load = async () => {
    try {
      const s = await api.get<MsStatus>('/integrations/ms365')
      setSt(s)
      if (s.client_id) setClientId(c => c || s.client_id!)
      if (s.tenant_id && s.tenant_id !== 'organizations') setTenantId(t => t || s.tenant_id!)
    } catch { /* ignore */ }
  }
  useEffect(() => { load() }, [])
  useEffect(() => {                                       // poll while waiting for the browser approval
    if (st?.state !== 'signing_in') return
    const t = window.setInterval(load, 3000)
    return () => window.clearInterval(t)
  }, [st?.state])
  async function signIn() {
    setBusy(true); setErr(null)
    try { await api.post('/integrations/ms365/connect', { client_id: clientId.trim(), tenant_id: tenantId.trim() || 'organizations' }); await load() }
    catch (e: any) { setErr(e.message) } finally { setBusy(false) }
  }
  return (
    <div className="mt-8">
      <h2 className="font-medium mb-1">Microsoft 365 (Teams & Outlook)</h2>
      <p className="text-xs text-zinc-500 mb-3">Sign in once with your work account. Terminator then sends Teams chats and Outlook email as you (colleagues found by name) and reads your calendar and inbox. Your password never reaches Terminator; the sign-in token is stored encrypted.</p>
      <Card className="p-4 text-sm space-y-3">
        {st?.state === 'connected' && (
          <div className="flex flex-wrap items-center gap-3">
            <p className="flex items-center gap-2 text-emerald-700 dark:text-emerald-400"><CheckCircle2 size={16} />Signed in as {st.account?.name} ({st.account?.email})</p>
            <Button variant="outline" className="ml-auto" onClick={async () => { await api.post('/integrations/ms365/disconnect'); load() }}>Sign out</Button>
          </div>)}
        {st?.state === 'signing_in' && (
          <div className="space-y-2">
            <p>Open <a className="text-accent underline" href={st.verification_uri} target="_blank" rel="noreferrer">{st.verification_uri}</a>, enter this code and approve:</p>
            <div className="flex items-center gap-3"><code className="rounded-lg bg-zinc-100 dark:bg-zinc-800 px-4 py-2 text-2xl font-semibold tracking-widest">{st.user_code}</code>
              <span className="flex items-center gap-1 text-xs text-zinc-500"><Loader2 size={12} className="animate-spin" />waiting for approval…</span></div>
          </div>)}
        {st && !['connected', 'signing_in'].includes(st.state) && (
          <>
            {st.state === 'error' && <ErrorNote error={`Sign-in failed: ${st.error}`} />}
            <ol className="list-decimal space-y-1 pl-5 text-xs text-zinc-600 dark:text-zinc-300">
              <li>Open <a className="text-accent underline" href="https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade" target="_blank" rel="noreferrer">entra.microsoft.com → App registrations</a> → <b>New registration</b>. Name: Terminator. Supported accounts: <i>this organizational directory only</i>. Register.</li>
              <li><b>Authentication</b> → Advanced settings → <b>Allow public client flows: Yes</b> → Save.</li>
              <li><b>API permissions</b> → Add → Microsoft Graph → <b>Delegated</b>: User.Read, People.Read, User.ReadBasic.All, Chat.ReadWrite, ChatMessage.Send, Mail.Read, Mail.Send, Calendars.ReadWrite. If your company requires it, ask IT to <i>grant admin consent</i>.</li>
              <li>From <b>Overview</b>, copy the Application (client) ID and Directory (tenant) ID here:</li>
            </ol>
            <div className="grid gap-2 sm:grid-cols-2">
              <div><Label>Application (client) ID</Label><Input value={clientId} onChange={e => setClientId(e.target.value)} placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx" /></div>
              <div><Label>Directory (tenant) ID</Label><Input value={tenantId} onChange={e => setTenantId(e.target.value)} placeholder="xxxxxxxx-xxxx-… or contoso.com" /></div>
            </div>
            <ErrorNote error={err} />
            <Button onClick={signIn} disabled={busy || !/^[0-9a-fA-F-]{36}$/.test(clientId.trim())}>{busy && <Loader2 size={14} className="animate-spin" />}Sign in with Microsoft</Button>
          </>)}
      </Card>
    </div>
  )
}

function GmailCard() {
  const [st, setSt] = useState<{ state: string; email?: string } | null>(null)
  const [email, setEmail] = useState(''); const [pw, setPw] = useState('')
  const [err, setErr] = useState<string | null>(null); const [busy, setBusy] = useState(false)
  const load = () => api.get<{ state: string; email?: string }>('/integrations/gmail').then(setSt).catch(() => {})
  useEffect(() => { load() }, [])
  async function connect() {
    setBusy(true); setErr(null)
    try { await api.post('/integrations/gmail/connect', { email: email.trim(), app_password: pw }); setPw(''); load() }
    catch (e: any) { setErr(e.message) } finally { setBusy(false) }
  }
  return (
    <div className="mt-8">
      <h2 className="font-medium mb-1">Gmail</h2>
      <p className="text-xs text-zinc-500 mb-3">Send, read and search Gmail with an app password (stored encrypted; revoke it anytime in your Google Account).</p>
      <Card className="p-4 text-sm space-y-3">
        {st?.state === 'connected' ? (
          <div className="flex flex-wrap items-center gap-3">
            <p className="flex items-center gap-2 text-emerald-700 dark:text-emerald-400"><CheckCircle2 size={16} />Connected as {st.email}</p>
            <Button variant="outline" className="ml-auto" onClick={async () => { await api.post('/integrations/gmail/disconnect'); load() }}>Disconnect</Button>
          </div>
        ) : (
          <>
            <ol className="list-decimal space-y-1 pl-5 text-xs text-zinc-600 dark:text-zinc-300">
              <li>Turn on <a className="text-accent underline" href="https://myaccount.google.com/signinoptions/twosv" target="_blank" rel="noreferrer">2-Step Verification</a> for your Google Account (if it isn't already).</li>
              <li>Open <a className="text-accent underline" href="https://myaccount.google.com/apppasswords" target="_blank" rel="noreferrer">App passwords</a>, create one named <b>Terminator</b>, and copy the 16-letter password.</li>
              <li>Enter your Gmail address and that app password here (not your normal password).</li>
            </ol>
            <div className="grid gap-2 sm:grid-cols-2">
              <div><Label>Gmail address</Label><Input type="email" value={email} onChange={e => setEmail(e.target.value)} placeholder="you@gmail.com" autoComplete="off" /></div>
              <div><Label>App password</Label><Input type="password" value={pw} onChange={e => setPw(e.target.value)} placeholder="xxxx xxxx xxxx xxxx" autoComplete="new-password" /></div>
            </div>
            <ErrorNote error={err} />
            <Button onClick={connect} disabled={busy || !email.includes('@') || pw.replace(/\s/g, '').length < 16}>{busy && <Loader2 size={14} className="animate-spin" />}Connect Gmail</Button>
          </>)}
      </Card>
    </div>
  )
}

type IgStatus = { state: string; username?: string; kind?: string; error?: string }

function InstagramCard() {
  const [st, setSt] = useState<IgStatus | null>(null)
  const [user, setUser] = useState(''); const [pw, setPw] = useState(''); const [code, setCode] = useState('')
  const [err, setErr] = useState<string | null>(null)
  const load = () => api.get<IgStatus>('/integrations/instagram').then(setSt).catch(() => {})
  useEffect(() => { load() }, [])
  useEffect(() => {
    if (!st || !['signing_in', 'needs_2fa', 'needs_code'].includes(st.state)) return
    const t = window.setInterval(load, 2000)
    return () => window.clearInterval(t)
  }, [st?.state])
  async function connect() {
    setErr(null)
    try { setSt(await api.post<IgStatus>('/integrations/instagram/connect', { username: user.trim(), password: pw })); setPw('') }
    catch (e: any) { setErr(e.message) }
  }
  async function sendCode() {
    setErr(null)
    try { await api.post('/integrations/instagram/code', { code: code.trim() }); setCode(''); setSt(s => s && { ...s, state: 'signing_in' }) }
    catch (e: any) { setErr(e.message) }
  }
  const state = st?.state
  return (
    <div className="mt-8">
      <h2 className="font-medium mb-1">Instagram</h2>
      <p className="text-xs text-zinc-500 mb-3">Send and read DMs through Instagram's unofficial app API. Against Instagram's terms: security checks, temporary blocks or a ban are possible. Your password is used once to sign in and never stored.</p>
      <Card className="p-4 text-sm space-y-3">
        {state === 'connected' && (
          <div className="flex flex-wrap items-center gap-3">
            <p className="flex items-center gap-2 text-emerald-700 dark:text-emerald-400"><CheckCircle2 size={16} />Connected as @{st?.username}</p>
            <Button variant="outline" className="ml-auto" onClick={async () => { await api.post('/integrations/instagram/disconnect'); load() }}>Disconnect</Button>
          </div>)}
        {state === 'signing_in' && <p className="flex items-center gap-2 text-zinc-600 dark:text-zinc-300"><Loader2 size={14} className="animate-spin" />Signing in to Instagram…</p>}
        {(state === 'needs_2fa' || state === 'needs_code') && (
          <form className="space-y-2" onSubmit={e => { e.preventDefault(); sendCode() }}>
            <p>{state === 'needs_2fa' ? 'Enter the 6-digit code from your authenticator app or SMS.' : `Instagram sent a security code${st?.kind ? ` by ${st.kind.toLowerCase()}` : ''}. Enter it here.`}</p>
            <div className="flex gap-2"><Input autoFocus inputMode="numeric" value={code} onChange={e => setCode(e.target.value)} placeholder="123456" className="w-40" />
              <Button type="submit" disabled={code.trim().length < 4}>Verify</Button></div>
          </form>)}
        {(state === 'not_connected' || state === 'error') && (
          <form className="space-y-3" onSubmit={e => { e.preventDefault(); connect() }}>
            {state === 'error' && <ErrorNote error={st?.error || 'Sign-in failed'} />}
            <div className="grid gap-2 sm:grid-cols-2">
              <div><Label>Instagram username</Label><Input value={user} onChange={e => setUser(e.target.value)} placeholder="username" autoComplete="off" /></div>
              <div><Label>Password</Label><Input type="password" value={pw} onChange={e => setPw(e.target.value)} autoComplete="new-password" /></div>
            </div>
            <ErrorNote error={err} />
            <Button type="submit" disabled={!user.trim() || !pw}>Connect Instagram</Button>
          </form>)}
      </Card>
    </div>
  )
}
