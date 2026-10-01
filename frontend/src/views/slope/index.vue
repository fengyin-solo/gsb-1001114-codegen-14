<template>
  <section class="page" data-module="slope">
    <header class="page-head">
      <div>
        <h2>边坡防护管理</h2>
        <p class="page-desc">
          边坡风险结论由单一版本状态机驱动：现场复核 → 评级发布 → 踏勘任务顺序推进；
          发布结论事务同步边坡台账、路面病害待办与工程清单。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" :class="{ primary: tab === 'ledger' }" type="button" @click="switchTab('ledger')">边坡台账</button>
        <button class="btn" :class="{ primary: tab === 'profile' }" type="button" @click="switchTab('profile')">稳定性剖面带</button>
        <button class="btn primary" type="button" @click="openCreate">登记边坡</button>
        <button class="btn" type="button" @click="exportRows">导出清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <!-- ============================ 边坡台账 ============================ -->
    <template v-if="tab === 'ledger'">
      <form class="filter-bar" @submit.prevent="reload">
        <label class="filter-item">
          <span>边坡编号 / 所属路段</span>
          <input v-model="keyword" placeholder="按编号或路段检索" />
        </label>
        <label class="filter-item">
          <span>结论阶段</span>
          <select v-model="phaseFilter">
            <option value="">全部</option>
            <option v-for="phase in phases" :key="phase" :value="phase">{{ phase }}</option>
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
            <th>结论阶段</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="String(row.id)">
            <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
            <td>v{{ row.version }}</td>
            <td><span :class="['phase-tag', phaseClass(row.phase)]">{{ row.phase }}</span></td>
            <td class="row-actions">
              <button
                v-if="row.phase === '待复核'"
                class="link"
                type="button"
                @click="openReview(row)"
              >现场复核</button>
              <button
                v-if="row.phase === '待发布'"
                class="link"
                type="button"
                @click="publish(row)"
              >评级发布</button>
              <button
                v-if="row.phase === '待生成踏勘' || row.phase === '踏勘中'"
                class="link"
                type="button"
                @click="openProfile(row)"
              >剖面带 / 踏勘</button>
              <button class="link" type="button" @click="openProfile(row)">稳定性剖面带</button>
            </td>
          </tr>
          <tr v-if="!rows.length">
            <td :colspan="columns.length + 3" class="empty-state">暂无符合条件的边坡记录</td>
          </tr>
        </tbody>
      </table>

      <footer class="page-foot">
        <span>共 {{ total }} 条边坡记录</span>
        <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
      </footer>
    </template>

    <!-- ========================== 稳定性剖面带 ========================== -->
    <template v-else>
      <form class="filter-bar" @submit.prevent="loadSegments(1)">
        <label class="filter-item">
          <span>所属路段</span>
          <input v-model="roadFilter" placeholder="按路段过滤测点" />
        </label>
        <button class="btn" type="submit">查询</button>
        <button class="btn ghost" type="button" @click="roadFilter = ''; loadSegments(1)">重置</button>
        <span class="hint">大量测点分段加载，只读本页，不产生计数写入</span>
      </form>

      <table class="data-table">
        <thead>
          <tr>
            <th>分段编号</th>
            <th>所属边坡</th>
            <th>里程区间</th>
            <th>坡高(m)</th>
            <th>防护形式</th>
            <th>巡检间隔(天)</th>
            <th>防护前提</th>
            <th>相邻病害</th>
            <th>偏离</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="seg in segmentRows" :key="String(seg.id)">
            <td>{{ seg.分段编号 }}</td>
            <td>{{ seg.边坡编号 }} · {{ seg.所属路段 }}</td>
            <td>{{ seg.起点桩号 }}~{{ seg.终点桩号 }}</td>
            <td>{{ seg.坡高 }}</td>
            <td>{{ seg.防护形式 }}</td>
            <td>{{ seg.巡检间隔天 }}</td>
            <td><span class="premise-tag">{{ seg.防护前提 }}</span></td>
            <td>
              <span v-if="!seg.相邻病害.length" class="muted">无相邻病害</span>
              <div v-for="defect in seg.相邻病害" :key="defect.病害编号" class="defect-badge">
                {{ defect.病害编号 }} {{ defect.病害类型 }}（{{ defect.严重程度 }}）
              </div>
            </td>
            <td>
              <span v-if="seg.偏离" class="warn-text">偏离坡段</span>
              <span v-else class="muted">—</span>
            </td>
            <td class="row-actions">
              <button class="link" type="button" @click="openProfileById(seg.边坡)">查看剖面</button>
            </td>
          </tr>
          <tr v-if="!segmentRows.length">
            <td colspan="10" class="empty-state">该路段暂无剖面分段</td>
          </tr>
        </tbody>
      </table>

      <footer class="page-foot">
        <div class="pager">
          <button class="btn" type="button" :disabled="segPage <= 1" @click="loadSegments(segPage - 1)">上一页</button>
          <span>第 {{ segPage }} 页 / 共 {{ segTotalPages }} 页，测点总数 {{ segTotal }}</span>
          <button class="btn" type="button" :disabled="segPage >= segTotalPages" @click="loadSegments(segPage + 1)">下一页</button>
        </div>
        <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
      </footer>
    </template>

    <!-- ======================== 登记边坡弹窗 ======================== -->
    <div v-if="createOpen" class="modal-mask" @click.self="createOpen = false">
      <div class="modal-card">
        <h3>登记边坡</h3>
        <p class="hint">登记后进入「待复核」阶段，由现场复核开启结论版本状态机。</p>
        <label class="form-line">
          <span>边坡编号 *</span>
          <input v-model="createForm.边坡编号" placeholder="如：SLOP-0004" />
        </label>
        <label class="form-line">
          <span>所属路段 *</span>
          <input v-model="createForm.所属路段" placeholder="如：连云大道" />
        </label>
        <label class="form-line">
          <span>边坡类型 *</span>
          <input v-model="createForm.边坡类型" placeholder="如：路堑高边坡" />
        </label>
        <div class="form-row">
          <label class="form-line">
            <span>起点桩号 *</span>
            <input v-model="createForm.起点桩号" placeholder="K12+000" />
          </label>
          <label class="form-line">
            <span>终点桩号 *</span>
            <input v-model="createForm.终点桩号" placeholder="K12+700" />
          </label>
        </div>
        <div class="form-row">
          <label class="form-line">
            <span>坡高</span>
            <input v-model="createForm.坡高" placeholder="如 18m" />
          </label>
          <label class="form-line">
            <span>防护形式</span>
            <input v-model="createForm.防护形式" placeholder="如：锚杆框架" />
          </label>
        </div>
        <div class="modal-actions">
          <button class="btn ghost" type="button" @click="createOpen = false">取消</button>
          <button class="btn primary" type="button" :disabled="submitting" @click="submitCreate">
            {{ submitting ? '提交中…' : '登记' }}
          </button>
        </div>
      </div>
    </div>

    <!-- ======================== 现场复核弹窗 ======================== -->
    <div v-if="reviewOpen" class="modal-mask" @click.self="reviewOpen = false">
      <div class="modal-card">
        <h3>现场复核 · {{ reviewForm.slopeNo }}（v{{ reviewForm.expectedVersion }}）</h3>
        <p class="hint">复核后进入「待发布」。评级冲突时以最近一次现场复核为准，过期版本提交会被拒绝。</p>
        <label class="form-line">
          <span>稳定性评级</span>
          <select v-model="reviewForm.rating">
            <option value="" disabled>请选择评级</option>
            <option v-for="rating in ratings" :key="rating" :value="rating">{{ rating }}</option>
          </select>
        </label>
        <label class="form-line">
          <span>防护形式（复核时现状）</span>
          <input v-model="reviewForm.protection" placeholder="如：锚杆框架 / 锚索格构" />
        </label>
        <label class="form-line">
          <span>复核说明</span>
          <textarea v-model="reviewForm.note" rows="3" placeholder="现场变形、渗水、裂缝等情况"></textarea>
        </label>
        <div class="modal-actions">
          <button class="btn ghost" type="button" @click="reviewOpen = false">取消</button>
          <button class="btn primary" type="button" :disabled="submitting" @click="submitReview">
            {{ submitting ? '提交中…' : '提交现场复核' }}
          </button>
        </div>
      </div>
    </div>

    <!-- ===================== 稳定性剖面带侧滑面板 ===================== -->
    <div v-if="profileOpen" class="drawer-mask" @click.self="profileOpen = false">
      <div class="drawer-card">
        <header class="drawer-head">
          <div>
            <h3>{{ profileSlope?.边坡编号 }} 稳定性剖面带</h3>
            <p class="hint">
              {{ profileSlope?.所属路段 }} {{ profileSlope?.起点桩号 }}~{{ profileSlope?.终点桩号 }}
              ｜评级：{{ profileSlope?.稳定性评级 }} ｜阶段：
              <span :class="['phase-tag', phaseClass(profileSlope?.phase)]">{{ profileSlope?.phase }}</span>
              ｜结论 v{{ profileSlope?.version }}
            </p>
          </div>
          <button class="btn" type="button" @click="profileOpen = false">关闭</button>
        </header>

        <!-- 沿里程的剖面带：分段为实心块，未覆盖的偏离空档为斜纹可点选区 -->
        <div class="band-wrap">
          <div
            v-for="cell in bandCells"
            :key="cell.key"
            :class="['band-cell', cell.kind]"
            :style="{ left: cell.left + '%', width: cell.width + '%' }"
            :title="cell.tooltip"
            @click="cell.kind === 'gap' && onPickGap(cell)"
          >
            <template v-if="cell.kind === 'segment'">
              <strong>{{ cell.segment.分段编号 }}</strong>
              <span>坡高 {{ cell.segment.坡高 }}m</span>
              <span>{{ cell.segment.防护形式 }}</span>
              <span>每 {{ cell.segment.巡检间隔天 }} 天巡检</span>
              <em v-if="cell.segment.相邻病害.length" class="band-defect">
                ⚠ {{ cell.segment.相邻病害.map(d => d.病害编号).join('、') }}
              </em>
            </template>
            <template v-else>
              <strong>偏离坡段</strong>
              <span>{{ cell.startStake }}~{{ cell.endStake }}</span>
              <em class="band-action">点选生成踏勘任务</em>
            </template>
          </div>
        </div>
        <div class="band-axis">
          <span>{{ profileSlope?.起点桩号 }}</span>
          <span>{{ profileSlope?.终点桩号 }}</span>
        </div>

        <section class="drawer-section">
          <h4>工程踏勘任务</h4>
          <table class="data-table compact">
            <thead>
              <tr><th>任务编号</th><th>来源</th><th>桩号</th><th>状态</th><th>结论版本</th><th>操作</th></tr>
            </thead>
            <tbody>
              <tr v-for="task in profileTasks" :key="String(task.id)">
                <td>{{ task.任务编号 }}</td>
                <td>{{ task.来源 }}</td>
                <td>{{ task.偏移桩号 }}</td>
                <td>{{ task.status }}</td>
                <td>v{{ task.结论版本 }}</td>
                <td>
                  <button v-if="task.pending" class="link" type="button" @click="completeTask(task)">完成踏勘</button>
                  <span v-else class="muted">已完成</span>
                </td>
              </tr>
              <tr v-if="!profileTasks.length">
                <td colspan="6" class="empty-state">暂无踏勘任务，点选上方偏离坡段即可生成</td>
              </tr>
            </tbody>
          </table>
          <p v-if="profileSlope?.phase === '踏勘中'" class="hint">
            踏勘中可继续补点；全部任务完成后状态机回到「待复核」，方可发起新一轮现场复核。
          </p>
        </section>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | boolean | null>
