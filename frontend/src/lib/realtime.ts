// Persistent two-way WebSocket channel with automatic reconnect and an offline send queue.
type Listener = (ev: any) => void
export type RtStatus = 'connecting' | 'open' | 'closed'

class Realtime {
  private ws: WebSocket | null = null
  private listeners = new Set<Listener>()
  private statusListeners = new Set<(s: RtStatus) => void>()
  private queue: string[] = []
  private retry = 0
  private timer: number | undefined
  private heartbeat: number | undefined
  private wanted = false
  status: RtStatus = 'closed'

  connect() {
    this.wanted = true
    if (this.ws && this.ws.readyState <= WebSocket.OPEN) return
    this.setStatus('connecting')
    const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/api/ws`)
    this.ws = ws
    ws.onopen = () => {
      this.retry = 0
      this.setStatus('open')
      for (const m of this.queue.splice(0)) ws.send(m)
      this.heartbeat = window.setInterval(() => this.send({ type: 'ping' }), 25_000)
    }
    ws.onmessage = e => {
      let ev: any
      try { ev = JSON.parse(e.data) } catch { return }
      this.listeners.forEach(l => l(ev))
    }
    ws.onclose = e => {
      window.clearInterval(this.heartbeat)
      this.ws = null
      this.setStatus('closed')
      if (!this.wanted || e.code === 4401 || e.code === 4403) return   // logged out / forbidden: don't loop
      const delay = Math.min(30_000, 500 * 2 ** this.retry++) + Math.random() * 300
      this.timer = window.setTimeout(() => this.connect(), delay)
    }
  }

  disconnect() {
    this.wanted = false
    window.clearTimeout(this.timer)
    this.ws?.close()
  }

  send(msg: object) {
    const s = JSON.stringify(msg)
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(s)
    else { this.queue.push(s); this.connect() }
  }

  isOpen() { return this.ws?.readyState === WebSocket.OPEN }

  on(fn: Listener) { this.listeners.add(fn); return () => { this.listeners.delete(fn) } }
  onStatus(fn: (s: RtStatus) => void) { this.statusListeners.add(fn); fn(this.status); return () => { this.statusListeners.delete(fn) } }
  private setStatus(s: RtStatus) { this.status = s; this.statusListeners.forEach(l => l(s)) }
}

export const rt = new Realtime()
