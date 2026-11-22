export const memoryPanels = [
  ['stats', '记忆统计'], ['history', '偏好历史'], ['profile', '当前画像'],
]

export function createMemorySync(panels, request, now) {
  const versions = {}

  function reset(keys = memoryPanels.map(([key]) => key)) {
    for (const key of keys) {
      versions[key] = (versions[key] ?? 0) + 1
      panels[key] = { status: 'idle', updatedAt: null, data: null }
    }
  }

  async function refresh(user, keys = memoryPanels.map(([key]) => key)) {
    await Promise.all(keys.map(async (key) => {
      const version = ++versions[key]
      panels[key] = { ...panels[key], status: 'loading' }
      try {
        const data = await request(key, user)
        if (versions[key] === version) panels[key] = { status: 'success', updatedAt: now(), data }
      } catch {
        if (versions[key] === version) {
          const old = panels[key]
          panels[key] = { ...old, status: old.updatedAt === null ? 'error' : 'stale' }
        }
      }
    }))
  }

  reset()
  return { reset, refresh }
}
