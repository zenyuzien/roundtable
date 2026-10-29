import React, { useEffect, useState } from 'react'
import { api } from '../api.js'

const get = (o, path) => path.split('.').reduce((x, k) => x?.[k], o)
const set = (o, path, v) => {
  const c = structuredClone(o)
  const ks = path.split('.')
  let x = c
  for (const k of ks.slice(0, -1)) x = x[k]
  x[ks.at(-1)] = v
  return c
}

export default function Settings({ state, meta, pid, act, notify, onDeleted, reloadMeta }) {
  const [s, setS] = useState(state.project.settings)
  useEffect(() => { setS(state.project.settings) }, [JSON.stringify(state.project.settings)])
  const dirty = JSON.stringify(s) !== JSON.stringify(state.project.settings)
  const F = ({ path, label, type = 'number', step, hint, options }) => {
    const v = get(s, path)
    if (type === 'bool') return (
      <label className="chk" title={hint}><input type="checkbox" checked={!!v} onChange={e => setS(set(s, path, e.target.checked))} /> {label}</label>)
    if (options) return (
      <label title={hint}>{label}<select value={v} onChange={e => setS(set(s, path, e.target.value))}>
        {options.map(([k, l]) => <option key={k} value={k}>{l}</option>)}</select></label>)
    return (
      <label title={hint}>{label}
        <input type={type} step={step} value={v ?? ''} onChange={e => setS(set(s, path, type === 'number'
          ? (e.target.value === '' ? null : Number(e.target.value)) : e.target.value))} /></label>)
  }
  const save = () => act(() => api.patch(`/api/projects/${pid}`, { settings: s }), state.busy
    ? 'Saved - settings that affect agents apply from the next round' : 'Settings saved')

  return (
    <div className="panel settings">
      <div className="panel-head">
        <h3>Settings</h3>
        <button className="btn primary" disabled={!dirty} onClick={save}>Save settings</button>
        {dirty && <button className="btn" onClick={() => setS(state.project.settings)}>Revert</button>}
      </div>

      <section className="card">
        <h4>Execution</h4>
        <div className="form-row">
          {F({ path: 'max_rounds', label: 'Maximum rounds' })}
          <label>Breakpoints (pause after rounds)
            <input value={s.breakpoints.join(', ')} onChange={e => setS({ ...s, breakpoints: e.target.value.split(/[\s,]+/).filter(Boolean).map(Number).filter(n => n > 0) })} /></label>
          {F({ path: 'failure_policy', label: 'If an agent fails', options: [
            ['retry_then_halt', 'auto-retry, then halt for me'], ['halt', 'halt for me immediately'],
            ['skip_failed', 'complete round without it (recorded)']] })}
          {F({ path: 'auto_retries', label: 'Auto retries', hint: 'for retry_then_halt' })}
          {F({ path: 'max_concurrency', label: 'Max concurrent agent calls' })}
          {F({ path: 'max_tool_iterations', label: 'Max tool iterations / agent / round' })}
        </div>
      </section>

      <section className="card">
        <h4>Budget</h4>
        <p className="muted small">Checked before every model call. New rounds do not start once the budget is exhausted; a call that would exceed it mid-round fails visibly instead of spending more.</p>
        <div className="form-row">
          {F({ path: 'budget.max_total_tokens', label: 'Max total tokens (0 = unlimited)' })}
          {F({ path: 'budget.max_cost_usd', label: 'Max cost USD (0 = unlimited)', step: '0.5' })}
        </div>
      </section>

      <section className="card">
        <h4>Context</h4>
        <p className="muted small">Recent rounds are included verbatim; older rounds as summaries (if enabled) or pointers. Agents can always
          read exact earlier text with the transcript tools. The stored transcript is never modified. Inspect any run to see exactly what an agent received.</p>
        <div className="form-row">
          {F({ path: 'context.recent_rounds_full', label: 'Recent rounds included verbatim' })}
          {F({ path: 'context.max_context_tokens', label: 'Max context tokens (estimate)' })}
          {F({ path: 'context.max_chars_per_output', label: 'Max chars per agent output' })}
          {F({ path: 'context.private_history_messages', label: 'Private messages kept' })}
          {F({ path: 'context.summarize_older', label: 'Summarize older rounds', type: 'bool' })}
          {F({ path: 'context.summarizer_provider', label: 'Summarizer provider', options: [['', '(same as first agent)'],
            ...Object.keys(meta?.providers || {}).filter(p => !meta.providers[p].hidden).map(p => [p, p])] })}
          {F({ path: 'context.summarizer_model', label: 'Summarizer model', type: 'text' })}
        </div>
      </section>

      <section className="card">
        <h4>Convergence</h4>
        <div className="form-row">
          {F({ path: 'convergence.enabled', label: 'Enable automatic convergence detection', type: 'bool' })}
          {F({ path: 'convergence.min_rounds', label: 'Minimum rounds' })}
          {F({ path: 'convergence.support_fraction', label: 'Fraction supporting termination', step: '0.05' })}
          {F({ path: 'convergence.max_open_objections', label: 'Max open objections (total)' })}
          {F({ path: 'convergence.max_unresolved_steps', label: 'Max unresolved derivation steps' })}
          {F({ path: 'convergence.max_unsupported_claims', label: 'Max unsupported claims' })}
          {F({ path: 'convergence.min_similarity', label: 'Min output stability (0-1, empty = off)', step: '0.05',
            hint: 'word-trigram overlap between consecutive rounds; high means little is changing' })}
          {F({ path: 'convergence.consecutive_rounds', label: 'Consecutive rounds required' })}
          {F({ path: 'convergence.require_no_workspace_conflicts', label: 'Require no workspace conflicts', type: 'bool' })}
          <label title="If set, only this agent's assessment decides termination (e.g. a neutral judge); other criteria above are then ignored">Decided by
            <select value={s.convergence.decider_agent ?? ''} onChange={e => setS(set(s, 'convergence.decider_agent', e.target.value ? Number(e.target.value) : null))}>
              <option value="">all agents (policy above)</option>
              {state.agents.map(a => <option key={a.id} value={a.id}>{a.name} only</option>)}
            </select></label>
          <label title="These agents run in a second phase of every round, after the others, and review that round's contributions">Review phase (after the others)
            <select multiple value={(s.review_agents || []).map(String)} onChange={e => setS({ ...s, review_agents: [...e.target.selectedOptions].map(o => Number(o.value)) })}>
              {state.agents.map(a => <option key={a.id} value={a.id}>{a.name}</option>)}
            </select></label>
          <label title="When the run ends (converged or max rounds), this agent writes one final synthesis turn that sees every round">Final synthesis by
            <select value={s.synthesis_agent ?? ''} onChange={e => setS({ ...s, synthesis_agent: e.target.value ? Number(e.target.value) : null })}>
              <option value="">nobody (off)</option>
              {state.agents.map(a => <option key={a.id} value={a.id}>{a.name}</option>)}
            </select></label>
        </div>
      </section>

      <section className="card">
        <h4>Tools &amp; sandbox</h4>
        <div className={meta?.sandbox?.isolated ? 'ok-box' : 'warn-box'}>Execution sandbox: <b>{meta?.sandbox?.mode}</b> - {meta?.sandbox?.note}</div>
        <div className="form-row">
          {F({ path: 'tools.allow_network', label: 'Allow network tools (web, arXiv)', type: 'bool' })}
          {F({ path: 'tools.allow_python', label: 'Allow Python execution', type: 'bool' })}
          {F({ path: 'tools.allow_bash', label: 'Allow Bash execution', type: 'bool' })}
          {F({ path: 'tools.exec_timeout_s', label: 'Execution timeout (s)' })}
          {F({ path: 'tools.exec_memory_mb', label: 'Execution memory limit (MB, 0 = none)' })}
          {F({ path: 'tools.python_executable', label: 'Python interpreter for agents', type: 'text', hint: 'empty = the app interpreter' })}
        </div>
      </section>

      <Keys meta={meta} notify={notify} reloadMeta={reloadMeta} />
      <Pricing notify={notify} />

      <section className="card">
        <h4>Project data</h4>
        <div className="row">
          <a className="btn" href={`/api/projects/${pid}/export`}>Export full project record (JSON)</a>
          <span className="spacer" />
          <button className="btn danger" disabled={state.busy} onClick={async () => {
            if (!window.confirm('Start over? This permanently deletes the task, agents, rounds, workspace and files.')) return
            const r = await act(() => api.del(`/api/projects/${pid}`))
            if (r) onDeleted()
          }}>Start over (new task)</button>
        </div>
      </section>
    </div>
  )
}

