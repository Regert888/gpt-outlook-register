<script setup>
import { computed } from 'vue'
import { storeToRefs } from 'pinia'
import { useRouter } from 'vue-router'
import { useStatsStore } from '@/stores/stats'
import { useRuntimeStore } from '@/stores/runtime'
import StatusDot from '@/components/StatusDot.vue'

const router = useRouter()
const { stats } = storeToRefs(useStatsStore())
const { autoStatus } = storeToRefs(useRuntimeStore())

const cards = computed(() => [
  { label: 'Total', value: stats.value.total, color: 'var(--brand)', icon: 'Files' },
  { label: 'Available', value: stats.value.available, color: '#4caf50', icon: 'CircleCheck' },
  { label: 'In use', value: stats.value.in_use, color: '#ff9800', icon: 'Loading' },
  { label: 'Completed', value: stats.value.done, color: '#2196f3', icon: 'Select' },
  { label: 'Failed', value: stats.value.failed, color: '#e53935', icon: 'CircleClose' },
])

const autoStateLabel = computed(() => ({
  stopped: 'Stopped', running: 'Running', paused: 'Paused',
}[autoStatus.value.state] || autoStatus.value.state))
const autoStateType = computed(() => ({
  stopped: 'info', running: 'success', paused: 'warning',
}[autoStatus.value.state] || 'info'))
</script>

<template>
  <div class="page">
    <el-row :gutter="16">
      <el-col v-for="c in cards" :key="c.label" :xs="12" :sm="8" :md="4" style="margin-bottom: 16px">
        <el-card class="stat-card" shadow="hover">
          <div style="display: flex; align-items: center; justify-content: space-between">
            <div>
              <div class="stat-value" :style="{ color: c.color }">{{ c.value }}</div>
              <div class="stat-label">{{ c.label }}</div>
            </div>
            <el-icon :size="30" :style="{ color: c.color, opacity: 0.5 }"><component :is="c.icon" /></el-icon>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="16">
      <el-col :md="12" style="margin-bottom: 16px">
        <el-card shadow="never">
          <template #header><span class="section-title" style="margin: 0">Automatic Registration Status</span></template>
          <el-descriptions :column="2" border size="small">
            <el-descriptions-item label="Status"><StatusDot :type="autoStateType" :text="autoStateLabel" /></el-descriptions-item>
            <el-descriptions-item label="Concurrency">{{ autoStatus.concurrency || 1 }}</el-descriptions-item>
            <el-descriptions-item label="Successful">{{ autoStatus.registered_ok || 0 }}</el-descriptions-item>
            <el-descriptions-item label="Failed">{{ autoStatus.registered_fail || 0 }}</el-descriptions-item>
          </el-descriptions>
          <div style="margin-top: 12px">
            <el-button type="primary" @click="router.push('/auto')">Open Batch Registration</el-button>
          </div>
        </el-card>
      </el-col>
      <el-col :md="12" style="margin-bottom: 16px">
        <el-card shadow="never">
          <template #header><span class="section-title" style="margin: 0">Quick Actions</span></template>
          <el-space wrap>
            <el-button @click="router.push('/import')"><el-icon><Upload /></el-icon>Import Email Accounts</el-button>
            <el-button @click="router.push('/register')"><el-icon><VideoPlay /></el-icon>Single Registration</el-button>
            <el-button @click="router.push('/pool')"><el-icon><Files /></el-icon>Email Pool</el-button>
            <el-button @click="router.push('/registered')"><el-icon><CircleCheck /></el-icon>Registration Results</el-button>
          </el-space>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>
