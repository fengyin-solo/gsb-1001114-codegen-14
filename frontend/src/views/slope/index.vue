<template>
  <section class="page" data-module="slope">
    <header class="page-head">
      <div>
        <h2>边坡防护管理</h2>
        <p class="page-desc">
          风险结论由单一版本状态机驱动：现场复核 → 评级发布 → 踏勘任务顺序推进；
          稳定性剖面带沿里程展示坡高、防护形式、巡检间隔与相邻病害。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" @click="exportRows">导出边坡防护清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in riskStats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>边坡编号</span>
        <input v-model="keyword" placeholder="按边坡编号检索" />
      </label>
      <label class="filter-item">
        <span>边坡状态</span>
        <select v-model="statusFilter">
          <option value="">全部</option>
          <option v-for="s in statuses" :key="s" :value="s">{{ s }}</option>
        </select>
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>结论版本</th>
          <th>工作流（顺序推进）</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>v{{ Number(row.version ?? 0) }}</td>
          <td class="workflow-cell">
            <ol class="stage-track">
              <li
                v-for="(stage, idx) in stageOrder"
                :key="stage"
                class="stage-node"
                :class="stageClass(row, idx)"
              >
                <span class="stage-dot">{{ idx + 1 }}</span>
                <span class="stage-name">{{ stage }}</span>
              </li>
            </ol>
            <div class="row-actions">
              <button
                class="link"
                type="button"
                :disabled="!canReview(row)"
                :title="canReview(row) ? '现场复核' : '当前阶段不可复核（不可跳级/倒序）'"
                @click="openReview(row)"
              >
                现场复核
              </button>
              <button
                class="link"
                type="button"
                :disabled="row['结论阶段'] !== '已复核'"
                :title="row['结论阶段'] === '已复核' ? '评级发布（三表同事务更新）' : '需先完成现场复核'"
                @click="publish(row)"
              >
                评级发布
              </button>
              <button class="link" type="button" @click="openProfile(row)">稳定性剖面带</button>
              <button
                class="link"
                type="button"
                :disabled="row['结论阶段'] !== '踏勘中'"
                :title="row['结论阶段'] === '踏勘中' ? '全部踏勘任务完成后收口' : '踏勘中阶段才可收口'"
                @click="completeSurvey(row)"
              >
                踏勘完成
              </button>
            </div>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 2" class="empty-state">暂无边坡防护数据</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条边坡防护记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
      <span v-if="noticeMessage" class="notice-text">{{ noticeMessage }}</span>
    </footer>

    <!-- 现场复核弹窗 -->
    <div v-if="reviewOpen" class="modal-mask" @click.self="reviewOpen = false">
      <div class="modal">
        <h3>现场复核 · {{ reviewForm.slope?.['边坡编号'] }}（v{{ Number(reviewForm.slope?.version ?? 0) }}）</h3>
        <p class="page-desc">评级冲突时以最近一次现场复核为准；提交后进入「已复核」，可改评一次再发布。</p>
        <label class="form-row">
          <span>复核人</span>
          <input v-model="reviewForm.复核人" placeholder="现场复核工程师" />
        </label>
        <label class="form-row">
          <span>现场评级</span>
          <select v-model="reviewForm.现场评级">
            <option value="" disabled>请选择现场评级</option>
            <option v-for="r in ratings" :key="r" :value="r">{{ r }}</option>
          </select>
        </label>
        <label class="form-row">
          <span>结论说明</span>
          <textarea v-model="reviewForm.结论说明" rows="3" placeholder="现场变形迹象、处置建议"></textarea>
        </label>
        <div class="modal-actions">
          <button class="btn ghost" type="button" @click="reviewOpen = false">取消</button>
          <button class="btn primary" type="button" :disabled="reviewSaving" @click="submitReview">
            {{ reviewSaving ? '提交中…' : '提交复核' }}
          </button>
        </div>
      </div>
    </div>

    <!-- 稳定性剖面带抽屉 -->
    <div v-if="profileOpen" class="drawer-mask" @click.self="profileOpen = false">
      <div class="drawer">
        <div class="drawer-head">
          <div>
            <h3>稳定性剖面带 · {{ profileSlope?.['边坡编号'] }}（{{ profileSlope?.['所属路段'] }}）</h3>
            <p class="page-desc">
              沿里程展示坡高 / 防护形式 / 巡检间隔 / 相邻病害；点选红色偏离坡段即可生成工程踏勘任务。
            </p>
          </div>
          <button class="btn ghost" type="button" @click="profileOpen = false">关闭</button>
        </div>

        <div class="profile-summary">
          <span>本带测点：{{ profileTotal }}</span>
          <span class="risk-text">风险点数（去重）：{{ profileRiskPoints }}</span>
          <span>已选偏离点：{{ selectedPoints.length }}</span>
          <button
            class="btn primary"
            type="button"
            :disabled="!selectedPoints.length || !canCreateSurvey"
            :title="canCreateSurvey ? '生成工程踏勘任务并同步三表' : '仅「已发布」后的结论可生成踏勘任务'"
            @click="createSurveyTasks"
          >
            生成工程踏勘任务
          </button>
          <button class="btn" type="button" :disabled="profilePage <= 1" @click="loadProfile(profilePage - 1)">
            上一段
          </button>
          <button
            class="btn"
            type="button"
            :disabled="profilePage * profileSize >= profileTotal"
            @click="loadProfile(profilePage + 1)"
          >
            下一段
          </button>
          <span class="page-desc">第 {{ profilePage }} 段（风险点数为全量去重值，勿跨段累加）</span>
        </div>

        <div class="profile-band">
          <div
            v-for="point in profilePoints"
            :key="String(point.point_key)"
            class="profile-cell"
            :class="{ risk: point.deviation, selected: isSelected(point.point_key) }"
            :title="point.deviation ? '偏离坡段，点击生成踏勘任务' : '正常坡段'"
            @click="togglePoint(point)"
          >
            <div class="cell-mileage">{{ point['里程桩号'] }}</div>
            <div class="cell-height">坡高 {{ point['坡高'] }}m</div>
            <div class="cell-protect">{{ point['防护形式'] }}</div>
            <div class="cell-interval">巡检 {{ point['巡检间隔'] }} 天</div>
            <div class="cell-disease">{{ point['相邻病害'] }}</div>
            <div v-if="point.deviation" class="cell-flag">偏离 · {{ point['投影状态'] }}</div>
          </div>
        </div>

        <h4>踏勘任务</h4>
        <table class="data-table">
          <thead>
            <tr><th>任务编号</th><th>里程桩号</th><th>踏勘事由</th><th>状态</th><th>来源版本</th></tr>
          </thead>
          <tbody>
            <tr v-for="task in surveyTasks" :key="String(task.id)">
              <td>{{ task['任务编号'] }}</td>
              <td>{{ task['里程桩号'] }}</td>
              <td>{{ task['踏勘事由'] }}</td>
              <td>{{ task['任务状态'] }}</td>
              <td>v{{ Number(task['来源版本'] ?? 0) }}</td>
            </tr>
            <tr v-if="!surveyTasks.length">
              <td colspan="5" class="empty-state">尚未生成踏勘任务</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | boolean | null>
