<template>
  <section class="page" data-module="drainage">
    <header class="page-head">
      <div>
        <h2>排水设施管理</h2>
        <p class="page-desc">排水设施处置闭环：空态/边界分支可复位、现场复测优先、台账待办清单同事务、中断续传与幂等回传。</p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" @click="exportRows">导出排水设施清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in disposalStats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <section class="panel">
      <header class="panel-head">
        <h3>处置回传</h3>
        <div class="preset-row">
          <span class="preset-hint">场景快填：</span>
          <button v-for="p in presets" :key="p.label" class="btn ghost" type="button" @click="applyPreset(p)">
            {{ p.label }}
          </button>
        </div>
      </header>
      <form class="disposal-form" @submit.prevent="submitDisposal">
        <label v-for="field in submitFields" :key="field.key" class="form-item" :class="{ wide: field.wide }">
          <span>{{ field.label }}</span>
          <input v-model="form[field.key]" :placeholder="field.placeholder ?? ''" />
        </label>
        <label class="form-item check">
          <span>复测超时</span>
          <input v-model="form.复测超时" type="checkbox" true-value="是" false-value="" />
        </label>
        <label class="form-item check">
          <span>模拟保存中断</span>
          <input v-model="form.模拟中断" type="checkbox" true-value="是" false-value="" />
        </label>
        <label class="form-item">
          <span>模拟落库失败阶段</span>
          <select v-model="form.模拟落库失败阶段">
            <option value="">不模拟</option>
            <option value="ledger">台账失败</option>
            <option value="todo">待办失败</option>
            <option value="monitor">清单失败</option>
            <option value="observation">观测历史失败</option>
            <option value="disposal">主记录失败</option>
          </select>
        </label>
        <div class="form-actions">
          <button class="btn primary" type="submit">回传处置结论</button>
          <button class="btn ghost" type="button" @click="resetForm">清空表单</button>
        </div>
      </form>
    </section>

    <section class="panel">
      <header class="panel-head">
        <h3>处置记录（含中断与可复位分支）</h3>
        <div class="filter-bar compact">
          <select v-model="recordFilter.status" @change="reloadDisposals">
            <option value="">全部状态</option>
            <option value="已完成">已完成</option>
            <option value="已中断">已中断</option>
            <option value="已复位">已复位</option>
          </select>
          <select v-model="recordFilter.branch" @change="reloadDisposals">
            <option value="">全部分支</option>
            <option v-for="(label, key) in branchLabels" :key="key" :value="key">{{ label }}</option>
          </select>
          <button class="btn" type="button" @click="reloadAll">刷新</button>
        </div>
      </header>
      <table class="data-table">
        <thead>
          <tr>
            <th>处置单号</th>
            <th>设施编号</th>
            <th>所属路段</th>
            <th>观测时间</th>
            <th>分支</th>
            <th>处置结论</th>
            <th>采信来源</th>
            <th>监测冲突</th>
            <th>状态/原因码</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in disposals" :key="String(row.id)">
            <td>{{ row.处置单号 }}</td>
            <td>{{ row.设施编号 || '—' }}</td>
            <td>{{ row.所属路段 || '—' }}</td>
            <td>{{ row.观测时间 }}</td>
            <td>
              <span class="badge" :class="row.分支编码 === 'NORMAL_DRAINAGE' ? 'normal' : 'boundary'">
                {{ row.分支名称 }}{{ row.可复位 ? '（可复位）' : '' }}
              </span>
            </td>
            <td>{{ row.处置结论 || '—' }}</td>
            <td>{{ row.数据来源 }}</td>
            <td>{{ row.监测复测冲突 ? '冲突·以现场为准' : '一致' }}</td>
            <td>
              <span :class="row.状态 === '已中断' ? 'error-text' : ''">{{ row.状态 }}</span>
              <span v-if="row.原因码" class="reason-code">{{ row.原因码 }}@{{ row.失败阶段 }}</span>
            </td>
            <td class="row-actions">
              <button
                v-if="row.状态 === '已中断'"
                class="link"
                type="button"
                @click="resumeDisposal(row)"
              >
                重连续传
              </button>
              <button
                v-if="row.可复位 && row.状态 === '已完成'"
                class="link"
                type="button"
                @click="resetDisposal(row)"
              >
                复位
              </button>
              <button class="link" type="button" @click="viewObservations(row)">观测历史</button>
            </td>
          </tr>
          <tr v-if="!disposals.length">
            <td colspan="10" class="empty-state">暂无处置记录，请先回传一条处置结论</td>
          </tr>
        </tbody>
      </table>
    </section>

    <section v-if="observationView.length" class="panel">
      <header class="panel-head">
        <h3>观测历史 · {{ observationFacility }}（监测与人工按观测时点分别留档）</h3>
        <button class="btn ghost" type="button" @click="observationView = []">关闭</button>
      </header>
      <table class="data-table">
        <thead>
          <tr>
            <th>观测记录号</th>
            <th>观测来源</th>
            <th>观测时点</th>
            <th>积水深度(m)</th>
            <th>泵站状态</th>
            <th>是否采信</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="o in observationView" :key="String(o.观测记录号)">
            <td>{{ o.观测记录号 }}</td>
            <td>{{ o.观测来源 }}</td>
            <td>{{ o.观测时点 }}</td>
            <td>{{ o.积水深度 || '—' }}</td>
            <td>{{ o.泵站状态 || '—' }}</td>
            <td>{{ o.是否采信 ? '采信' : '留档' }}</td>
          </tr>
        </tbody>
      </table>
    </section>

    <section class="panel">
      <header class="panel-head">
        <h3>设施整改待办 / 路段监测清单</h3>
      </header>
      <div class="split-grid">
        <table class="data-table">
          <thead>
            <tr><th>待办编号</th><th>设施编号</th><th>类型</th><th>状态</th><th>关联单号</th></tr>
          </thead>
          <tbody>
            <tr v-for="t in todos" :key="String(t.待办编号)">
              <td>{{ t.待办编号 }}</td>
              <td>{{ t.设施编号 || '—' }}</td>
              <td>{{ t.待办类型 }}</td>
              <td>{{ t.待办状态 }}</td>
              <td>{{ t.关联单号 }}</td>
            </tr>
            <tr v-if="!todos.length"><td colspan="5" class="empty-state">暂无待办</td></tr>
          </tbody>
        </table>
        <table class="data-table">
          <thead>
            <tr><th>清单编号</th><th>设施编号</th><th>结论</th><th>清单状态</th><th>关联单号</th></tr>
          </thead>
          <tbody>
            <tr v-for="m in monitorItems" :key="String(m.清单编号)">
              <td>{{ m.清单编号 }}</td>
              <td>{{ m.设施编号 || '—' }}</td>
              <td>{{ m.积水结论 }}</td>
              <td>{{ m.清单状态 }}</td>
              <td>{{ m.关联单号 }}</td>
            </tr>
            <tr v-if="!monitorItems.length"><td colspan="5" class="empty-state">暂无清单记录</td></tr>
          </tbody>
        </table>
      </div>
    </section>

    <footer class="page-foot">
      <span>共 {{ total }} 条处置记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | boolean | null>

