<script setup>
import { nextTick, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { useRuntimeStore } from '@/stores/runtime'

const runtime = useRuntimeStore()
const { logs } = storeToRefs(runtime)
const boxRef = ref(null)

// Scroll to the newest log entry automatically.
watch(
  () => logs.value.length,
  async () => {
    await nextTick()
    const el = boxRef.value
    if (el) el.scrollTop = el.scrollHeight
  },
)
</script>

<template>
  <div class="log-wrap">
    <div class="log-head">
      <span class="section-title" style="margin: 0">Live Log</span>
      <el-button size="small" text @click="runtime.clearLogs">Clear</el-button>
    </div>
    <div ref="boxRef" class="log-box">
      <div v-for="l in logs" :key="l.id" class="line" :class="l.kind">{{ l.text }}</div>
      <div v-if="!logs.length" class="line" style="color: #8a7">Waiting for log output…</div>
    </div>
  </div>
</template>

<style scoped>
.log-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}
</style>
