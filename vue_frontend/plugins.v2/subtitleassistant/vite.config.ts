import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import federation from '@originjs/vite-plugin-federation'

export default defineConfig({
  // node_modules is shared with the main worktree; keep Vite's mutable cache local.
  cacheDir: '.vite',
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
      // 本插件只作为联邦远程运行：宿主通过 remoteEntry.js 加载 AppPage 与 Config，
      // 没有可独立打开的页面。远端暴露块由 federation 插件自己发射，因此这里给一个
      // 空入口集合即可，避免产出一个永远不会被加载的页面 chunk。
      input: {},
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
            /*
             * Vuetify CSS belongs to the host. remoteEntry injects this artifact's CSS into
             * document.head, so an unanchored `.v-`/`.mdi-` rule would restyle the host's own
             * components — the federation guide drops every one of them for that reason.
             *
             * A component's `:deep(.v-…)` override cannot reach the host: after compilation it
             * is anchored by that component's `[data-v-…]` attribute. Removing those shipped a
             * build whose layout silently differed from the host, so they stay.
             * Both spellings are accepted because this plugin runs before Vue's scoped
             * transform today — matching the compiled form too keeps the rules alive if that
             * order ever changes, instead of silently dropping them again.
             *
             * Filter per comma-separated part so a mixed list can't smuggle a bare `.v-`
             * selector through on the back of an anchored one.
             */
            root.walkRules(rule => {
              if (!rule.selector) return
              const parts = rule.selector.split(',')
              const kept = parts.filter(part => {
                const touchesVuetify = part.includes('.v-') || part.includes('.mdi-')
                const scopedToThisPlugin = part.includes(':deep(') || part.includes('[data-v-')
                return !touchesVuetify || scopedToThisPlugin
              })
              if (kept.length === parts.length) return
              if (kept.length) rule.selector = kept.join(',')
              else rule.remove()
            })
          },
        },
      ],
    },
  },
})