const ENDPOINT = '/api/drainage'

const branchLabels: Record<string, string> = {
  NORMAL_DRAINAGE: '正常排水',
  NO_WATER: '无积水',
  PUMP_OFFLINE: '泵站离线',
  RECHECK_TIMEOUT: '复测超时',
  FACILITY_MISSING: '设施编号缺失',
}

interface SubmitField {
  key: string
  label: string
  placeholder?: string
  wide?: boolean
}

const submitFields: SubmitField[] = [
  { key: '设施编号', label: '设施编号（幂等键）', placeholder: '如 DRAI-0001', wide: true },
  { key: '观测时间', label: '观测时间（幂等键）', placeholder: 'ISO 时间，如 2026-10-02T09:00:00' },
  { key: '设施类型', label: '设施类型' },
  { key: '所属路段', label: '所属路段' },
  { key: '桩号位置', label: '桩号位置' },
  { key: '监测积水深度', label: '监测积水深度(m)' },
  { key: '监测泵站状态', label: '监测泵站状态' },
  { key: '监测时间', label: '监测观测时点' },
  { key: '人工积水深度', label: '人工复测水深(m)' },
  { key: '人工泵站状态', label: '人工复测泵站' },
  { key: '人工复测时间', label: '人工复测时点' },
]

function emptyForm(): Record<string, string> {
  const values: Record<string, string> = { 模拟落库失败阶段: '' }
  for (const field of submitFields) values[field.key] = ''
  values.复测超时 = ''
  values.模拟中断 = ''
  return values
}

