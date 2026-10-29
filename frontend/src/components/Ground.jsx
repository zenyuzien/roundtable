import React, { useEffect, useMemo, useRef, useState } from 'react'
import { api, agentColor, duration, fmtTokens } from '../api.js'
import Markdown from '../Markdown.jsx'
import { openFile, runFileUrl } from './FileViewer.jsx'

const IMG = /\.(png|jpe?g|gif|svg|webp|bmp)$/i
const MD = /\.(md|markdown)$/i
const BINARY = /\.(npy|npz|pt|pth|ckpt|pkl|pickle|zip|gz|tar|h5|hdf5|bin|so|parquet|feather|pdf)$/i
const PREVIEW_LIMIT = 200_000
const CONV_RE = /```convergence\s*\n[\s\S]*?```/g

// The activity screen: the common ground (everything every agent sees) plus one ground per agent.
export default function Ground({ state, messages, pid, act, onInspect }) {
  const [tab, setTab] = useState('common')
  const agents = state.agents
  const idx = useMemo(() => Object.fromEntries(agents.map((a, i) => [a.id, i])), [agents])
  const outByRun = useMemo(() => {
    const m = {}
    for (const x of messages) if (x.kind === 'agent_output') m[x.agent_run_id] = x
    return m
  }, [messages])
  const task = messages.find(m => m.kind === 'task')
  const channel = tab === 'common' ? 'common' : `agent:${tab}`
  const agentId = tab === 'common' ? null : Number(tab)
  const chanMsgs = messages.filter(m => m.channel === channel && m.kind === 'user' && m.status !== 'retracted')
  const pending = chanMsgs.filter(m => m.status === 'pending')
  const rounds = state.rounds.filter(r => r.status !== 'discarded')
  const bottom = useRef(null)
  const live = !!state.running_round
  useEffect(() => { if (live) bottom.current?.scrollIntoView({ block: 'end', behavior: 'smooth' }) }, [live, rounds.length])

  return (
    <div className="ground">
      <Header state={state} pid={pid} act={act} />
      <div className="ground-tabs">
        <button className={tab === 'common' ? 'on' : ''} onClick={() => setTab('common')}>
          Common ground {state.pending_common > 0 && <span className="badge pending">{state.pending_common}</span>}
        </button>
        {agents.map((a, i) => (
          <button key={a.id} style={agentColor(i)} className={tab === String(a.id) ? 'on' : ''} onClick={() => setTab(String(a.id))}>
            <span className="agent-chip">{a.name}</span>
            {a.pending_messages > 0 && <span className="badge pending">{a.pending_messages}</span>}
          </button>
        ))}
      </div>

      <div className="ground-body">
        <div className="ground-note muted small">
          {agentId == null
            ? 'Everything here is seen by every agent at the start of the next round: the goal, all outputs, all files they wrote, and what you add below.'
            : `Only ${agents.find(a => a.id === agentId)?.name} sees what you add here (on top of the common ground), from the next round.`}
        </div>

        {agentId == null && task && (
          <section className="g-round">
            <div className="g-round-head"><b>Goal</b></div>
            <div className="g-card user"><Markdown text={task.content} /></div>
          </section>
        )}

        {rounds.map(r => {
          const delivered = chanMsgs.filter(m => m.visible_from_round === r.number && m.status !== 'pending')
          const runs = r.runs.filter(x => !x.superseded && (agentId == null || x.agent_id === agentId))
          if (!runs.length && !delivered.length) return null
          return (
            <section key={r.id} className={'g-round ' + r.status}>
              <div className="g-round-head">
                <b>{r.kind === 'synthesis' ? 'Final synthesis' : `Round ${r.number}`}</b>
                <span className={'state-pill ' + r.status}>{r.status}</span>
                {r.finished_at && <span className="muted small">{duration(r.started_at, r.finished_at)}</span>}
              </div>
              {delivered.map(m => <UserMsg key={m.id} m={m} />)}
              {runs.map(run => (
                <AgentTurn key={run.id} run={run} out={outByRun[run.id]} i={idx[run.agent_id] ?? 0}
                  roundDone={r.status === 'completed'} onInspect={onInspect} />
              ))}
            </section>
          )
        })}

        {pending.length > 0 && (
          <section className="g-round">
            <div className="g-round-head"><b>Queued for the next round</b></div>
            {pending.map(m => (
              <UserMsg key={m.id} m={m} onRetract={() => act(() => api.del(`/api/messages/${m.id}`))} />
            ))}
          </section>
        )}
        <div ref={bottom} />
      </div>

      <Composer channel={channel} pid={pid} act={act} target={agentId == null ? 'all agents' : agents.find(a => a.id === agentId)?.name} />
    </div>
  )
}

