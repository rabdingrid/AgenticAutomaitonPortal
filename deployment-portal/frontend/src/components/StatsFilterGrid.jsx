import React from 'react'
import { blockedCount } from '../utils/taskFilters.js'

const BOXES = [
  { key: 'all', stat: (s) => s.total, label: 'Total requests', color: null },
  { key: 'pending', stat: (s) => s.pending ?? 0, label: 'Pending', color: 'var(--amber)' },
  { key: 'resolved', stat: (s) => s.resolved, label: 'Resolved', color: 'var(--green)' },
  { key: 'in_progress', stat: (s) => s.in_progress, label: 'In progress', color: 'var(--blue)' },
  { key: 'blocked', stat: blockedCount, label: 'Failed / blocked', color: 'var(--red)' },
]

export default function StatsFilterGrid({ stats, activeFilter, onFilterChange }) {
  if (!stats) return null

  return (
    <div className="stats-grid stats-grid-5">
      {BOXES.map(({ key, stat, label, color }) => {
        const active = activeFilter === key
        return (
          <button
            key={key}
            type="button"
            className={`stat-box stat-box-btn ${active ? 'active' : ''}`}
            onClick={() => onFilterChange(key)}
            aria-pressed={active}
          >
            <div className="stat-num" style={color ? { color } : undefined}>
              {stat(stats)}
            </div>
            <div className="stat-label">{label}</div>
          </button>
        )
      })}
    </div>
  )
}