type ProfilePoint = Row & { point_key: string; deviation: boolean }
type SurveyTask = Row

const ENDPOINT = '/api/slope'
const columns = ['边坡编号', '所属路段', '边坡类型', '坡高', '防护形式', '稳定性评级', '最近巡检', '边坡状态']
const statuses = ['待复核', '待发布', '稳定', '局部变形', '失稳', '踏勘中']
const ratings = ['稳定', '较稳定', '较差', '不稳定']
const stageOrder = ['待复核', '已复核', '已发布', '踏勘中', '已踏勘']
const stageIndex: Record<string, number> = {
  待复核: 0,
  已复核: 1,
  已发布: 2,
  踏勘中: 3,
  已踏勘: 4,
  历史归档: -1,
}

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const noticeMessage = ref('')
const keyword = ref('')
const statusFilter = ref('')

const riskStats = ref([
  { label: '风险点数（去重）', value: 0 },
  { label: '已投影测点', value: 0 },
  { label: '待踏勘任务', value: 0 },
  { label: '待复核结论', value: 0 },
  { label: '异常边坡', value: 0 },
])

function stageClass(row: Row, idx: number): string {
  const current = stageIndex[String(row['结论阶段'] ?? '')] ?? -2
  if (current === -1) return idx === 0 ? 'archived' : 'idle'
  if (idx < current) return 'done'
  if (idx === current) return 'current'
  return 'idle'
}

function canReview(row: Row): boolean {
  const stage = String(row['结论阶段'] ?? '')
  return stage === '待复核' || stage === '已复核'
}

