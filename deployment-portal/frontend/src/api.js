const BASE = '/api'

async function request(path, options = {}) {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    const detail = body.detail
    const message = typeof detail === 'string' ? detail : Array.isArray(detail) ? detail.map((d) => d.msg).join(', ') : `Request failed: ${res.status}`
    throw new Error(message)
  }
  return res.json()
}

export const api = {
  health: () => request('/health'),

  getEnvironments: () => request('/catalog/environments'),
  getApprovers: () => request('/catalog/approvers'),
  getServices: (section, type) => {
    const params = new URLSearchParams()
    if (section) params.set('section', section)
    if (type) params.set('type', type)
    const qs = params.toString()
    return request(`/catalog/services${qs ? `?${qs}` : ''}`)
  },
  getBranches: (serviceKey, query = '') =>
    request(`/catalog/branches?service_key=${encodeURIComponent(serviceKey)}&query=${encodeURIComponent(query)}`),
  getCodeFreeze: () => request('/catalog/code-freeze'),
  setCodeFreeze: (enabled, updatedBy = 'demo-devops') =>
    request(`/catalog/code-freeze?enabled=${enabled}&updated_by=${encodeURIComponent(updatedBy)}`, { method: 'PUT' }),

  validateRequest: (payload) =>
    request('/tasks/validate', { method: 'POST', body: JSON.stringify(payload) }),

  createTask: (payload) =>
    request('/tasks', { method: 'POST', body: JSON.stringify(payload) }),

  listTasks: (params = {}) => {
    const qs = new URLSearchParams(params).toString()
    return request(`/tasks${qs ? `?${qs}` : ''}`)
  },

  getTask: (taskId) => request(`/tasks/${taskId}`),

  approveTask: (taskId, payload) =>
    request(`/tasks/${taskId}/approve`, { method: 'POST', body: JSON.stringify(payload) }),

  getJob: (jobId) => request(`/jobs/${jobId}`),

  updateJobStatus: (jobId, status, logLine) =>
    request(`/jobs/${jobId}/status`, {
      method: 'PATCH',
      body: JSON.stringify({ status, log_line: logLine }),
    }),

  getStats: (period = 'weekly') => request(`/stats?period=${period}`),

  getActivity: (limit = 6) => request(`/activity?limit=${limit}`),

  seedDemo: () => request('/demo/seed', { method: 'POST' }),
  resetDemo: () => request('/demo/reset', { method: 'POST' }),
}
