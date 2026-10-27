import React, { useEffect, useState } from 'react'
import { api, agentColor } from '../api.js'
import Markdown from '../Markdown.jsx'

const DEFAULT_TOOLS = ['workspace', 'files', 'transcript', 'web_search', 'fetch_url', 'arxiv', 'python']

// Optional template (fully editable). Based on judge-moderated debate research: a neutral judge that
// tracks cruxes and concessions, never argues, and decides when further rounds stop adding anything.
const JUDGE_INSTRUCTIONS = `You are a neutral judge and moderator. Do not argue for any side and do not declare a winner during the discussion.
Each round, in under 300 words:
1. State the single most important point of disagreement (the crux) in one sentence.
2. List what each agent has conceded so far, and what is still unresolved.
3. Name arguments that were ignored or dodged, and any claim presented as proven or established without a retrieved source or a valid argument.
4. Ask each agent, by name, one question that would make the most progress.
Do not favour longer answers or the agent you read first.
In your convergence assessment, support stopping only when further rounds would no longer add new arguments or resolve anything.
In the final synthesis: map the positions in their strongest form, what was agreed, the unresolved cruxes, and what evidence or argument would resolve each.`

const GROUND_RULES = `

Discussion rules: answer the judge's question to you first. Concede at least one point per round where another agent is right. Engage the strongest version of the opposing argument. Keep your response under about 600 words, with the argument itself in the response.`

