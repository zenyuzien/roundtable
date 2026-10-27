import React, { useEffect, useState } from 'react'
import { api, fmtTime } from '../api.js'

export default function Activity({ pid, seq, state, notify }) {
  const [events, setEvents] = useState([])
  useEffect(() => { api.get(`/api/projects/${pid}/events?limit=300`).then(setEvents).catch(notify) }, [pid, seq, notify])
  const names = state.agent_names
  return (
    <div className="panel">
      <div className="panel-head"><h3>Activity log</h3><span className="muted">Engine events, newest first.</span></div>
      <table className="tbl small events">
        <tbody>{events.map(e => (
          <tr key={e.id} className={'ev-' + e.type}>
            <td className="muted">{fmtTime(e.created_at)}</td>
            <td><code>{e.type}</code></td>
            <td>{describe(e, names)}</td>
          </tr>
        ))}</tbody>
      </table>
    </div>
  )
}

function describe(e, names) {
  const d = e.data
  switch (e.type) {
    case 'state': return `${d.state}${d.reason ? ' - ' + d.reason : ''}`
    case 'round_started': return `round ${d.round} started with ${d.members.map(m => names[m]).join(', ')}`
    case 'round_completed': return `round ${d.round} completed${d.skipped?.length ? ` (skipped: ${d.skipped.map(m => names[m]).join(', ')})` : ''}`
    case 'provider_retry': return `run ${d.run_id}: retry ${d.attempt} in ${d.delay}s - ${d.error}`
    case 'agent_run': return `run ${d.run_id} ${d.status}`
    case 'convergence': return `round ${d.round}: ${d.converged ? 'converged' : 'not converged'}`
    default: return Object.keys(d).length ? JSON.stringify(d).slice(0, 300) : ''
  }
}
