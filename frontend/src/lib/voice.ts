// Voice I/O: browser speech recognition (with server-transcription fallback) and
// sentence-by-sentence speech synthesis that can start speaking while a reply is still streaming.

const SR: any = (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition
export const canRecognize = !!SR
export const canRecord = typeof MediaRecorder !== 'undefined' && !!navigator.mediaDevices?.getUserMedia
export const canSpeak = 'speechSynthesis' in window

/** Turn markdown into something pleasant to hear. */
export function speakable(md: string): string {
  return md
    .replace(/!\[[^\]]*\]\([^)]*\)/g, '')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/`([^`]+)`/g, '$1')
    .replace(/^\s{0,3}#{1,6}\s+/gm, '')
    .replace(/^\s*[-*+]\s+/gm, '')
    .replace(/^\s*\|.*\|\s*$/gm, '')
    .replace(/[*_~>#|]/g, '')
    .replace(/https?:\/\/\S+/g, 'link')
    .replace(/\s+/g, ' ')
    .trim()
}

/** Queue that accepts streamed markdown, speaks each complete sentence as soon as it arrives,
 *  and skips fenced code blocks (announcing them once). Create one per reply. */
export class SpeechQueue {
  private buf = ''
  private inCode = false
  private saidCode = false
  private pending = 0
  private finished = false
  private cancelled = false
  onIdle: (() => void) | null = null
  rate = 1.05

  feed(delta: string) { this.buf += delta; this.drain(false) }

  /** Call when the reply is complete; onIdle fires once everything queued has been spoken. */
  flush() {
    this.drain(true)
    this.finished = true
    if (this.pending === 0 && !this.cancelled) this.onIdle?.()
  }

  private drain(final: boolean) {
    for (;;) {
      if (this.inCode) {
        const end = this.buf.indexOf('```')
        if (end < 0) { if (final) this.buf = ''; return }   // still inside code: keep waiting
        this.buf = this.buf.slice(end + 3)
        this.inCode = false
        continue
      }
      const fence = this.buf.indexOf('```')
      if (fence >= 0) {
        this.say(this.buf.slice(0, fence))
        if (!this.saidCode) { this.say("I've put the code on screen."); this.saidCode = true }
        this.buf = this.buf.slice(fence + 3)
        this.inCode = true
        continue
      }
      // a sentence is complete once its terminator is followed by whitespace (avoids "3.14", "e.g.")
      const re = /[.!?](?=\s)|\n\n/g
      let cut = -1, m: RegExpExecArray | null
      while ((m = re.exec(this.buf))) cut = m.index + m[0].length
      if (cut > 0) { this.say(this.buf.slice(0, cut)); this.buf = this.buf.slice(cut) }
      if (final) { this.say(this.buf); this.buf = '' }
      return
    }
  }

  say(text: string) {
    const t = speakable(text)
    if (!t || !canSpeak || this.cancelled) return
    const u = new SpeechSynthesisUtterance(t)
    u.rate = this.rate
    u.lang = navigator.language
    this.pending++
    const done = () => {
      this.pending = Math.max(0, this.pending - 1)
      if (this.pending === 0 && this.finished && !this.cancelled) this.onIdle?.()
    }
    u.onend = done
    u.onerror = done
    speechSynthesis.speak(u)
  }

  cancel() {
    this.cancelled = true
    this.buf = ''
    this.pending = 0
    if (canSpeak) speechSynthesis.cancel()
  }

  get speaking() { return this.pending > 0 }
}

/** Listen once. Resolves with the final transcript ('' if nothing heard). Calls onInterim while speaking. */
export function listenOnce(onInterim: (t: string) => void, signal: AbortSignal): Promise<string> {
  if (canRecognize) {
    return new Promise((resolve, reject) => {
      const r = new SR()
      r.lang = navigator.language
      r.interimResults = true
      r.continuous = false
      let final = ''
      r.onresult = (e: any) => {
        let interim = ''
        for (let i = e.resultIndex; i < e.results.length; i++) {
          if (e.results[i].isFinal) final += e.results[i][0].transcript
          else interim += e.results[i][0].transcript
        }
        onInterim(final + interim)
      }
      r.onerror = (e: any) => (e.error === 'no-speech' || e.error === 'aborted' ? resolve('') : reject(new Error(`Speech recognition: ${e.error}`)))
      r.onend = () => resolve(final.trim())
      signal.addEventListener('abort', () => r.abort())
      r.start()
    })
  }
  if (canRecord) return recordAndTranscribe(signal)
  return Promise.reject(new Error('Voice input is not supported in this browser'))
}

/** Fallback for browsers without SpeechRecognition: record until ~1.5s of silence, transcribe server-side. */
async function recordAndTranscribe(signal: AbortSignal): Promise<string> {
  const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
  const rec = new MediaRecorder(stream)
  const chunks: Blob[] = []
  rec.ondataavailable = e => chunks.push(e.data)
  const ctx = new AudioContext()
  const analyser = ctx.createAnalyser()
  ctx.createMediaStreamSource(stream).connect(analyser)
  const data = new Uint8Array(analyser.fftSize)
  let heard = false, silentSince = performance.now()
  const started = performance.now()
  rec.start()
  await new Promise<void>(resolve => {
    const tick = () => {
      if (signal.aborted) return resolve()
      analyser.getByteTimeDomainData(data)
      const level = Math.max(...data) - 128
      if (level > 12) { heard = true; silentSince = performance.now() }
      const now = performance.now()
      if ((heard && now - silentSince > 1500) || now - started > 30_000 || (!heard && now - started > 8000)) return resolve()
      requestAnimationFrame(tick)
    }
    tick()
  })
  const blob: Blob = await new Promise(res => { rec.onstop = () => res(new Blob(chunks, { type: rec.mimeType })); rec.stop() })
  stream.getTracks().forEach(t => t.stop()); ctx.close()
  if (!heard || signal.aborted) return ''
  const fd = new FormData(); fd.append('file', blob, 'speech.webm')
  const res = await fetch('/api/voice/transcribe', { method: 'POST', body: fd, credentials: 'include' })
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || 'Transcription failed')
  return ((await res.json()).text || '').trim()
}
