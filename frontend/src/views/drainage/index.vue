<template>
  <section class="page" data-module="drainage">
    <header class="page-head">
      <div>
        <h2>排水设施管理</h2>
        <p class="page-desc">处置回传按边界分流；现场复测与监测冲突时以现场复测为准，并按观测时点留档。</p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" @click="exportRows">导出排水设施清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <section class="panel disposal-panel">
      <header class="panel-head">
        <div>
          <h3>排水处置回传</h3>
          <p>设施编号缺失、无积水、泵站离线、复测超时进入独立可复位分支，不与正常排水混用。</p>
        </div>
      </header>
      <form class="disposal-form" @submit.prevent="submitDisposal">
        <label>
          <span>设施编号</span>
          <input v-model="disposalForm.facility_code" placeholder="缺失时留空" />
        </label>
        <label>
          <span>观测时点</span>
          <input v-model="disposalForm.observed_at" placeholder="2026-10-01T08:00" />
        </label>
        <label>
          <span>所属路段</span>
          <input v-model="disposalForm.road_name" />
        </label>
        <label>
          <span>桩号位置</span>
          <input v-model="disposalForm.station" />
        </label>
        <label>
          <span>监测结论</span>
          <select v-model="disposalForm.monitoring_conclusion">
            <option value="">无监测</option>
            <option>正常排水</option>
            <option>无积水</option>
            <option>淤积</option>
            <option>堵塞</option>
            <option>损坏</option>
          </select>
        </label>
        <label>
          <span>人工复测结论</span>
          <select v-model="disposalForm.manual_conclusion">
            <option value="">未复测</option>
            <option>正常排水</option>
            <option>无积水</option>
            <option>淤积</option>
            <option>堵塞</option>
            <option>损坏</option>
          </select>
        </label>
        <label>
          <span>泵站状态</span>
          <select v-model="disposalForm.pump_status">
            <option value="">未填报</option>
            <option value="在线">在线</option>
            <option value="离线">离线</option>
          </select>
        </label>
        <label>
          <span>复测状态</span>
          <select v-model="disposalForm.retest_status">
            <option value="">已完成</option>
            <option value="超时">超时</option>
          </select>
        </label>
        <button class="btn primary" type="submit">提交处置回传</button>
      </form>
      <p v-if="resultMessage" :class="resultClass" class="result-message">{{ resultMessage }}</p>
    </section>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <section class="table-section">
      <h3>排水设施台账</h3>
      <table class="data-table">
        <thead>
          <tr>
            <th v-for="column in columns" :key="column">{{ column }}</th>
            <th>权威来源</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="String(row.id)">
            <td v-for="column in columns" :key="column">{{ display(row[column]) }}</td>
            <td>{{ display(row['权威来源']) }}</td>
          </tr>
          <tr v-if="!rows.length">
            <td :colspan="columns.length + 1" class="empty-state">暂无排水设施数据，可通过处置回传形成台账</td>
          </tr>
        </tbody>
      </table>
    </section>

    <section class="linked-grid">
      <div class="panel">
        <h3>设施整改待办</h3>
        <ul class="compact-list">
          <li v-for="item in todos" :key="String(item.id)">
            <strong>{{ display(item['待办编号']) }}</strong>
            <span>{{ display(item['设施编号']) }} · {{ display(item['处置结论']) }}</span>
            <em>{{ display(item['待办状态']) }}</em>
          </li>
          <li v-if="!todos.length" class="empty-inline">暂无整改待办</li>
        </ul>
      </div>

      <div class="panel">
        <h3>路段监测清单</h3>
        <ul class="compact-list">
          <li v-for="item in monitoringRows" :key="String(item.id)">
            <strong>{{ display(item['清单编号']) }}</strong>
            <span>{{ display(item['设施编号']) }} · {{ display(item['所属路段']) }}</span>
            <em>{{ display(item['监测状态']) }}</em>
          </li>
          <li v-if="!monitoringRows.length" class="empty-inline">暂无监测清单</li>
        </ul>
      </div>
    </section>

    <section class="linked-grid">
      <div class="panel">
        <h3>可复位分支</h3>
        <ul class="compact-list">
          <li v-for="item in resetBranches" :key="String(item.id)">
            <strong>{{ display(item['分支状态']) }}</strong>
            <span>{{ display(item['设施编号']) }} · {{ display(item['观测时点']) }}</span>
            <button class="link" type="button" @click="resetBranch(Number(item.id))">复位</button>
          </li>
          <li v-if="!resetBranches.length" class="empty-inline">暂无空态或边界分支</li>
        </ul>
      </div>

      <div class="panel">
        <h3>保存中断 / 重连续传</h3>
        <ul class="compact-list">
          <li v-for="item in interruptions" :key="String(item.id)">
            <strong>{{ display(item.reasonCode) }}</strong>
            <span>{{ display(item['设施编号']) }} · {{ display(item.status) }}</span>
            <button
              v-if="item.status !== '已继续'"
              class="link"
              type="button"
              @click="resumeInterruption(Number(item.id))"
            >
              重连续传
            </button>
            <em v-else>已继续</em>
          </li>
          <li v-if="!interruptions.length" class="empty-inline">暂无中断记录</li>
        </ul>
      </div>
    </section>

    <section class="table-section">
      <h3>处置历史（按观测时点留档）</h3>
      <table class="data-table">
        <thead>
          <tr>
            <th>设施编号</th>
            <th>观测时点</th>
            <th>监测结论</th>
            <th>人工复测结论</th>
            <th>权威来源</th>
            <th>处置结论</th>
            <th>冲突</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in historyRows" :key="String(item.id)">
            <td>{{ display(item['设施编号']) }}</td>
            <td>{{ display(item['观测时点']) }}</td>
            <td>{{ display(item['监测结论']) }}</td>
            <td>{{ display(item['人工复测结论']) }}</td>
            <td>{{ display(item['权威来源']) }}</td>
            <td>{{ display(item['处置结论']) }}</td>
            <td>{{ item['冲突'] ? '已按现场复测裁决' : '无' }}</td>
          </tr>
          <tr v-if="!historyRows.length">
            <td colspan="7" class="empty-state">暂无正式处置历史</td>
          </tr>
        </tbody>
      </table>
    </section>

    <footer class="page-foot">
      <span>共 {{ total }} 条排水设施记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, unknown>