function Header({ state, pid, act }) {
  const p = state.project
  const ctl = (a) => act(() => api.post(`/api/projects/${pid}/control/${a}`))
  const inc = state.incomplete_round
  const busy = state.busy
  const cur = state.rounds.filter(r => r.status !== 'discarded').slice(-1)[0]
  const runs = cur ? cur.runs.filter(x => !x.superseded) : []
  const running = runs.filter(x => x.status === 'running').map(x => x.agent_name)
  const debateDone = state.rounds.filter(r => r.status === 'completed' && r.kind !== 'synthesis').length
  const u = state.usage
  const newRun = async () => {
    if (!window.confirm('Start a new run? The current run (messages, files, workspace) is deleted.')) return
    await act(async () => {
      if (busy) await api.post(`/api/projects/${pid}/control/stop`)
      await api.del(`/api/projects/${pid}`)
      window.location.reload()
    })
  }
  return (
    <div className="g-header">
      <div className="g-status">
        <span className={'state-pill ' + p.state}>{p.state.replace('_', ' ')}</span>
        <b>{state.running_round ? `Round ${state.running_round} running` : `${debateDone} / ${p.settings.max_rounds} rounds`}</b>
        {running.length > 0 && <span className="muted">working: {running.join(', ')}</span>}
        {p.state_reason && <span className="muted small">{p.state_reason}</span>}
        <span className="muted small">{fmtTokens(u.tokens_in + u.tokens_out)} tokens</span>
      </div>
      <div className="controls">
        {!busy && !inc && p.state !== 'completed' && (
          <button className="btn primary" onClick={() => ctl(state.last_completed ? 'resume' : 'start')}>
            ▶ {state.last_completed ? 'Continue' : 'Start'}</button>
        )}
        {busy && p.state === 'running' && <button className="btn" onClick={() => ctl('pause')}>Pause after this round</button>}
        {busy && <button className="btn danger" onClick={() => ctl('stop')}>Stop</button>}
        {!busy && inc && (
          <>
            <button className="btn primary" onClick={() => ctl('retry_failed')}>Retry failed agents</button>
            <button className="btn" onClick={() => ctl('skip_failed')}>Continue without them</button>
          </>
        )}
        <button className="btn" onClick={newRun}>New run</button>
      </div>
    </div>
  )
}

function UserMsg({ m, onRetract }) {
  return (
    <div className={'g-card user' + (m.status === 'pending' ? ' pending' : '')}>
      <div className="g-meta">
        <b>You</b>
        {m.status === 'pending' ? <span className="status-tag pending">delivered next round</span>
          : <span className="status-tag processed">seen in round {m.visible_from_round}</span>}
        {onRetract && <button className="link" onClick={onRetract}>remove</button>}
      </div>
      {m.content && <Markdown text={m.content} />}
      {m.attachments?.map(a => <FileBlock key={a.file_id} path={a.filename} url={`/api/files/${a.file_id}/raw`} />)}
    </div>
  )
}

