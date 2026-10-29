import React, { memo } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import rehypeKatex from 'rehype-katex'
import 'katex/dist/katex.min.css'

// LLMs emit \[...\], \(...\) and inline $$...$$; normalise to what remark-math expects,
// leaving code spans and fenced blocks untouched.
function normalizeMath(src) {
  if (!src) return ''
  const parts = src.split(/(```[\s\S]*?```|`[^`\n]*`)/g)
  return parts
    .map((p, i) => {
      if (i % 2) return p
      return p
        .replace(/\\\[([\s\S]+?)\\\]/g, (_, m) => `\n$$\n${m.trim()}\n$$\n`)
        .replace(/\\\(([\s\S]+?)\\\)/g, (_, m) => `$${m.trim()}$`)
        .replace(/(^|[^\n])[ \t]*\$\$([^\n$][\s\S]*?)\$\$/g, (_, pre, m) => `${pre}\n$$\n${m.trim()}\n$$\n`)
    })
    .join('')
}

const katexOpts = { throwOnError: false, strict: false, trust: false }

function Markdown({ text, className }) {
  return (
    <div className={'md ' + (className || '')}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMath]}
        rehypePlugins={[[rehypeKatex, katexOpts]]}
        components={{ a: ({ node, ...props }) => <a {...props} target="_blank" rel="noreferrer" /> }}
      >
        {normalizeMath(text)}
      </ReactMarkdown>
    </div>
  )
}

export default memo(Markdown)
