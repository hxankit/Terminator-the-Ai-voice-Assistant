import { useEffect, useState } from 'react'
import { api } from '../api'
import { Badge, Button, Card, PageHeader, fmtDate } from '../components/ui'

export default function Activity() {
  const [agents, setAgents] = useState<any[]>([]); const [runs, setRuns] = useState<any[]>([]); const [usage, setUsage] = useState<any[]>([])
  const [health, setHealth] = useState<any>(null)
  const [envs, setEnvs] = useState<any[]>([]); const [envMsg, setEnvMsg] = useState<string | null>(null)
  const loadEnvs = () => api.get<any[]>('/environments').then(setEnvs).catch(() => {})
  useEffect(() => {
    api.get<any[]>('/agents').then(setAgents); api.get<any[]>('/tools/runs?limit=50').then(setRuns)
    api.get<any[]>('/usage').then(setUsage); api.get('/health').then(setHealth); loadEnvs()
  }, [])
  const tone = (s: string) => s === 'ok' || s === 'approved' ? 'green' : s === 'error' || s === 'denied' ? 'red' : 'amber'
  return (
    <div className="mx-auto max-w-5xl p-6 space-y-8">
      <PageHeader title="Agents & activity" desc="Specialized agents, their tools, the tool execution log and token usage." />
      {health && <div className="flex flex-wrap gap-2 text-xs">
        <Badge tone={health.mongo === 'ok' ? 'green' : 'red'}>MongoDB: {health.mongo}</Badge>
        <Badge tone={health.sandbox ? 'green' : 'red'}>Code sandbox: {health.sandbox || 'unavailable'}</Badge>
        <Badge tone={health.web_search ? 'green' : 'amber'}>Web search: {health.web_search ? 'configured' : 'not configured (set SEARXNG_URL or TAVILY_API_KEY)'}</Badge>
      </div>}
      <section>
        <h2 className="font-medium mb-3">Agents</h2>
        <div className="grid gap-3 sm:grid-cols-2">
          {agents.map(a => (
            <Card key={a.key} className="p-4">
              <div className="font-medium text-sm">{a.name}</div><p className="text-xs text-zinc-500 mt-0.5">{a.description}</p>
              <div className="mt-2 flex flex-wrap gap-1">{a.tools.map((t: any) => <span key={t.name} title={t.reason || t.risk}><Badge tone={!t.available ? 'red' : t.risk === 'approval' ? 'amber' : 'zinc'}>{t.name}{t.risk === 'approval' && ' 🔒'}</Badge></span>)}</div>
            </Card>))}
        </div>
        <p className="text-xs text-zinc-500 mt-2">🔒 = requires your approval each time · red = unavailable (missing configuration)</p>
      </section>
      <section>
        <h2 className="font-medium mb-1">Saved environments</h2>
        <p className="text-xs text-zinc-500 mb-3">Open tabs, VS Code projects, terminals and apps. Saved when you shut down through Terminator and automatically every 10 minutes. Say “open previous environment” to reopen the latest.</p>
        {envMsg && <p className="mb-2 text-sm text-emerald-700 dark:text-emerald-400">{envMsg}</p>}
        <Card>
          {!envs.length && <p className="p-6 text-center text-sm text-zinc-500">Nothing saved yet.</p>}
          <ul className="divide-y divide-zinc-200 dark:divide-zinc-800">
            {envs.slice(0, 10).map(e => (
              <li key={e.id} className="flex items-center gap-3 px-4 py-3 text-sm">
                <div className="min-w-0 flex-1"><div className="truncate">{e.summary}</div>
                  <div className="text-xs text-zinc-500">{fmtDate(e.created_at)} · {e.reason === 'auto' ? 'automatic' : e.reason}</div></div>
                <Button variant="outline" onClick={async () => { try { const r = await api.post<{ restored: string[] }>(`/environments/${e.id}/restore`); setEnvMsg('Reopened: ' + r.restored.join(', ')) } catch (err: any) { setEnvMsg(err.message) } }}>Restore</Button>
              </li>))}
          </ul>
        </Card>
      </section>
      <section>
        <h2 className="font-medium mb-3">Token usage (30 days)</h2>
        <Card className="overflow-x-auto">
          <table className="w-full text-sm"><thead className="text-left text-xs text-zinc-500"><tr>{['Model', 'Calls', 'Prompt tokens', 'Completion tokens', 'Avg latency', 'Avg first token'].map(h => <th key={h} className="px-4 py-2 font-medium">{h}</th>)}</tr></thead>
            <tbody>{usage.map(u => <tr key={u.model} className="border-t border-zinc-200 dark:border-zinc-800"><td className="px-4 py-2 font-mono">{u.model}</td><td className="px-4 py-2">{u.calls}</td><td className="px-4 py-2">{u.prompt_tokens.toLocaleString()}</td><td className="px-4 py-2">{u.completion_tokens.toLocaleString()}</td><td className="px-4 py-2">{Math.round(u.avg_ms)} ms</td><td className="px-4 py-2">{u.avg_ttft_ms ? Math.round(u.avg_ttft_ms) + ' ms' : '—'}</td></tr>)}
              {!usage.length && <tr><td colSpan={6} className="px-4 py-6 text-center text-zinc-500">No usage yet</td></tr>}</tbody></table>
        </Card>
      </section>
      <section>
        <h2 className="font-medium mb-3">Tool execution log</h2>
        <Card className="overflow-x-auto">
          <table className="w-full text-sm"><thead className="text-left text-xs text-zinc-500"><tr>{['Time', 'Tool', 'Arguments', 'Status', 'Duration'].map(h => <th key={h} className="px-4 py-2 font-medium">{h}</th>)}</tr></thead>
            <tbody>{runs.map(r => <tr key={r.id} className="border-t border-zinc-200 dark:border-zinc-800 align-top"><td className="px-4 py-2 whitespace-nowrap text-xs">{fmtDate(r.created_at)}</td><td className="px-4 py-2 font-mono text-xs">{r.tool}</td><td className="px-4 py-2 text-xs text-zinc-500 max-w-xs truncate">{JSON.stringify(r.args)}</td><td className="px-4 py-2"><Badge tone={tone(r.status)}>{r.status}</Badge></td><td className="px-4 py-2 text-xs">{r.duration_ms != null ? `${r.duration_ms} ms` : ''}</td></tr>)}
              {!runs.length && <tr><td colSpan={5} className="px-4 py-6 text-center text-zinc-500">No tool runs yet</td></tr>}</tbody></table>
        </Card>
      </section>
    </div>
  )
}