type Segment = {
  id: number
  边坡: number
  边坡编号: string
  所属路段: string
  分段编号: string
  起点桩号: string
  终点桩号: string
  起点米: number
  终点米: number
  坡高: number
  防护形式: string
  巡检间隔天: number
  防护前提: string
  偏离: boolean
  相邻病害: { 病害编号: string; 病害类型: string; 严重程度: string }[]
}
type SurveyTask = {
  id: number
  任务编号: string
  来源: string
  偏移桩号: string
  status: string
  pending: boolean
  结论版本: number
}
type SlopeRow = Row & {
  id: number
  version: number
  phase: string
  边坡编号: string
  稳定性评级: string
}

const ENDPOINT = '/api/slope'
const columns = ['边坡编号', '所属路段', '边坡类型', '坡高', '防护形式', '稳定性评级', '最近巡检']
const phases = ['待复核', '待发布', '待生成踏勘', '踏勘中']
const ratings = ['稳定', '基本稳定', '欠稳定', '不稳定']

const tab = ref<'ledger' | 'profile'>('ledger')
const rows = ref<SlopeRow[]>([])
const total = ref(0)
const errorMessage = ref('')
const keyword = ref('')
const phaseFilter = ref('')
const submitting = ref(false)

const stats = ref([
  { label: '风险点数', value: 0 },
  { label: '待踏勘任务', value: 0 },
  { label: '待发布结论', value: 0 },
  { label: '踏勘中边坡', value: 0 },
])

