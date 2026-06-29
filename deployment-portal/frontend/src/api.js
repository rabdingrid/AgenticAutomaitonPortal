const BASE = '/api'

function getToken() {
  return localStorage.getItem('aap_token')
}

async function request(path, options = {}) {
  const token = getToken()
  const headers = {
    'Content-Type': 'application/json',
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    ...options.headers,
  }

  const res = await fetch(`${BASE}${path}`, { ...options, headers })

  if (res.status === 401) {
    localStorage.removeItem('aap_token')
    localStorage.removeItem('aap_user')
    window.location.href = '/login'
    return
  }

  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    const detail = body.detail
    const message =
      typeof detail === 'string'
        ? detail
        : Array.isArray(detail)
          ? detail.map((d) => d.msg).join(', ')
          : `Request failed: ${res.status}`
    throw new Error(message)
  }

  return res.json()
}

export const api = {
  health: () => request('/health'),

  getEnvironments: () => request('/catalog/environments'),
  getApprovers: () => request('/catalog/approvers'),

  createTask: (payload) =>
    request('/tasks', { method: 'POST', body: JSON.stringify(payload) }),

  listTasks: (params = {}) => {
    const qs = new URLSearchParams(params).toString()
    return request(`/tasks${qs ? `?${qs}` : ''}`)
  },

  getTask: (taskId) => request(`/tasks/${taskId}`),

  approveTask: (taskId, role) =>
    request(`/tasks/${taskId}/approve`, {
      method: 'POST',
      body: JSON.stringify({ role }),
    }),

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
