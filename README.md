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

## 排水设施处置闭环

排水设施在普通台账动作之外，另有处置回传闭环（`app/services/drainage_disposal.py`）：

- **分支互斥**：设施编号缺失 `FACILITY_MISSING`、无积水 `NO_WATER`、泵站离线
  `PUMP_OFFLINE`、复测超时 `RECHECK_TIMEOUT` 各自进入「可复位」分支，台账状态、
  待办类型、清单状态都带边界口径，不与 `NORMAL_DRAINAGE` 正常排水混用。
- **现场复测优先**：监测与人工复测冲突时采信人工复测，两侧观测按各自观测时点分别
  留档（`drainage_observation`），互不覆盖。
- **三表同事务**：处置结论在一个事务内写入排水设施台账 `drainage`、设施整改待办
  `drainage_rectify_todo`、路段监测清单 `road_section_monitor`（另含观测历史与
  处置主记录），任一步失败整批回滚，只留下中断主记录。
- **中断续传**：保存中断保留原因码（`CLIENT_OFFLINE` 或
  `DB_<阶段>_FAILED@ledger/todo/monitor/...`），`POST
  /api/drainage/disposals/{id}/resume` 从失败分支继续；重复续传安全。
- **幂等回传**：按「设施编号 + 观测时间」幂等，重复回传返回首次结论不重复落库；
  复位后的旧记录不再参与幂等。
- 可复位分支完成后可 `POST /api/drainage/disposals/{id}/reset` 复位，待办与清单
  同步关闭；正常排水与未续传完成的中断单不允许复位。

回传时传 `模拟中断=true` 或 `模拟落库失败阶段=ledger|todo|monitor|observation|disposal`
可演练中断回滚与续传。

契约测试（仅依赖标准库，未装 pytest 也能跑）：

```bash
cd backend
python3 -m tests.test_drainage_disposal
```

