const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const test = require('node:test')
const { createRequire } = require('node:module')
const { compileScript, parse } = require('@vue/compiler-sfc')
const ts = require('typescript')
const vue = require('vue')

const root = path.resolve(__dirname, '..')
const loaded = new Map()

// 使用真实 Vue 编译及响应式运行时，仅替换宿主组件与浏览器渲染边界。
function loadModule(filename) {
  if (loaded.has(filename)) return loaded.get(filename)
  const source = fs.readFileSync(filename, 'utf8')
  const content = filename.endsWith('.vue')
    ? compileScript(parse(source, { filename }).descriptor, { id: 'page-test', inlineTemplate: true }).content
    : source
  const output = ts.transpileModule(content, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText
  const module = { exports: {} }
  loaded.set(filename, module.exports)
  const localRequire = createRequire(filename)
  new Function('require', 'module', 'exports', output)((name) => {
    if (name.startsWith('@/')) return loadModule(path.join(root, 'src', `${name.slice(2)}.ts`))
    return localRequire(name)
  }, module, module.exports)
  loaded.set(filename, module.exports)
  return module.exports
}

function element(type, text = '') {
  return { type, text, props: {}, children: [], parent: null }
}

const renderer = vue.createRenderer({
  createElement: element,
  createText: (text) => element('text', text),
  createComment: (text) => element('comment', text),
  setText: (node, text) => { node.text = text },
  setElementText: (node, text) => { node.text = text; node.children = [] },
  patchProp: (node, key, _previous, value) => { node.props[key] = value },
  insert(node, parent, anchor = null) {
    if (node.parent) this.remove(node)
    const index = parent.children.indexOf(anchor)
    parent.children.splice(index < 0 ? parent.children.length : index, 0, node)
    node.parent = parent
  },
  remove(node) {
    const index = node.parent?.children.indexOf(node) ?? -1
    if (index >= 0) node.parent.children.splice(index, 1)
    node.parent = null
  },
  parentNode: (node) => node.parent,
  nextSibling: (node) => node.parent?.children[node.parent.children.indexOf(node) + 1] ?? null,
})

function text(node) {
  return node.type === 'comment' ? '' : node.text + node.children.map(text).join('')
}

function all(node, predicate) {
  return [...(predicate(node) ? [node] : []), ...node.children.flatMap((child) => all(child, predicate))]
}

async function settle() {
  await new Promise(setImmediate)
  await vue.nextTick()
}

function mountPage(api) {
  const pagePath = path.join(root, 'src/components/Page.vue')
  const app = renderer.createApp(loadModule(pagePath).default, { api, pluginId: 'Alist2Strm-test' })
  const componentNames = new Set(fs.readFileSync(pagePath, 'utf8').match(/\bV[A-Z][A-Za-z]+/g))
  for (const name of componentNames) {
    app.component(name, {
      inheritAttrs: false,
      setup(_props, { attrs, slots }) {
        return () => vue.h(name === 'VBtn' ? 'button' : 'div', attrs, slots.default?.())
      },
    })
  }
  const rootNode = element('root')
  app.mount(rootNode)
  return { rootNode, unmount: () => app.unmount() }
}

function mountConfig(api, initialConfig = {}) {
  const configPath = path.join(root, 'src/components/Config.vue')
  const app = renderer.createApp(loadModule(configPath).default, {
    api: { get: async () => ({ success: true, data: initialConfig }), ...api },
    initialConfig,
    pluginId: 'Alist2Strm-test',
  })
  const componentNames = new Set(fs.readFileSync(configPath, 'utf8').match(/\bV[A-Z][A-Za-z]+/g))
  for (const name of componentNames) {
    app.component(name, {
      inheritAttrs: false,
      setup(_props, { attrs, slots }) {
        return () => vue.h(name === 'VBtn' ? 'button' : 'div', attrs, slots.default?.())
      },
    })
  }
  const rootNode = element('root')
  app.mount(rootNode)
  return { rootNode, unmount: () => app.unmount() }
}

test('Config reads current default extensions on every reopen without saving', async () => {
  // 宿主浅合并持久化配置后，实例内的展示字段会从 initialConfig 中丢失。
  const initialConfig = { instances: [{
    id: 'saved-instance', name: '已保存实例', scan_url: 'https://openlist.example.invalid',
    mappings: [{
      source_dir: '/media', output_dir: '/strm', whitelist_mode: 'follow_host',
      extensions: [], exclude_keywords: [],
    }],
  }] }
  const calls = []
  let puts = 0
  for (const extensions of [['.mkv', '.srt'], ['.mp4', '.ass']]) {
    const config = mountConfig({
      async get(url) {
        calls.push(url)
        return { success: true, data: { instances: [{
          ...initialConfig.instances[0], api_token_configured: true,
          mappings: [{ ...initialConfig.instances[0].mappings[0], effective_extensions: extensions }],
        }] } }
      },
      async put() { puts += 1 },
    }, initialConfig)
    try {
      await settle()
      assert.ok(text(config.rootNode).includes(extensions.join('  ')))
      assert.equal(button(config.rootNode, '保存').props.disabled, true)
      assert.equal(all(config.rootNode, (node) => node.props.label === '管理员 API Token')[0].props.placeholder,
        '已配置，留空保持不变')
    } finally {
      config.unmount()
    }
  }
  assert.deepEqual(calls, ['plugin/Alist2Strm-test/config', 'plugin/Alist2Strm-test/config'])
  assert.equal(puts, 0)
})

test('Config retains edits and a completed save when the initial read arrives late', async () => {
  const initialConfig = { instances: [{ id: 'saved-instance', name: '原名称', mappings: [] }] }
  for (const saveBeforeRead of [false, true]) {
    let finishRead
    const config = mountConfig({
      get: () => new Promise((resolve) => { finishRead = resolve }),
      put: async (_url, payload) => ({ success: true, data: payload }),
    }, initialConfig)
    try {
      const name = all(config.rootNode, (node) => node.props.label === '名称')[0]
      name.props['onUpdate:modelValue']('新名称')
      await settle()
      assert.equal(button(config.rootNode, '保存').props.disabled, false)
      if (saveBeforeRead) await click(button(config.rootNode, '保存'))
      finishRead({ success: true, data: initialConfig })
      await settle()
      assert.equal(all(config.rootNode, (node) => node.props.label === '名称')[0].props.modelValue, '新名称')
      assert.equal(button(config.rootNode, '保存').props.disabled, saveBeforeRead)
    } finally {
      config.unmount()
    }
  }
})

test('Config retains the initial form and reports a configuration read failure', async () => {
  const config = mountConfig({ get: async () => { throw new Error('配置读取失败，请重新打开设置。') } },
    { instances: [{ id: 'saved-instance', name: '已保存实例', mappings: [] }] })
  try {
    await settle()
    assert.match(text(config.rootNode), /配置读取失败，请重新打开设置/)
    assert.equal(all(config.rootNode, (node) => node.props.label === '名称')[0].props.modelValue, '已保存实例')
    assert.equal(button(config.rootNode, '保存').props.disabled, true)
  } finally {
    config.unmount()
  }
})

test('Config edits all pruning times and retains the draft on save failure', async () => {
  const calls = []
  let fail = true
  const config = mountConfig({
    async put(url, payload) {
      calls.push({ url, payload })
      if (fail) throw new Error('保存失败，请重试')
      return { success: true, data: payload }
    },
  })
  try {
    await click(button(config.rootNode, '通用设置'))
    const fields = all(config.rootNode, (node) => node.props.suffix === '天' && node.props.hint)
    assert.deepEqual(fields.map((node) => node.props.modelValue), [30, 7, 90, 7, 365, 30, 90])
    const values = [5, 3, 12, 2, 20, 4, 6]
    fields.forEach((node, index) => node.props['onUpdate:modelValue'](values[index]))
    await settle()
    await click(button(config.rootNode, '保存'))
    assert.match(text(config.rootNode), /保存失败，请重试/)
    assert.deepEqual(fields.map((node) => node.props.modelValue), values)
    fail = false
    await click(button(config.rootNode, '保存'))
    assert.deepEqual(calls[1], {
      url: 'plugin/Alist2Strm-test/config',
      payload: {
        instances: [], schedule_enabled: false, schedule_cron: '0 3 * * *', history_retention_days: 90,
        time_pruning: {
          recent_days: 5, cold_after_days: 12, archive_after_days: 20,
          stale_recheck_days: 2, cold_recheck_days: 4, archive_recheck_days: 6, empty_recheck_days: 3,
        },
      },
    })
    assert.equal(button(config.rootNode, '保存').props.disabled, true)
  } finally {
    config.unmount()
  }
})

test('Config loads saved pruning times and prevents saving invalid days or ordering', async () => {
  let puts = 0
  const config = mountConfig({ put: async () => { puts += 1 } }, { time_pruning: {
    recent_days: 5, cold_after_days: 12, archive_after_days: 20,
    stale_recheck_days: 2, cold_recheck_days: 4, archive_recheck_days: 6, empty_recheck_days: 3,
  } })
  try {
    await click(button(config.rootNode, '通用设置'))
    const fields = all(config.rootNode, (node) => node.props.suffix === '天' && node.props.hint)
    assert.deepEqual(fields.map((node) => node.props.modelValue), [5, 3, 12, 2, 20, 4, 6])
    for (const [index, value] of [[0, 0], [0, 1.5], [0, ''], [0, 12], [3, 5], [6, 3]]) {
      const previous = fields[index].props.modelValue
      fields[index].props['onUpdate:modelValue'](value)
      await settle()
      assert.equal(button(config.rootNode, '保存').props.disabled, true)
      assert.ok(fields.some((node) => node.props['error-messages']))
      fields[index].props['onUpdate:modelValue'](previous)
      await settle()
    }
    assert.equal(puts, 0)
  } finally {
    config.unmount()
  }
})

const idle = {
  state: 'idle', status: 'idle', running: false, current_mapping: null, total_mappings: 0,
  counters: { generated: 0, downloaded: 0, updated: 0, skipped: 0, deleted: 0, failed: 0 },
  mappings: [], started_at: null, finished_at: null,
}

function apiFixture(post) {
  return {
    async get(url) {
      if (url.endsWith('/config')) return { success: true, data: {
        instances: [{ id: 'saved-instance', name: '已保存实例', mappings: [
          { source_dir: '/saved-one', output_dir: '/output-one' },
          { source_dir: '/saved-two', output_dir: '/output-two' },
        ] }],
      } }
      if (url.endsWith('/scan/status')) return idle
      throw new Error(`Unexpected GET: ${url}`)
    },
    post,
  }
}

function button(rootNode, label, index = 0) {
  const found = all(rootNode, (node) => node.type === 'button' && text(node).trim() === label)[index]
  assert.ok(found, `按钮不存在：${label}`)
  return found
}

async function click(node) {
  assert.ok(!node.props.disabled, '按钮应可操作')
  node.props.onClick()
  await settle()
}

test('Page distinguishes normal and force-complete clicks for the selected saved mapping', async () => {
  const previousWindow = global.window
  global.window = { setInterval: () => 1, clearInterval() {} }
  const calls = []
  const page = mountPage(apiFixture(async (url, payload) => {
    calls.push({ url, payload })
    return { accepted: true, message: '扫描已在后台启动' }
  }))
  try {
    await settle()
    await click(button(page.rootNode, '运行此映射', 1))
    await click(button(page.rootNode, '强制完整扫描', 1))
    await click(button(page.rootNode, '运行全部'))
    assert.deepEqual(calls, [
      { url: 'plugin/Alist2Strm-test/scan/mapping', payload: {
        scan_mode: 'normal', target: { instance_id: 'saved-instance', mapping_index: 2, source_dir: '/saved-two' },
      } },
      { url: 'plugin/Alist2Strm-test/scan/mapping', payload: {
        scan_mode: 'force_complete', target: { instance_id: 'saved-instance', mapping_index: 2, source_dir: '/saved-two' },
      } },
      { url: 'plugin/Alist2Strm-test/scan', payload: { scan_mode: 'normal' } },
    ])
    assert.match(text(page.rootNode), /仅绕过时间剪枝/)
    assert.match(text(page.rootNode), /已有文件跳过及播放地址更新规则仍生效/)
  } finally {
    page.unmount()
    global.window = previousWindow
  }
})

test('Page disables both scan modes while pending or running and reports a rejected duplicate', async () => {
  const previousWindow = global.window
  global.window = { setInterval: () => 1, clearInterval() {} }
  let completeRequest
  let posts = 0
  const api = apiFixture(() => {
    posts += 1
    return new Promise((resolve) => { completeRequest = resolve })
  })
  const page = mountPage(api)
  try {
    await settle()
    await click(button(page.rootNode, '强制完整扫描', 1))
    for (const label of ['运行全部', '运行此映射', '强制完整扫描']) {
      for (const node of all(page.rootNode, (node) => node.type === 'button' && text(node).trim() === label)) {
        assert.equal(node.props.disabled, true)
      }
    }
    assert.equal(posts, 1)
    const originalGet = api.get
    api.get = async (url) => url.endsWith('/scan/status') ? { ...idle, state: 'running', running: true } : originalGet(url)
    completeRequest({ accepted: false, message: '已有扫描任务正在运行' })
    await settle()
    assert.match(text(page.rootNode), /已有扫描任务正在运行/)
    assert.equal(button(page.rootNode, '运行此映射').props.disabled, true)
    assert.equal(button(page.rootNode, '强制完整扫描').props.disabled, true)
  } finally {
    page.unmount()
    global.window = previousWindow
  }
})

test('Page reports a force-complete request failure and restores the controls', async () => {
  const previousWindow = global.window
  global.window = { setInterval: () => 1, clearInterval() {} }
  const page = mountPage(apiFixture(async () => { throw new Error('请求失败') }))
  try {
    await settle()
    await click(button(page.rootNode, '强制完整扫描'))
    assert.match(text(page.rootNode), /启动扫描失败：请求失败/)
    assert.equal(button(page.rootNode, '强制完整扫描').props.disabled, false)
  } finally {
    page.unmount()
    global.window = previousWindow
  }
})