export default function Setup({ pid, meta, state, onDone, notify }) {
  const [live, setLive] = useState([])
  useEffect(() => { api.get('/api/models/google').then(setLive).catch(() => {}) }, [])
  const models = live.length ? live.map(m => m.id) : (meta?.providers?.google?.suggested_models || ['gemini-3.5-flash-lite'])
  const [step, setStep] = useState(1)
  const [task, setTask] = useState(state.task || '')
  const [preview, setPreview] = useState(false)
  const [count, setCount] = useState(Math.max(1, state.agents.length - (state.project.settings.synthesis_agent ? 1 : 0) || 2))
  const [model, setModel] = useState(state.agents[0]?.model || 'gemini-3.5-flash-lite')
  const [maxRounds, setMaxRounds] = useState(state.agents.length ? state.project.settings.max_rounds : 3)
  const judgeId = state.project.settings.synthesis_agent ?? state.project.settings.convergence.decider_agent
  const existingJudge = state.agents.find(a => a.id === judgeId)
  const [agents, setAgents] = useState(state.agents.filter(a => a.id !== judgeId).map(a => ({ name: a.name, instructions: a.instructions })))
  const [withJudge, setWithJudge] = useState(existingJudge ? true : state.agents.length === 0)
  const [judge, setJudge] = useState({ name: existingJudge?.name || 'Judge', instructions: existingJudge?.instructions || JUDGE_INSTRUCTIONS })
  const [rules, setRules] = useState(true)
  const [busy, setBusy] = useState(false)
  const [check, setCheck] = useState(null)
  const testModel = async () => {
    setCheck({ pending: true })
    try { setCheck(await api.post('/api/models/check', { provider: 'google', model })) } catch (e) { setCheck({ ok: false, error: e.message }) }
  }

  const ensure = (n) => {
    const out = agents.slice(0, n)
    while (out.length < n) out.push({ name: `Agent ${out.length + 1}`, instructions: '' })
    setAgents(out.map(a => {
      const has = a.instructions.includes('Discussion rules:')
      if (withJudge && rules && !has) return { ...a, instructions: a.instructions.trimEnd() + GROUND_RULES }
      if ((!withJudge || !rules) && has) return { ...a, instructions: a.instructions.replace(GROUND_RULES, '').replace(/\n*Discussion rules:[\s\S]*$/, '') }
      return a
    }))
  }
  const upd = (i, k, v) => setAgents(agents.map((a, j) => (j === i ? { ...a, [k]: v } : a)))
  const n = Math.max(1, Number(count) || 1)

  const submit = async (start) => {
    setBusy(true)
    try {
      const all = agents.map(a => ({ ...a, provider: 'google', model, tools: DEFAULT_TOOLS }))
      if (withJudge) all.push({ ...judge, provider: 'google', model, tools: [], max_tokens: 3000, decider: true, synthesis: true })
      await api.post(`/api/projects/${pid}/setup`, {
        task,
        agents: all,
        settings: {
          max_rounds: Number(maxRounds) || 1,
          ...(withJudge ? { convergence: { enabled: true, min_rounds: 2 } } : {}),
        },
      })
      if (start) await api.post(`/api/projects/${pid}/control/start`)
      onDone()
    } catch (e) { notify(e) } finally { setBusy(false) }
  }

  return (
    <div className="setup">
      <div className="steps">
        {['Task', 'Agents', 'Instructions'].map((t, i) => (
          <button key={t} className={'step ' + (step === i + 1 ? 'on' : step > i + 1 ? 'done' : '')}
            onClick={() => (i + 1 < step || (i === 1 && task.trim()) || (i === 2 && task.trim() && agents.length)) && setStep(i + 1)}>
            <span className="step-n">{i + 1}</span> {t}
          </button>
        ))}
      </div>

      {step === 1 && (
        <div className="card">
          <h3>What should the agents work on?</h3>
          <p className="muted">Every agent receives this task in round 1. Markdown and LaTeX ($...$, $$...$$) are supported.</p>
          <div className="seg"><button className={!preview ? 'on' : ''} onClick={() => setPreview(false)}>Write</button>
            <button className={preview ? 'on' : ''} onClick={() => setPreview(true)}>Preview</button></div>
          {preview ? <div className="preview-box"><Markdown text={task} /></div>
            : <textarea className="mono big" autoFocus rows={14} value={task} onChange={e => setTask(e.target.value)}
              placeholder="e.g. Derive the weight update of a hierarchical predictive coding network and determine when it equals the backprop gradient..." />}
          <div className="row"><button className="btn primary" disabled={!task.trim()} onClick={() => { ensure(n); setStep(2) }}>Next →</button></div>
        </div>
      )}

      {step === 2 && (
        <div className="card">
          <h3>How many agents?</h3>
          <p className="muted">All agents are peers that run in synchronized rounds. Their roles come only from the instructions you write next.</p>
          <div className="form-row">
            <label>Number of agents<input type="number" min="1" value={count} onChange={e => setCount(e.target.value)} /></label>
            <label>Model (Google AI Studio)<select value={model} onChange={e => { setModel(e.target.value); setCheck(null) }}>
              {[...new Set([model, ...models])].map(m => <option key={m} value={m}>{(live.find(x => x.id === m) || {}).label || m}{m === 'gemini-3.5-flash-lite' ? ' (recommended)' : ''}</option>)}
            </select></label>
            <button className="btn" onClick={testModel} disabled={check?.pending}>{check?.pending ? 'Testing...' : 'Test model'}</button>
            <label>Maximum rounds<input type="number" min="1" value={maxRounds} onChange={e => setMaxRounds(e.target.value)} /></label>
          </div>
          <div className="judge-opt">
            <label className="chk"><input type="checkbox" checked={withJudge} onChange={e => setWithJudge(e.target.checked)} />
              <b>Add a neutral judge</b> (recommended)</label>
            <p className="muted small">One extra agent that never argues: each round it names the crux, tracks concessions, flags dodged
              or unsupported claims and asks each agent a question. It decides when further rounds stop adding anything, then writes
              a final synthesis. It has no tools, so it adds only about 10-15% to the cost. Its instructions are editable on the next page.</p>
            {withJudge && <label className="chk small"><input type="checkbox" checked={rules} onChange={e => setRules(e.target.checked)} />
              add discussion rules to each agent (answer the judge, concede a point per round, under ~600 words)</label>}
          </div>
          {check && !check.pending && (check.ok ? <div className="ok-box">{model} responded - ready to use.</div>
            : <div className="warn-box">{model} is not usable right now: {check.error}</div>)}
          <p className="muted small">Models listed are the ones your key can use. With a rate-limited key, fewer agents and a "flash" model keep rounds fast; rate-limit errors are retried automatically.</p>
          <div className="row">
            <button className="btn" onClick={() => setStep(1)}>← Back</button>
            <button className="btn primary" onClick={() => { ensure(n); setStep(3) }}>Next →</button>
          </div>
        </div>
      )}

      {step === 3 && (
        <>
          <div className="card">
            <h3>Instruction pages</h3>
            <p className="muted">Write what each agent should focus on and how it should work. You can edit these later; changes apply from the next round.</p>
          </div>
          {agents.map((a, i) => (
            <div key={i} className="card agent-card" style={agentColor(i)}>
              <div className="form-row">
                <label>Name<input value={a.name} onChange={e => upd(i, 'name', e.target.value)} /></label>
              </div>
              <label className="block">Instructions for {a.name || `Agent ${i + 1}`}
                <textarea className="mono" rows={8} value={a.instructions} onChange={e => upd(i, 'instructions', e.target.value)}
                  placeholder="e.g. Derive results rigorously and state every assumption. / Challenge the other agents' arguments and look for counterexamples. / Test claims numerically with Python." />
              </label>
              {!a.instructions.trim() && <div className="muted small">Empty instructions: this agent will only follow the shared task.</div>}
            </div>
          ))}
          {withJudge && (
            <div className="card agent-card judge-card" style={agentColor(agents.length)}>
              <div className="form-row">
                <label>Judge name<input value={judge.name} onChange={e => setJudge({ ...judge, name: e.target.value })} /></label>
                <span className="muted small">no tools - decides when to stop - writes the final synthesis</span>
              </div>
              <label className="block">Instructions for the judge
                <textarea className="mono" rows={10} value={judge.instructions} onChange={e => setJudge({ ...judge, instructions: e.target.value })} />
              </label>
            </div>
          )}
          <div className="row setup-actions">
            <button className="btn" onClick={() => setStep(2)}>← Back</button>
            <span className="spacer" />
            <button className="btn" disabled={busy} onClick={() => submit(false)}>Save without starting</button>
            <button className="btn primary" disabled={busy || !task.trim()} onClick={() => submit(true)}>▶ Start round 1</button>
          </div>
        </>
      )}
    </div>
  )
}
