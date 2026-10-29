import React, { useCallback, useEffect, useRef, useState } from 'react'
import { api } from './api.js'
import Setup from './components/Setup.jsx'
import Ground from './components/Ground.jsx'
import Options from './components/Options.jsx'
import Inspector from './components/Inspector.jsx'
import FileViewer from './components/FileViewer.jsx'

// Two pages: Setup (when no run is configured) and Activity. Everything else lives under Options.
export default function App() {
  const [pid, setPid] = useState(null)
  const [page, setPage] = useState(() => (window.location.hash === '#/options' ? 'options' : 'activity'))
  const [state, setState] = useState(null)
  const [messages, setMessages] = useState([])
  const [meta, setMeta] = useState(null)
  const [inspect, setInspect] = useState(null)
  const [viewFile, setViewFile] = useState(null)
  const [toast, setToast] = useState(null)
  const seqRef = useRef(-1)

  useEffect(() => {
    const h = (e) => setViewFile(e.detail)
    window.addEventListener('rt:view-file', h)
    return () => window.removeEventListener('rt:view-file', h)
  }, [])

  const notify = useCallback((e, kind = 'error') => {
    setToast({ text: e?.message || String(e), kind })
    setTimeout(() => setToast(null), 6000)
  }, [])
  const reloadMeta = useCallback(() => api.get('/api/meta').then(setMeta).catch(notify), [notify])
  const loadProject = useCallback(async () => {
    try { setPid((await api.get('/api/project')).id) } catch (e) { notify(e) }
  }, [notify])

  useEffect(() => { loadProject(); reloadMeta() }, [loadProject, reloadMeta])
  useEffect(() => {
    const target = page === 'options' ? '#/options' : '#/'
    if (window.location.hash !== target) window.history.replaceState(null, '', target)
  }, [page])

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

  const needsSetup = !!state && !state.rounds.some(r => r.status !== 'discarded') && (!state.has_task || !state.agents.length)

  return (
    <div className="app2">
      <header className="nav">
        <span className="brand">Roundtable</span>
        {state && !needsSetup && (
          <>
            <button className={'nav-tab ' + (page === 'activity' ? 'on' : '')} onClick={() => setPage('activity')}>Activity</button>
            <span className="spacer" />
            <button className={'nav-tab ' + (page === 'options' ? 'on' : '')} onClick={() => setPage('options')}>Options</button>
          </>
        )}
      </header>
      <main className="page">
        {!state && <div className="empty">Loading...</div>}
        {state && needsSetup && meta && (
          <Setup pid={pid} meta={meta} reloadMeta={reloadMeta} notify={notify} onDone={() => { setPage('activity'); refresh() }} />
        )}
        {state && !needsSetup && page === 'activity' && (
          <Ground state={state} messages={messages} pid={pid} act={act} onInspect={setInspect} />
        )}
        {state && !needsSetup && page === 'options' && (
          <Options state={state} messages={messages} meta={meta} pid={pid} act={act} notify={notify} reloadMeta={reloadMeta}
            onInspect={setInspect} onDeleted={() => { setState(null); setPage('activity'); loadProject() }} />
        )}
      </main>
      {inspect && <Inspector runId={inspect} state={state} onClose={() => setInspect(null)} notify={notify} />}
      {viewFile && <FileViewer file={viewFile} onClose={() => setViewFile(null)} />}
      {toast && <div className={'toast ' + toast.kind} onClick={() => setToast(null)}>{toast.text}</div>}
    </div>
  )
}
