<script setup>
import { computed, onActivated, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { getSmsConfig, saveSmsConfig, testSms, getSmsAllCountries } from '@/api/settings'
import FooterToolbar from '@/components/FooterToolbar.vue'

const enabled = ref(false)
const provider = ref('smsbower')
const apiKey = ref('')
const apiKeyPh = ref('粘贴接码平台 API Key')
const service = ref('dr')
const maxPrice = ref('')
const fixedPrice = ref('')
const phoneSuccessMax = ref('3')
const reusePhone = ref(false)
const autoMinStock = ref('20')
const allowed = ref([]) // 允许国家 id 数组
const maxPhoneAttempts = ref('')
const perPhoneTimeout = ref('80')

const allCountries = ref([])
const countriesLoading = ref(false)
const saving = ref(false)
const testing = ref(false)

const countryOptions = computed(() =>
  allCountries.value.map((c) => ({
    value: c.id,
    label: `${c.id}·${c.name_cn}${c.price != null ? ` (${c.price}/${c.count})` : ''}`,
    safe: c.openai_sms_safe,
  })),
)

async function loadCountries(p) {
  countriesLoading.value = true
  try {
    const r = await getSmsAllCountries(p || provider.value)
    allCountries.value = r.countries || []
  } catch (e) {
    console.error('加载国家列表失败:', e)
  } finally { countriesLoading.value = false }
}

async function load() {
  try {
    const { config } = await getSmsConfig()
    provider.value = config.sms_provider || 'smsbower'
    await loadCountries(provider.value)
    enabled.value = config.sms_enabled === '1'
    apiKey.value = ''
    apiKeyPh.value = config.sms_api_key === '***' ? '已设置（留空不修改）' : '粘贴接码平台 API Key'
    service.value = config.sms_service || 'dr'
    maxPrice.value = config.sms_max_price || ''
    fixedPrice.value = config.sms_fixed_price || ''
    phoneSuccessMax.value = config.sms_phone_success_max || '3'
    reusePhone.value = config.sms_reuse_phone === '1'
    autoMinStock.value = config.sms_auto_min_stock || '20'
    allowed.value = (config.sms_allowed_countries || '').split(',').map((s) => s.trim()).filter(Boolean)
    maxPhoneAttempts.value = config.sms_max_phone_attempts || ''
    perPhoneTimeout.value = config.sms_per_phone_timeout || '80'
  } catch (e) { ElMessage.error(e.message) }
}

async function onProviderChange() {
  allowed.value = []
  await loadCountries(provider.value)
}

async function save() {
  saving.value = true
  try {
    // 单价只暴露一个输入框：既是租号时的价格上限，也是自动挑国家时的筛选条件。
    const price = maxPrice.value.trim()
    await saveSmsConfig({
      sms_enabled: enabled.value ? '1' : '0',
      sms_provider: provider.value,
      sms_api_key: apiKey.value.trim() || '***',
      sms_service: service.value.trim() || 'dr',
      sms_max_price: price,
      sms_auto_max_price: price,
      sms_fixed_price: fixedPrice.value.trim(),
      sms_phone_success_max: phoneSuccessMax.value.trim() || '3',
      sms_reuse_phone: reusePhone.value ? '1' : '0',
      // 自动选号已改为常驻能力，不再有开关；锁死单一国家请在”允许使用的国家”里只勾一个。
      sms_auto_country: '1',
      sms_allowed_countries: allowed.value.join(','),
      sms_auto_min_stock: autoMinStock.value.trim() || '20',
      sms_max_phone_attempts: maxPhoneAttempts.value.trim(),
      sms_per_phone_timeout: perPhoneTimeout.value.trim() || '80',
    })
    ElMessage.success('保存成功')
    setTimeout(load, 300)
  } catch (e) { ElMessage.error(e.message) }
  finally { saving.value = false }
}

async function test() {
  testing.value = true
  try { const r = await testSms(); ElMessage.success(r.message || '连通正常') }
  catch (e) { ElMessage.error(e.message) }
  finally { testing.value = false }
}

onActivated(() => load())
</script>
<template>
  <div class="page">
    <el-card shadow="never" style="max-width: 820px">
      <template #header><span class="section-title" style="margin: 0">SMS 接码配置</span></template>

      <el-form label-position="top">
        <el-form-item>
          <el-checkbox v-model="enabled">
            <b>启用 SMS 接码</b>（命中 add-phone 时自动租号，否则回退到环境变量路径）
          </el-checkbox>
        </el-form-item>

        <el-form-item label="接码平台">
          <el-radio-group v-model="provider" @change="onProviderChange">
            <el-radio value="smsbower">
              <span>SmsBower（立即取消就退款）</span>
              <a :href="'https://smsbower.app/cn?ref=499410'" target="_blank" class="sms-reg-link" @click.stop>前往注册 ↗</a>
            </el-radio>
            <el-radio value="herosms">
              <span>HeroSMS（取消后 20 分钟自动退款）</span>
              <a :href="'https://hero-sms.com/?ref=738021'" target="_blank" class="sms-reg-link" @click.stop>前往注册 ↗</a>
            </el-radio>
          </el-radio-group>
        </el-form-item>

        <el-row :gutter="16">
          <el-col :span="16">
            <el-form-item label="API Key">
              <el-input v-model="apiKey" type="password" show-password :placeholder="apiKeyPh" />
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="Service 代码（OpenAI = dr）">
              <el-input v-model="service" placeholder="dr" />
            </el-form-item>
          </el-col>
        </el-row>

        <el-divider content-position="left">选号策略（按价格 + 库存自动挑国家）</el-divider>
        <el-form-item label="允许使用的国家（多选，可搜索）">
          <el-select
            v-model="allowed" multiple filterable clearable collapse-tags collapse-tags-tooltip
            :loading="countriesLoading" placeholder="搜索国家名称或 ID…" style="width: 100%"
          >
            <el-option v-for="o in countryOptions" :key="o.value" :label="o.label" :value="o.value">
              <span>{{ o.label }}</span>
              <el-tag v-if="o.safe" size="small" type="success" style="margin-left: 6px">安全</el-tag>
            </el-option>
          </el-select>
          <div class="hint" style="margin-top: 4px">
            已选 {{ allowed.length }} 个国家 · 留空 = 全平台自动挑最便宜的；只勾 1 个 = 锁死用这个国家
          </div>
        </el-form-item>
        <el-row :gutter="16">
          <el-col :span="12">
            <el-form-item label="最低库存（低于这个数的国家不选）">
              <el-input v-model="autoMinStock" type="number" placeholder="20" />
            </el-form-item>
          </el-col>
        </el-row>

        <el-divider content-position="left">号码与费用</el-divider>
        <el-row :gutter="16">
          <el-col :span="12">
            <el-form-item label="号码最高单价（空=不限，同时用于筛选国家）">
              <el-input v-model="maxPrice" placeholder="0.5" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="号码固定单价（空=不限，优先级高于最高单价）">
              <el-input v-model="fixedPrice" placeholder="0.3" />
            </el-form-item>
          </el-col>
        </el-row>
        <el-row :gutter="16">
          <el-col :span="12">
            <el-form-item label="同号成功复用上限（默认 3）">
              <el-input v-model="phoneSuccessMax" type="number" />
            </el-form-item>
          </el-col>
        </el-row>
        <el-form-item>
          <el-checkbox v-model="reusePhone"><b>启用号码复用</b>（gpt风控，号码短时间无法复用）</el-checkbox>
        </el-form-item>

        <el-divider content-position="left">失败重试策略</el-divider>
        <el-row :gutter="16">
          <el-col :span="12">
            <el-form-item label="最多换号次数（空=平台默认，一般 3）">
              <el-input v-model="maxPhoneAttempts" type="number" placeholder="留空使用平台默认" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="单号等待秒数（默认 80）">
              <el-input v-model="perPhoneTimeout" type="number" placeholder="80" />
            </el-form-item>
          </el-col>
        </el-row>

      </el-form>
    </el-card>

    <FooterToolbar>
      <template #left>接码平台：{{ provider === 'herosms' ? 'HeroSMS' : 'SmsBower' }}{{ allowed.length ? ` · 允许国家 ${allowed.length} 个` : ' · 全平台自动选号' }}</template>
      <el-button :loading="testing" @click="test">测试余额</el-button>
      <el-button type="primary" :loading="saving" @click="save">保存配置</el-button>
    </FooterToolbar>
  </div>
</template>

<style scoped>
.sms-reg-link { margin-left: 6px; color: var(--el-color-primary); text-decoration: none; font-size: 12px; white-space: nowrap; vertical-align: middle; }
.sms-reg-link:hover { text-decoration: underline; }
</style>
