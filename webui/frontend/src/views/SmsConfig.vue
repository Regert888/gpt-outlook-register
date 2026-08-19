<script setup>
import { computed, onActivated, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { getSmsConfig, saveSmsConfig, testSms, getSmsAllCountries } from '@/api/settings'
import FooterToolbar from '@/components/FooterToolbar.vue'

const enabled = ref(false)
const provider = ref('smsbower')
const apiKey = ref('')
const apiKeyPh = ref('Paste the SMS provider API key')
const service = ref('dr')
const maxPrice = ref('')
const fixedPrice = ref('')
const phoneSuccessMax = ref('3')
const reusePhone = ref(false)
const autoMinStock = ref('20')
const allowed = ref([]) // Allowed country IDs.
const maxPhoneAttempts = ref('')
const perPhoneTimeout = ref('80')

const allCountries = ref([])
const countriesLoading = ref(false)
const saving = ref(false)
const testing = ref(false)

const countryOptions = computed(() =>
  allCountries.value.map((c) => ({
    value: c.id,
    label: `${c.id}·${c.name || c.name_en || `Country ${c.id}`}${c.price != null ? ` (${c.price}/${c.count})` : ''}`,
    safe: c.openai_sms_safe,
  })),
)

async function loadCountries(p) {
  countriesLoading.value = true
  try {
    const r = await getSmsAllCountries(p || provider.value)
    allCountries.value = r.countries || []
  } catch (e) {
    console.error('Failed to load country list:', e)
  } finally { countriesLoading.value = false }
}

async function load() {
  try {
    const { config } = await getSmsConfig()
    provider.value = config.sms_provider || 'smsbower'
    await loadCountries(provider.value)
    enabled.value = config.sms_enabled === '1'
    apiKey.value = ''
    apiKeyPh.value = config.sms_api_key === '***' ? 'Already configured (leave blank to keep it)' : 'Paste the SMS provider API key'
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
    // One price input serves as both the rental ceiling and the automatic country filter.
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
      // Automatic country selection is always enabled. Select one allowed country to lock selection to it.
      sms_auto_country: '1',
      sms_allowed_countries: allowed.value.join(','),
      sms_auto_min_stock: autoMinStock.value.trim() || '20',
      sms_max_phone_attempts: maxPhoneAttempts.value.trim(),
      sms_per_phone_timeout: perPhoneTimeout.value.trim() || '80',
    })
    ElMessage.success('Configuration saved')
    setTimeout(load, 300)
  } catch (e) { ElMessage.error(e.message) }
  finally { saving.value = false }
}

async function test() {
  testing.value = true
  try { const r = await testSms(); ElMessage.success(r.message || 'Connection successful') }
  catch (e) { ElMessage.error(e.message) }
  finally { testing.value = false }
}

onActivated(() => load())
</script>
<template>
  <div class="page">
    <el-card shadow="never" style="max-width: 820px">
      <template #header><span class="section-title" style="margin: 0">SMS Verification Configuration</span></template>

      <el-form label-position="top">
        <el-form-item>
          <el-checkbox v-model="enabled">
            <b>Enable SMS verification</b> (automatically rent a number when the add-phone step appears; otherwise use the environment-variable fallback)
          </el-checkbox>
        </el-form-item>

        <el-form-item label="SMS provider">
          <el-radio-group v-model="provider" @change="onProviderChange">
            <el-radio value="smsbower">
              <span>SmsBower (immediate refund after cancellation)</span>
              <a :href="'https://smsbower.app/en?ref=499410'" target="_blank" class="sms-reg-link" @click.stop>Register ↗</a>
            </el-radio>
            <el-radio value="herosms">
              <span>HeroSMS (automatic refund 20 minutes after cancellation)</span>
              <a :href="'https://hero-sms.com/?ref=738021'" target="_blank" class="sms-reg-link" @click.stop>Register ↗</a>
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
            <el-form-item label="Service code (OpenAI = dr)">
              <el-input v-model="service" placeholder="dr" />
            </el-form-item>
          </el-col>
        </el-row>

        <el-divider content-position="left">Number Selection (automatic by price and stock)</el-divider>
        <el-form-item label="Allowed countries (multi-select, searchable)">
          <el-select
            v-model="allowed" multiple filterable clearable collapse-tags collapse-tags-tooltip
            :loading="countriesLoading" placeholder="Search by country name or ID…" style="width: 100%"
          >
            <el-option v-for="o in countryOptions" :key="o.value" :label="o.label" :value="o.value">
              <span>{{ o.label }}</span>
              <el-tag v-if="o.safe" size="small" type="success" style="margin-left: 6px">OpenAI-compatible</el-tag>
            </el-option>
          </el-select>
          <div class="hint" style="margin-top: 4px">
            {{ allowed.length }} countries selected · Leave blank to automatically choose the cheapest option across the platform; select one country to restrict selection to it.
          </div>
        </el-form-item>
        <el-row :gutter="16">
          <el-col :span="12">
            <el-form-item label="Minimum stock (countries below this level are excluded)">
              <el-input v-model="autoMinStock" type="number" placeholder="20" />
            </el-form-item>
          </el-col>
        </el-row>

        <el-divider content-position="left">Numbers and Pricing</el-divider>
        <el-row :gutter="16">
          <el-col :span="12">
            <el-form-item label="Maximum price per number (blank = unlimited; also filters countries)">
              <el-input v-model="maxPrice" placeholder="0.5" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="Fixed price per number (blank = unlimited; takes priority over maximum price)">
              <el-input v-model="fixedPrice" placeholder="0.3" />
            </el-form-item>
          </el-col>
        </el-row>
        <el-row :gutter="16">
          <el-col :span="12">
            <el-form-item label="Successful reuse limit per number (default: 3)">
              <el-input v-model="phoneSuccessMax" type="number" />
            </el-form-item>
          </el-col>
        </el-row>
        <el-form-item>
          <el-checkbox v-model="reusePhone"><b>Enable number reuse</b> (OpenAI risk controls may prevent immediate reuse)</el-checkbox>
        </el-form-item>

        <el-divider content-position="left">Retry Policy</el-divider>
        <el-row :gutter="16">
          <el-col :span="12">
            <el-form-item label="Maximum replacement attempts (blank = provider default, usually 3)">
              <el-input v-model="maxPhoneAttempts" type="number" placeholder="Leave blank to use the provider default" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="Timeout per number in seconds (default: 80)">
              <el-input v-model="perPhoneTimeout" type="number" placeholder="80" />
            </el-form-item>
          </el-col>
        </el-row>

      </el-form>
    </el-card>

    <FooterToolbar>
      <template #left>SMS provider: {{ provider === 'herosms' ? 'HeroSMS' : 'SmsBower' }}{{ allowed.length ? ` · ${allowed.length} allowed countries` : ' · automatic platform-wide selection' }}</template>
      <el-button :loading="testing" @click="test">Test Balance</el-button>
      <el-button type="primary" :loading="saving" @click="save">Save Configuration</el-button>
    </FooterToolbar>
  </div>
</template>

<style scoped>
.sms-reg-link { margin-left: 6px; color: var(--el-color-primary); text-decoration: none; font-size: 12px; white-space: nowrap; vertical-align: middle; }
.sms-reg-link:hover { text-decoration: underline; }
</style>
