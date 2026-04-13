/**
 * Агрегация сырых точек (время + скорость) в интервалы фиксированной длительности.
 * Возвращает интервалы с наименьшей средней скоростью (самые «тяжёлые»).
 */
export function rankIntervalsByAvgSpeed(rawTimes, rawSpeeds, bucketHours, topN = 8, minSamples = 2) {
  if (!rawTimes?.length || !rawSpeeds?.length || rawTimes.length !== rawSpeeds.length) {
    return []
  }
  const bucketMs = bucketHours * 3600 * 1000
  const buckets = new Map()

  for (let i = 0; i < rawTimes.length; i++) {
    const t = new Date(rawTimes[i]).getTime()
    const sp = Number(rawSpeeds[i])
    if (Number.isNaN(t) || Number.isNaN(sp)) continue
    const key = Math.floor(t / bucketMs) * bucketMs
    if (!buckets.has(key)) buckets.set(key, [])
    buckets.get(key).push(sp)
  }

  const rows = []
  for (const [startMs, speeds] of buckets.entries()) {
    if (speeds.length < minSamples) continue
    const sum = speeds.reduce((a, b) => a + b, 0)
    rows.push({
      startMs,
      endMs: startMs + bucketMs,
      count: speeds.length,
      avgSpeed: sum / speeds.length,
    })
  }

  rows.sort((a, b) => a.avgSpeed - b.avgSpeed)
  return rows.slice(0, topN)
}
