# 分拣过机台（Sortbay）

骨架：过机扫描装载 → 口门路由裁决 → 错分回流批次 → 封袋班次对账

- 前端 http://127.0.0.1:6600
- API http://127.0.0.1:11600

## 集包袋准入架构（全局唯一口径）

本仓库选定 **架构 A：仅已路由可入袋（`ROUTED_ONLY`）**，不混用“待路由先入袋 + 后补裁决”。

规则与实现边界：

1. 扫描必须处于 `已路由`，且 `scans.chute_id = bags.chute_id`，袋处于 `集包中`，班次未封，才允许入袋。
2. `待路由`、`错分回流`、已入他袋、口门不一致等情况整笔失败：HTTP 4xx，事务回滚；不写 `scans.bag_id/status`，不增袋件数，不写袋册事件。
3. 入袋写口使用 `BEGIN IMMEDIATE` 和条件更新 `status='已路由'`，路由/回流也使用条件状态迁移，避免入袋与裁决并发产生双终态。
4. 每次准入（包括拒绝）都写入 `admission_requests` 和审计；响应中的 `准入编号` 可通过以下接口重放：
   - `GET /api/封袋/准入/{id}`
   - `POST /api/封袋/准入/{id}/重放`
   - `GET /api/审计/准入/{id}`
5. 袋件数始终由 `scans.bag_id` 当前集合计算，袋册接口不过滤异常状态；`GET /api/封袋/一致性`、`/api/口门/一致性`、总览和班次对账会暴露袋册/扫描/口门拧花。
6. 若入袋事务在扫描更新后、提交前发生异常，整个事务回滚，袋件数和路由态不会长期分叉。`backend/tests/test_admission.py` 覆盖待路由点入袋、口门不符、幂等重放、并发入袋/裁决、半截故障回滚、回流移袋和人工拧花可读。
7. 不存在写口事务之外的“成功灯”旁路：任何 `surface_*` 设置键、审计假事件或独立读模型覆盖都被禁止（历史脏数据由启动迁移清除）。`backend/tests/test_admission_tear.py` 用 14 个撕裂夹具钉死：成功回执必须能由准入谓词∪袋册成员∪扫描状态机同命运重放，覆盖灯亮而状态机待路由、幽灵成员、半截写入、口门/袋双占、封袋闸、回流移袋、重放对账与全链路条码-目的地-分拣线-班次写口。

## 本地验证

```bash
# 后端
python3 -m venv /tmp/sortbay-venv
/tmp/sortbay-venv/bin/pip install -r backend/requirements.txt pytest httpx
SORTBAY_DB=/tmp/sortbay.db /tmp/sortbay-venv/bin/python -m pytest

# 前端
npm install --prefix frontend
npm run build --prefix frontend
```
