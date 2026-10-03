import { reactive } from 'vue'

/** 配置弹窗与详情 Page 共享的浏览器草稿状态，不参与服务端保存。 */
export const configurationEditorState = reactive({ dirty: false })
