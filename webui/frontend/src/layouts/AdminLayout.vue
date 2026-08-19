<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { storeToRefs } from 'pinia'
import { useThemeStore } from '@/stores/theme'
import { useStatsStore } from '@/stores/stats'
import { useRuntimeStore } from '@/stores/runtime'

const route = useRoute()
const router = useRouter()
const theme = useThemeStore()
const statsStore = useStatsStore()
const runtime = useRuntimeStore()
const { stats } = storeToRefs(statsStore)
const { banner } = storeToRefs(runtime)

const collapse = ref(false)
const adDismissed = ref(false)

const GROUP_ORDER = ['Overview', 'Registration', 'Data', 'Settings']
const groups = computed(() => {
  const map = {}
  for (const r of router.getRoutes()) {
    if (!r.meta?.title) continue
    const g = r.meta.group || 'Other'
    ;(map[g] ||= []).push(r)
  }
  return GROUP_ORDER.filter((g) => map[g]).map((g) => ({ name: g, items: map[g] }))
})

const activeMenu = computed(() => route.path)
const crumb = computed(() => [route.meta.group, route.meta.title].filter(Boolean))

const menuOptions = computed(() =>
  router.getRoutes()
    .filter((r) => r.meta?.title)
    .map((r) => ({ value: r.path, label: `${r.meta.group} / ${r.meta.title}` })),
)
const search = ref('')
function onSearch(path) {
  if (path) router.push(path)
  search.value = ''
}

const statPills = computed(() => [
  { label: 'Total', value: stats.value.total, type: 'info' },
  { label: 'Available', value: stats.value.available, type: 'success' },
  { label: 'In progress', value: stats.value.in_use, type: 'warning' },
  { label: 'Completed', value: stats.value.done, type: 'primary' },
  { label: 'Failed', value: stats.value.failed, type: 'danger' },
])

onMounted(() => {
  theme.apply()
  statsStore.startPolling()
  runtime.connectAutoStream()
})
</script>
<template>
  <el-container class="admin">
    <el-aside :width="collapse ? '64px' : '220px'" class="sidebar">
      <div class="brand" :class="{ mini: collapse }">
        <span class="logo"><el-icon :size="18"><Platform /></el-icon></span>
        <span v-if="!collapse" class="brand-name">Outlook Register</span>
      </div>
      <el-scrollbar>
        <el-menu :default-active="activeMenu" router :collapse="collapse" class="side-menu">
          <el-menu-item-group v-for="grp in groups" :key="grp.name" :title="collapse ? '' : grp.name">
            <el-menu-item v-for="r in grp.items" :key="r.path" :index="r.path">
              <el-icon><component :is="r.meta.icon" /></el-icon>
              <template #title>{{ r.meta.title }}</template>
            </el-menu-item>
          </el-menu-item-group>
        </el-menu>
      </el-scrollbar>
    </el-aside>

    <el-container>
      <el-header class="topbar">
        <div class="left">
          <el-button text @click="collapse = !collapse">
            <el-icon :size="18"><Fold v-if="!collapse" /><Expand v-else /></el-icon>
          </el-button>
          <el-breadcrumb separator="/">
            <el-breadcrumb-item v-for="c in crumb" :key="c">{{ c }}</el-breadcrumb-item>
          </el-breadcrumb>
        </div>
        <div class="right">
          <el-select
            v-model="search" filterable clearable placeholder="Search features"
            size="small" class="search-box" @change="onSearch"
          >
            <el-option v-for="o in menuOptions" :key="o.value" :label="o.label" :value="o.value" />
          </el-select>
          <div class="pills">
            <el-tag v-for="p in statPills" :key="p.label" :type="p.type" size="small" effect="plain">
              {{ p.label }} <b>{{ p.value }}</b>
            </el-tag>
          </div>
          <el-tooltip :content="theme.dark ? 'Light mode' : 'Dark mode'">
            <el-button circle text @click="theme.toggle">
              <el-icon :size="18"><Moon v-if="!theme.dark" /><Sunny v-else /></el-icon>
            </el-button>
          </el-tooltip>
          <el-dropdown>
            <span class="avatar">
              <el-avatar :size="28" class="avatar-img"><el-icon><User /></el-icon></el-avatar>
              <span class="avatar-name">Administrator</span>
              <el-icon :size="12"><ArrowDown /></el-icon>
            </span>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item @click="theme.toggle">
                  {{ theme.dark ? 'Light mode' : 'Dark mode' }}
                </el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </el-header>

      <el-main class="content">
        <div v-if="!adDismissed" class="ad-banner">
          <div class="ad-content">
            <el-icon :size="16" style="color: #e6a23c; flex-shrink: 0"><Bell /></el-icon>
            <span>Community QQ group: <b>259844673</b></span>
            <span class="ad-sep">|</span>
            <span>Recommended hosting: <a href="http://www.ransuyun.com" target="_blank" rel="noopener">Ransuyun</a></span>
          </div>
          <el-button text size="small" class="ad-close" @click="adDismissed = true">
            <el-icon :size="14"><Close /></el-icon>
          </el-button>
        </div>
        <el-alert
          v-if="banner" :title="banner" type="error" show-icon
          class="circuit-banner" @close="runtime.dismissBanner"
        />
        <router-view v-slot="{ Component }">
          <keep-alive>
            <component :is="Component" />
          </keep-alive>
        </router-view>
      </el-main>
    </el-container>
  </el-container>
