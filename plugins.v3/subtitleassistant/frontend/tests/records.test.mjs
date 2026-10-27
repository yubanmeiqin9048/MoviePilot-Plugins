import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { test } from 'node:test'
import vm from 'node:vm'
import { compileScript, parse } from '@vue/compiler-sfc'
import ts from 'typescript'
import * as vue from 'vue'

const require = createRequire(import.meta.url)
const clientModule = { exports: {} }
vm.runInNewContext(ts.transpileModule(
  readFileSync(new URL('../src/api/client.ts', import.meta.url), 'utf8'),
  { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } },
).outputText, { exports: clientModule.exports, module: clientModule })
const client = clientModule.exports

// 执行真实组件 setup，用内存 API 替换网络和界面依赖。
function setupComponent(file, api, extraProps = {}) {
  const { descriptor } = parse(readFileSync(new URL(`../src/${file}`, import.meta.url), 'utf8'))
  const script = compileScript(descriptor, { id: 'records-test' })
  const code = ts.transpileModule(script.content, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText
  const events = []
  const props = vue.reactive({ api: {}, pluginId: 'SubtitleAssistant', active: true, ...extraProps })
  const module = { exports: {} }
  vm.runInNewContext(code, {
    exports: module.exports,
    module,
    require: name => {
      if (name === 'vue') return { ...vue, useId: () => 'test' }
      if (name === 'vuetify') return { useDisplay: () => ({ mdAndUp: vue.ref(true), smAndDown: vue.ref(false) }) }
      if (name === '@/api/client') return { ...client, ...api }
      if (name === '@/types') return { MAX_RECORD_BATCH_SIZE: 100 }
      if (name.endsWith('useDebouncedValue')) return { useDebouncedValue: value => value }
      if (name.endsWith('useStickyTableClipping')) return { useStickyTableClipping() {}, useStickyContentClipping() {} }
      if (name.endsWith('useDialogFocusReturn')) return { useDialogFocusReturn: () => ({ captureFocus() {}, restoreFocus() {} }) }
      if (name.startsWith('@/')) return {}
      return require(name)
    },
  })
  const scope = vue.effectScope()
  const state = scope.run(() => module.exports.default.setup(props, { expose() {}, emit: (...args) => events.push(args) }))
  return { state, props, events, stop: () => scope.stop() }
}

const record = id => ({ id, subtitle_file_name: `${id}.srt`, status: 'matched', path: `/${id}.srt`, location: 'media_directory', updated_at: '2026-10-27T00:00:00Z' })
const settle = () => new Promise(resolve => setImmediate(resolve))

test('跨页选择超过上限、取消末页后删除，回退至剩余记录所在页', async () => {
  let records = Array.from({ length: 110 }, (_, i) => record(String(i)))
  const requests = []
  const component = setupComponent('views/RecordsView.vue', {
    listRecords: async (_api, _id, { page, pageSize }) => {
      requests.push(page)
      return { items: records.slice((page - 1) * pageSize, page * pageSize), total: records.length }
    },
  })
  try {
    await settle()
    const s = component.state
    for (let page = 1; page <= 5; page++) {
      await s.loadPage({ requestedPage: page })
      s.setCurrentPageSelected(true)
    }
    assert.equal(s.selectedCount.value, 110)
    assert.ok(s.selectionLimitError.value)
    s.setCurrentPageSelected(false)
    assert.equal(s.selectedCount.value, 100)
    s.openBatchDelete()
    const deleted = s.batchDeleteSessionRecords.value.map(item => item.id)
    records = records.filter(item => !deleted.includes(item.id))
    await s.handleBatchDeleteComplete({ items: deleted.map(record_id => ({ record_id, status: 'success' })) }, [])
    assert.equal(s.page.value, 1)
    assert.equal(s.items.value.length, 10)
    assert.equal(s.total.value, 10)
    assert.equal(s.selectedCount.value, 0)
    assert.deepEqual(requests.slice(-2), [5, 1])
    records.push(record('new'))
    const refresh = s.loadPage({ silent: true })
    assert.equal(s.refreshing.value, true)
    await refresh
    assert.equal(s.items.value.length, 11)
    assert.equal(s.refreshing.value, false)
  } finally { component.stop() }
})

test('共享文件预检失败显示具体原因，允许更换删除范围，不误报刷新可解决', async () => {
  const component = setupComponent('components/BatchRecordDeleteDialog.vue', {
    deleteBatchRecords: async () => { throw { response: { status: 409, data: { detail: { items: [
      { record_id: '1', executable: false, error_code: 'shared_record_file', message: '当前字幕文件被其他匹配记录引用，不能安全删除' },
    ] } } } } },
  }, { modelValue: false, records: [record('1')] })
  try {
    component.props.modelValue = true
    await vue.nextTick()
    component.state.mode.value = 'record_and_file'
    await component.state.submit()
    assert.match(component.state.generalError.value, /1.srt：当前字幕文件被其他匹配记录引用/)
    assert.equal(component.events.length, 0)
    component.state.mode.value = 'record_only'
    assert.equal(component.state.canSubmit.value, true)
  } finally { component.stop() }
})

test('记录版本变化时保留预检原因并要求刷新重选', async () => {
  const component = setupComponent('components/BatchRecordDeleteDialog.vue', {
    deleteBatchRecords: async () => { throw { response: { status: 409, data: { detail: { items: [
      { record_id: '1', executable: false, error_code: 'record_version_conflict', message: '匹配记录已发生变化，请刷新后重新确认删除' },
    ] } } } } },
  }, { modelValue: false, records: [record('1')] })
  try {
    component.props.modelValue = true
    await vue.nextTick()
    component.state.mode.value = 'record_only'
    await component.state.submit()
    assert.equal(component.events[0][0], 'refresh-required')
    assert.match(component.events[0][1], /1.srt：匹配记录已发生变化/)
    assert.equal(component.events[1][0], 'update:modelValue')
    assert.equal(component.events[1][1], false)
  } finally { component.stop() }
})

for (const wrapper of ['message', 'detail']) {
  test(`宿主将预检结果序列化到 ${wrapper} 时仍显示具体原因并保留删除范围选择`, async () => {
    const detail = { code: 'batch_preflight_failed', message: '批量删除预检未通过', items: [
      { record_id: 'ok', executable: true, error_code: null, message: null },
      { record_id: 'shared', executable: false, error_code: 'shared_record_file', message: '当前字幕文件被其他匹配记录引用，不能安全删除' },
    ] }
    const error = { response: { status: 409, data: { success: false, [wrapper]: JSON.stringify(detail) } } }
    assert.equal(client.getErrorMessage(error, 'fallback'), detail.message)
    assert.equal(client.getErrorCode(error), detail.code)
    const component = setupComponent('components/BatchRecordDeleteDialog.vue', {
      deleteBatchRecords: async () => { throw error },
    }, { modelValue: false, records: [record('ok'), record('shared')] })
    try {
      component.props.modelValue = true
      await vue.nextTick()
      component.state.mode.value = 'record_and_file'
      await component.state.submit()
      assert.match(component.state.generalError.value, /shared.srt：当前字幕文件被其他匹配记录引用/)
      assert.match(component.state.generalError.value, /删除记录，保留当前字幕文件/)
      assert.doesNotMatch(component.state.generalError.value, /ok.srt|executable|列表已刷新/)
      assert.equal(component.events.length, 0)
    } finally { component.stop() }
  })
}

test('普通文本错误及验证错误保持可读', () => {
  for (const message of ['普通错误', '{非 JSON']) {
    assert.equal(client.getErrorMessage({ response: { data: { message } } }, 'fallback'), message)
  }
  assert.equal(client.getErrorMessage({ response: { data: { detail: [{ msg: '参数必填' }] } } }, 'fallback'), '参数必填')
})
