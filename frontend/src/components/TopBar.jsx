import React, { useEffect, useState } from 'react'
import { api, fmtCost, fmtTokens } from '../api.js'

export default function TopBar({ state, act, pid }) {
  const p = state.project
  const st = p.settings
  const ctl = (a, msg) => act(() => api.post(`/api/projects/${pid}/control/${a}`), msg)
  const live = state.rounds.filter(r => r.status !== 'discarded')
  const cur = live.slice(-1)[0]
  const runs = cur ? cur.runs.filter(r => !r.superseded) : []
  const count = (s) => runs.filter(r => r.status === s).length
  const inc = state.incomplete_round
  const busy = state.busy
  const u = state.usage

  const [maxR, setMaxR] = useState(st.max_rounds)
  const [bps, setBps] = useState(st.breakpoints.join(', '))
  useEffect(() => { setMaxR(st.max_rounds); setBps(st.breakpoints.join(', ')) }, [st.max_rounds, st.breakpoints.join(',')])

  const saveQuick = () => act(() => api.patch(`/api/projects/${pid}`, {
    settings: {
      max_rounds: Number(maxR) || 1,
      breakpoints: bps.split(/[\s,]+/).filter(Boolean).map(Number).filter(n => n > 0),
    }
  }), 'Execution settings saved')
  const dirty = Number(maxR) !== st.max_rounds || bps.split(/[\s,]+/).filter(Boolean).join(',') !== st.breakpoints.join(',')

  const roundLabel = state.running_round
    ? `Round ${state.running_round} running`
    : inc ? `Round ${inc.number} incomplete (${inc.status})`
      : `Completed ${state.last_completed} / ${st.max_rounds}`

  const budgetTok = st.budget.max_total_tokens
  const totalTok = u.tokens_in + u.tokens_out

  return (
    <div className="topbar">
      <div className="topbar-row">
        <div className="title-block">
          <ProjectName p={p} pid={pid} act={act} />
          <span className={'state-pill ' + p.state}>{p.state.replace('_', ' ')}</span>
          {p.state_reason && <span className="state-reason">{p.state_reason}</span>}
        </div>
        <div className="controls">
          {!busy && !inc && (
            <>
              <button className="btn primary" onClick={() => ctl(state.last_completed ? 'resume' : 'start')}
                disabled={!state.has_task} title={state.has_task ? '' : 'Set the initial task first'}>
                {state.last_completed ? '▶ Resume' : '▶ Start'}
              </button>
              <button className="btn" onClick={() => ctl('step')} disabled={!state.has_task}
                title="Run exactly one round, then pause">Step 1 round</button>
            </>
          )}
          {busy && p.state === 'running' && (
            <button className="btn" onClick={() => ctl('pause', 'Will pause after the current round')}>⏸ Pause after round</button>
          )}
          {busy && <button className="btn danger" onClick={() => ctl('stop')}
            title="Cancel in-flight calls now. Completed rounds are kept; the partial round can be retried or discarded.">■ Stop</button>}
          {!busy && inc && (
            <>
              <button className="btn primary" onClick={() => ctl('retry_failed')}
                title="Re-run only the agents that did not succeed, with the same frozen snapshot">↻ Retry failed agents</button>
              <button className="btn" onClick={() => ctl('skip_failed')}
                title="Complete the round without the failed agents (their failure stays recorded)">Continue without them</button>
              <button className="btn danger" onClick={() => ctl('discard_round')}
                title="Mark the round discarded (kept in history) and re-run it later from a fresh snapshot">Discard round</button>
            </>
          )}
        </div>
      </div>
      <div className="topbar-row stats">
        <span className="stat"><b>{roundLabel}</b></span>
        {cur && (
          <span className="stat run-counts">
            {count('running') > 0 && <span className="rc running">{count('running')} running</span>}
            {count('pending') > 0 && <span className="rc pending">{count('pending')} queued</span>}
            <span className="rc succeeded">{count('succeeded')} done</span>
            {count('failed') + count('interrupted') + count('aborted') > 0 &&
              <span className="rc failed">{count('failed') + count('interrupted') + count('aborted')} failed</span>}
            {count('skipped') > 0 && <span className="rc skipped">{count('skipped')} skipped</span>}
            <span className="muted">in round {cur.number}</span>
          </span>
        )}
        <span className="stat" title="User messages / files / workspace edits waiting for the next round boundary">
          Pending: {state.pending_common + state.agents.reduce((s, a) => s + a.pending_messages, 0)} msg
          {state.pending_files ? `, ${state.pending_files} file` : ''}{state.pending_workspace ? `, ${state.pending_workspace} ws edit` : ''}
        </span>
        <span className="stat" title={`in ${fmtTokens(u.tokens_in)} / out ${fmtTokens(u.tokens_out)}`}>
          Tokens {fmtTokens(totalTok)}{budgetTok ? ` / ${fmtTokens(budgetTok)}` : ''}
        </span>
        <span className="stat" title={u.unpriced_runs ? `${u.unpriced_runs} run(s) used models without pricing data` : ''}>
          Cost {fmtCost(u.cost_usd)}{st.budget.max_cost_usd ? ` / ${fmtCost(st.budget.max_cost_usd)}` : ''}{u.unpriced_runs ? ' *' : ''}
        </span>
        <span className="quick">
          <label>max rounds <input type="number" min="1" value={maxR} onChange={e => setMaxR(e.target.value)} /></label>
          <label>break after <input className="bp" placeholder="e.g. 2, 5" value={bps} onChange={e => setBps(e.target.value)} /></label>
          <label className="chk" title="Stop automatically when the convergence policy is satisfied">
            <input type="checkbox" checked={st.convergence.enabled}
              onChange={e => act(() => api.patch(`/api/projects/${pid}`, { settings: { convergence: { enabled: e.target.checked } } }))} />
            convergence
          </label>
          {dirty && <button className="btn small primary" onClick={saveQuick}>Apply</button>}
        </span>
      </div>
    </div>
  )
}

function ProjectName({ p, pid, act }) {
  const [edit, setEdit] = useState(false)
  const [name, setName] = useState(p.name)
  if (!edit) return <h2 className="proj-name" onDoubleClick={() => { setName(p.name); setEdit(true) }} title="Double-click to rename">{p.name}</h2>
  return (
    <input className="proj-name-input" autoFocus value={name} onChange={e => setName(e.target.value)}
      onBlur={() => { setEdit(false); if (name !== p.name) act(() => api.patch(`/api/projects/${pid}`, { name })) }}
      onKeyDown={e => { if (e.key === 'Enter') e.target.blur(); if (e.key === 'Escape') setEdit(false) }} />
  )
}
