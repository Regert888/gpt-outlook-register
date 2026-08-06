<script setup>
import { onActivated, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { getExportConfig, saveExportConfig, testExport } from '@/api/settings'
import FooterToolbar from '@/components/FooterToolbar.vue'

const cpa = reactive({ enabled: false, url: '', key: '', keyPh: '粘贴 CPA 管理密钥', timeout: 30 })
const sub = reactive({ enabled: false, url: '', key: '', keyPh: '粘贴面板里生成的 x-api-key', groupIds: '2', timeout: 30 })
const saving = ref(false)
const testingCpa = ref(false)
const testingSub = ref(false)

async function load() {
  try {
    const { config } = await getExportConfig()
    cpa.enabled = config.cpa_enabled === '1'
    cpa.url = config.cpa_url || ''
    cpa.key = ''
    cpa.keyPh = config.cpa_mgmt_key === '***' ? '已设置（留空不修改）' : '粘贴 CPA 管理密钥'
    cpa.timeout = Number(config.cpa_timeout || 30)
    sub.enabled = config.sub2api_enabled === '1'
    sub.url = config.sub2api_url || ''
    sub.key = ''
    sub.keyPh = config.sub2api_api_key === '***' ? '已设置（留空不修改）' : '粘贴面板里生成的 x-api-key'
    sub.groupIds = config.sub2api_group_ids || '2'
    sub.timeout = Number(config.sub2api_timeout || 30)
  } catch (e) { ElMessage.error(e.message) }
}

async function save() {
  saving.value = true
  try {
    await saveExportConfig({
      cpa_enabled: cpa.enabled ? '1' : '0',
      cpa_url: cpa.url.trim(),
      cpa_mgmt_key: cpa.key.trim() || '***',
      cpa_timeout: String(cpa.timeout || 30),
      sub2api_enabled: sub.enabled ? '1' : '0',
      sub2api_url: sub.url.trim(),
      sub2api_api_key: sub.key.trim() || '***',
      sub2api_group_ids: sub.groupIds.trim() || '2',
      sub2api_timeout: String(sub.timeout || 30),
    })
    ElMessage.success('保存成功')
    load()
  } catch (e) { ElMessage.error(e.message) }
  finally { saving.value = false }
}

async function test(target) {
  const flag = target === 'cpa' ? testingCpa : testingSub
  flag.value = true
  try { const r = await testExport(target); ElMessage.success(r.message || '连通正常') }
  catch (e) { ElMessage.error(e.message) }
  finally { flag.value = false }
}

onActivated(() => load())
</script>

<template>
  <div class="page">
    <el-card shadow="never" style="max-width: 760px">
      <template #header>
        <span class="section-title" style="margin: 0">注册完成后自动导出</span>
        <el-tag type="danger" size="small" effect="dark" style="margin-left: 8px">仅带 RT 的账号可用</el-tag>
      </template>
      <p class="hint">勾选启用后，每次注册成功落库会导出到对应面板。没勾选完全不执行，导出失败只记日志、不影响注册。</p>
      <p class="hint" style="color: var(--el-color-danger); font-weight: 600">
        注意：只有带 <b>RT（refresh_token）</b>的账号，导出后才能正常使用。没有 RT 的号推过去面板也用不了 —— 想拿到 RT 需要<b>配置接码</b>。
      </p>

      <el-form label-position="top">
        <el-divider content-position="left">CPA 面板</el-divider>
        <el-form-item>
          <el-checkbox v-model="cpa.enabled">启用 CPA 自动导出（POST /v0/management/auth-files）</el-checkbox>
        </el-form-item>
        <el-form-item label="CPA URL">
          <el-input v-model="cpa.url" placeholder="https://cpa.example.com" />
        </el-form-item>
        <el-form-item label="管理密钥（Authorization Bearer + X-Management-Key）">
          <el-input v-model="cpa.key" type="password" show-password :placeholder="cpa.keyPh" />
        </el-form-item>
        <el-form-item label="超时 (秒)">
          <el-input-number v-model="cpa.timeout" :min="5" :max="300" />
        </el-form-item>
        <el-button :loading="testingCpa" @click="test('cpa')">测试 CPA 连通性</el-button>

        <el-divider content-position="left">SUB2API 面板</el-divider>
        <el-form-item>
          <el-checkbox v-model="sub.enabled">启用 SUB2API 自动导出（POST /api/v1/admin/accounts）</el-checkbox>
        </el-form-item>
        <el-form-item label="SUB2API URL">
          <el-input v-model="sub.url" placeholder="https://sub2api.example.com" />
        </el-form-item>
        <el-form-item label="API Key（安全与认证-管理员 API Key）">
          <el-input v-model="sub.key" type="password" show-password :placeholder="sub.keyPh" />
        </el-form-item>
        <el-form-item label="分组 IDs（逗号分隔，如 2 或 1,2,3）">
          <el-input v-model="sub.groupIds" placeholder="2" />
        </el-form-item>
        <el-form-item label="超时 (秒)">
          <el-input-number v-model="sub.timeout" :min="5" :max="300" />
        </el-form-item>
        <el-button :loading="testingSub" @click="test('sub2api')">测试 SUB2API 连通性</el-button>

      </el-form>
    </el-card>

    <FooterToolbar>
      <template #left>
        CPA {{ cpa.enabled ? '已启用' : '未启用' }} · SUB2API {{ sub.enabled ? '已启用' : '未启用' }}
      </template>
      <el-button type="primary" :loading="saving" @click="save">保存配置</el-button>
    </FooterToolbar>
  </div>
</template>
