import React, { useState } from 'react'
import TopBar from './TopBar.jsx'
import Channel from './Channel.jsx'
import Agents from './Agents.jsx'
import Workspace from './Workspace.jsx'
import Files from './Files.jsx'
import Convergence from './Convergence.jsx'
import Settings from './Settings.jsx'
import Activity from './Activity.jsx'

const TABS = [
  ['agents', 'Agents & models'],
  ['settings', 'Settings & keys'],
  ['workspace', 'Workspace'],
  ['files', 'Files & sources'],
  ['convergence', 'Convergence'],
  ['detail', 'Detailed timeline'],
  ['log', 'Event log'],
]

// Everything that is not Setup or the Activity screen.
export default function Options({ state, messages, meta, pid, act, notify, reloadMeta, onInspect, onDeleted }) {
  const [tab, setTab] = useState('agents')
  const seq = state.project.seq
  return (
    <div className="options">
      <div className="options-tabs">
        {TABS.map(([k, l]) => <button key={k} className={tab === k ? 'on' : ''} onClick={() => setTab(k)}>{l}</button>)}
      </div>
      <div className="options-body">
        {tab === 'agents' && <Agents state={state} meta={meta} pid={pid} act={act} />}
        {tab === 'settings' && <Settings state={state} meta={meta} pid={pid} act={act} notify={notify}
          onDeleted={onDeleted} reloadMeta={reloadMeta} />}
        {tab === 'workspace' && <Workspace pid={pid} seq={seq} meta={meta} act={act} notify={notify} />}
        {tab === 'files' && <Files pid={pid} seq={seq} state={state} notify={notify} />}
        {tab === 'convergence' && <Convergence pid={pid} seq={seq} state={state} notify={notify} />}
        {tab === 'detail' && (
          <div className="detail-wrap">
            <TopBar state={state} act={act} pid={pid} />
            <div className="content"><Channel channel="common" state={state} messages={messages} pid={pid} act={act} onInspect={onInspect} /></div>
          </div>
        )}
        {tab === 'log' && <Activity pid={pid} seq={seq} state={state} notify={notify} />}
      </div>
    </div>
  )
}
