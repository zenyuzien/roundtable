import React, { useCallback, useEffect, useRef, useState } from 'react'
import { api, agentColor } from './api.js'
import TopBar from './components/TopBar.jsx'
import Channel from './components/Channel.jsx'
import Agents from './components/Agents.jsx'
import Workspace from './components/Workspace.jsx'
import Files from './components/Files.jsx'
import Convergence from './components/Convergence.jsx'
import Settings from './components/Settings.jsx'
import Activity from './components/Activity.jsx'
import Inspector from './components/Inspector.jsx'
import Setup from './components/Setup.jsx'
import FileViewer from './components/FileViewer.jsx'

function parseView() {
  const m = window.location.hash.match(/^#\/(.+)/)
  return m ? m[1] : 'common'
}

const PANELS = [
  ['agents', 'Agents'],
  ['workspace', 'Workspace'],
  ['files', 'Files & sources'],
  ['convergence', 'Convergence'],
  ['settings', 'Settings'],
  ['activity', 'Activity'],
]

export default function App() {
  const [pid, setPid] = useState(null)
  const [view, setView] = useState(parseView)
  const [state, setState] = useState(null)
  const [messages, setMessages] = useState([])
  const [meta, setMeta] = useState(null)
  const [inspect, setInspect] = useState(null)
  const [viewFile, setViewFile] = useState(null)
  useEffect(() => {
    const h = (e) => setViewFile(e.detail)
    window.addEventListener('rt:view-file', h)
    return () => window.removeEventListener('rt:view-file', h)
  }, [])
  const [toast, setToast] = useState(null)
  const [editSetup, setEditSetup] = useState(false)
  const seqRef = useRef(-1)

  const notify = useCallback((e, kind = 'error') => {
    setToast({ text: e?.message || String(e), kind })
    setTimeout(() => setToast(null), 6000)
  }, [])

  const loadProject = useCallback(async () => {
    try { setPid((await api.get('/api/project')).id) } catch (e) { notify(e) }
  }, [notify])

  useEffect(() => {
    loadProject()
    api.get('/api/meta').then(setMeta).catch(notify)
  }, [loadProject, notify])

  useEffect(() => {
    const target = `#/${view}`
    if (window.location.hash !== target) window.history.replaceState(null, '', target)
  }, [view])

  const refresh = useCallback(async () => {
    if (!pid) return
    try {
      const [s, m] = await Promise.all([api.get(`/api/projects/${pid}/state`), api.get(`/api/projects/${pid}/messages`)])
      setState(s)
      setMessages(m)
      seqRef.current = s.project.seq
    } catch (e) {
      if (String(e.message).includes('no such project')) loadProject(); else notify(e)
    }
  }, [pid, notify, loadProject])

  useEffect(() => {
    seqRef.current = -1
    setState(null)
    setMessages([])
    refresh()
    if (!pid) return
    const t = setInterval(async () => {
      try {
        const { seq } = await api.get(`/api/projects/${pid}/seq`)
        if (seq !== seqRef.current) refresh()
      } catch { /* server restarting; keep polling */ }
    }, 1000)
    return () => clearInterval(t)
  }, [pid, refresh])

  const act = useCallback(async (fn, okText) => {
    try {
      const r = await fn()
      await refresh()
      if (okText) notify({ message: okText }, 'ok')
      return r
    } catch (e) { notify(e) }
  }, [refresh, notify])

  const agents = state?.agents || []
  const started = !!state?.rounds?.some(r => r.status !== 'discarded')
  const inSetup = !!state && !started && (editSetup || !state.has_task || agents.length === 0)
  const lastRound = state?.rounds?.filter(r => r.status !== 'discarded').slice(-1)[0]
  const runStatus = (aid) => lastRound?.runs.filter(r => r.agent_id === aid && !r.superseded).slice(-1)[0]?.status

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">Roundtable</div>
        {state && !inSetup && (
          <>
            <div className="nav-label">Channels</div>
            <button className={'nav-item ' + (view === 'common' ? 'active' : '')} onClick={() => setView('common')}>
              <span className="chan-icon">#</span> Common
              {state.pending_common > 0 && <span className="badge pending" title="pending interventions">{state.pending_common}</span>}
            </button>
            {agents.map((a, i) => (
              <button key={a.id} style={agentColor(i)}
                className={'nav-item agent-nav ' + (view === `agent:${a.id}` ? 'active' : '') + (a.enabled ? '' : ' disabled')}
                onClick={() => setView(`agent:${a.id}`)}>
                <span className={'dot ' + (runStatus(a.id) || 'idle')} />
                <span className="agent-name-chip">{a.name}</span>
                {!a.enabled && <span className="muted small">off</span>}
                {a.pending_messages > 0 && <span className="badge pending">{a.pending_messages}</span>}
              </button>
            ))}
            {!started && !inSetup && <button className="nav-item" onClick={() => setEditSetup(true)}>✎ Edit setup</button>}
            <div className="nav-label">Project</div>
            {PANELS.map(([k, label]) => (
              <button key={k} className={'nav-item ' + (view === k ? 'active' : '')} onClick={() => setView(k)}>{label}</button>
            ))}
          </>
        )}
        <div className="sidebar-foot muted small">
          {meta && <span title={meta.sandbox.note}>sandbox: {meta.sandbox.mode}</span>}
        </div>
      </aside>

      <main className="main">
        {!state && <div className="empty">Loading...</div>}
        {state && inSetup && (
          <div className="content"><Setup pid={pid} meta={meta} state={{ ...state, task: messages.find(m => m.kind === 'task')?.content }}
            notify={notify} onDone={() => { setEditSetup(false); setView('common'); refresh() }} /></div>
        )}
        {state && !inSetup && (
          <>
            <TopBar state={state} act={act} pid={pid} />
            <div className="content">
              {(view === 'common' || view.startsWith('agent:')) && (
                <Channel key={view} channel={view} state={state} messages={messages} pid={pid} act={act}
                  onInspect={setInspect} />
              )}
              {view === 'agents' && <Agents state={state} meta={meta} pid={pid} act={act} />}
              {view === 'workspace' && <Workspace pid={pid} seq={state.project.seq} meta={meta} act={act} notify={notify} />}
              {view === 'files' && <Files pid={pid} seq={state.project.seq} state={state} notify={notify} />}
              {view === 'convergence' && <Convergence pid={pid} seq={state.project.seq} state={state} notify={notify} />}
              {view === 'settings' && <Settings state={state} meta={meta} pid={pid} act={act} notify={notify}
                onDeleted={() => { setState(null); setView('common'); loadProject() }} reloadMeta={() => api.get('/api/meta').then(setMeta)} />}
              {view === 'activity' && <Activity pid={pid} seq={state.project.seq} state={state} notify={notify} />}
            </div>
          </>
        )}
      </main>

      {inspect && <Inspector runId={inspect} state={state} onClose={() => setInspect(null)} notify={notify} />}
      {viewFile && <FileViewer file={viewFile} onClose={() => setViewFile(null)} />}
      {toast && <div className={'toast ' + toast.kind} onClick={() => setToast(null)}>{toast.text}</div>}
    </div>
  )
}
