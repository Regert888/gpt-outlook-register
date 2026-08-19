<script setup>
import en from 'element-plus/es/locale/lang/en'
import AdminLayout from '@/layouts/AdminLayout.vue'
</script>

<template>
  <el-config-provider :locale="en">
    <AdminLayout />
  </el-config-provider>
</template>