const form = ref<Record<string, string>>(emptyForm())

const presets: { label: string; values: Record<string, string> }[] = [
  {
    label: '正常排水',
    values: {
      设施编号: 'DRAI-0001', 观测时间: '2026-10-02T09:00:00', 设施类型: '雨水口', 所属路段: '港城东大街',
      桩号位置: 'K12+300', 监测积水深度: '0.30', 监测泵站状态: '在线', 监测时间: '2026-10-02T08:55:00',
      人工积水深度: '0.30', 人工泵站状态: '在线', 人工复测时间: '2026-10-02T09:05:00',
    },
  },
  {
    label: '冲突·以人工为准',
    values: {
      设施编号: 'DRAI-0002', 观测时间: '2026-10-02T09:10:00', 所属路段: '山海路',
      监测积水深度: '0', 监测泵站状态: '', 监测时间: '2026-10-02T09:00:00',
      人工积水深度: '0.42', 人工泵站状态: '在线', 人工复测时间: '2026-10-02T09:08:00',
    },
  },
  { label: '设施编号缺失', values: { 设施编号: '', 观测时间: '2026-10-02T09:20:00', 监测积水深度: '0.2', 监测泵站状态: '在线' } },
  { label: '无积水', values: { 设施编号: 'DRAI-0003', 观测时间: '2026-10-02T09:30:00', 监测积水深度: '0', 人工积水深度: '0' } },
  { label: '泵站离线', values: { 设施编号: 'DRAI-0004', 观测时间: '2026-10-02T09:40:00', 监测泵站状态: '离线', 监测积水深度: '0.5', 人工泵站状态: '离线', 人工积水深度: '0.5' } },
  { label: '复测超时', values: { 设施编号: 'DRAI-0005', 观测时间: '2026-10-02T09:50:00', 复测超时: '是', 监测积水深度: '0.35', 监测泵站状态: '在线' } },
]

const disposals = ref<Row[]>([])
const todos = ref<Row[]>([])
const monitorItems = ref<Row[]>([])
const observationView = ref<Row[]>([])
const observationFacility = ref('')
const total = ref(0)
const errorMessage = ref('')
const recordFilter = ref<{ status: string; branch: string }>({ status: '', branch: '' })

const disposalStats = computed(() => {
  const count = (predicate: (row: Row) => boolean) => disposals.value.filter(predicate).length
  return [
    { label: '处置记录', value: total.value },
    { label: '已中断（待续传）', value: count((r) => r.状态 === '已中断') },
    { label: '可复位分支', value: count((r) => !!r.可复位 && r.状态 === '已完成') },
    { label: '冲突·采信现场', value: count((r) => !!r.监测复测冲突) },
  ]
})

function applyPreset(preset: { values: Record<string, string> }) {
  form.value = { ...emptyForm(), ...preset.values }
  errorMessage.value = ''
}

