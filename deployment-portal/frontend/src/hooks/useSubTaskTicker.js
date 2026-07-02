import { useEffect, useRef, useCallback } from 'react'
import { api } from '../api.js'

/**
 * useSubTaskTicker(taskStatus, subTasks, onUpdate)
 *
 * While a task is "running", polls each running sub-task every
 * TICK_INTERVAL_MS and calls onUpdate(updatedSubTask) for each tick.
 *
 * Automatically stops polling when:
 *   - task status is not "running"
 *   - all sub-tasks are done or failed
 *   - the component unmounts
 *
 * TICK_INTERVAL_MS is 2000ms for the demo so the UI feels live without being
 * too fast to follow. When real Jenkins webhooks land, replace this polling
 * with a WebSocket/SSE listener and delete this hook.
 */
const TICK_INTERVAL_MS = 2000

export function useSubTaskTicker(taskStatus, subTasks, onUpdate) {
  const intervalRef = useRef(null)
  const inFlightRef = useRef(false)

  const tick = useCallback(async () => {
    if (inFlightRef.current) return
    const running = (subTasks || []).filter((st) => st.status === 'running')
    if (running.length === 0) return

    inFlightRef.current = true
    try {
      await Promise.all(
        running.map(async (st) => {
          try {
            const result = await api.tickSubTask(st.sub_task_id)
            if (result?.sub_task) onUpdate(result.sub_task)
          } catch (e) {
            console.warn('tick failed for', st.sub_task_id, e)
          }
        }),
      )
    } finally {
      inFlightRef.current = false
    }
  }, [subTasks, onUpdate])

  useEffect(() => {
    if (taskStatus !== 'running') {
      clearInterval(intervalRef.current)
      return
    }

    const allDone = (subTasks || []).every((st) => ['done', 'failed'].includes(st.status))
    if (allDone) {
      clearInterval(intervalRef.current)
      return
    }

    intervalRef.current = setInterval(tick, TICK_INTERVAL_MS)
    return () => clearInterval(intervalRef.current)
  }, [taskStatus, subTasks, tick])
}
