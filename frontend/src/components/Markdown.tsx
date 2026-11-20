import { memo, useState, isValidElement, type ReactNode } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeHighlight from 'rehype-highlight'
import { Check, Copy } from 'lucide-react'

function textOf(n: ReactNode): string {
  if (typeof n === 'string' || typeof n === 'number') return String(n)
  if (Array.isArray(n)) return n.map(textOf).join('')
  if (isValidElement(n)) return textOf((n.props as any).children)
  return ''
}

export function CopyButton({ text, label }: { text: string; label?: string }) {
  const [ok, setOk] = useState(false)
  return (
    <button aria-label={label || 'Copy'} title={label || 'Copy'}
      onClick={() => navigator.clipboard.writeText(text).then(() => { setOk(true); setTimeout(() => setOk(false), 1500) })}
      className="inline-flex items-center gap-1 rounded px-1.5 py-1 text-xs text-zinc-500 hover:bg-zinc-100 hover:text-zinc-800 dark:hover:bg-zinc-800 dark:hover:text-zinc-200">
      {ok ? <Check size={14} /> : <Copy size={14} />}{label && <span>{ok ? 'Copied' : label}</span>}
    </button>
  )
}

function Pre({ children }: { children?: ReactNode }) {
  const code = isValidElement(children) ? (children.props as any) : {}
  const lang = /language-(\w+)/.exec(code.className || '')?.[1] || 'text'
  return (
    <pre>
      <div className="flex items-center justify-between bg-zinc-800 dark:bg-zinc-900 px-3 py-1 text-xs text-zinc-400">
        <span>{lang}</span>
        <button onClick={e => { navigator.clipboard.writeText(textOf(code.children)); const b = e.currentTarget; b.textContent = 'Copied'; setTimeout(() => (b.textContent = 'Copy'), 1500) }}
          className="hover:text-white">Copy</button>
      </div>
      {children}
    </pre>
  )
}

export const Markdown = memo(function Markdown({ text }: { text: string }) {
  return (
    <div className="prose prose-zinc dark:prose-invert max-w-none prose-p:my-2 prose-li:my-0.5 prose-headings:mt-4 prose-headings:mb-2 prose-a:text-accent">
      <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[[rehypeHighlight, { ignoreMissing: true }]]}
        components={{
          pre: Pre as any,
          a: ({ href, children }) => href?.startsWith('#/')
            ? <a href={href}>{children}</a>                                  // in-app link (e.g. Settings)
            : <a href={href} target="_blank" rel="noopener noreferrer">{children}</a>,
          table: ({ children }) => <div className="overflow-x-auto"><table>{children}</table></div>,
        }}>
        {text}
      </ReactMarkdown>
    </div>
  )
})
