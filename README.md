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
- `POST /api/requests/{id}/schedule`、`/reschedule`：排程或重排被抢占请求。排程时按卫星已排(scheduled/receiving)与已接收(received)数据之和扣减星上存储余量，容量不足返回 `insufficient_storage` 并给出 `available_mb`。
- `POST /api/schedules/{id}/start`、`/complete`、`/cancel`、`/preempt`：接收状态和紧急抢占。取消或抢占后存储余量立即放出；已接收数据继续占住存储。
- `POST /api/visibility-windows/{id}/change`：窗口变化并返回受影响排程；未接收排程失效并放出容量，已接收数据保留。
- `GET /api/satellites/{id}/storage`：查询卫星星上存储容量、已占用与可用余量。
- `GET /api/state`、`GET /api/schedules/{id}`：权限化状态查询。

## 并发与原子性

排程写入在 `BEGIN IMMEDIATE` 事务内完成，两个调度员同时提交同一颗卫星时后到者会等待前者提交，再按最新余量扣减，不会超发。写入失败时余量与排程在同一事务内一起回滚，不会出现排程已建而余量未扣（或反之）的不一致。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 主要局限

速率和容量按静态 Mbps 与时长计算，不包含链路预算、调制编码、雨衰、天线跟踪和存储卸载策略。租户身份使用请求头模拟；SQLite 和单进程 HTTP 服务适用于原型，生产环境需要统一身份、共享数据库和分布式资源锁。
