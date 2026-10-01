# 市政道路桥梁养护管理平台

覆盖道路巡查、桥隧定检、路面病害、交安设施、绿化管养、除雪防汛及养护工程管理的市政道桥全要素养护后台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 路段管理 | `road_section` | 管养路段 | 路段编号、路段名称、起止桩号 |
| 日常巡查 | `patrol` | 巡查记录 | 巡查编号、巡查路段、巡查日期 |
| 路面病害 | `pavement` | 病害记录 | 病害编号、所属路段、病害类型 |
| 桥梁定检 | `bridge` | 检测记录 | 检测编号、桥梁名称、检测类型 |
| 桥梁档案 | `bridge_info` | 桥梁 | 桥梁编号、桥梁名称、桥型结构 |
| 隧道管养 | `tunnel` | 隧道 | 隧道编号、隧道名称、隧道长度 |
| 交安设施 | `traffic_facility` | 交安设施 | 设施编号、设施类型、所属路段 |
| 排水设施 | `drainage` | 排水设施 | 设施编号、设施类型、所属路段 |
| 绿化管养 | `green` | 绿化区域 | 区域编号、区域名称、植物品种 |
| 路灯照明 | `lighting` | 路灯设施 | 灯具编号、灯具类型、功率 |
| 除雪防滑 | `winter` | 除雪作业 | 作业编号、作业路段、作业日期 |
| 防汛应急 | `flood` | 防汛记录 | 记录编号、预警级别、影响路段 |
| 边坡防护 | `slope` | 边坡 | 边坡编号、所属路段、边坡类型 |
| 伸缩缝管理 | `expansion` | 伸缩缝 | 缝编号、所属桥梁、缝类型 |
| 支座维护 | `bearing` | 桥梁支座 | 支座编号、所属桥梁、支座类型 |
| 养护工程 | `project` | 养护工程 | 工程编号、工程名称、工程类型 |
| 养护车辆 | `vehicle` | 养护车辆 | 车辆编号、车辆类型、车牌号 |
| 养护材料 | `material` | 养护材料 | 材料编号、材料名称、材料类别 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 边坡防护：结论版本状态机与风险联动

边坡风险结论由单一版本状态机驱动，只允许顺序推进，跳级 / 倒序一律拒绝：

```text
待复核 ──现场复核──▶ 待发布 ──评级发布──▶ 待生成踏勘 ──生成踏勘任务──▶ 踏勘中
  ▲                                                                     │
  └──────────────────── 踏勘任务全部完成 ◀──────────────────────────────┘
```

- 评级为稳定/基本稳定且无偏离坡段时，发布后直接闭环回「待复核」。
- 每次现场复核生成新的 `version`；提交必须带 `expected_version`，
  版本过期返回 409——并发改评由进程锁串行化，只有一个结论生效。
  评级冲突以最近一次现场复核为准，复核历史保留在「复核记录」里。
- 评级发布在 `store.transaction()` 内同时刷写边坡台账、路面病害待办
  （`pavement._边坡来源`）与工程清单（`project._边坡来源`），异常整体回滚。
- 稳定性剖面带（`GET /api/slope/segments`、`GET /api/slope/{id}/profile`）
  沿里程展示坡高、防护形式、巡检间隔与相邻路面病害；分段只读分页加载，
  不产生写入。点选偏离空档（既有分段未覆盖的桩号）经
  `POST /api/slope/{id}/survey-tasks` 生成工程踏勘任务，重复测点去重。
- 历史分段保留建档时的「防护形式@结论版本」前提，新分段继承当前防护前提。
- 风险投影（`risk_projection` 内部表）按边坡合并去重，是运营概览
  「风险点数」卡片的唯一数据源，踏勘任务再多也不重复计数。

后端回归测试：`cd backend && .venv/bin/python -m pytest tests/ -q`。
