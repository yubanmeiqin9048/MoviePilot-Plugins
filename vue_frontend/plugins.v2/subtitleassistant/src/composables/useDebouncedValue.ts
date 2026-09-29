import { onBeforeUnmount, ref, watch, type Ref } from 'vue'

export function useDebouncedValue(source: Ref<string>, delay = 300): Ref<string> {
  const debounced = ref(source.value)
  let timer: ReturnType<typeof setTimeout> | undefined

  watch(source, value => {
    if (timer) clearTimeout(timer)
    timer = setTimeout(() => {
      debounced.value = value.trim()
    }, delay)
  })

  onBeforeUnmount(() => {
    if (timer) clearTimeout(timer)
  })

  return debounced
}