// ------------------------------------------------- 剖面带总览（分段分页）
const segmentRows = ref<Segment[]>([])
const segPage = ref(1)
const segSize = 5
const segTotal = ref(0)
const roadFilter = ref('')
const segTotalPages = computed(() => Math.max(1, Math.ceil(segTotal.value / segSize)))

// ------------------------------------------------------------- 复核弹窗
const reviewOpen = ref(false)
const reviewForm = ref({
  slopeId: 0,
  slopeNo: '',
  rating: '',
  protection: '',
  note: '',
  expectedVersion: 1,
})

// ------------------------------------------------------------- 登记弹窗
const createOpen = ref(false)
const createForm = ref({
  边坡编号: '',
  所属路段: '',
  边坡类型: '',
  起点桩号: '',
  终点桩号: '',
  坡高: '',
  防护形式: '',
})

function openCreate() {
  createForm.value = {
    边坡编号: '',
    所属路段: '',
    边坡类型: '',
    起点桩号: '',
    终点桩号: '',
    坡高: '',
    防护形式: '',
  }
  createOpen.value = true
}

async function submitCreate() {
  submitting.value = true
  errorMessage.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST',
      body: JSON.stringify({ values: { ...createForm.value } }),
    })
    const data = await response.json()
    if (!response.ok || data.ok === false) {
      errorMessage.value = data.detail || data.message || '边坡登记失败'
      return
    }
    createOpen.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '边坡登记失败'
  } finally {
    submitting.value = false
  }
}

