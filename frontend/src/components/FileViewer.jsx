import React, { useEffect, useState } from 'react'
import Markdown from '../Markdown.jsx'

const IMG = /\.(png|jpe?g|gif|svg|webp|bmp)$/i
const PDF = /\.pdf$/i
const MD = /\.(md|markdown)$/i
const BINARY = /\.(npy|npz|pt|pth|ckpt|pkl|pickle|zip|gz|tar|h5|hdf5|bin|so|parquet|feather)$/i
const MAX_TEXT = 2_000_000

// file = { url, path, label? }
export default function FileViewer({ file, onClose }) {
  const [text, setText] = useState(null)
  const [err, setErr] = useState(null)
  const [raw, setRaw] = useState(false)
  const kind = IMG.test(file.path) ? 'image' : PDF.test(file.path) ? 'pdf' : BINARY.test(file.path) ? 'binary' : 'text'

  useEffect(() => {
    setText(null); setErr(null)
    if (kind !== 'text') return
    fetch(file.url).then(async r => {
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`)
      const t = await r.text()
      setText(t.length > MAX_TEXT ? t.slice(0, MAX_TEXT) + '\n\n[... truncated for display; download for the full file]' : t)
    }).catch(e => setErr(e.message))
  }, [file.url, kind])

  useEffect(() => {
    const k = (e) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', k)
    return () => window.removeEventListener('keydown', k)
  }, [onClose])

  return (
    <div className="drawer-backdrop viewer-backdrop" onClick={onClose}>
      <div className="viewer" onClick={e => e.stopPropagation()}>
        <div className="drawer-head">
          <div>
            <h3 className="mono">{file.path}</h3>
            {file.label && <div className="muted small">{file.label}</div>}
          </div>
          <div className="row" style={{ marginTop: 0 }}>
            {kind === 'text' && MD.test(file.path) && <label className="chk small"><input type="checkbox" checked={raw} onChange={e => setRaw(e.target.checked)} /> raw</label>}
            <a className="btn" href={file.url} download={file.path.split('/').pop()}>Download</a>
            <button className="btn" onClick={onClose}>Close ✕</button>
          </div>
        </div>
        <div className="drawer-body">
          {err && <div className="error-box">{err}</div>}
          {kind === 'image' && <img className="viewer-img" src={file.url} alt={file.path} />}
          {kind === 'pdf' && <iframe className="viewer-pdf" src={file.url} title={file.path} />}
          {kind === 'binary' && <div className="muted">Binary file - use Download to open it locally.</div>}
          {kind === 'text' && text == null && !err && <div className="muted">Loading...</div>}
          {kind === 'text' && text != null && (MD.test(file.path) && !raw
            ? <Markdown text={text} />
            : <pre className="mono viewer-pre">{text || '(empty file)'}</pre>)}
        </div>
      </div>
    </div>
  )
}

export function runFileUrl(runId, f) {
  return `/api/runs/${runId}/file?path=${encodeURIComponent(f.path)}&where=${f.where}`
}

export const WHERE_LABEL = {
  staged: 'staged - in this agent\'s working copy, shared with others when the round completes',
  committed: 'committed to the shared file tree',
  archived: 'not committed (failed / skipped / discarded run) - kept in the archive',
}

// Open a file in the app-wide viewer from anywhere: openFile({ url, path, label })
export function openFile(file) {
  window.dispatchEvent(new CustomEvent('rt:view-file', { detail: file }))
}

export function FileChips({ runId, files, max = 8 }) {
  if (!files?.length) return null
  const shown = files.slice(0, max)
  return (
    <div className="file-chips">
      <span className="muted small">files:</span>
      {shown.map((f, i) => (
        <button key={i} className={'file-chip clickable w-' + f.where} title={WHERE_LABEL[f.where]}
          onClick={() => openFile({ url: runFileUrl(runId, f), path: f.path, label: WHERE_LABEL[f.where] })}>
          📄 {f.path}{f.where !== 'committed' ? ` (${f.where})` : f.status === 'conflict' ? ' (conflict)' : ''}
        </button>
      ))}
      {files.length > max && <span className="muted small">+{files.length - max} more (inspect → files)</span>}
    </div>
  )
}
