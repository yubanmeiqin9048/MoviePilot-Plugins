import { onBeforeUnmount, onMounted, onUpdated } from 'vue'

/** 裁掉滚入玻璃固定表头后的表体，避免透字；移动端隐藏表头时恢复完整内容。 */
export function useStickyTableClipping(getTable: () => HTMLTableElement | null | undefined): () => void {
  return useStickyContentClipping(
    () => getTable()?.tHead,
    () => Array.from(getTable()?.tBodies || []),
  )
}

/** 裁掉玻璃吸顶栏后方的内容；没有可见吸顶栏时取消裁剪。 */
export function useStickyContentClipping(
  getHead: () => HTMLElement | null | undefined,
  getBodies: () => HTMLElement[],
): () => void {
  let frame: number | null = null

  function update(): void {
    frame = null
    const head = getHead()
    const headBottom = head?.getBoundingClientRect().bottom || 0
    const headVisible = head && head.getClientRects().length > 0
    for (const body of getBodies()) {
      const inset = headVisible ? Math.max(0, headBottom - body.getBoundingClientRect().top) : 0
      body.style.clipPath = inset > 0 ? `inset(${inset}px 0 0)` : ''
    }
  }

  function schedule(): void {
    if (frame == null) frame = requestAnimationFrame(update)
  }

  onMounted(() => {
    window.addEventListener('scroll', schedule, true)
    window.addEventListener('resize', schedule)
    schedule()
  })
  onUpdated(schedule)
  onBeforeUnmount(() => {
    window.removeEventListener('scroll', schedule, true)
    window.removeEventListener('resize', schedule)
    if (frame != null) cancelAnimationFrame(frame)
  })

  return schedule
}
