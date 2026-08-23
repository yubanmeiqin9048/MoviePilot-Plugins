import { createVuetify } from 'vuetify'
import * as components from 'vuetify/components'
import * as directives from 'vuetify/directives'
import '@mdi/font/css/materialdesignicons.css'
import 'vuetify/styles'

const themes = {
  light: {
    dark: false,
    colors: {
      background: '#f7f8fa',
      surface: '#ffffff',
      'surface-variant': '#eef1f4',
      primary: '#2f6f8f',
      secondary: '#66737d',
      success: '#2e7d5b',
      info: '#286f9e',
      warning: '#a16207',
      error: '#b42318',
    },
  },
  dark: {
    dark: true,
    colors: {
      background: '#111417',
      surface: '#191d21',
      'surface-variant': '#252b30',
      primary: '#76b4d0',
      secondary: '#aab7bf',
      success: '#7acaa3',
      info: '#82c4e4',
      warning: '#f3c46d',
      error: '#ff9b93',
    },
  },
  purple: {
    dark: true,
    colors: {
      background: '#201d2d',
      surface: '#2a263a',
      'surface-variant': '#39334d',
      primary: '#c0a6ff',
      secondary: '#c2b9d4',
      success: '#8bd1ad',
      info: '#9acaf1',
      warning: '#f1c879',
      error: '#ffaaa5',
    },
  },
  transparent: {
    dark: true,
    colors: {
      background: '#101214',
      surface: '#1b1e21',
      'surface-variant': '#2b3034',
      primary: '#a9cfe2',
      secondary: '#bac4ca',
      success: '#91d4b2',
      info: '#9bcce9',
      warning: '#f0c878',
      error: '#ffa9a2',
    },
  },
}

export const vuetify = createVuetify({
  components,
  directives,
  theme: {
    defaultTheme: 'light',
    themes,
  },
  defaults: {
    VBtn: { color: 'primary', elevation: 0 },
    VTextField: { variant: 'outlined', density: 'comfortable', hideDetails: 'auto' },
    VSelect: { variant: 'outlined', density: 'comfortable', hideDetails: 'auto' },
    VTextarea: { variant: 'outlined', density: 'comfortable', hideDetails: 'auto' },
    VTable: { density: 'comfortable' },
  },
})