function resetFilters() {
  keyword.value = ''
  statusFilter.value = ''
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function flash(message: string, isError = false) {
  errorMessage.value = isError ? message : ''
  noticeMessage.value = isError ? '' : message
  if (!isError) {
    window.setTimeout(() => {
      noticeMessage.value = ''
    }, 5000)
  }
}

async function postAction(
  path: string,
  body: unknown,
  successReload = true,
): Promise<{ ok: boolean; message: string; entry: Row | null }> {
  const response = await request(path, { method: 'POST', body: JSON.stringify(body) })
  const payload = (await response.json()) as { ok: boolean; message: string; entry: Row | null }
  if (!payload.ok && payload.entry && Number((payload.entry as Row).code) === 409) {
    // 版本锁冲突：结论已被他人推进，强制刷新后由最近一次现场复核生效
    flash(`版本冲突：${payload.message}。列表已刷新，请基于最新结论重试。`, true)
    await reload()
    return payload
  }
  if (!payload.ok) {
    flash(payload.message || '操作被拒绝', true)
    return payload
  }
  flash(payload.message)
  if (successReload) {
    await reload()
  }
  return payload
}

// ---- 现场复核 ----
const reviewOpen = ref(false)
const reviewSaving = ref(false)
const reviewForm = ref<{
  slope: Row | null
  复核人: string
  现场评级: string
  结论说明: string
}>({ slope: null, 复核人: '', 现场评级: '', 结论说明: '' })

function openReview(row: Row) {
  reviewForm.value = { slope: row, 复核人: '', 现场评级: '', 结论说明: '' }
  reviewOpen.value = true
}

async function submitReview() {
  const slope = reviewForm.value.slope
  if (!slope) return
  if (!reviewForm.value.复核人 || !reviewForm.value.现场评级) {
    flash('请填写复核人并选择现场评级', true)
    return
  }
  reviewSaving.value = true
  try {
    const result = await postAction(`${ENDPOINT}/${slope.id}/reviews`, {
      expected_version: Number(slope.version),
      复核人: reviewForm.value.复核人,
      现场评级: reviewForm.value.现场评级,
      结论说明: reviewForm.value.结论说明,
    })
    if (result.ok) {
      reviewOpen.value = false
    }
  } finally {
    reviewSaving.value = false
  }
}

// ---- 评级发布 ----
async function publish(row: Row) {
  await postAction(`${ENDPOINT}/${row.id}/publish`, { expected_version: Number(row.version) })
}

// ---- 踏勘完成 ----
async function completeSurvey(row: Row) {
  await postAction(`${ENDPOINT}/${row.id}/survey-complete`, {
    expected_version: Number(row.version),
  })
}

// ---- 稳定性剖面带 ----
const profileOpen = ref(false)
const profileSlope = ref<Row | null>(null)
const profilePoints = ref<ProfilePoint[]>([])
const profileTotal = ref(0)
const profileRiskPoints = ref(0)
const profilePage = ref(1)
const profileSize = 8
const selectedKeys = ref<Set<string>>(new Set())
const surveyTasks = ref<SurveyTask[]>([])

const selectedPoints = computed(() =>
  profilePoints.value.filter((point) => selectedKeys.value.has(point.point_key)),
)
const canCreateSurvey = computed(() => {
  const stage = String(profileSlope.value?.['结论阶段'] ?? '')
  return stage === '已发布' || stage === '踏勘中'
})

async function openProfile(row: Row) {
  profileSlope.value = row
  profileOpen.value = true
  selectedKeys.value = new Set()
  await loadProfile(1)
  await loadSurveys()
}

async function loadProfile(page: number) {
  if (!profileSlope.value) return
  profilePage.value = page
  const response = await request(
    `${ENDPOINT}/profile?slope_id=${profileSlope.value.id}&page=${page}&size=${profileSize}`,
  )
  const payload = (await response.json()) as {
    items: ProfilePoint[]
    total: number
    risk_points: number
  }
  profilePoints.value = payload.items
  profileTotal.value = payload.total
  // 风险点数由服务端按 point_key 全量去重，分段切换时这个值不变
  profileRiskPoints.value = payload.risk_points
}

async function loadSurveys() {
  if (!profileSlope.value) return
  const response = await request(`${ENDPOINT}/surveys?slope_id=${profileSlope.value.id}`)
  const payload = (await response.json()) as { items: SurveyTask[] }
  surveyTasks.value = payload.items
}

function isSelected(key: string): boolean {
  return selectedKeys.value.has(key)
}

function togglePoint(point: ProfilePoint) {
  if (!point.deviation) {
    flash('该测点未偏离预警阈值，只有偏离坡段才能生成工程踏勘任务', true)
    return
  }
  const next = new Set(selectedKeys.value)
  if (next.has(point.point_key)) {
    next.delete(point.point_key)
  } else {
    next.add(point.point_key)
  }
  selectedKeys.value = next
  errorMessage.value = ''
}

async function createSurveyTasks() {
  const slope = profileSlope.value
  if (!slope || !selectedKeys.value.size) return
  const result = await postAction(
    `${ENDPOINT}/${slope.id}/survey-tasks`,
    {
      expected_version: Number(slope.version),
      point_keys: [...selectedKeys.value],
    },
    false,
  )
  if (result.ok) {
    selectedKeys.value = new Set()
    await loadProfile(profilePage.value)
    await loadSurveys()
    await reload()
    // 同步最新坡段版本，避免下一次操作撞版本锁
    const fresh = rows.value.find((item) => item.id === slope.id)
    if (fresh) {
      profileSlope.value = fresh
    }
  }
}

async function loadRiskStats() {
  try {
    const response = await request(`${ENDPOINT}/risk-summary`)
    const payload = (await response.json()) as Record<string, number>
    riskStats.value = [
      { label: '风险点数（去重）', value: payload.risk_points ?? 0 },
      { label: '已投影测点', value: payload.projected_points ?? 0 },
      { label: '待踏勘任务', value: payload.pending_surveys ?? 0 },
      { label: '待复核结论', value: payload.pending_reviews ?? 0 },
      { label: '异常边坡', value: payload.abnormal_slopes ?? 0 },
    ]
  } catch {
    // 统计接口不可用时保留 0，不阻塞台账读取
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams()
  if (keyword.value) query.set('keyword', keyword.value)
  if (statusFilter.value) query.set('status', statusFilter.value)
  try {
    const response = await request(`${ENDPOINT}?${query.toString()}`)
    if (!response.ok) {
      throw new Error('边坡列表读取失败')
    }
    const payload = (await response.json()) as { items: Row[]; total: number }
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    await loadRiskStats()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '边坡防护列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.notice-text {
  color: #067647;
}
.workflow-cell {
  min-width: 320px;
}
.stage-track {
  display: flex;
  align-items: center;
  gap: 4px;
  list-style: none;
  margin: 0 0 6px;
  padding: 0;
}
.stage-node {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  color: #94a3b8;
}
.stage-node:not(:last-child)::after {
  content: '→';
  margin: 0 2px;
  color: #cbd5e1;
}
.stage-dot {
  display: inline-flex;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  align-items: center;
  justify-content: center;
  background: #e2e8f0;
  color: #64748b;
  font-size: 11px;
}
.stage-node.done .stage-dot {
  background: #a7f3d0;
  color: #065f46;
}
.stage-node.done {
  color: #067647;
}
.stage-node.current .stage-dot {
  background: #1f6feb;
  color: #fff;
}
.stage-node.current {
  color: #1f6feb;
  font-weight: 600;
}
.stage-node.archived .stage-dot {
  background: #cbd5e1;
}
.link:disabled {
  color: #94a3b8;
  cursor: not-allowed;
}
.modal-mask,
.drawer-mask {
  position: fixed;
  inset: 0;
  background: rgba(15, 23, 42, 0.45);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 50;
}
.modal {
  width: 460px;
  background: #fff;
  border-radius: 10px;
  padding: 18px 20px;
}
.form-row {
  display: block;
  margin: 10px 0;
}
.form-row span {
  display: block;
  font-size: 12px;
  color: #64748b;
  margin-bottom: 4px;
}
.form-row input,
.form-row select,
.form-row textarea {
  width: 100%;
  padding: 6px 8px;
  border: 1px solid #d8dee6;
  border-radius: 6px;
  font: inherit;
}
.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 14px;
}
.drawer {
  width: min(960px, 92vw);
  max-height: 90vh;
  overflow: auto;
  background: #fff;
  border-radius: 10px;
  padding: 18px 20px;
}
.drawer-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
}
.profile-summary {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 12px;
  margin: 12px 0;
  font-size: 13px;
}
.risk-text {
  color: #b42318;
  font-weight: 600;
}
.profile-band {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 10px;
  margin-bottom: 16px;
}
@media (max-width: 900px) {
  .profile-band {
    grid-template-columns: repeat(2, 1fr);
  }
}
.profile-cell {
  border: 1px solid #d8dee6;
  border-radius: 8px;
  padding: 8px 10px;
  font-size: 12px;
  background: #f8fafc;
  cursor: default;
}
.profile-cell .cell-mileage {
  font-weight: 600;
  margin-bottom: 4px;
}
.profile-cell .cell-disease {
  color: #475569;
  margin-top: 2px;
}
.profile-cell.risk {
  border-color: #fda29b;
  background: #fef3f2;
  cursor: pointer;
}
.profile-cell.risk:hover {
  border-color: #d92d20;
}
.profile-cell.selected {
  border-width: 2px;
  border-color: #1f6feb;
  box-shadow: 0 0 0 2px rgba(31, 111, 235, 0.15);
}
.cell-flag {
  margin-top: 4px;
  color: #b42318;
  font-weight: 600;
}
</style>
