import { useState, type FormEvent } from 'react'
import { Bot } from 'lucide-react'
import { api, type User } from '../api'
import { Button, ErrorNote, Input, Label } from '../components/ui'

export default function Auth({ onAuthed }: { onAuthed: (u: User) => void }) {
  const [mode, setMode] = useState<'login' | 'register'>('login')
  const [email, setEmail] = useState(''); const [password, setPassword] = useState(''); const [name, setName] = useState('')
  const [error, setError] = useState<string | null>(null); const [busy, setBusy] = useState(false)

  async function submit(e: FormEvent) {
    e.preventDefault(); setBusy(true); setError(null)
    try {
      const r = await api.post<{ user: User }>(`/auth/${mode}`, mode === 'login' ? { email, password } : { email, password, name })
      onAuthed(r.user)
    } catch (err: any) { setError(err.message) } finally { setBusy(false) }
  }

  return (
    <div className="min-h-full flex items-center justify-center p-4 bg-zinc-50 dark:bg-zinc-950">
      <form onSubmit={submit} className="w-full max-w-sm space-y-4 rounded-2xl bg-white dark:bg-zinc-900 p-7 shadow-sm border border-zinc-200 dark:border-zinc-800">
        <div className="flex items-center gap-2 mb-2"><div className="rounded-lg bg-accent p-1.5 text-white"><Bot size={20} /></div><span className="text-lg font-semibold">Terminator</span></div>
        <h1 className="text-sm text-zinc-500">{mode === 'login' ? 'Sign in to your assistant' : 'Create your account'}</h1>
        {mode === 'register' && <div><Label>Name</Label><Input value={name} onChange={e => setName(e.target.value)} autoComplete="name" /></div>}
        <div><Label>Email</Label><Input type="email" required value={email} onChange={e => setEmail(e.target.value)} autoComplete="email" /></div>
        <div><Label hint={mode === 'register' ? 'min 8 characters' : undefined}>Password</Label>
          <Input type="password" required minLength={mode === 'register' ? 8 : 1} value={password} onChange={e => setPassword(e.target.value)} autoComplete={mode === 'login' ? 'current-password' : 'new-password'} /></div>
        <ErrorNote error={error} />
        <Button type="submit" disabled={busy} className="w-full py-2">{busy ? 'Please wait…' : mode === 'login' ? 'Sign in' : 'Create account'}</Button>
        <p className="text-center text-sm text-zinc-500">
          {mode === 'login' ? 'No account?' : 'Have an account?'}{' '}
          <button type="button" className="text-accent hover:underline" onClick={() => { setMode(mode === 'login' ? 'register' : 'login'); setError(null) }}>
            {mode === 'login' ? 'Register' : 'Sign in'}</button>
        </p>
      </form>
    </div>
  )
}
