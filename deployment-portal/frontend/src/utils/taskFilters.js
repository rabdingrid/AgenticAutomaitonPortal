export const STATUS_FILTER_KEYS = ['all', 'pending', 'resolved', 'in_progress', 'blocked']

export const STATUS_FILTERS = {
  all: {
    label: 'Total requests',
    heading: 'Recent activity',
    listHeading: 'All tasks',
    match: () => true,
  },
  pending: {
    label: 'Pending',
    heading: 'Pending requests',
    listHeading: 'Pending requests',
    match: (task) => task.status === 'pending_approval',
  },
  resolved: {
    label: 'Resolved',
    heading: 'Resolved requests',
    listHeading: 'Resolved requests',
    match: (task) => task.status === 'done',
  },
  in_progress: {
    label: 'In progress',
    heading: 'In progress',
    listHeading: 'In progress',
    match: (task) => task.status === 'running' || task.status === 'queued',
  },
  blocked: {
    label: 'Failed / blocked',
    heading: 'Failed / blocked requests',
    listHeading: 'Failed / blocked requests',
    match: (task) => ['blocked', 'failed', 'rejected'].includes(task.status),
  },
}

export function filterTasksByStatus(tasks, filterKey) {
  if (!filterKey || filterKey === 'all') return tasks
  const filter = STATUS_FILTERS[filterKey]
  return filter ? tasks.filter(filter.match) : tasks
}

export function blockedCount(stats) {
  if (!stats) return 0
  return stats.blocked ?? 0
}
