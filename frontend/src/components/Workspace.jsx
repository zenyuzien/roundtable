import React, { useEffect, useState } from 'react'
import { api, fmtTime } from '../api.js'
import Markdown from '../Markdown.jsx'

const LABEL = {
  research_question: 'Research questions', hypothesis: 'Hypotheses', derivation: 'Derivations',
  literature_note: 'Literature notes', experiment_proposal: 'Experiment proposals', citation: 'Citations',
  code: 'Code', result: 'Results', open_question: 'Open questions', rejected_hypothesis: 'Rejected hypotheses', note: 'Notes',
}

export default function Workspace({ pid, seq, meta, act, notify }) {
  const [data, setData] = useState(null)
  const [edit, setEdit] = useState(null)
  const [hist, setHist] = useState({})
  useEffect(() => { api.get(`/api/projects/${pid}/workspace`).then(setData).catch(notify) }, [pid, seq, notify])
  if (!data) return <div className="empty">Loading...</div>
  const names = data.agent_names
  const who = (v) => v.author_type === 'user' ? 'you' : v.author_type === 'agents' ? 'several agents' : (names[v.agent_id] || `agent ${v.agent_id}`)
  const groups = {}
  for (const e of data.entries) (groups[e.kind] = groups[e.kind] || []).push(e)
  const kinds = meta?.workspace_kinds || Object.keys(LABEL)

  return (
    <div className="panel">
      <div className="panel-head">
        <h3>Shared research workspace</h3>
        <span className="muted">Persistent and independent of the chat. Agent proposals are committed only at round boundaries;
          simultaneous edits to the same entry are all kept and flagged as conflicts. Your edits are delivered at the next boundary.</span>
        <button className="btn primary" onClick={() => setEdit({ key: '', kind: 'note', title: '', content: '', status: 'active', isNew: true })}>+ New entry</button>
      </div>
      {edit && <EntryEditor e={edit} kinds={kinds} onCancel={() => setEdit(null)}
        onSave={async (x) => { const r = await act(() => api.post(`/api/projects/${pid}/workspace`, x), 'Edit queued for the next round'); if (r) setEdit(null) }} />}
      {(data.staged_proposals.length > 0 || data.pending_user_edits.length > 0) && (
        <div className="card pending-card">
          <b>Waiting for the round boundary</b>
          {data.staged_proposals.map(p => <div key={p.id} className="small">round {p.round_number} - {names[p.agent_id]} staged <code>{p.op}</code> on <code>{p.key}</code></div>)}
          {data.pending_user_edits.map(p => <div key={p.id} className="small">your edit of <code>{p.key}</code> (pending)</div>)}
        </div>
      )}
      {data.entries.length === 0 && <div className="empty">The workspace is empty. Agents with the workspace tool can propose entries; you can add them too.</div>}
      {kinds.filter(k => groups[k]).map(k => (
        <div key={k} className="ws-group">
          <h4>{LABEL[k] || k} <span className="muted">({groups[k].length})</span></h4>
          {groups[k].map(e => (
            <div key={e.key} className={'card ws-entry ' + (e.conflict ? 'conflict' : '') + ' st-' + e.status}>
              <div className="ws-head">
                <b>{e.title || e.key}</b> <code className="muted">{e.key}</code>
                <span className="status-tag">v{e.version}</span>
                {e.status !== 'active' && <span className="status-tag">{e.status}</span>}
                {e.conflict && <span className="status-tag failed">conflict</span>}
                <span className="muted small">{who(e)} - round {e.round_number ?? '-'} - {fmtTime(e.created_at)}</span>
                <span className="spacer" />
                <button className="link" onClick={() => setEdit({ key: e.key, kind: e.kind, title: e.title, content: e.content, status: e.status })}>
                  {e.conflict ? 'resolve' : 'edit'}</button>
                <button className="link" onClick={async () => setHist({ ...hist, [e.key]: hist[e.key] ? null : await api.get(`/api/projects/${pid}/workspace/history?key=${encodeURIComponent(e.key)}`) })}>history</button>
              </div>
              {e.conflict ? (
                <div className="variants">
                  {e.variants.map(v => (
                    <div key={v.id} className="variant">
                      <div className="muted small">variant by <b>{who(v)}</b> (round {v.round_number})
                        <button className="link" onClick={() => setEdit({ key: e.key, kind: v.kind, title: v.title, content: v.content, status: 'active' })}>adopt</button></div>
                      <Markdown text={v.content} />
                    </div>
                  ))}
                </div>
              ) : <Markdown text={e.content} />}
              {hist[e.key] && (
                <div className="history">
                  {hist[e.key].map(v => (
                    <details key={v.id}>
                      <summary>v{v.version ?? '(pending)'} - {who(v)} - round {v.round_number ?? '-'} {v.conflict ? '(conflict variant)' : ''} - {fmtTime(v.created_at)}</summary>
                      <Markdown text={v.content} />
                    </details>
                  ))}
                </div>
              )}
            </div>
          ))}
        </div>
      ))}
    </div>
  )
}

function EntryEditor({ e, kinds, onSave, onCancel }) {
  const [d, setD] = useState(e)
  return (
    <div className="card">
      <div className="form-row">
        <label>Key<input value={d.key} disabled={!e.isNew} onChange={x => setD({ ...d, key: x.target.value })} placeholder="stable-identifier" /></label>
        <label>Kind<select value={d.kind} onChange={x => setD({ ...d, kind: x.target.value })}>{kinds.map(k => <option key={k}>{k}</option>)}</select></label>
        <label>Title<input value={d.title} onChange={x => setD({ ...d, title: x.target.value })} /></label>
        <label>Status<input value={d.status} onChange={x => setD({ ...d, status: x.target.value })} placeholder="active / supported / rejected" /></label>
      </div>
      <textarea className="mono" rows={10} value={d.content} onChange={x => setD({ ...d, content: x.target.value })} />
      <div className="row">
        <button className="btn primary" disabled={!d.key.trim()} onClick={() => onSave(d)}>Queue edit</button>
        <button className="btn" onClick={onCancel}>Cancel</button>
      </div>
    </div>
  )
}
