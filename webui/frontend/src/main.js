import { createApp } from 'vue'
import { createPinia } from 'pinia'

// Keep the full CSS bundle so programmatic components such as ElMessage and
// ElMessageBox are styled; unplugin tree-shakes JavaScript imports.
import 'element-plus/dist/index.css'
import 'element-plus/theme-chalk/dark/css-vars.css'
import './styles/theme.css'
import 'nprogress/nprogress.css'

// Register only icons in use. Dynamic <component :is="name"> values require global registration.
import {
  Platform, Fold, Expand, Moon, Sunny, User, ArrowDown,
  Odometer, Upload, VideoPlay, MagicStick, Connection, Files,
  CircleCheck, Document, Message, Iphone, Share,
  Loading, Select, CircleClose, Refresh, CopyDocument,
  Bell, Close, Download, Delete,
} from '@element-plus/icons-vue'

import App from './App.vue'
import router from './router'

const ICONS = {
  Platform, Fold, Expand, Moon, Sunny, User, ArrowDown,
  Odometer, Upload, VideoPlay, MagicStick, Connection, Files,
  CircleCheck, Document, Message, Iphone, Share,
  Loading, Select, CircleClose, Refresh, CopyDocument,
  Bell, Close, Download, Delete,
}

const app = createApp(App)
for (const [name, comp] of Object.entries(ICONS)) app.component(name, comp)

app.use(createPinia())
app.use(router)
app.mount('#app')
