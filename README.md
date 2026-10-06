# 文化遗产开放日排班服务

本项目是使用 Python、FastAPI 与 SQLite 实现的服务端应用，覆盖活动、场次、讲解员、主题、排班、评价、变更、预警和统计。它可在单个 Linux 应用容器内完成安装、测试、编译和接口验收，不依赖浏览器、外部数据库、缓存、消息队列或额外运行服务。

## 多资源统一暂占

沉浸式主题活动的一个场次除讲解员外，还会同时占用展厅、无线设备套装和主题教具。系统提供统一的多资源暂占机制：

- **资源登记**：`POST /api/resources` 登记资源及总容量（展厅按 `venue_id` 关联场地，主题教具按 `theme_id` 关联主题，无线设备套装为全馆资源池）。
- **草稿/预审暂占**：创建场次（草稿）或提交变更申请（预审）时，按时间段锁定所需数量；任一资源不足则整体失败，返回可解释的冲突集合（资源、时段、需求/可用量、占用方明细），不产生部分占用。
- **审批/执行/取消/超时**：审批通过确认暂占（暂占过期则重新校验，不足则审批失败）；执行变更新组合确认到场次后释放旧组合；取消、拒绝释放暂占；超时未确认的暂占自动过期（服务重启时也会清理）。
- **容量不超卖**：暂占的检查与写入在同一事务内完成（资源行写锁串行化），重复审批/执行幂等，并发场次不会超卖。

常用接口：

```
GET/POST        /api/resources                     资源列表 / 登记资源
GET/PUT         /api/resources/{id}                资源详情 / 调整容量
GET             /api/resources/{id}/availability   查询时段可用量与占用明细
GET/POST        /api/resource-holds                暂占列表 / 手动暂占
POST            /api/resource-holds/{id}/confirm   确认暂占（幂等）
POST            /api/resource-holds/{id}/release   释放暂占（幂等）
POST            /api/resource-holds/cleanup        清理超时暂占
POST            /api/changes/{id}/cancel           取消变更并释放暂占
```

场次的设备/教具需求通过 `device_sets_needed`、`teaching_aids_needed` 字段表达；变更申请可用 `new_device_sets_needed`、`new_teaching_aids_needed` 调整。功能验证：`python3 test_resource_holds.py`（需在空目录下运行）。

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
