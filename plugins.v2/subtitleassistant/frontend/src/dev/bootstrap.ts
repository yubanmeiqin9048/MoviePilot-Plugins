import { createApp } from 'vue'

import { vuetify } from '@/plugins/vuetify'
import DevApp from './DevApp.vue'

import './dev.css'

createApp(DevApp).use(vuetify).mount('#app')
