import { createRouter, createWebHashHistory } from 'vue-router'
import NProgress from 'nprogress'

NProgress.configure({ showSpinner: false, trickleSpeed: 120, minimum: 0.15 })

// Hash routing avoids requiring an SPA fallback from FastAPI or a future Gin backend.
const routes = [
  {
    path: '/',
    name: 'dashboard',
    component: () => import('@/views/Dashboard.vue'),
    meta: { title: 'Dashboard', icon: 'Odometer', group: 'Overview' },
  },
  {
    path: '/import',
    name: 'import',
    component: () => import('@/views/Import.vue'),
    meta: { title: 'Import mailboxes', icon: 'Upload', group: 'Registration' },
  },
  {
    path: '/register',
    name: 'register',
    component: () => import('@/views/Register.vue'),
    meta: { title: 'Single registration', icon: 'VideoPlay', group: 'Registration' },
  },
  {
    path: '/auto',
    name: 'auto',
    component: () => import('@/views/AutoLoop.vue'),
    meta: { title: 'Automatic batch', icon: 'MagicStick', group: 'Registration' },
  },
  {
    path: '/proxy',
    name: 'proxy',
    component: () => import('@/views/ProxyPool.vue'),
    meta: { title: 'Proxy pool', icon: 'Connection', group: 'Registration' },
  },
  {
    path: '/pool',
    name: 'pool',
    component: () => import('@/views/Pool.vue'),
    meta: { title: 'Mailbox pool', icon: 'Files', group: 'Data' },
  },
  {
    path: '/registered',
    name: 'registered',
    component: () => import('@/views/Registered.vue'),
    meta: { title: 'Registered accounts', icon: 'CircleCheck', group: 'Data' },
  },
  {
    path: '/runs',
    name: 'runs',
    component: () => import('@/views/Runs.vue'),
    meta: { title: 'Runs', icon: 'Document', group: 'Data' },
  },
  {
    path: '/settings/mail',
    name: 'mail',
    component: () => import('@/views/MailConfig.vue'),
    meta: { title: 'Mailbox settings', icon: 'Message', group: 'Settings' },
  },
  {
    path: '/settings/sms',
    name: 'sms',
    component: () => import('@/views/SmsConfig.vue'),
    meta: { title: 'SMS settings', icon: 'Iphone', group: 'Settings' },
  },
  {
    path: '/settings/export',
    name: 'export',
    component: () => import('@/views/ExportConfig.vue'),
    meta: { title: 'Export settings', icon: 'Share', group: 'Settings' },
  },
]

const router = createRouter({
  history: createWebHashHistory(),
  routes,
})

// Top progress bar for route changes.
router.beforeEach((to, from, next) => {
  NProgress.start()
  if (to.meta?.title) document.title = `${to.meta.title} · Outlook Register`
  next()
})
router.afterEach(() => {
  NProgress.done()
})

export default router