type JsonRecord = Record<string, unknown>
type PagePayload = { items?: Row[]; total?: number }

const ENDPOINT = '/api/drainage'
const columns = ['设施编号', '设施类型', '所属路段', '桩号位置', '设施状态', '处置结论', '最近观测时间']
const filterFields = columns.slice(0, 3)

const emptyForm = (): JsonRecord => ({
  facility_code: '',
  observed_at: '',
  road_name: '',
  station: '',
  monitoring_conclusion: '',
  manual_conclusion: '',
  pump_status: '',
  retest_status: '',
})

const rows = ref<Row[]>([])
const todos = ref<Row[]>([])
const monitoringRows = ref<Row[]>([])
const resetBranches = ref<Row[]>([])
const interruptions = ref<Row[]>([])
const historyRows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const resultMessage = ref('')
const filters = ref<Record<string, string>>({})
const disposalForm = reactive<JsonRecord>(emptyForm())

const stats = computed(() => [
  { label: '台账记录', value: rows.value.length },
  { label: '待整改', value: todos.value.filter((item) => item.pending).length },
  { label: '可复位分支', value: resetBranches.value.length },
  { label: '待续传', value: interruptions.value.filter((item) => item.status !== '已继续').length },
])
const resultClass = computed(() => (resultMessage.value.startsWith('处置') ? 'success-text' : 'warning-text'))

function display(value: unknown): string {
  return value === null || value === undefined || value === '' ? '—' : String(value)
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

async function postJson(path: string, body?: JsonRecord): Promise<{ status: number; payload: JsonRecord }> {
  const response = await request(path, {
    method: 'POST',
    body: JSON.stringify(body ?? {}),
  })
  const payload = (await response.json()) as JsonRecord
  return { status: response.status, payload }
}

async function submitDisposal() {
  errorMessage.value = ''
  resultMessage.value = ''
  const observations: JsonRecord = {}
  if (disposalForm.monitoring_conclusion) {
    observations.monitoring = {
      conclusion: disposalForm.monitoring_conclusion,
      observed_at: disposalForm.observed_at,
    }
  }
  if (disposalForm.manual_conclusion || disposalForm.retest_status === '超时') {
    observations.manual = {
      conclusion: disposalForm.manual_conclusion || undefined,
      observed_at: disposalForm.observed_at,
      retest_status: disposalForm.retest_status || undefined,
    }
  }
  const payload: JsonRecord = {
    facility_code: disposalForm.facility_code || undefined,
    facility_type: '排水设施',
    road_name: disposalForm.road_name || undefined,
    station: disposalForm.station || undefined,
    observed_at: disposalForm.observed_at || undefined,
    pump_status: disposalForm.pump_status || undefined,
    retest_status: disposalForm.retest_status || undefined,
    observations,
  }

  try {
    const { status, payload: result } = await postJson(`${ENDPOINT}/disposals`, payload)
    if (status === 503) {
      resultMessage.value = `${String(result.message ?? '保存中断')}（原因码：${display(result.reasonCode)}）`
    } else if (result.resettable) {
      resultMessage.value = `已进入${display(result.branchStatus)}可复位分支：${display(result.message)}`
    } else {
      resultMessage.value = `处置完成：${display(result.message)}`
    }
    Object.assign(disposalForm, emptyForm())
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '排水处置回传失败'
  }
}

async function resetBranch(branchId: number) {
  try {
    await postJson(`${ENDPOINT}/disposals/reset-branches/${branchId}/reset`)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '复位失败'
  }
}

async function resumeInterruption(interruptionId: number) {
  try {
    const { payload } = await postJson(`${ENDPOINT}/disposals/interruptions/${interruptionId}/resume`)
    resultMessage.value = payload.ok ? '重连续传成功，整批写入已完成' : `续传仍中断：${display(payload.reasonCode)}`
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '重连续传失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const [ledger, todo, monitoring, reset, interruption, history] = await Promise.all([
      request(`${ENDPOINT}?${query}`),
      request(`${ENDPOINT}/disposals/todos`),
      request(`${ENDPOINT}/disposals/monitoring-list`),
      request(`${ENDPOINT}/disposals/reset-branches`),
      request(`${ENDPOINT}/disposals/interruptions`),
      request(`${ENDPOINT}/disposals/history`),
    ])
    if (!ledger.ok) throw new Error('排水设施列表读取失败')
    const ledgerPayload = (await ledger.json()) as PagePayload
    rows.value = ledgerPayload.items ?? []
    total.value = ledgerPayload.total ?? rows.value.length
    todos.value = todo.ok ? (((await todo.json()) as PagePayload).items ?? []) : []
    monitoringRows.value = monitoring.ok ? (((await monitoring.json()) as PagePayload).items ?? []) : []
    resetBranches.value = reset.ok ? (((await reset.json()) as PagePayload).items ?? []) : []
    interruptions.value = interruption.ok ? (((await interruption.json()) as PagePayload).items ?? []) : []
    historyRows.value = history.ok ? (((await history.json()) as PagePayload).items ?? []) : []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '排水设施列表读取失败'
  }
}

onMounted(reload)
</script>
