import React, { useEffect, useState } from 'react'

let liveGoogle = null
function useGoogleModels() {
  const [m, setM] = useState(liveGoogle || [])
  useEffect(() => { if (!liveGoogle) api.get('/api/models/google').then(x => { liveGoogle = x.map(y => y.id); setM(liveGoogle) }).catch(() => {}) }, [])
  return m
}
import { api, agentColor, fmtTime } from '../api.js'

const TOOL_LABELS = {
  workspace: 'Workspace (read / propose)',
  files: 'Files (read / staged write)',
  transcript: 'Transcript & tool records',
  memory: 'Private memory',
  web_search: 'Web search',
  fetch_url: 'Fetch URL',
  arxiv: 'arXiv papers',
  python: 'Python execution',
  bash: 'Bash (restricted)',
}

export default function Agents({ state, meta, pid, act }) {
  const [adding, setAdding] = useState(false)
  return (
    <div className="panel">
      <div className="panel-head">
        <h3>Agents</h3>
        <span className="muted">All agents are peers. Roles, authority and methods come only from each agent's instructions.
          Edits made while a round is running are versioned and take effect at the next round boundary.</span>
        <button className="btn primary" onClick={() => setAdding(true)}>+ Add agent</button>
      </div>
      {adding && <NewAgent meta={meta} pid={pid} act={act} onDone={() => setAdding(false)} />}
      {state.agents.length === 0 && <div className="empty">No agents yet. Add at least one.</div>}
      {state.agents.map((a, i) => (
        <AgentEditor key={a.id} a={a} i={i} meta={meta} act={act} state={state}
          neighbors={[state.agents[i - 1], state.agents[i + 1]]} />
      ))}
    </div>
  )
}

function NewAgent({ meta, pid, act, onDone }) {
  const provs = Object.keys(meta?.providers || {}).filter(p => !meta.providers[p].hidden)
  const [d, setD] = useState({ name: '', provider: provs.includes('google') ? 'google' : provs[0], model: '' })
  const models = meta?.providers?.[d.provider]?.suggested_models || []
  return (
    <div className="card new-agent">
      <div className="form-row">
        <label>Name<input value={d.name} onChange={e => setD({ ...d, name: e.target.value })} placeholder="e.g. Agent 4" /></label>
        <label>Provider<select value={d.provider} onChange={e => setD({ ...d, provider: e.target.value, model: '' })}>
          {provs.map(p => <option key={p} value={p}>{meta.providers[p].label}</option>)}</select></label>
        <label>Model<input list="new-models" value={d.model} placeholder={models[0]} onChange={e => setD({ ...d, model: e.target.value })} />
          <datalist id="new-models">{models.map(m => <option key={m} value={m} />)}</datalist></label>
      </div>
      <div className="row">
        <button className="btn primary" onClick={async () => {
          const r = await act(() => api.post(`/api/projects/${pid}/agents`, { ...d, model: d.model || models[0] || 'mock' }), 'Agent added')
          if (r) onDone()
        }}>Create</button>
        <button className="btn" onClick={onDone}>Cancel</button>
      </div>
    </div>
  )
}

