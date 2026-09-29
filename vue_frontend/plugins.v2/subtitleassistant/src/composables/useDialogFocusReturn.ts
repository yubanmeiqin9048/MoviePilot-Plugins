import { onBeforeUnmount, ref } from 'vue'

export function useDialogFocusReturn(fallbackId = 'subtitleassistant-workbench-title') {
  const returnFocusTo = ref<HTMLElement | null>(null)

  function captureFocus(): void {
    if (typeof document === 'undefined') return
    returnFocusTo.value = document.activeElement instanceof HTMLElement && document.activeElement !== document.body
      ? document.activeElement
      : null
  }

  function restoreFocus(): void {
    if (typeof document === 'undefined') return
    const target = returnFocusTo.value
    returnFocusTo.value = null
    const fallback = document.getElementById(fallbackId)
    const focusTarget = target?.isConnected ? target : fallback
    focusTarget?.focus({ preventScroll: true })
  }

  onBeforeUnmount(restoreFocus)

  return { captureFocus, restoreFocus }
}
