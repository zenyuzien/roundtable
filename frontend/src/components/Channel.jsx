import React, { useMemo, useRef, useState } from 'react'
import { api, agentColor, duration, fmtCost, fmtTime, fmtTokens } from '../api.js'
import Markdown from '../Markdown.jsx'
import { FileChips } from './FileViewer.jsx'

function useLocal(key, init) {
  const [v, setV] = useState(() => {
    try { return localStorage.getItem(key) ?? init } catch { return init }
  })
  const set = (x) => { setV(x); try { localStorage.setItem(key, x) } catch { /* ignore */ } }
  return [v, set]
}

export default function Channel({ channel, state, messages, pid, act, onInspect }) {
  const isCommon = channel === 'common'
  const agentId = isCommon ? null : Number(channel.split(':')[1])
  const agent = state.agents.find(a => a.id === agentId)
  const [layout, setLayout] = useLocal('rt.layout', 'timeline')
  const idx = useMemo(() => Object.fromEntries(state.agents.map((a, i) => [a.id, i])), [state.agents])
  const names = state.agent_names
  const outByRun = useMemo(() => {
    const m = {}
    for (const x of messages) if (x.kind === 'agent_output') m[x.agent_run_id] = x
    return m
  }, [messages])
  const chanMsgs = messages.filter(m => m.channel === channel && m.kind !== 'agent_output')
  const task = messages.find(m => m.kind === 'task')
  const rounds = state.rounds
  const started = rounds.some(r => r.status !== 'discarded')
  const pending = chanMsgs.filter(m => m.status === 'pending' && m.kind === 'user')

  if (!isCommon && !agent) return <div className="empty">This agent no longer exists.</div>

  return (
    <div className="channel">
      <div className="channel-head">
        {isCommon ? (
          <>
            <h3># Common channel</h3>
            <span className="muted">Initial task, your shared messages and every agent's output, by round. All agents read this channel.</span>
            <div className="seg">
              <button className={layout === 'timeline' ? 'on' : ''} onClick={() => setLayout('timeline')}>Timeline</button>
              <button className={layout === 'grid' ? 'on' : ''} onClick={() => setLayout('grid')}>Side by side</button>
            </div>
          </>
        ) : (
          <>
            <h3 style={agentColor(idx[agentId])}><span className="agent-chip">{agent.name}</span> private channel</h3>
            <span className="muted">Only {agent.name} receives messages and files sent here. {agent.provider} / {agent.model} -
              instructions v{agent.instruction_version}{agent.enabled ? '' : ' - disabled'}</span>
          </>
        )}
      </div>

      <div className="channel-body">
        {isCommon && (!started ? <TaskEditor task={task} pid={pid} act={act} /> : task && (
          <div className="round-block">
            <div className="round-head"><span className="round-num">Round 0</span><span className="muted">Initial task</span></div>
            <div className="msg user-msg"><div className="msg-meta"><b>You</b> - initial task</div><Markdown text={task.content} />
              <Attachments m={task} /></div>
          </div>
        ))}

        {isCommon && layout === 'grid' && started
          ? <Grid state={state} outByRun={outByRun} idx={idx} names={names} onInspect={onInspect} messages={messages} />
          : rounds.map(r => (
            <RoundBlock key={r.id} r={r} channel={channel} agentId={agentId} chanMsgs={chanMsgs} outByRun={outByRun}
              idx={idx} names={names} onInspect={onInspect} />
          ))}

        {pending.length > 0 && (
          <div className="round-block pending-block">
            <div className="round-head"><span className="round-num">Pending</span>
              <span className="muted">queued; delivered at the next round boundary ({nextDelivery(state)}), never into a running round</span></div>
            {pending.map(m => (
              <div key={m.id} className="msg user-msg pending">
                <div className="msg-meta"><b>You</b> <span className="status-tag pending">pending</span>
                  {m.submitted_during_round ? <span className="muted"> submitted during round {m.submitted_during_round}</span> : null}
                  <span className="muted"> {fmtTime(m.created_at)}</span>
                  <button className="link" onClick={() => act(() => api.del(`/api/messages/${m.id}`), 'Message retracted')}>retract</button>
                </div>
                <Markdown text={m.content} /><Attachments m={m} />
              </div>
            ))}
          </div>
        )}
      </div>

      {(started || !isCommon) && <Composer channel={channel} pid={pid} act={act} state={state} />}
    </div>
  )
}

function nextDelivery(state) {
  if (state.running_round) return `round ${state.running_round + 1}`
  if (state.incomplete_round) return `the round after round ${state.incomplete_round.number} is resolved`
  return `round ${state.last_completed + 1}`
}