const KEY_LABELS = { anthropic: 'Anthropic', openai: 'OpenAI', google: 'Google AI Studio (Gemini)' }

function Keys({ meta, notify, reloadMeta }) {
  const [vals, setVals] = useState({ openai: '', anthropic: '', google: '' })
  const st = meta?.secrets || {}
  const save = async (p, key) => {
    try { await api.put('/api/secrets', { provider: p, key }); setVals({ ...vals, [p]: '' }); await reloadMeta(); notify({ message: `${p} key ${key ? 'saved' : 'removed'}` }, 'ok') }
    catch (e) { notify(e) }
  }
  return (
    <section className="card">
      <h4>API keys</h4>
      <p className="muted small">One key per provider serves all agents. Stored locally in <code>data/secrets.json</code> (mode 600) or read from
        <code> OPENAI_API_KEY</code> / <code>ANTHROPIC_API_KEY</code> / <code>GEMINI_API_KEY</code> (or <code>GOOGLE_API_KEY</code>). Keys are never sent back to the browser, logged, or exposed to agent tools.</p>
      {Object.entries(KEY_LABELS).map(([p, label]) => (
        <div key={p} className="form-row key-row">
          <label>{label} key
            <input type="password" autoComplete="off" value={vals[p]} placeholder={st[p]?.configured ? `configured (${st[p].source})` : 'not configured'}
              onChange={e => setVals({ ...vals, [p]: e.target.value })} /></label>
          <button className="btn primary" disabled={!vals[p]} onClick={() => save(p, vals[p])}>Save</button>
          {st[p]?.source === 'file' && <button className="btn" onClick={() => save(p, null)}>Remove</button>}
        </div>
      ))}
    </section>
  )
}

function Pricing({ notify }) {
  const [text, setText] = useState('')
  useEffect(() => { api.get('/api/pricing').then(t => setText(JSON.stringify(t, null, 2))).catch(notify) }, [notify])
  return (
    <section className="card">
      <h4>Model pricing (USD per 1M tokens)</h4>
      <p className="muted small">Defaults may be outdated - check your provider's pricing page. Models without an entry show cost as n/a (tokens are still counted and budgeted).
        Dated model ids match their prefix (e.g. <code>gpt-4o-2024-08-06</code> uses <code>gpt-4o</code>).</p>
      <textarea className="mono" rows={10} value={text} onChange={e => setText(e.target.value)} />
      <button className="btn" onClick={async () => {
        try { const t = await api.put('/api/pricing', JSON.parse(text)); setText(JSON.stringify(t, null, 2)); notify({ message: 'Pricing saved' }, 'ok') }
        catch (e) { notify(e) }
      }}>Save pricing</button>
    </section>
  )
}
