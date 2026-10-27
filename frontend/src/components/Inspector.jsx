import React, { useEffect, useState } from 'react'
import { api, duration, fmtCost, fmtTokens } from '../api.js'
import Markdown from '../Markdown.jsx'
import { WHERE_LABEL, openFile, runFileUrl } from './FileViewer.jsx'

const TABS = ['output', 'context', 'tools', 'files', 'transcript', 'assessment', 'config']

export default function Inspector({ runId, state, onClose, notify }) {
  const [run, setRun] = useState(null)
  const [tab, setTab] = useState('output')
  const [raw, setRaw] = useState(false)
  const seq = state?.project.seq
  useEffect(() => { api.get(`/api/runs/${runId}`).then(setRun).catch(notify) }, [runId, seq, notify])
  useEffect(() => {
    const k = (e) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', k)
    return () => window.removeEventListener('keydown', k)
  }, [onClose])
  if (!run) return null
  const c = run.context
  const files = state?.rounds.flatMap(r => r.runs).find(x => x.id === runId)?.files || []
  return (
    <div className="drawer-backdrop" onClick={onClose}>
      <div className="drawer" onClick={e => e.stopPropagation()}>
        <div className="drawer-head">
          <div>
            <h3>{run.agent_config.name} - round {run.round_number} {run.attempt > 1 ? `(attempt ${run.attempt})` : ''}</h3>
            <div className="muted small">{run.agent_config.provider} / {run.agent_config.model} - instructions v{run.agent_config.instruction_version} -
              status <b>{run.status}</b> - {fmtTokens(run.tokens_in)} in / {fmtTokens(run.tokens_out)} out - {fmtCost(run.cost)} -
              {' '}{duration(run.started_at, run.finished_at)}</div>
          </div>
          <button className="btn" onClick={onClose}>Close ✕</button>
        </div>
        <div className="tabs">
          {TABS.map(t => <button key={t} className={tab === t ? 'on' : ''} onClick={() => setTab(t)}>
            {t}{t === 'tools' ? ` (${run.tool_calls.length})` : t === 'files' ? ` (${files.length})` : ''}</button>)}
          {['output', 'context'].includes(tab) && <label className="chk"><input type="checkbox" checked={raw} onChange={e => setRaw(e.target.checked)} /> raw text</label>}
        </div>
        <div className="drawer-body">
          {tab === 'output' && (
            <>
              {run.error && <div className="error-box">{run.error}</div>}
              {run.output ? (raw ? <pre className="mono wrap">{run.output}</pre> : <Markdown text={run.output} />) : <div className="muted">No output.</div>}
            </>
          )}
          {tab === 'context' && (
            <>
              <p className="muted small">Exactly what this agent received at the start of round {run.round_number} (frozen snapshot; ~{fmtTokens(c.token_estimate)} tokens).
                Snapshot: {c.snapshot?.message_ids?.length} messages, workspace versions {JSON.stringify(c.snapshot?.workspace_versions)}.</p>
              <div className="sections">{c.sections.map((s, i) => (
                <span key={i} className={'file-chip ' + (s.truncated ? 'trunc' : '')}>{s.section}: {s.chars} chars{s.truncated ? (s.omitted ? ' (omitted)' : ' (truncated)') : ''}</span>
              ))}</div>
              <details><summary><b>System prompt</b> ({c.system.length} chars)</summary><pre className="mono wrap">{c.system}</pre></details>
              <h4>User message</h4>
              {raw ? <pre className="mono wrap">{c.user}</pre> : <Markdown text={c.user} />}
            </>
          )}
          {tab === 'tools' && (
            <>
              {run.tool_calls.length === 0 && <div className="muted">No tool calls.</div>}
              {run.tool_calls.map(t => (
                <details key={t.id} className={'tool-call ' + t.status}>
                  <summary><code>#{t.id} {t.tool}</code> <span className={'status-tag ' + (t.status === 'ok' ? 'processed' : 'failed')}>{t.status}</span>
                    {t.private ? <span className="status-tag">private</span> : null} <span className="muted small">{t.args.slice(0, 140)}</span></summary>
                  <h5>Arguments</h5><pre className="mono wrap">{pretty(t.args)}</pre>
                  <h5>Result</h5><pre className="mono wrap">{t.result}</pre>
                </details>
              ))}
              {run.proposals.length > 0 && (
                <>
                  <h4>Workspace proposals</h4>
                  {run.proposals.map(p => <div key={p.id} className="small"><code>{p.op}</code> <code>{p.key}</code> - {p.status}</div>)}
                </>
              )}
            </>
          )}
          {tab === 'files' && (
            <>
              <p className="muted small">Every file this run created or changed, including intermediate files. Staged files live in the
                agent's working copy until the round completes; files from runs that were not committed are archived, never deleted.
                Code the agent executed is under the tools tab (run_python / run_bash arguments).</p>
              {files.length === 0 && <div className="muted">This run has not written any files.</div>}
              <table className="tbl">
                <tbody>{files.map((f, i) => (
                  <tr key={i}>
                    <td><button className="link mono" onClick={() => openFile({ url: runFileUrl(run.id, f), path: f.path, label: WHERE_LABEL[f.where] })}>{f.path}</button></td>
                    <td><span className={'status-tag w-' + f.where}>{f.where}</span></td>
                    <td className="muted small">{f.status}</td>
                    <td className="num">{f.size != null ? `${f.size} B` : ''}</td>
                  </tr>))}</tbody>
              </table>
            </>
          )}
          {tab === 'transcript' && (
            <>
              <p className="muted small">The full message sequence of this run's tool-use loop.</p>
              {run.transcript.map((m, i) => (
                <div key={i} className="tmsg">
                  <div className="muted small"><b>{m.role}</b></div>
                  {m.role === 'tool'
                    ? m.results.map((r, j) => <pre key={j} className="mono wrap small">{r.name}: {r.content.slice(0, 4000)}{r.content.length > 4000 ? '\n[...]' : ''}</pre>)
                    : <pre className="mono wrap small">{(m.content || '').slice(0, i === 0 ? 3000 : 20000)}{i === 0 && m.content?.length > 3000 ? '\n[... see context tab]' : ''}
                      {m.tool_calls?.length ? '\n→ ' + m.tool_calls.map(tc => `${tc.name}(${JSON.stringify(tc.args).slice(0, 300)})`).join('\n→ ') : ''}</pre>}
                </div>
              ))}
            </>
          )}
          {tab === 'assessment' && (run.assessment
            ? <pre className="mono wrap">{JSON.stringify(run.assessment, null, 2)}</pre>
            : <div className="muted">No structured convergence assessment for this run (convergence disabled, or none parsable).</div>)}
          {tab === 'config' && <pre className="mono wrap">{JSON.stringify(run.agent_config, null, 2)}</pre>}
        </div>
      </div>
    </div>
  )
}

function pretty(s) {
  try { return JSON.stringify(JSON.parse(s), null, 2) } catch { return s }
}