// ----------------------------------------------------------- 剖面侧滑面板
const profileOpen = ref(false)
const profileSlope = ref<SlopeRow | null>(null)
const profileSegments = ref<Segment[]>([])
const profileTasks = ref<SurveyTask[]>([])

type BandCell =
  | {
      key: string
      kind: 'segment'
      left: number
      width: number
      tooltip: string
      segment: Segment
    }
  | {
      key: string
      kind: 'gap'
      left: number
      width: number
      tooltip: string
      startStake: string
      endStake: string
      stake: string
    }

const bandCells = computed<BandCell[]>(() => {
  const slope = profileSlope.value
  if (!slope) return []
  const startM = parseStake(String(slope.起点桩号))
  const endM = parseStake(String(slope.终点桩号))
  if (startM === null || endM === null || endM <= startM) return []
  const span = endM - startM
  const pct = (m: number) => ((m - startM) / span) * 100
  const cells: BandCell[] = []
  const segs = [...profileSegments.value].sort((a, b) => a.起点米 - b.起点米)
  let cursor = startM
  segs.forEach((segment, index) => {
    if (segment.起点米 > cursor) {
      cells.push({
        key: `gap-${index}`,
        kind: 'gap',
        left: pct(cursor),
        width: Math.max(pct(segment.起点米) - pct(cursor), 1.5),
        startStake: formatMeter(cursor),
        endStake: formatMeter(segment.起点米),
        stake: formatMeter((cursor + segment.起点米) / 2),
        tooltip: `偏离空档 ${formatMeter(cursor)}~${formatMeter(segment.起点米)}，点选生成踏勘任务`,
      })
    }
    cells.push({
      key: `seg-${segment.id}`,
      kind: 'segment',
      left: pct(segment.起点米),
      width: Math.max(pct(segment.终点米) - pct(segment.起点米), 1.5),
      tooltip: `${segment.分段编号} 坡高${segment.坡高}m ${segment.防护形式}`,
      segment,
    })
    cursor = Math.max(cursor, segment.终点米)
  })
  if (cursor < endM) {
    cells.push({
      key: 'gap-tail',
      kind: 'gap',
      left: pct(cursor),
      width: Math.max(100 - pct(cursor), 1.5),
      startStake: formatMeter(cursor),
      endStake: formatMeter(endM),
      stake: formatMeter((cursor + endM) / 2),
      tooltip: `尾部偏离空档 ${formatMeter(cursor)}~${formatMeter(endM)}，点选生成踏勘任务`,
    })
  }
  return cells
})

