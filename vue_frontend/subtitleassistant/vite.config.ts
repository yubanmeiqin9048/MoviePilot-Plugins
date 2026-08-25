import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import federation from '@originjs/vite-plugin-federation'

const runtimeProcess = (globalThis as typeof globalThis & {
  process?: { env?: Record<string, string | undefined> }
}).process

/**
 * `server.origin` 把静态资源改写成绝对 URL。只有「MoviePilot 宿主通过 module federation
 * 远程加载本插件」才需要它 —— 那时相对 URL 会解析到宿主 origin 而不是本 dev server。
 *
 * 独立开发验收壳是同源加载，设了反而有害：端口一变，资源仍被请求到旧端口；若那个端口上
 * 正跑着别的服务（例如 MoviePilot 自己的前端），它会对字体路径返回 200 的 HTML，于是三种
 * 字体格式都「下载成功但解析失败」，表现为全部图标静默变空白，且不像 404。
 *
 * 所以默认不设，只在显式提供 SUBTITLE_ASSISTANT_DEV_ORIGIN 时启用。
 */
const devOrigin = runtimeProcess?.env?.SUBTITLE_ASSISTANT_DEV_ORIGIN

interface DevServerLike {
  httpServer?: { once: (event: string, listener: () => void) => void; address: () => unknown } | null
  config: { logger: { warn: (message: string, options?: { timestamp?: boolean }) => void } }
}

/** origin 与实际监听端口不一致时静态资源会打到别的服务器；把静默失败换成显式警告。 */
function warnOnOriginPortMismatch(origin: string | undefined) {
  return {
    name: 'subtitleassistant:dev-origin-check',
    apply: 'serve' as const,
    configureServer(server: DevServerLike) {
      if (!origin) return
      server.httpServer?.once('listening', () => {
        const address = server.httpServer?.address()
        const actual = address && typeof address === 'object' && 'port' in address
          ? Number((address as { port: number }).port)
          : undefined
        let expected: number | undefined
        try {
          expected = Number(new URL(origin).port) || undefined
        } catch {
          server.config.logger.warn(
            `[subtitleassistant] SUBTITLE_ASSISTANT_DEV_ORIGIN=${origin} 不是合法 URL，已按未设置处理。`,
            { timestamp: true },
          )
          return
        }
        if (actual != null && expected != null && actual !== expected) {
          server.config.logger.warn(
            `[subtitleassistant] SUBTITLE_ASSISTANT_DEV_ORIGIN=${origin} 与实际监听端口 ${actual} 不一致：`
            + '静态资源会被请求到前者，字体与图标可能全部空白。独立验收壳无需设置该变量。',
            { timestamp: true },
          )
        }
      })
    },
  }
}

export default defineConfig(({ command }) => ({
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
    warnOnOriginPortMismatch(devOrigin),
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
    // 5173 只是首选端口。不锁死端口：被占用时 Vite 自动换一个，资源用相对 URL 仍然正确，
    // 实际地址看 Vite 启动输出即可。
    port: 5173,
    cors: true,
    ...(devOrigin ? { origin: devOrigin } : {}),
  },
}))
