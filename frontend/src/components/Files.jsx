import React, { useEffect, useState } from 'react'
import { api, fmtTime } from '../api.js'
import { WHERE_LABEL, openFile, runFileUrl } from './FileViewer.jsx'

function size(n) {
  return n > 1e6 ? (n / 1e6).toFixed(1) + ' MB' : n > 1e3 ? (n / 1e3).toFixed(1) + ' kB' : n + ' B'
}

export default function Files({ pid, seq, state, notify }) {
  const [data, setData] = useState(null)
  const [sources, setSources] = useState([])
  useEffect(() => {
    api.get(`/api/projects/${pid}/files`).then(setData).catch(notify)
    api.get(`/api/projects/${pid}/sources`).then(setSources).catch(notify)
  }, [pid, seq, notify])
  if (!data) return <div className="empty">Loading...</div>
  const names = state.agent_names
  const raw = (path, scope = 'shared') => `/api/projects/${pid}/files/raw?path=${encodeURIComponent(path)}&scope=${scope}`
  return (
    <div className="panel">
      <div className="panel-head">
        <h3>Files &amp; sources</h3>
        <span className="muted">The shared file tree is read-only during a round; each agent writes to its own staged copy, merged at
          the boundary. When two agents change the same file in a round, both versions are kept as <code>.conflict-r&lt;t&gt;-&lt;agent&gt;</code>.</span>
      </div>
      <Staged state={state} names={names} />
      {data.archived?.some(a => a.files.length) && (
        <div className="card">
          <h4>Archived (work from failed, skipped or discarded runs - never committed, never deleted)</h4>
          {data.archived.filter(a => a.files.length).map(a => (
            <div key={a.run_id} className="small">
              <b>round {a.round}</b>, run #{a.run_id}:{' '}
              {a.files.map(f => <button key={f.path} className="file-chip clickable w-archived"
                onClick={() => openFile({ url: runFileUrl(a.run_id, { path: f.path, where: 'archived' }), path: f.path, label: WHERE_LABEL.archived })}>📄 {f.path}</button>)}
            </div>
          ))}
        </div>
      )}
      <div className="two-col">
        <div className="card">
          <h4>Shared files ({data.shared.length})</h4>
          {data.shared.length === 0 && <div className="muted">No files yet.</div>}
          <table className="tbl">
            <tbody>{data.shared.map(f => (
              <tr key={f.path} className={f.path.includes('.conflict-r') ? 'conflict-row' : ''}>
                <td><button className="link mono" onClick={() => openFile({ url: raw(f.path), path: f.path, label: 'shared file tree' })}>{f.path}</button></td><td className="num">{size(f.size)}</td>
              </tr>))}</tbody>
          </table>
          {Object.entries(data.private).filter(([, fs]) => fs.length).map(([aid, fs]) => (
            <div key={aid}>
              <h4>Private to {names[aid]}</h4>
              <table className="tbl"><tbody>{fs.map(f => (
                <tr key={f.path}><td><button className="link mono" onClick={() => openFile({ url: raw(f.path, `private:${aid}`), path: f.path, label: `private to ${names[aid]}` })}>{f.path}</button></td><td className="num">{size(f.size)}</td></tr>
              ))}</tbody></table>
            </div>
          ))}
        </div>
        <div className="card">
          <h4>Change log</h4>
          {data.changes.length === 0 && <div className="muted">No committed changes yet.</div>}
          <table className="tbl small">
            <tbody>{data.changes.map(c => (
              <tr key={c.id} className={c.conflict ? 'conflict-row' : ''}>
                <td>r{c.round_number}</td><td>{names[c.agent_id] || c.agent_id}</td><td>{c.action}</td><td>{c.path}</td>
              </tr>))}</tbody>
          </table>
          <h4>Uploads</h4>
          <table className="tbl small">
            <tbody>{data.uploads.map(u => (
              <tr key={u.id}><td>{u.filename}</td><td>{u.channel === 'common' ? 'common' : `private: ${names[u.channel.split(':')[1]]}`}</td>
                <td><span className={'status-tag ' + u.status}>{u.status}{u.visible_from_round ? ` (round ${u.visible_from_round})` : ''}</span></td><td>{size(u.size)}</td></tr>
            ))}</tbody>
          </table>
        </div>
      </div>
      <div className="card">
        <h4>Retrieved sources ({sources.length})</h4>
        <p className="muted small">Every paper or page actually fetched by a tool is recorded here with the agent and round. Agents are told
          that only these (and your files) count as verified sources.</p>
        <table className="tbl">
          <thead><tr><th>Ref</th><th>Title</th><th>Retrieved by</th><th>Round</th></tr></thead>
          <tbody>{sources.map(s => (
            <tr key={s.id}>
              <td><code>{s.kind}:{s.ref.length > 40 ? s.ref.slice(0, 40) + '...' : s.ref}</code></td>
              <td>{s.url ? <a href={s.url} target="_blank" rel="noreferrer">{s.title || s.url}</a> : s.title}
                {s.meta?.authors && <div className="muted small">{s.meta.authors.slice(0, 5).join(', ')}{s.meta.published ? ` (${s.meta.published})` : ''}</div>}</td>
              <td>{names[s.agent_id]}</td><td>{s.round_number}</td>
            </tr>))}</tbody>
        </table>
      </div>
      <div className="muted small">Last refreshed {fmtTime(new Date().toISOString())}</div>
    </div>
  )
}

function Staged({ state, names }) {
  const live = state.rounds.filter(r => !['completed', 'discarded'].includes(r.status))
  const items = live.flatMap(r => r.runs.filter(x => !x.superseded).map(x => ({ r, x, files: (x.files || []).filter(f => f.where === 'staged') })))
    .filter(i => i.files.length)
  if (!items.length) return null
  return (
    <div className="card pending-card">
      <h4>Staged in the current round (not yet visible to other agents)</h4>
      {items.map(({ r, x, files }) => (
        <div key={x.id} className="small">
          <b>{names[x.agent_id]}</b> (round {r.number}, {x.status}):{' '}
          {files.map(f => <button key={f.path} className="file-chip clickable w-staged"
            onClick={() => openFile({ url: runFileUrl(x.id, f), path: f.path, label: WHERE_LABEL.staged })}>📄 {f.path} ({f.status})</button>)}
        </div>
      ))}
    </div>
  )
}
