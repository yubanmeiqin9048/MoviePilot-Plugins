<script setup lang="ts">
import { computed, onBeforeUnmount, ref, useId, watch } from 'vue'
import { useDisplay } from 'vuetify'

const props = withDefaults(defineProps<{
  modelValue: boolean
  title: string
  subtitle?: string | null
  closeLabel?: string
  returnFocusKey?: string | null
}>(), {
  subtitle: null,
  closeLabel: '关闭详情',
  returnFocusKey: null,
})

const emit = defineEmits<{ 'update:modelValue': [value: boolean] }>()
const { mdAndUp } = useDisplay()
const returnFocusTo = ref<HTMLElement | null>(null)
const storedFocusKey = ref<string | null>(null)
const storedFocusIndex = ref(0)
const isDesktop = computed(() => mdAndUp.value)
const titleId = `subtitleassistant-detail-${useId()}`

watch(
  () => props.modelValue,
  open => {
    if (!open) return
    returnFocusTo.value = document.activeElement instanceof HTMLElement && document.activeElement !== document.body
      ? document.activeElement
      : null
    storedFocusKey.value = props.returnFocusKey
    const triggers = focusTriggers(props.returnFocusKey)
    const matchingIndex = triggers.findIndex(element => focusKey(element) === props.returnFocusKey)
    storedFocusIndex.value = matchingIndex >= 0 ? matchingIndex : 0
  },
)

onBeforeUnmount(restoreFocus)

function updateModel(open: boolean): void {
  emit('update:modelValue', open)
}

function restoreFocus(): void {
  const target = returnFocusTo.value
  returnFocusTo.value = null
  if (target?.isConnected) {
    target.focus({ preventScroll: true })
    clearStoredFocus()
    return
  }

  const triggers = focusTriggers(storedFocusKey.value)
  const matchingTrigger = triggers.find(element => focusKey(element) === storedFocusKey.value)
  const fallback = matchingTrigger
    || triggers[Math.min(storedFocusIndex.value, Math.max(0, triggers.length - 1))]
    || document.getElementById('subtitleassistant-workbench-title')
  fallback?.focus({ preventScroll: true })
  clearStoredFocus()
}

function focusKey(element: HTMLElement): string | null {
  return element.getAttribute('data-subtitleassistant-detail-trigger')
}

function focusTriggers(key: string | null): HTMLElement[] {
  const group = key?.split(':', 1)[0]
  return Array.from(document.querySelectorAll<HTMLElement>('[data-subtitleassistant-detail-trigger]'))
    .filter(element => !group || focusKey(element)?.startsWith(`${group}:`))
}

function clearStoredFocus(): void {
  storedFocusKey.value = null
  storedFocusIndex.value = 0
}
</script>

<template>
  <VDialog
    :model-value="modelValue"
    :fullscreen="!isDesktop"
    max-width="48rem"
    scrollable
    retain-focus
    content-class="subtitleassistant-detail-overlay"
    transition="slide-x-reverse-transition"
    :aria-labelledby="titleId"
    @update:model-value="updateModel"
    @after-leave="restoreFocus"
  >
    <VCard class="detail-drawer-card">
      <header class="detail-drawer-header">
        <VBtn
          :icon="isDesktop ? 'mdi-close' : 'mdi-arrow-left'"
          variant="text"
          :aria-label="isDesktop ? closeLabel : '返回列表'"
          @click="updateModel(false)"
        />
        <div class="detail-drawer-heading">
          <h3 :id="titleId">{{ title }}</h3>
          <p v-if="subtitle" :title="subtitle">{{ subtitle }}</p>
        </div>
        <VSpacer />
        <slot name="actions" />
      </header>

      <div class="detail-drawer-body">
        <slot />
      </div>
    </VCard>
  </VDialog>
</template>

<style scoped>
.detail-drawer-card {
  display: flex;
  width: 100%;
  height: 100%;
  min-height: 0;
  flex-direction: column;
  overflow: hidden !important;
  border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
  border-radius: 0.375rem;
  background: rgb(var(--v-theme-surface));
}

.detail-drawer-header {
  display: flex;
  min-height: 4rem;
  flex: 0 0 auto;
  align-items: center;
  gap: 0.5rem;
  padding: 0.5rem 0.75rem;
  border-bottom: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
  background: rgb(var(--v-theme-surface));
}

.detail-drawer-heading {
  min-width: 0;
}

.detail-drawer-heading h3 {
  margin: 0;
  color: rgb(var(--v-theme-on-surface));
  font-size: 1rem;
  font-weight: 650;
  letter-spacing: 0;
}

.detail-drawer-heading p {
  margin: 0.2rem 0 0;
  overflow: hidden;
  color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity));
  font-size: 0.75rem;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.detail-drawer-body {
  min-height: 0;
  flex: 1 1 auto;
  overflow-y: auto;
  overscroll-behavior: contain;
}

@media (max-width: 959px) {
  .detail-drawer-card { border: 0; border-radius: 0; }
  .detail-drawer-header { min-height: 3.75rem; padding-inline: 0.5rem; }
}
</style>

<style>
.subtitleassistant-detail-overlay {
  inset: 0.75rem 0.75rem 0.75rem auto !important;
  width: min(48rem, calc(100vw - 1.5rem)) !important;
  max-width: 48rem !important;
  height: calc(100dvh - 1.5rem);
  max-height: calc(100dvh - 1.5rem) !important;
  margin: 0 !important;
}

@media (max-width: 959px) {
  .subtitleassistant-detail-overlay {
    inset: 0 !important;
    width: 100% !important;
    max-width: none !important;
    height: 100% !important;
    max-height: none !important;
    margin: 0 !important;
  }
}

@media (prefers-reduced-motion: reduce) {
  .subtitleassistant-detail-overlay,
  .subtitleassistant-detail-overlay * {
    transition-duration: 0.01ms !important;
    animation-duration: 0.01ms !important;
  }
}
</style>
