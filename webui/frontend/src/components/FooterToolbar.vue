<script setup>
// Sticky Ant Pro-style action bar that keeps save controls visible while scrolling.
</script>

<template>
  <div class="footer-toolbar">
    <div class="ft-left"><slot name="left" /></div>
    <div class="ft-right"><slot /></div>
  </div>
</template>

<style scoped>
.footer-toolbar {
  position: sticky;
  bottom: 0;
  z-index: 10;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin: 16px -4px -4px;
  padding: 12px 20px;
  background: var(--app-header-bg);
  border: 1px solid var(--app-border);
  border-radius: 8px;
  box-shadow: 0 -4px 16px rgba(0, 0, 0, 0.06);
}
.ft-left { color: var(--el-text-color-secondary); font-size: 12px; }
.ft-right { display: flex; align-items: center; gap: 10px; margin-left: auto; }
</style>