</template>

<style scoped>
.admin { height: 100vh; }
.sidebar {
  background: var(--app-sidebar-bg);
  border-right: 1px solid var(--app-border);
  display: flex;
  flex-direction: column;
  transition: width 0.2s;
}
.brand {
  height: 56px;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 0 18px;
  font-size: 16px;
  font-weight: 600;
  color: var(--app-title);
  border-bottom: 1px solid var(--app-border);
  white-space: nowrap;
  overflow: hidden;
}
.brand .logo {
  width: 30px;
  height: 30px;
  border-radius: 6px;
  background: var(--brand);
  color: #fff;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}
.brand.mini { justify-content: center; padding: 0; }
.side-menu { border-right: none; }
.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: var(--app-header-bg);
  border-bottom: 1px solid var(--app-border);
  padding: 0 16px;
}
.topbar .left { display: flex; align-items: center; gap: 12px; }
.topbar .right { display: flex; align-items: center; gap: 14px; }
.search-box { width: 180px; }
.pills { display: flex; gap: 6px; }
.pills b { color: inherit; }
.avatar { display: flex; align-items: center; gap: 6px; cursor: pointer; outline: none; }
.avatar-img { background: var(--el-fill-color-darker); color: var(--el-text-color-regular); }
.avatar-name { font-size: 13px; color: var(--el-text-color-regular); }
.content { background: var(--app-content-bg); padding: 16px; }
.circuit-banner { margin-bottom: 14px; }
.ad-banner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 14px;
  margin-bottom: 14px;
  border-radius: 6px;
  background: linear-gradient(135deg, #fff7e6 0%, #fff1d6 100%);
  border: 1px solid #ffd88a;
  font-size: 13px;
  color: #6b5900;
}
:root.dark .ad-banner {
  background: linear-gradient(135deg, #2a2517 0%, #302818 100%);
  border-color: #5c4a1e;
  color: #d4b96a;
}
.ad-content {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.ad-content a {
  color: var(--brand);
  text-decoration: none;
  font-weight: 600;
}
.ad-content a:hover { text-decoration: underline; }
.ad-sep { color: #c0a050; margin: 0 2px; }
.ad-close { flex-shrink: 0; color: #a08040; }
@media (max-width: 768px) {
  .pills, .search-box, .avatar-name { display: none; }
}
</style>