function parseStake(text: string): number | null {
  const match = /K?\s*(\d+)\s*\+\s*(\d+(?:\.\d+)?)/i.exec(text)
  if (match) return Number(match[1]) * 1000 + Number(match[2])
  const value = Number(text)
  return Number.isFinite(value) ? value : null
}

function formatMeter(meter: number): string {
  const rounded = Math.round(meter)
  return `K${Math.floor(rounded / 1000)}+${String(rounded % 1000).padStart(3, '0')}`
}

function phaseClass(phase?: string): string {
  return {
    待复核: 'phase-init',
    待发布: 'phase-reviewed',
    待生成踏勘: 'phase-waiting',
    踏勘中: 'phase-surveying',
  }[phase ?? ''] ?? ''
}

function resetFilters() {
  keyword.value = ''
  phaseFilter.value = ''
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

async function readError(response: Response, fallback: string): Promise<string> {
  try {
    const data = await response.json()
    return data.detail || data.message || fallback
  } catch {
    return fallback
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams()
  if (keyword.value) query.set('keyword', keyword.value)
  if (phaseFilter.value) query.set('status', phaseFilter.value)
  try {
    const response = await request(`${ENDPOINT}?${query.toString()}`)
    if (!response.ok) throw new Error('边坡列表读取失败')
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '边坡列表读取失败'
  }
  void loadSummary()
}

async function loadSummary() {
  try {
    const response = await request(`${ENDPOINT}/risk-summary`)
    if (!response.ok) return
    const data = await response.json()
    stats.value = [
      { label: '风险点数', value: data.风险点数 ?? 0 },
      { label: '待踏勘任务', value: data.待踏勘任务 ?? 0 },
      { label: '待发布结论', value: data.阶段分布?.待发布 ?? 0 },
      { label: '踏勘中边坡', value: data.阶段分布?.踏勘中 ?? 0 },
    ]
  } catch {
    // 汇总卡片加载失败不阻塞台账操作
  }
}

async function loadSegments(page: number) {
  segPage.value = Math.max(1, page)
  errorMessage.value = ''
  const query = new URLSearchParams({ page: String(segPage.value), size: String(segSize) })
  if (roadFilter.value) query.set('road', roadFilter.value)
  try {
    const response = await request(`${ENDPOINT}/segments?${query.toString()}`)
    if (!response.ok) throw new Error('剖面分段读取失败')
    const payload = await response.json()
    segmentRows.value = payload.items ?? []
    segTotal.value = payload.total ?? 0
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '剖面分段读取失败'
  }
}

function switchTab(target: 'ledger' | 'profile') {
  tab.value = target
  if (target === 'profile') void loadSegments(1)
  else void reload()
}

// ----------------------------------------------------------- 状态机动作
function openReview(row: SlopeRow) {
  reviewForm.value = {
    slopeId: row.id,
    slopeNo: row.边坡编号,
    rating: '',
    protection: '',
    note: '',
    expectedVersion: row.version,
  }
  reviewOpen.value = true
}

async function submitReview() {
  if (!reviewForm.value.rating) {
    errorMessage.value = '请先选择稳定性评级'
    return
  }
  submitting.value = true
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${reviewForm.value.slopeId}/reviews`, {
      method: 'POST',
      body: JSON.stringify({
        values: {
          rating: reviewForm.value.rating,
          protection: reviewForm.value.protection,
          note: reviewForm.value.note,
          expected_version: reviewForm.value.expectedVersion,
        },
      }),
    })
    const data = await response.json()
    if (!response.ok || data.ok === false) {
      errorMessage.value = data.detail || data.message || '现场复核未生效'
      return
    }
    reviewOpen.value = false
    await reload()
    if (profileOpen.value) await loadProfile(reviewForm.value.slopeId)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '现场复核提交失败'
  } finally {
    submitting.value = false
  }
}

async function publish(row: SlopeRow) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/publish`, {
      method: 'POST',
      body: JSON.stringify({ values: { expected_version: row.version } }),
    })
    const data = await response.json()
    if (!response.ok || data.ok === false) {
      errorMessage.value = data.detail || data.message || '评级发布未生效'
      return
    }
    window.alert(data.message)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '评级发布失败'
  }
}