function AgentEditor({ a, i, meta, act, state, neighbors }) {
  const init = () => ({
    name: a.name, enabled: !!a.enabled, provider: a.provider, model: a.model,
    temperature: a.temperature ?? '', max_tokens: a.max_tokens, tools: a.tools, memory_enabled: !!a.memory_enabled,
    instructions: a.instructions,
  })
  const [d, setD] = useState(init)
  const [history, setHistory] = useState(null)
  const [open, setOpen] = useState(state.agents.length <= 3)
  useEffect(() => { setD(init()) }, [a.id, a.instruction_version, a.name, a.enabled, a.provider, a.model, a.max_tokens,
    a.temperature, a.memory_enabled, JSON.stringify(a.tools)])
  const orig = init()
  const dirty = JSON.stringify(d) !== JSON.stringify(orig)
  const gm = useGoogleModels()
  const models = d.provider === 'google' && gm.length ? gm : (meta?.providers?.[d.provider]?.suggested_models || [])
  const keyOk = d.provider === 'mock' || meta?.secrets?.[d.provider]?.configured
  const sandbox = meta?.sandbox
  const tset = new Set(d.tools)
  const toggleTool = (t) => { const s = new Set(d.tools); s.has(t) ? s.delete(t) : s.add(t); setD({ ...d, tools: [...s] }) }
  const settingsTools = state.project.settings.tools

  const save = () => act(() => api.patch(`/api/agents/${a.id}`, {
    ...d, temperature: d.temperature === '' ? null : Number(d.temperature), max_tokens: Number(d.max_tokens),
  }), state.busy ? 'Saved - applies from the next round' : 'Agent saved')

  const move = (other) => other && act(async () => {
    await api.patch(`/api/agents/${a.id}`, { position: other.position })
    await api.patch(`/api/agents/${other.id}`, { position: a.position })
  })

  return (
    <div className="card agent-card" style={agentColor(i)}>
      <div className="agent-card-head" onClick={() => setOpen(!open)}>
        <span className="agent-chip">{a.name}</span>
        <span className="muted">{a.provider} / {a.model} - instructions v{a.instruction_version}</span>
        {!a.enabled && <span className="status-tag">disabled</span>}
        {dirty && <span className="status-tag pending">unsaved</span>}
        <span className="spacer" />
        <button className="link" onClick={e => { e.stopPropagation(); move(neighbors[0]) }} disabled={!neighbors[0]}>↑</button>
        <button className="link" onClick={e => { e.stopPropagation(); move(neighbors[1]) }} disabled={!neighbors[1]}>↓</button>
        <span className="muted">{open ? '▾' : '▸'}</span>
      </div>
      {open && (
        <div className="agent-card-body">
          <div className="form-row">
            <label>Name<input value={d.name} onChange={e => setD({ ...d, name: e.target.value })} /></label>
            <label>Provider<select value={d.provider} onChange={e => setD({ ...d, provider: e.target.value })}>
              {Object.keys(meta?.providers || {}).filter(p => !meta.providers[p].hidden || p === d.provider).map(p => <option key={p} value={p}>{meta.providers[p].label}</option>)}</select></label>
            <label>Model<input list={`models-${a.id}`} value={d.model} onChange={e => setD({ ...d, model: e.target.value })} />
              <datalist id={`models-${a.id}`}>{models.map(m => <option key={m} value={m} />)}</datalist></label>
            <label title="Leave empty to use the provider default (some reasoning models reject temperature)">Temperature
              <input type="number" step="0.1" min="0" max="2" value={d.temperature} placeholder="default"
                onChange={e => setD({ ...d, temperature: e.target.value })} /></label>
            <label>Max output tokens<input type="number" min="16" value={d.max_tokens} onChange={e => setD({ ...d, max_tokens: e.target.value })} /></label>
            <label className="chk"><input type="checkbox" checked={d.enabled} onChange={e => setD({ ...d, enabled: e.target.checked })} /> enabled</label>
          </div>
          {!keyOk && <div className="warn-box">No API key configured for {d.provider}. Add one in Settings → API keys.</div>}
          <div className="tools-row">
            <span className="muted">Tools:</span>
            {meta?.tools.map(t => {
              const vetoed = (t === 'python' && !settingsTools.allow_python) || (t === 'bash' && !settingsTools.allow_bash) ||
                (['web_search', 'fetch_url', 'arxiv'].includes(t) && !settingsTools.allow_network)
              return (
                <label key={t} className={'chk tool ' + (vetoed ? 'vetoed' : '')}
                  title={vetoed ? 'Disabled project-wide in Settings → Tools' : (t === 'python' || t === 'bash') ? sandbox?.note : ''}>
                  <input type="checkbox" checked={tset.has(t)} onChange={() => toggleTool(t)} /> {TOOL_LABELS[t] || t}
                </label>
              )
            })}
            <label className="chk tool"><input type="checkbox" checked={d.memory_enabled}
              onChange={e => setD({ ...d, memory_enabled: e.target.checked })} /> persistent memory in context</label>
          </div>
          <label className="block">Instructions (system prompt - the only source of this agent's role and behaviour)
            <textarea className="mono" rows={12} value={d.instructions} onChange={e => setD({ ...d, instructions: e.target.value })}
              placeholder="Describe what this agent should focus on, how it should work, and how it should treat other agents' contributions." />
          </label>
          <div className="row">
            <button className="btn primary" disabled={!dirty} onClick={save}>Save{d.instructions !== orig.instructions ? ' (new instruction version)' : ''}</button>
            {dirty && <button className="btn" onClick={() => setD(init())}>Revert</button>}
            <button className="btn" onClick={async () => setHistory(history ? null : await api.get(`/api/agents/${a.id}/instructions`))}>
              {history ? 'Hide' : 'Instruction history'}</button>
            <span className="spacer" />
            <button className="btn danger" onClick={() => {
              if (window.confirm(`Remove ${a.name}? Its history is kept; it will not take part in future rounds.`))
                act(() => api.del(`/api/agents/${a.id}`), 'Agent removed')
            }}>Remove</button>
          </div>
          {history && (
            <div className="history">
              {history.map(v => (
                <details key={v.id}>
                  <summary>v{v.version} - {fmtTime(v.created_at)} {new Date(v.created_at).toLocaleDateString()}
                    {v.created_during_round ? ` (edited during round ${v.created_during_round}; applied from round ${v.created_during_round + 1})` : ''}</summary>
                  <pre className="mono">{v.text || '(empty)'}</pre>
                  <button className="btn small" onClick={() => setD({ ...d, instructions: v.text })}>Load into editor</button>
                </details>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