function Attachments({ m }) {
  if (!m.attachments?.length) return null
  return <div className="attachments">{m.attachments.map(a => <span key={a.file_id} className="file-chip">📎 {a.workspace_path || a.filename}</span>)}</div>
}

function StatusTag({ m }) {
  const map = {
    incorporated: [`in round ${m.visible_from_round} snapshot`, 'incorporated'],
    processed: [`processed in round ${m.visible_from_round}`, 'processed'],
    pending: ['pending', 'pending'],
  }
  const [label, cls] = map[m.status] || [m.status, '']
  return <span className={'status-tag ' + cls}>{label}</span>
}

function RoundBlock({ r, channel, agentId, chanMsgs, outByRun, idx, names, onInspect }) {
  const delivered = chanMsgs.filter(m => m.kind === 'user' && m.visible_from_round === r.number && m.status !== 'pending')
  const [open, setOpen] = useState(r.status !== 'discarded')
  let runs = r.runs
  if (agentId != null) runs = runs.filter(x => x.agent_id === agentId)
  if (agentId != null && runs.length === 0 && delivered.length === 0) return null
  const current = runs.filter(x => !x.superseded)
  const old = runs.filter(x => x.superseded)
  return (
    <div className={'round-block ' + r.status + (r.kind === 'synthesis' ? ' synthesis' : '')}>
      <div className="round-head" onClick={() => r.status === 'discarded' && setOpen(!open)}>
        <span className="round-num">{r.kind === 'synthesis' ? `Final synthesis (round ${r.number})` : `Round ${r.number}`}</span>
        <span className={'state-pill ' + r.status}>{r.status}</span>
        <span className="muted">{fmtTime(r.started_at)}{r.finished_at ? ` - ${duration(r.started_at, r.finished_at)}` : ''}</span>
        {r.note && <span className="muted">- {r.note}</span>}
      </div>
      {open && (
        <>
          {delivered.map(m => (
            <div key={m.id} className="msg user-msg">
              <div className="msg-meta"><b>You</b>{channel !== 'common' ? ' (private)' : ''} <StatusTag m={m} />
                <span className="muted"> {fmtTime(m.created_at)}</span></div>
              <Markdown text={m.content} /><Attachments m={m} />
            </div>
          ))}
          <div className={'outputs ' + (current.length > 1 ? 'multi' : '')}>
            {current.map(run => <OutputCard key={run.id} run={run} out={outByRun[run.id]} idx={idx} names={names}
              onInspect={onInspect} old={old.filter(o => o.agent_id === run.agent_id)} roundStatus={r.status} />)}
          </div>
        </>
      )}
    </div>
  )
}

function OutputCard({ run, out, idx, onInspect, old, roundStatus, compact }) {
  const [collapsed, setCollapsed] = useState(false)
  const failed = ['failed', 'interrupted', 'aborted', 'skipped'].includes(run.status)
  return (
    <div className={'msg agent-msg ' + run.status + (compact ? ' compact' : '')} style={agentColor(idx[run.agent_id] ?? 0)}>
      <div className="msg-meta">
        <span className="agent-chip">{run.agent_name}</span>
        <span className={'run-status ' + run.status}>{run.status === 'running' ? <span className="spinner" /> : null}{run.status}</span>
        {run.attempt > 1 && <span className="muted">attempt {run.attempt}</span>}
        <span className="muted">{fmtTokens(run.tokens_in + run.tokens_out)} tok - {fmtCost(run.cost)}</span>
        {run.started_at && <span className="muted">{duration(run.started_at, run.finished_at)}</span>}
        <span className="spacer" />
        {out && !compact && <button className="link" onClick={() => setCollapsed(!collapsed)}>{collapsed ? 'expand' : 'collapse'}</button>}
        <button className="link" onClick={() => onInspect(run.id)} title="Exact context, tool calls and transcript">inspect</button>
      </div>
      {failed && run.error && <div className="error-box">{run.error}</div>}
      {old?.length > 0 && <div className="muted small">earlier attempts: {old.map(o => `#${o.attempt} ${o.status}${o.error ? ` (${o.error.slice(0, 80)})` : ''}`).join('; ')}</div>}
      {out && out.status === 'unconfirmed' && roundStatus !== 'completed' &&
        <div className="muted small">Not yet shared - becomes visible to other agents when the round completes.</div>}
      {run.status === 'running' && !out && <div className="muted">working...</div>}
      {run.status === 'pending' && <div className="muted">waiting for a concurrency slot...</div>}
      <FileChips runId={run.id} files={run.files} max={compact ? 3 : 8} />
      {out && !collapsed && <div className={compact ? 'cell-scroll' : ''}><Markdown text={splitAssessment(out.content)[0]} /></div>}
      {out && <AssessmentLine a={splitAssessment(out.content)[1]} />}
    </div>
  )
}