// ------------------------------------------------------------- 剖面面板
async function openProfileById(slopeId: number) {
  await loadProfile(slopeId)
}

async function openProfile(row: SlopeRow) {
  await loadProfile(row.id)
}

async function loadProfile(slopeId: number) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${slopeId}/profile`)
    if (!response.ok) {
      errorMessage.value = await readError(response, '剖面带读取失败')
      return
    }
    const data = await response.json()
    profileSlope.value = data.slope
    profileSegments.value = data.segments ?? []
    profileTasks.value = data.tasks ?? []
    profileOpen.value = true
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '剖面带读取失败'
  }
}

async function onPickGap(cell: Extract<BandCell, { kind: 'gap' }>) {
  const slope = profileSlope.value
  if (!slope) return
  if (slope.phase !== '待生成踏勘' && slope.phase !== '踏勘中') {
    window.alert('需先完成现场复核与评级发布，且结论存在风险时，才能为偏离坡段生成踏勘任务')
    return
  }
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${slope.id}/survey-tasks`, {
      method: 'POST',
      body: JSON.stringify({
        values: { expected_version: slope.version, points: [{ stake: cell.stake }] },
      }),
    })
    const data = await response.json()
    if (!response.ok || data.ok === false) {
      // 409 版本冲突时提示用户刷新；其他业务原因直接透出
      errorMessage.value = data.detail || data.message || '踏勘任务未生成'
      if (response.status === 409) await loadProfile(slope.id)
      return
    }
    await loadProfile(slope.id)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '踏勘任务生成失败'
  }
}

