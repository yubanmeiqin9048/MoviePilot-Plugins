<script setup lang="ts">
import type { StatePresentation } from '@/types/presentation'

defineProps<{
  state: StatePresentation
  size?: 'x-small' | 'small' | 'default'
}>()
</script>

<template>
  <VChip
    :color="state.color"
    :size="size || 'small'"
    :prepend-icon="state.icon"
    variant="tonal"
    label
  >
    {{ state.label }}
  </VChip>
</template>
