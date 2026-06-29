const BASE = '/api'

async function request(path, options = {}) {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(body.detail || `Request failed: ${res.status}`)
  }
  return res.json()
}

export const api = {
  health: () => request('/health'),

  createTask: (payload) =>
    request('/tasks', { method: 'POST', body: JSON.stringify(payload) }),

  listTasks: (params = {}) => {
    const qs = new URLSearchParams(params).toString()
    return request(`/tasks${qs ? `?${qs}` : ''}`)
  },

  getTask: (taskId) => request(`/tasks/${taskId}`),

  getJob: (jobId) => request(`/jobs/${jobId}`),

  updateJobStatus: (jobId, status, logLine) =>
    request(`/jobs/${jobId}/status`, {
      method: 'PATCH',
      body: JSON.stringify({ status, log_line: logLine }),
    }),

  getStats: (period = 'weekly') => request(`/stats?period=${period}`),

  seedDemo: () => request('/demo/seed', { method: 'POST' }),
  resetDemo: () => request('/demo/reset', { method: 'POST' }),
}
