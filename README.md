# 文化遗产开放日排班服务

本项目是使用 Python、FastAPI 与 SQLite 实现的服务端应用，覆盖活动、场次、讲解员、主题、排班、评价、变更、预警和统计。它可在单个 Linux 应用容器内完成安装、测试、编译和接口验收，不依赖浏览器、外部数据库、缓存、消息队列或额外运行服务。

## 安装

```bash
python3 -m pip install -r requirements.txt -r requirements-dev.txt
```

## 测试

```bash
python3 -m pytest -q
```

## 编译

```bash
python3 -m compileall -q .
```

## 接口验收

```bash
python3 -c "from app.main import app; assert len(app.routes) > 5; print(len(app.routes))"
```

## 启动

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 多资源统一暂占

沉浸式场次同时占用三类资源：**展厅**（每厅容量 1，同一时段只能有一场）、
**无线设备套装**（全局共享容量池，默认 8 套，可用环境变量 `DEVICE_POOL_CAPACITY` 调整）、
**主题教具**（按主题各一个容量 1 的池）。

核心规则：

- **草稿/变更预审即暂占**：创建场次、提交变更申请时按时间段锁定所需数量；
  草稿暂占 TTL 由 `DRAFT_HOLD_TTL_SECONDS` 控制（默认 1800 秒），
  变更预审暂占 TTL 由 `REVIEW_HOLD_TTL_SECONDS` 控制（默认 86400 秒）。
- **原子性，不泄漏**：同一暂占组合（group）在单个写事务内先全量校验再整体写入；
  任一资源不足整体回滚，返回 409 和可解释的冲突集合
  （容量、时段内最大并发占用、余量、占用方场次/变更单清单）。
- **审批确认 / 拒绝释放**：审批通过确认预审暂占（有效期延至场次结束）；
  拒绝或取消变更释放预审暂占；场次取消释放其全部暂占（含关联待审变更）。
- **执行换组**：先验证新组合成功，再释放旧组合；改期失败时旧组合保持有效。
  审批、执行重复调用均幂等。
- **不超卖**：进程锁 + SQLite `BEGIN IMMEDIATE` 串行化写事务，容量按时间段
  最大并发计量，首尾相接的场次可复用同一资源。
- **超时与重启清理**：后台线程按 `HOLD_SWEEP_INTERVAL_SECONDS` 清扫 TTL 过期暂占，
  服务启动时先清扫一次；过期暂占即使尚未清扫也不再计入容量。

新增接口：

```text
GET    /api/resources/pools                    - 资源台账列表
POST   /api/resources/pools/ensure             - 按现有展厅/主题幂等补齐台账
PUT    /api/resources/pools/{id}               - 调整容量/启停（不得低于当前并发）
GET    /api/resources/availability             - 时间段内各资源占用与余量
POST   /api/resources/preview                  - 不落库的资源可用性预审
GET    /api/resources/holds                    - 暂占单查询（可按场次/变更/状态）
POST   /api/resources/sweep                    - 手动触发过期清理
POST   /api/changes/{id}/cancel                - 取消变更申请并释放预审暂占
```

场次创建/更新接口在资源不足时返回 `409`，响应体 `detail.conflicts` 为冲突集合。
