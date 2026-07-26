import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import federation from '@originjs/vite-plugin-federation'

const runtimeProcess = (globalThis as typeof globalThis & {
  process?: { env?: Record<string, string | undefined> }
}).process
const devOrigin = runtimeProcess?.env?.SUBTITLE_ASSISTANT_DEV_ORIGIN || 'http://localhost:5173'

export default defineConfig(({ command }) => ({
  plugins: [
    vue(),
    federation({
      name: 'SubtitleAssistant',
      filename: 'remoteEntry.js',
      exposes: {
        './AppPage': './src/components/AppPage.vue',
        './Config': './src/components/Config.vue',
      },
      shared: {
        vue: {
          requiredVersion: false,
          generate: false,
          singleton: true,
        },
        vuetify: {
          requiredVersion: false,
          generate: false,
          singleton: true,
        },
        'vuetify/styles': {
          requiredVersion: false,
          generate: false,
          singleton: true,
        },
      },
      format: 'esm',
    }),
  ],
  resolve: {
    alias: {
      '@': '/src',
    },
  },
  build: {
    target: 'esnext',
    cssCodeSplit: true,
    outDir: 'dist',
    emptyOutDir: true,
    rollupOptions: {
      output: {
        chunkFileNames: chunkInfo =>
          chunkInfo.name.startsWith('ConfirmDialog')
            ? 'assets/__federation_[name]-[hash].js'
            : 'assets/[name]-[hash].js',
        assetFileNames: assetInfo =>
          (assetInfo.name ?? '').startsWith('ConfirmDialog')
            ? 'assets/__federation_[name]-[hash][extname]'
            : 'assets/[name]-[hash][extname]',
      },
    },
  },
  css: {
    postcss: {
      plugins: [
        {
          postcssPlugin: 'internal:charset-removal',
          AtRule: {
            charset: (atRule: { remove: () => void }) => atRule.remove(),
          },
        },
        {
          postcssPlugin: 'vuetify-filter',
          Root(root: {
            walkRules: (callback: (rule: { selector?: string; remove: () => void }) => void) => void
          }) {
            // Vuetify CSS belongs to the host. Keep it in the standalone shell during dev.
            if (command !== 'build') return
            root.walkRules(rule => {
              if (rule.selector && (rule.selector.includes('.v-') || rule.selector.includes('.mdi-'))) {
                rule.remove()
              }
            })
          },
        },
      ],
    },
  },
  server: {
    host: 'localhost',
    port: 5173,
    strictPort: true,
    cors: true,
    origin: devOrigin,
  },
}))
