# 加油站维修审批系统

基于 Django 5.2 开发的加油站维修工单审批与执行管理系统，服务端渲染，中文界面。

## 技术栈

- **后端**：Django 5.2
- **数据库**：PostgreSQL（开发环境可切换 SQLite）
- **前端**：Django Templates + 原生 HTML/CSS，无前端构建链
- **编码**：Python 3.11

## 快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# 数据库迁移
python manage.py migrate

# 创建管理员
python manage.py createsuperuser

# 启动开发服务器（默认端口 6321，也可用 python manage.py runserver <端口> 覆盖）
python manage.py runserver
```

访问 `http://127.0.0.1:6321/`，管理员后台 `http://127.0.0.1:6321/admin/`。

## 角色与权限

| 角色 | 说明 |
|------|------|
| 超级管理员 | 拥有全部权限 |
| 油站上报员 | 提报维修工单 |
| 片区领导 | 审批本片区工单 |
| 安数部（SAFETY_REPAIR） | 设备档案、备件库存、工单安全审核、验收评价 |
| 工程队队长 | 接单、完工上报 |
| 管理员审核 | 派工单审核、结算 |

## 核心功能模块

### 1. 基础档案
- **片区 / 油站**：组织架构，油站归属片区。
- **设备档案（Equipment）**：设备编码自动生成（`EQ+年月+序号`），可生成二维码，关联油站。
- **材料价格目录（Material）**：标准化内/外部材料的最高限价，用于维修单计价。

### 2. 维修工单（RepairRequest）
- 油站上报 → 片区审批 → 派工 → 工程队执行 → 完工上报 → 验收评价 → 管理员/安数部审核 → 结算。
- 工单状态机：`pending_approval → approved → dispatching → in_progress → completed → rated → settled/closed`。
- 支持附件上传、工单历程（TicketEvent，只增不改）。

### 3. 派工单（DispatchOrder）
- 状态机：`pending_accept → rejected / in_progress → accepting → admin_review / safety_review → closed`。
- 支持改派、现场照片上传（DispatchMedia）、实际故障记录。

### 4. 备件库存（SparePart + StockMovement）
独立于 Material 价格目录的备件库存管理，全局单仓库。

- **备件档案（SparePart）**
  - 编码自动生成：`SP+YYYYMM+4位序号`（行锁保证连续）。
  - 字段：名称、规格、单位、当前库存、安全库存、参考单价、是否启用；可空关联 Material 价格目录。
  - 低库存判定：`stock_quantity <= safety_stock`（含等于）。
- **出入库流水（StockMovement）**
  - 入库 / 出库两类，**只追加不修改**（admin 禁增删改）。
  - 出库可关联派工单；入库禁挂派工单。
  - `balance_after` 记录操作后结存快照。
- **唯一库存写入口** `services.apply_stock_movement()`
  - 事务 + `select_for_update()` 行锁。
  - 停用件拒绝操作；出库超量原子回滚（库存不为负、不留半截流水）。
  - **不变量**：`stock_quantity` 恒等于该备件流水带符号（入+出-）聚合。
- **低库存预警**：出库后若跌破安全库存，向安数部角色发送 `category=stock` 通知，首页显示预警计数与红卡。
- **关联派工单**：出库关联派工单时，在维修单历程写入 `STOCK_USED` 事件；派工单详情页展示备件领用记录。
- 权限：仅安数部（及超管）可新增/编辑备件、登记出入库。

### 5. 结算（SettlementOrder）
- 维修单计价明细（RepairMaterialItem）按 Material 限价校验。
- 与备件库存**不自动联动**（库存与计价相互独立）。

### 6. 通知（Notification）
- 角色通知 / 个人通知，含库存预警、工单流转等类别。

## 项目结构

```
repair_system/
├── approvals/                  # 主应用
│   ├── models.py               # 数据模型
│   ├── services.py             # 业务服务（含库存写入口）
│   ├── views.py                # 视图
│   ├── forms.py                # 表单
│   ├── urls.py                 # 路由
│   ├── admin.py                # 后台注册
│   ├── tests.py                # 单元测试
│   ├── migrations/             # 数据库迁移
│   └── templates/approvals/    # 模板
├── repair_system/              # 项目配置（settings/urls/wsgi）
├── templates/admin/            # 后台定制
├── media/                      # 用户上传（现场照片等）
├── manage.py
└── db.sqlite3                  # 本地开发库
```

## 测试

```bash
python manage.py test approvals -v 1
```

测试覆盖：工单审批流、派工状态机、设备编码、备件编码生成、出入库台账平衡、超扣拦截、库存权限、关联派工单事件、低库存筛选与预警等。

## 关键约束

- `SparePart.stock_quantity` **只能**通过 `apply_stock_movement()` 修改；表单与 admin 均设为只读。
- 流水只追加，禁止修改/删除。
- 维修计价（Material / RepairMaterialItem）与备件库存是两个独立维度，互不联动。
