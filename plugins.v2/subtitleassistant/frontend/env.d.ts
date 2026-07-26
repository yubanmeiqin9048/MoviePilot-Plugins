/// <reference types="vite/client" />

declare module '*.vue' {
  import type { DefineComponent } from 'vue'

  const component: DefineComponent<Record<string, unknown>, Record<string, unknown>, unknown>
  export default component
}

declare module 'virtual:__federation__' {
  export function __federation_method_setRemote(name: string, config: unknown): void
  export function __federation_method_getRemote(name: string, path: string): Promise<unknown>
  export function __federation_method_unwrapDefault(module: unknown): unknown
}