async function completeTask(task: SurveyTask) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/survey-tasks/${task.id}/complete`, { method: 'POST' })
    const data = await response.json()
    if (!response.ok || data.ok === false) {
      errorMessage.value = data.detail || data.message || '踏勘任务完成提交失败'
      return
    }
    if (profileSlope.value) await loadProfile(profileSlope.value.id)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '踏勘任务完成提交失败'
  }
}

onMounted(() => {
  void reload()
})
</script>

<style scoped>
.hint { color: var(--muted); font-size: 12px; }
.muted { color: var(--muted); }
.warn-text { color: #b42318; font-weight: 600; }
.phase-tag { padding: 1px 8px; border-radius: 10px; font-size: 12px; white-space: nowrap; }
.phase-init { background: #e2e8f0; color: #475569; }
.phase-reviewed { background: #fef3c7; color: #92400e; }
.phase-waiting { background: #ffedd5; color: #c2410c; }
.phase-surveying { background: #dbeafe; color: #1d4ed8; }
.premise-tag { font-size: 12px; color: #475569; background: #f1f5f9; padding: 1px 6px; border-radius: 4px; }
.defect-badge { font-size: 12px; color: #b42318; }
.pager { display: flex; gap: 10px; align-items: center; }
.pager .btn:disabled { opacity: 0.5; cursor: not-allowed; }

/* 现场复核弹窗 */
.modal-mask { position: fixed; inset: 0; background: rgba(15, 23, 42, 0.45); display: flex; align-items: center; justify-content: center; z-index: 30; }
.modal-card { background: #fff; border-radius: 10px; padding: 20px 24px; width: 460px; max-width: 92vw; }
.modal-card h3 { margin: 0 0 6px; }
.form-line { display: flex; flex-direction: column; gap: 4px; margin: 12px 0; font-size: 13px; }
.form-row { display: flex; gap: 12px; }
.form-row .form-line { flex: 1; }
.form-line span { color: var(--muted); font-size: 12px; }
.form-line input, .form-line select, .form-line textarea { padding: 6px 8px; border: 1px solid var(--border); border-radius: 6px; font: inherit; }
.modal-actions { display: flex; justify-content: flex-end; gap: 10px; margin-top: 16px; }

/* 剖面带侧滑面板 */
.drawer-mask { position: fixed; inset: 0; background: rgba(15, 23, 42, 0.45); z-index: 20; display: flex; justify-content: flex-end; }
.drawer-card { background: #f8fafc; width: 78vw; max-width: 980px; height: 100%; overflow-y: auto; padding: 18px 22px; }
.drawer-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
.drawer-head h3 { margin: 0 0 4px; }
.drawer-section { margin-top: 18px; }
.drawer-section h4 { margin: 0 0 8px; }
.data-table.compact th, .data-table.compact td { padding: 6px 8px; font-size: 12px; }

/* 沿里程的剖面带 */
.band-wrap { position: relative; height: 168px; margin: 14px 0 4px; background: #fff; border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }
.band-cell { position: absolute; top: 8px; bottom: 8px; border-radius: 6px; padding: 8px 10px; display: flex; flex-direction: column; gap: 3px; font-size: 12px; overflow: hidden; }
.band-cell.segment { background: #e0edff; border: 1px solid #93c5fd; color: #1e3a8a; }
.band-cell.segment strong { font-size: 13px; }
.band-cell.gap {
  background: repeating-linear-gradient(45deg, #fff7ed, #fff7ed 8px, #fed7aa 8px, #fed7aa 16px);
  border: 1px dashed #ea580c;
  color: #9a3412;
  cursor: pointer;
  justify-content: center;
  align-items: center;
  text-align: center;
}
.band-cell.gap:hover { border-color: #c2410c; box-shadow: inset 0 0 0 2px rgba(234, 88, 12, 0.25); }
.band-defect { color: #b42318; font-style: normal; }
.band-action { color: #c2410c; font-style: normal; font-weight: 600; }
.band-axis { display: flex; justify-content: space-between; color: var(--muted); font-size: 12px; }
</style>