const CONV_RE = /```convergence\s*\n([\s\S]*?)```/g

function splitAssessment(text) {
  const blocks = [...(text || '').matchAll(CONV_RE)]
  if (!blocks.length) return [text, null]
  let a = null
  try { a = JSON.parse(blocks.at(-1)[1]) } catch { a = { unparsable: true } }
  return [text.replace(CONV_RE, '').trim(), a]
}

function AssessmentLine({ a }) {
  if (!a) return null
  if (a.unparsable) return <div className="assess-line"><span className="status-tag failed">assessment not parsable</span></div>
  const n = (k) => (Array.isArray(a[k]) ? a[k].length : 0)
  return (
    <div className="assess-line" title={a.rationale || ''}>
      <span className={'status-tag ' + (a.support_termination ? 'processed' : 'pending')}>
        {a.support_termination ? 'supports stopping' : 'wants to continue'}</span>
      <span className="muted small">{n('open_objections')} objections - {n('unresolved_steps')} unresolved steps - {n('unsupported_claims')} unsupported claims</span>
    </div>
  )
}

function Grid({ state, outByRun, idx, names, onInspect }) {
  const live = state.rounds.filter(r => r.status !== 'discarded')
  const ids = []
  for (const a of state.agents) ids.push(a.id)
  for (const r of live) for (const m of r.members) if (!ids.includes(m)) ids.push(m)
  const cols = ids.filter(id => live.some(r => r.members.includes(id)))
  return (
    <div className="grid-wrap">
      <table className="grid">
        <thead>
          <tr><th className="rcol">Round</th>{cols.map(id => <th key={id} style={agentColor(idx[id] ?? 0)}><span className="agent-chip">{names[id]}</span></th>)}</tr>
        </thead>
        <tbody>
          {live.map(r => (
            <tr key={r.id}>
              <td className="rcol"><b>{r.number}</b><br /><span className={'state-pill ' + r.status}>{r.status}</span></td>
              {cols.map(id => {
                const run = r.runs.filter(x => x.agent_id === id && !x.superseded).slice(-1)[0]
                return <td key={id}>{run ? <OutputCard run={run} out={outByRun[run.id]} idx={idx} names={names}
                  onInspect={onInspect} roundStatus={r.status} compact /> : <span className="muted">not a member</span>}</td>
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function TaskEditor({ task, pid, act }) {
  const [text, setText] = useState(task?.content || '')
  const [preview, setPreview] = useState(false)
  return (
    <div className="task-editor">
      <h4>Initial task (round 0)</h4>
      <p className="muted">Every agent receives this task in round 1. It becomes fixed once round 1 starts; afterwards, intervene through the channels.</p>
      <div className="seg"><button className={!preview ? 'on' : ''} onClick={() => setPreview(false)}>Edit</button>
        <button className={preview ? 'on' : ''} onClick={() => setPreview(true)}>Preview</button></div>
      {preview ? <div className="preview-box"><Markdown text={text} /></div>
        : <textarea className="mono big" rows={14} value={text} onChange={e => setText(e.target.value)}
          placeholder="Describe the research task. Markdown and LaTeX ($...$, $$...$$) are supported." />}
      <div className="row">
        <button className="btn primary" disabled={!text.trim() || text === task?.content}
          onClick={() => act(() => api.put(`/api/projects/${pid}/task`, { content: text }), 'Task saved')}>Save task</button>
        {task && <span className="muted">saved</span>}
      </div>
    </div>
  )
}

function Composer({ channel, pid, act, state }) {
  const [text, setText] = useState('')
  const [files, setFiles] = useState([])
  const fileRef = useRef(null)
  const send = async () => {
    if (!text.trim() && !files.length) return
    const fd = new FormData()
    fd.append('channel', channel)
    fd.append('content', text)
    for (const f of files) fd.append('files', f)
    const r = await act(() => api.post(`/api/projects/${pid}/messages`, fd))
    if (r) { setText(''); setFiles([]); if (fileRef.current) fileRef.current.value = '' }
  }
  const target = channel === 'common' ? 'all agents' : state.agents.find(a => `agent:${a.id}` === channel)?.name
  return (
    <div className="composer">
      <textarea value={text} onChange={e => setText(e.target.value)} rows={3}
        placeholder={`Message ${target} - delivered at the next round boundary (${nextDelivery(state)}). Ctrl+Enter to send.`}
        onKeyDown={e => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) send() }} />
      <div className="composer-actions">
        <input ref={fileRef} type="file" multiple onChange={e => setFiles([...e.target.files])} />
        <button className="btn primary" onClick={send} disabled={!text.trim() && !files.length}>Queue message</button>
      </div>
    </div>
  )
}
