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

## 边坡风险结论工作流

边坡防护（`slope`）在普通台账之外，由一套**单一版本状态机**驱动风险结论，阶段只能顺序
推进，跳级与倒序都会被拒绝：

```text
待复核 ──现场复核──▶ 已复核 ──评级发布──▶ 已发布 ──点选偏离坡段生成踏勘任务──▶ 踏勘中 ──踏勘完成──▶ 已踏勘
```

- **现场复核**（`POST /api/slope/{id}/reviews`）：记录现场评级；已复核后再提交视为改评，
  旧版本作废（`superseded=true`）。评级冲突一律以**最近一次现场复核**为准。
- **评级发布**（`POST /api/slope/{id}/publish`）：结论在同一个数据库事务内写入
  **边坡台账、路面病害待办、工程清单**（`pavement`/`project` 行带 `来源=slope-conclusion:<id>`，
  重复发布按来源键幂等更新）。
- **稳定性剖面带**（`GET /api/slope/profile`）：沿里程返回每个测点的坡高、防护形式、
  巡检间隔、相邻病害与偏离标记，支持分段加载。风险点数按测点主键 `point_key` 全量去重统计，
  随每段返回同一个 `risk_points`，前端不能跨段累加。
- **点选偏离坡段生成踏勘任务**（`POST /api/slope/{id}/survey-tasks`）：只接受偏离测点，
  任务按 `point_key` 幂等；任务生成与剖面结论（边坡台账、路面病害清单、工程待办）在同一
  事务提交，概览风险点数同步重算。
- **并发版本锁**：所有推进动作都要带 `expected_version`，服务端在事务内二次校验；版本过期
  返回业务冲突（`ok=false`，`entry.code=409`），并发改评只有一个结论生效。
- **历史坡段**：`结论阶段=历史归档`（`version=0`）的坡段按原防护前提保留，拒绝一切评级流转。
- 踏勘任务清单见 `GET /api/slope/surveys`，单坡全貌（版本历史+剖面+任务）见
  `GET /api/slope/{id}/workflow`，风险总览数字见 `GET /api/slope/risk-summary`。

存储层（`app/store.py`）提供 `store.transaction()` 工作单元：异常时按操作日志逆序回滚，
并通过版本号快照保证并发结论的一致性；`/api/overview` 新增“边坡风险点数”卡片。
