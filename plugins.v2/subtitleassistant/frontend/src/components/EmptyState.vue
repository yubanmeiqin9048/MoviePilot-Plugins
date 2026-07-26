<script setup lang="ts">
defineProps<{
  icon: string
  title: string
  message: string
}>()
</script>

<template>
  <div class="empty-state" role="status">
    <VIcon :icon="icon" size="32" color="medium-emphasis" aria-hidden="true" />
    <div class="empty-state__title">{{ title }}</div>
    <div class="empty-state__message">{{ message }}</div>
    <div v-if="$slots.actions" class="empty-state__actions">
      <slot name="actions" />
    </div>
  </div>
</template>

<style scoped>
.empty-state {
  display: grid;
  min-height: 15rem;
  place-content: center;
  justify-items: center;
  gap: 0.5rem;
  padding: 2rem 1rem;
  text-align: center;
}

.empty-state__title {
  color: rgb(var(--v-theme-on-surface));
  font-size: 1rem;
  font-weight: 600;
}

.empty-state__message {
  max-width: 32rem;
  color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity));
  font-size: 0.875rem;
  line-height: 1.6;
}

.empty-state__actions {
  margin-top: 0.5rem;
}
</style>
