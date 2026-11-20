// Thin API client. Session lives in an httpOnly cookie (same-origin via Vite proxy / FastAPI static serving).
export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) { super(message); this.status = status }
}

async function req<T>(method: string, path: string, body?: unknown): Promise<T> {
  const isForm = body instanceof FormData
  const res = await fetch(`/api${path}`, {
    method, credentials: 'include',
    headers: body && !isForm ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? (isForm ? body : JSON.stringify(body)) : undefined,
  })
  if (!res.ok) {
    let msg = res.statusText
    try {
      const j = await res.json()
      msg = j.errors?.length ? j.errors.map((e: any) => `${e.field}: ${e.message}`).join('; ') : j.detail || msg
    } catch { /* non-JSON error */ }
    throw new ApiError(res.status, msg)
  }
  return res.status === 204 ? (undefined as T) : res.json()
}

export const api = {
  get: <T,>(p: string) => req<T>('GET', p),
  post: <T,>(p: string, b?: unknown) => req<T>('POST', p, b ?? {}),
  put: <T,>(p: string, b: unknown) => req<T>('PUT', p, b),
  patch: <T,>(p: string, b: unknown) => req<T>('PATCH', p, b),
  del: <T,>(p: string) => req<T>('DELETE', p),
}

export type StreamEvent =
  | { type: 'meta'; agent: string; agent_name: string; route: string; model: string; turn_id: string }
  | { type: 'text'; delta: string }
  | { type: 'tool_start'; id: string; name: string; args: any; retract?: number }
  | { type: 'tool_end'; id: string; name: string; status: string; preview: string }
  | { type: 'approval_required'; run_id: string; name: string; args: any }
  | { type: 'question'; run_id: string; name: string; args: { question: string; options?: string[] } }
  | { type: 'citations'; items: Citation[] }
  | { type: 'done'; message_id?: string; usage?: any; stopped?: boolean; awaiting_approval?: boolean; awaiting_user?: boolean }
  | { type: 'error'; message: string }

/** POST and parse the SSE stream, calling onEvent for each event. Abortable. */
export async function streamChat(cid: string, body: unknown, onEvent: (e: StreamEvent) => void, signal: AbortSignal) {
  const res = await fetch(`/api/conversations/${cid}/chat`, {
    method: 'POST', credentials: 'include', signal,
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  })
  if (!res.ok || !res.body) {
    let msg = res.statusText
    try { msg = (await res.json()).detail || msg } catch { /* ignore */ }
    throw new ApiError(res.status, msg)
  }
  const reader = res.body.getReader()
  const dec = new TextDecoder()
  let buf = ''
  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    buf += dec.decode(value, { stream: true })
    let i
    while ((i = buf.indexOf('\n\n')) >= 0) {
      const block = buf.slice(0, i); buf = buf.slice(i + 2)
      const data = block.split('\n').filter(l => l.startsWith('data: ')).map(l => l.slice(6)).join('')
      if (data) onEvent(JSON.parse(data))
    }
  }
}

export interface User { id: string; email: string; name: string; prefs: Record<string, any> }
export interface Conversation { id: string; title: string; updated_at: string; pinned?: boolean; project_id?: string | null; last_agent?: string }
export interface Citation { type: 'web' | 'kb'; title: string; url?: string; location?: string; document_id?: string }
export interface Message {
  id: string; role: 'user' | 'assistant' | 'tool'; content: string; turn_id: string; created_at: string
  tool_calls?: { id: string; name: string; arguments: string }[]; name?: string; status?: string; run_id?: string
  attachments?: string[]; stopped?: boolean
  meta?: { agent?: string; model?: string; citations?: Citation[]; usage?: any }
}
export interface ModelCfg { name: string; temperature: number; max_tokens?: number | null; context_window: number; supports_tools?: boolean | null }
export interface Provider { id: string; name: string; base_url: string; models: ModelCfg[]; embedding_model: string; has_api_key: boolean | null; readonly?: boolean }
export interface Doc { id: string; filename: string; size: number; status: string; chunks?: number; retrieval?: string; error?: string; created_at: string }
export interface Memory { id: string; kind: 'long_term' | 'project' | 'preference'; content: string; source: string; updated_at: string }
export interface Task { id: string; title: string; notes: string; due_at: string | null; priority: string; status: 'open' | 'done' }
export interface Project { id: string; name: string; description: string }
export interface Notification { id: string; kind: 'reminder' | 'memory' | string; title: string; body: string; read: boolean; created_at: string; data?: any }
