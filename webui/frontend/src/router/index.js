import { createRouter, createWebHashHistory } from 'vue-router'
import NProgress from 'nprogress'

NProgress.configure({ showSpinner: false, trickleSpeed: 120, minimum: 0.15 })

// hash 路由：不依赖后端做 SPA 回退，FastAPI / 未来 Gin 都零配置可用。
const routes = [
  {
    path: '/',
    name: 'dashboard',
    component: () => import('@/views/Dashboard.vue'),
    meta: { title: '仪表盘', icon: 'Odometer', group: '概览' },
  },
  {
    path: '/import',
    name: 'import',
    component: () => import('@/views/Import.vue'),
    meta: { title: '导入邮箱', icon: 'Upload', group: '注册' },
  },
  {
    path: '/register',
    name: 'register',
    component: () => import('@/views/Register.vue'),
    meta: { title: '单次注册', icon: 'VideoPlay', group: '注册' },
  },
  {
    path: '/auto',
    name: 'auto',
    component: () => import('@/views/AutoLoop.vue'),
    meta: { title: '全自动批量', icon: 'MagicStick', group: '注册' },
  },
  {
    path: '/proxy',
    name: 'proxy',
    component: () => import('@/views/ProxyPool.vue'),
    meta: { title: '代理池', icon: 'Connection', group: '注册' },
  },
  {
    path: '/pool',
    name: 'pool',
    component: () => import('@/views/Pool.vue'),
    meta: { title: '邮箱列表', icon: 'Files', group: '数据' },
  },
  {
    path: '/registered',
    name: 'registered',
    component: () => import('@/views/Registered.vue'),
    meta: { title: '注册结果', icon: 'CircleCheck', group: '数据' },
  },
  {
    path: '/runs',
    name: 'runs',
    component: () => import('@/views/Runs.vue'),
    meta: { title: '运行记录', icon: 'Document', group: '数据' },
  },
  {
    path: '/settings/mail',
    name: 'mail',
    component: () => import('@/views/MailConfig.vue'),
    meta: { title: '邮箱配置', icon: 'Message', group: '配置' },
  },
  {
    path: '/settings/sms',
    name: 'sms',
    component: () => import('@/views/SmsConfig.vue'),
    meta: { title: '接码配置', icon: 'Iphone', group: '配置' },
  },
  {
    path: '/settings/export',
    name: 'export',
    component: () => import('@/views/ExportConfig.vue'),
    meta: { title: '自动导出', icon: 'Share', group: '配置' },
  },
  {
    path: '/settings/webhook',
    name: 'webhook',
    component: () => import('@/views/WebhookConfig.vue'),
    meta: { title: 'Webhook', icon: 'Bell', group: '配置' },
  },
]

const router = createRouter({
  history: createWebHashHistory(),
  routes,
})

// 路由切换顶部进度条
router.beforeEach((to, from, next) => {
  NProgress.start()
  if (to.meta?.title) document.title = `${to.meta.title} · Outlook Register`
  next()
})
router.afterEach(() => {
  NProgress.done()
})

export default router