function AgentTurn({ run, out, i, roundDone, onInspect }) {
  const failed = ['failed', 'interrupted', 'aborted', 'skipped'].includes(run.status)
  const text = out ? out.content.replace(CONV_RE, '').trim() : ''
  const files = (run.files || []).filter(f => f.where !== 'archived')
  return (
    <div className={'g-card agent ' + run.status} style={agentColor(i)}>
      <div className="g-meta">
        <span className="agent-chip">{run.agent_name}</span>
        <span className={'run-status ' + run.status}>{run.status === 'running' && <span className="spinner" />}{run.status}</span>
        {out && !roundDone && <span className="muted small">others see this next round</span>}
        <span className="spacer" />
        <button className="link" onClick={() => onInspect(run.id)} title="exact context, tool calls, transcript">details</button>
      </div>
      {failed && run.error && <div className="error-box">{run.error}</div>}
      {run.status === 'running' && !out && <div className="muted">working...</div>}
      {text && <Markdown text={text} />}
      {files.map(f => <FileBlock key={f.where + f.path} path={f.path} url={runFileUrl(run.id, f)}
        tag={f.where === 'staged' ? 'being written' : f.status === 'conflict' ? 'conflict version' : null} />)}
    </div>
  )
}

// Inline file content: text/markdown/code shown in full (up to a limit), images inline.
function FileBlock({ path, url, tag }) {
  const [text, setText] = useState(null)
  const [err, setErr] = useState(null)
  const [open, setOpen] = useState(true)
  const kind = IMG.test(path) ? 'image' : BINARY.test(path) ? 'binary' : 'text'
  useEffect(() => {
    if (kind !== 'text') return
    let alive = true
    fetch(url).then(async r => {
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`)
      const t = await r.text()
      if (alive) setText(t)
    }).catch(e => alive && setErr(e.message))
    return () => { alive = false }
  }, [url, kind])
  return (
    <div className="file-block">
      <div className="file-head" onClick={() => setOpen(!open)}>
        <span>{open ? '▾' : '▸'} 📄 <span className="mono">{path}</span></span>
        {tag && <span className="status-tag pending">{tag}</span>}
        <span className="spacer" />
        <button className="link" onClick={e => { e.stopPropagation(); openFile({ url, path }) }}>open</button>
      </div>
      {open && (
        <div className="file-content">
          {err && <div className="muted small">{err}</div>}
          {kind === 'image' && <img src={url} alt={path} />}
          {kind === 'binary' && <div className="muted small">Binary file - use "open" to download it.</div>}
          {kind === 'text' && text == null && !err && <div className="muted small">loading...</div>}
          {kind === 'text' && text != null && (
            MD.test(path) ? <Markdown text={text.slice(0, PREVIEW_LIMIT)} />
              : <pre className="mono">{text.slice(0, PREVIEW_LIMIT) || '(empty file)'}</pre>
          )}
          {text && text.length > PREVIEW_LIMIT && <div className="muted small">[truncated here; "open" shows the whole file]</div>}
        </div>
      )}
    </div>
  )
}

function Composer({ channel, pid, act, target }) {
  const [text, setText] = useState('')
  const [files, setFiles] = useState([])
  const ref = useRef(null)
  const send = async () => {
    if (!text.trim() && !files.length) return
    const fd = new FormData()
    fd.append('channel', channel)
    fd.append('content', text)
    for (const f of files) fd.append('files', f)
    const r = await act(() => api.post(`/api/projects/${pid}/messages`, fd))
    if (r) { setText(''); setFiles([]); if (ref.current) ref.current.value = '' }
  }
  return (
    <div className="composer">
      <textarea rows={2} value={text} onChange={e => setText(e.target.value)}
        placeholder={`Add something for ${target} - they see it from the next round. Ctrl+Enter to send.`}
        onKeyDown={e => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) send() }} />
      <div className="composer-actions">
        <input ref={ref} type="file" multiple onChange={e => setFiles([...e.target.files])} />
        <button className="btn primary" onClick={send} disabled={!text.trim() && !files.length}>Add for next round</button>
      </div>
    </div>
  )
}
