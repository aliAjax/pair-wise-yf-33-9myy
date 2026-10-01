# 多站卫星地面站排程系统

使用标准库与 SQLite 实现的独立排程原型。系统维护卫星、地面站、天线、维护时段、可见窗口、租户配额和数据请求，并检查速率、数据量、截止时间、设备重叠、卫星同时接收、天气和租户配额。

## 运行

```bash
python3 app.py --db satellite_scheduling.db
```

默认监听 `127.0.0.1:8204`，首页 `/`，健康检查 `/health`。

身份头为 `X-User-Id`、`X-Role`；`requester` 还需 `X-Tenant`。角色：`viewer`、`requester`、`operator`、`commander`、`auditor`。

## 主要接口

- `POST /api/satellites`、`/api/stations`、`/api/antennas`、`/api/maintenance`、`/api/visibility-windows`、`/api/quotas`：资源配置。
- `POST /api/requests`：创建数据接收请求。
- `POST /api/requests/{id}/schedule`、`/reschedule`：排程或重排被抢占请求。
- `POST /api/schedules/{id}/start`、`/complete`、`/cancel`、`/preempt`：接收状态和紧急抢占。
- `POST /api/visibility-windows/{id}/change`：窗口变化并返回受影响排程；已接收数据保留。
- `GET /api/satellites/{id}/storage`：查询星上存储容量、已占用、可用余量及按状态/站的占用明细。
- `GET /api/state`、`GET /api/schedules/{id}`：权限化状态查询。

## 星上存储余量规则

- 容量取自卫星的 `storage_capacity_mb`；占用 = 该星在**所有地面站**状态为 `scheduled`、`receiving`、`received` 的数据量之和（值班员可通过 storage 接口看到余量，避免同容量重复排给两段）。
- 排新任务时在同一事务内先读余量再插入：`data_mb > available_mb` 即拒绝（409 `storage_capacity_exceeded`），details 返回 `capacity_mb / used_mb / available_mb / breakdown_mb / required_mb`。
- 取消、紧急抢占、窗口变化导致未接收排程失效时，其数据量立即释放；`received` 数据在卸载前持续占用，窗口变化时只标记 `preserve_received_data`，不释放。
- 两个调度员并发提交同一颗星时，写事务使用 `BEGIN IMMEDIATE` + WAL 单写者，后到者阻塞后整体重试并按最新余量重新判定，不能超发。
- 余量占用由 `schedules` 行派生，与排程插入/状态更新在同一事务：任何写入失败都会一起回滚，不会出现“余量扣了但排程没建成”。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 主要局限

速率和容量按静态 Mbps 与时长计算，不包含链路预算、调制编码、雨衰、天线跟踪和存储卸载策略。租户身份使用请求头模拟；SQLite 和单进程 HTTP 服务适用于原型，生产环境需要统一身份、共享数据库和分布式资源锁。
