<script setup lang="ts">
defineProps<{
  label: string
  value?: string | number | null
}>()
</script>

<template>
  <div class="detail-row">
    <dt>{{ label }}</dt>
    <dd><slot>{{ value ?? '未记录' }}</slot></dd>
  </div>
</template>

<style scoped>
.detail-row {
  display: grid;
  grid-template-columns: minmax(5.5rem, 8rem) minmax(0, 1fr);
  gap: 0.75rem;
  padding: 0.375rem 0;
}

.detail-row dt {
  color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity));
  font-size: 0.8125rem;
}

.detail-row dd {
  min-width: 0;
  margin: 0;
  overflow-wrap: anywhere;
  color: rgb(var(--v-theme-on-surface));
  font-size: 0.875rem;
}

@media (max-width: 37.5rem) {
  .detail-row {
    grid-template-columns: 1fr;
    gap: 0.125rem;
  }
}
</style>
