import React from 'react'
import { Routes, Route, NavLink } from 'react-router-dom'
import NewRequest from './pages/NewRequest.jsx'
import TaskDetail from './pages/TaskDetail.jsx'
import History from './pages/History.jsx'

export default function App() {
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="sidebar-logo">🚀 DeployPortal</div>
        <div className="sidebar-sub">Automation request system</div>

        <NavLink to="/" className={({ isActive }) => `nav-link ${isActive ? 'active' : ''}`}>
          ➕ New request
        </NavLink>
        <NavLink to="/history" className={({ isActive }) => `nav-link ${isActive ? 'active' : ''}`}>
          📋 History
        </NavLink>
      </aside>

      <main className="main-content">
        <Routes>
          <Route path="/" element={<NewRequest />} />
          <Route path="/tasks/:taskId" element={<TaskDetail />} />
          <Route path="/history" element={<History />} />
        </Routes>
      </main>
    </div>
  )
}
