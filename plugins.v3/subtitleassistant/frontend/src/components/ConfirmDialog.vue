<script setup lang="ts">
const props = withDefaults(defineProps<{
  modelValue: boolean
  title: string
  message: string
  confirmText?: string
  loading?: boolean
}>(), {
  confirmText: '确认删除',
  loading: false,
})

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  'confirm': []
}>()

function close(): void {
  if (!props.loading) emit('update:modelValue', false)
}
</script>

<template>
  <VDialog
    :model-value="modelValue"
    max-width="30rem"
    :persistent="loading"
    @update:model-value="value => emit('update:modelValue', value)"
  >
    <VCard>
      <VCardTitle class="dialog-title">
        <VIcon icon="mdi-alert-outline" color="error" size="22" />
        <span>{{ title }}</span>
      </VCardTitle>
      <VCardText class="dialog-message">{{ message }}</VCardText>
      <VCardActions>
        <VSpacer />
        <VBtn variant="text" color="default" :disabled="loading" @click="close">取消</VBtn>
        <VBtn
          color="error"
          variant="flat"
          prepend-icon="mdi-delete-outline"
          :loading="loading"
          @click="emit('confirm')"
        >
          {{ confirmText }}
        </VBtn>
      </VCardActions>
    </VCard>
  </VDialog>
</template>

<style scoped>
.dialog-title {
  display: flex;
  align-items: center;
  gap: 0.625rem;
  font-size: 1rem;
  letter-spacing: 0;
}

.dialog-message {
  color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity));
  font-size: 0.875rem;
  line-height: 1.65;
}
</style>
