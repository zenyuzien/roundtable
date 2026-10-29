import React, { useState } from 'react'
import { api, agentColor } from '../api.js'

const DEFAULT_TOOLS = ['workspace', 'files', 'transcript', 'web_search', 'fetch_url', 'arxiv', 'python']
const LABEL = { google: 'Google AI Studio (Gemini)', anthropic: 'Anthropic (Claude)', openai: 'OpenAI' }

// Optional preset, fully editable: a neutral judge that never argues, tracks cruxes and concessions,
// decides when further rounds stop adding anything, and writes the final synthesis.
const JUDGE = `You are a neutral judge. You review each round after the other agents have responded. Do not argue for any side and do not declare a winner during the discussion. Refer only to what the agents actually wrote this round.
Each round, in under 300 words:
1. State the single most important point of disagreement (the crux).
2. List what each agent has conceded so far, and what is still unresolved.
3. Name arguments that were ignored or dodged, and claims presented as established without a source or valid argument. Check every reported number against the recorded tool output shown with each contribution: flag numbers that do not appear in any successful run, runs that failed, and claims that contradict an agent's own derivation or data.
4. Ask each agent, by name, one question that would make the most progress.
Do not favour longer answers. Support stopping only when further rounds would add nothing new.
In the final synthesis: the positions in their strongest form, what was agreed, the unresolved cruxes, and what would resolve them. If the goal asks the user to decide something, end with concrete decision guidance: which option fits which priorities or circumstances, and the few questions the user should answer to choose.`

export default function Setup({ pid, meta, reloadMeta, onDone, notify }) {
  const configured = Object.entries(meta?.secrets || {}).filter(([, v]) => v.configured).map(([k]) => k)
  const [provider, setProvider] = useState(configured.includes('google') ? 'google' : configured[0] || '')
  const [goal, setGoal] = useState('')
  const [agents, setAgents] = useState([{ name: 'Agent 1', prompt: '' }, { name: 'Agent 2', prompt: '' }])
  const [maxRounds, setMaxRounds] = useState(3)
  const [busy, setBusy] = useState(false)
  const [newKey, setNewKey] = useState({ provider: 'google', key: '' })

  const setCount = (n) => {
    n = Math.max(1, Math.min(50, Number(n) || 1))
    const debaters = agents.filter(a => !a.judge)
    const judge = agents.filter(a => a.judge)
    const out = debaters.slice(0, n)
    while (out.length < n) out.push({ name: `Agent ${out.length + 1}`, prompt: '' })
    setAgents([...out, ...judge])
  }
  const upd = (i, k, v) => setAgents(agents.map((a, j) => (j === i ? { ...a, [k]: v } : a)))
  const hasJudge = agents.some(a => a.judge)
  const count = agents.filter(a => !a.judge).length

  const saveKey = async () => {
    try {
      await api.put('/api/secrets', newKey)
      await reloadMeta()
      setProvider(newKey.provider)
      setNewKey({ ...newKey, key: '' })
    } catch (e) { notify(e) }
  }

  const start = async () => {
    setBusy(true)
    try {
      await api.post(`/api/projects/${pid}/setup`, {
        task: goal,
        agents: agents.map(a => a.judge
          ? { name: a.name, instructions: a.prompt, provider, tools: [], max_tokens: 3000, decider: true, synthesis: true, review: true }
          : { name: a.name, instructions: a.prompt, provider, tools: DEFAULT_TOOLS, max_tokens: 16384 }),
        settings: {
          max_rounds: Number(maxRounds) || 1,
          convergence: hasJudge ? { enabled: true, min_rounds: Math.min(2, Number(maxRounds) || 1) } : { enabled: false },
        },
      })
      await api.post(`/api/projects/${pid}/control/start`)
      onDone()
    } catch (e) { notify(e) } finally { setBusy(false) }
  }

  const ready = goal.trim() && provider && count >= 1
  return (
    <div className="setup">
      <h2>New run</h2>

      <section className="card">
        <h3>1. Goal</h3>
        <textarea className="big" autoFocus rows={6} value={goal} onChange={e => setGoal(e.target.value)}
          placeholder="What should the agents work on? (Markdown and LaTeX supported)" />
      </section>

      <section className="card">
        <h3>2. API key</h3>
        {configured.length === 0 && (
          <>
            <div className="warn-box">No API key found. Add one to continue (stored locally, never shown again).</div>
            <div className="form-row">
              <label>Provider<select value={newKey.provider} onChange={e => setNewKey({ ...newKey, provider: e.target.value })}>
                {Object.entries(LABEL).map(([k, l]) => <option key={k} value={k}>{l}</option>)}</select></label>
              <label className="grow">Key<input type="password" autoComplete="off" value={newKey.key}
                onChange={e => setNewKey({ ...newKey, key: e.target.value })} /></label>
              <button className="btn primary" disabled={!newKey.key} onClick={saveKey}>Save key</button>
            </div>
          </>
        )}
        {configured.length === 1 && <div className="ok-box">Using your {LABEL[provider]} key.</div>}
        {configured.length > 1 && (
          <div className="choice-row">
            <span>Several keys found - which one?</span>
            {configured.map(p => (
              <label key={p} className="chk"><input type="radio" name="prov" checked={provider === p} onChange={() => setProvider(p)} /> {LABEL[p]}</label>
            ))}
          </div>
        )}
        {provider && <div className="muted small">Model: {meta?.default_models?.[provider]} (change per agent in Options)</div>}
      </section>

      <section className="card">
        <h3>3. Agents</h3>
        <div className="form-row">
          <label>How many agents<input type="number" min="1" value={count} onChange={e => setCount(e.target.value)} /></label>
          {!hasJudge && <button className="btn" onClick={() => setAgents([...agents, { name: 'Judge', prompt: JUDGE, judge: true }])}
            title="An extra neutral agent that moderates, decides when to stop and writes a final synthesis">+ Add a neutral judge</button>}
        </div>
        {agents.map((a, i) => (
          <div key={i} className="agent-setup" style={agentColor(i)}>
            <div className="agent-setup-head">
              <input className="name-input" value={a.name} onChange={e => upd(i, 'name', e.target.value)} />
              {a.judge && <span className="muted small">reviews each round after the others, decides when to stop, writes the final synthesis</span>}
              {a.judge && <button className="link" onClick={() => setAgents(agents.filter((_, j) => j !== i))}>remove judge</button>}
            </div>
            <textarea rows={a.judge ? 8 : 4} value={a.prompt} onChange={e => upd(i, 'prompt', e.target.value)}
              placeholder={`Role for ${a.name}: what it should do, believe, or focus on.`} />
          </div>
        ))}
      </section>

      <section className="card">
        <h3>4. Maximum rounds</h3>
        <input type="number" min="1" value={maxRounds} onChange={e => setMaxRounds(e.target.value)} />
      </section>

      <div className="setup-go">
        <button className="btn primary big-btn" disabled={!ready || busy} onClick={start}>▶ Start</button>
        {!ready && <span className="muted small">{!goal.trim() ? 'Write a goal. ' : ''}{!provider ? 'Add an API key.' : ''}</span>}
      </div>
    </div>
  )
}
