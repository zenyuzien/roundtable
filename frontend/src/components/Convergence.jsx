import React, { useEffect, useState } from 'react'
import { api } from '../api.js'

export default function Convergence({ pid, seq, state, notify }) {
  const [data, setData] = useState(null)
  useEffect(() => { api.get(`/api/projects/${pid}/convergence`).then(setData).catch(notify) }, [pid, seq, notify])
  const cfg = state.project.settings.convergence
  const names = state.agent_names
  if (!data) return <div className="empty">Loading...</div>
  const rounds = [...new Set([...data.decisions.map(d => d.round_number), ...data.assessments.map(a => a.round_number)])].sort((a, b) => b - a)
  return (
    <div className="panel">
      <div className="panel-head">
        <h3>Convergence</h3>
        <span className="muted">{cfg.enabled ? 'Enabled' : 'Disabled (toggle in the top bar or Settings)'}. Agreement alone never ends a run:
          every criterion below must hold{cfg.consecutive_rounds > 1 ? ` for ${cfg.consecutive_rounds} consecutive rounds` : ''}.
          Agents report structured assessments, kept separate from their outputs.</span>
      </div>
      <div className="card small">
        <b>Policy:</b> min rounds {cfg.min_rounds}; support fraction ≥ {cfg.support_fraction}; open objections ≤ {cfg.max_open_objections};
        unresolved steps ≤ {cfg.max_unresolved_steps}; unsupported claims ≤ {cfg.max_unsupported_claims};
        {cfg.min_similarity ? ` output stability ≥ ${cfg.min_similarity};` : ' stability not required;'}
        {cfg.require_no_workspace_conflicts ? ' no unresolved workspace conflicts.' : ''}
      </div>
      {rounds.length === 0 && <div className="empty">No assessments yet.</div>}
      {rounds.map(r => {
        const d = data.decisions.find(x => x.round_number === r)?.decision
        const as = data.assessments.filter(a => a.round_number === r)
        return (
          <div key={r} className="card">
            <h4>Round {r} {d && <span className={'state-pill ' + (d.converged ? 'completed' : 'paused')}>{d.converged ? 'converged' : 'not converged'}</span>}</h4>
            {d && (
              <table className="tbl small">
                <tbody>{d.criteria.map(c => (
                  <tr key={c.name}><td>{c.passed ? '✅' : '❌'}</td><td><b>{c.name}</b></td><td>{c.detail}</td></tr>
                ))}
                  <tr><td /><td><b>consecutive</b></td><td>{d.consecutive} / {d.required_consecutive}</td></tr>
                </tbody>
              </table>
            )}
            <div className="assess-grid">
              {as.map(a => (
                <div key={a.id} className="assess">
                  <b>{names[a.agent_id]}</b>{' '}
                  {!a.parsed_ok ? <span className="status-tag failed">no parsable assessment</span> :
                    <span className={'status-tag ' + (a.assessment.support_termination ? 'processed' : 'pending')}>
                      {a.assessment.support_termination ? 'supports stopping' : 'wants to continue'}</span>}
                  {a.assessment && (
                    <>
                      <List title="Open objections" items={a.assessment.open_objections} />
                      <List title="Unresolved steps" items={a.assessment.unresolved_steps} />
                      <List title="Unsupported claims" items={a.assessment.unsupported_claims} />
                      {a.assessment.rationale && <div className="muted small">{a.assessment.rationale}</div>}
                    </>
                  )}
                </div>
              ))}
            </div>
          </div>
        )
      })}
    </div>
  )
}

function List({ title, items }) {
  if (!items?.length) return null
  return <div className="small"><i>{title}:</i><ul>{items.map((x, i) => <li key={i}>{x}</li>)}</ul></div>
}