function resetForm() {
  form.value = emptyForm()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

async function submitDisposal() {
  errorMessage.value = ''
  const values = Object.fromEntries(Object.entries(form.value).filter(([, v]) => v !== ''))
  try {
    const response = await request(`${ENDPOINT}/disposals`, {
      method: 'POST',
      body: JSON.stringify({ values }),
    })
    const payload = await response.json()
    if (!payload.ok) throw new Error(payload.message || '处置回传未生效')
    errorMessage.value = payload.message
    await reloadAll()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '处置回传失败'
  }
}

async function resumeDisposal(row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/disposals/${row.id}/resume`, {
      method: 'POST',
      body: JSON.stringify({ values: {} }),
    })
    const payload = await response.json()
    if (!payload.ok) throw new Error(payload.detail || payload.message || '续传失败')
    errorMessage.value = payload.message
    await reloadAll()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '续传失败'
  }
}

async function resetDisposal(row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/disposals/${row.id}/reset`, { method: 'POST' })
    const payload = await response.json()
    if (!payload.ok) throw new Error(payload.detail || payload.message || '复位失败')
    errorMessage.value = payload.message
    await reloadAll()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '复位失败'
  }
}

async function viewObservations(row: Row) {
  errorMessage.value = ''
  const code = row.设施编号 ? String(row.设施编号) : ''
  const query = code ? `?facility=${encodeURIComponent(code)}` : ''
  try {
    const response = await request(`${ENDPOINT}/disposals/observations${query}`)
    const payload = await response.json()
    observationView.value = (payload.items ?? []).filter((item: Row) => !code || item.设施编号 === code)
    observationFacility.value = code || '缺失编号设施'
  } catch {
    errorMessage.value = '观测历史读取失败'
  }
}

async function reloadDisposals() {
  const params = new URLSearchParams()
  if (recordFilter.value.status) params.set('status', recordFilter.value.status)
  if (recordFilter.value.branch) params.set('branch', recordFilter.value.branch)
  params.set('size', '100')
  const response = await request(`${ENDPOINT}/disposals?${params.toString()}`)
  if (!response.ok) throw new Error('处置记录读取失败')
  const payload = await response.json()
  disposals.value = payload.items ?? []
  total.value = payload.total ?? disposals.value.length
}

async function reloadSideTables() {
  const [todoResp, monitorResp] = await Promise.all([
    request(`${ENDPOINT}/disposals/todos`),
    request(`${ENDPOINT}/disposals/monitor-items`),
  ])
  if (todoResp.ok) todos.value = (await todoResp.json()).items ?? []
  if (monitorResp.ok) monitorItems.value = (await monitorResp.json()).items ?? []
}

async function reloadAll() {
  try {
    await Promise.all([reloadDisposals(), reloadSideTables()])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '处置数据读取失败'
  }
}

onMounted(reloadAll)
</script>

<style scoped>
.panel {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px 14px;
  margin-bottom: 14px;
}
.panel-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}
.panel-head h3 { margin: 0; font-size: 15px; }
.preset-row { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.preset-hint { font-size: 12px; color: var(--muted); }
.disposal-form {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
}
.form-item { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); }
.form-item.wide { grid-column: span 2; }
.form-item input,
.form-item select {
  padding: 6px 8px;
  border: 1px solid var(--border);
  border-radius: 6px;
  font-size: 13px;
  color: #1f2937;
}
.form-item.check { justify-content: space-between; }
.form-item.check input { width: 16px; height: 16px; }
.form-actions { grid-column: 1 / -1; display: flex; gap: 8px; }
.filter-bar.compact { margin: 0; }
.badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 12px;
}
.badge.normal { background: #e7f6ec; color: #1a7f37; }
.badge.boundary { background: #fef3e2; color: #b45309; }
.reason-code {
  display: block;
  font-size: 11px;
  color: #b42318;
}
.split-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}
</style>
