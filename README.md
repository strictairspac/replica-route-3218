# 站点读流量路由演练

栖原站点运维组（11人）在交接班前，用节点位点与会话时序复核读写路由，明确何时读副本、何时回主库、何时必须拒绝读取。

## 场景与模块

原先值班员在配置表和聊天记录中核对副本延迟，以“延迟很低”推断写后读安全。痛点：低延迟不等于已应用会话写位点、故障时回退策略不清、决策无逐请求证据。上游输入为人工确认的节点状态和请求轨迹，本站承接路由规则演练与证据留档，下游为站点值班与变更复核人员。

三个核心实体：复制节点 Node（主副角色/在线状态/日志谱系/位点/延迟），会话请求集 RequestSet（带时序的读写、复制应用、故障恢复事件），路由试验 Trial（冻结输入、策略参数与结果）。第5页另有断电时序脚本 CrashScript 与断电演练 CrashDrill（缓存容量、WAL、恢复证据）。5业务页+登录页，另有管理员审计。

**这是受控复制路由模型，不连接真实主从集群。** MySQL真实保存节点、请求、任务及证据；Python真实逐事件计算策略。日志位点采用命名谱系中的单调整数，未实现完整GTID集合/真实复制协议。

## 快速启动

```sh
cp .env.example .env
docker compose up -d --build
docker compose exec -T backend python -m scripts.seed_demo
```

前端 http://localhost:18247 ，后端 http://localhost:19247/docs ，MySQL localhost:17747。

|角色|账号|密码|权限|
|---|---|---|---|
|admin|admin@qiyuan.test|Demo2026!|全部业务写入、运行与审计|
|operator|operator@qiyuan.test|Demo2026!|节点/请求编辑、运行、导出|
|viewer|viewer@qiyuan.test|Demo2026!|只读和导出，写API403|

## 一天的工作剧本

09:00 operator登录；09:05查看harbor-primary位点105、north-replica位点104/150ms、south-replica位点105/20ms；09:10打开 `QY-1117-A` 请求集；09:15确认八事件时序；09:20以100ms延迟门槛和500ms粘滞执行 `shift-20261117`；09:25检查：写入主库位点106、100ms读仍走主库、600ms南站追平106、700ms读走南站、800ms南站故障、900ms回主库、1000ms主库故障、1100ms拒绝读取；09:30导出route-1.json；09:40 viewer核验拒绝理由；10:00 admin看审计。

另有 `QY-1117-B` 高延迟副本回退主库、`QY-1117-C` 主库故障且副本未追平的拒绝案例。所有试验使用冻结快照，不会把模型故障写回节点台账。断电恢复页有 `QY-CRASH-A/B/C` 三条时序，见下文。

## 真实计算规则

输入上限12节点、100事件；时间0–86400000ms，事件按at_ms非递减，时间相同按输入顺序执行。必须有且仅一个主节点，同谱系副本初始位点不能超过主库。事件字段：

```json
[
  {"at_ms":0,"kind":"write","session":"lab-A"},
  {"at_ms":100,"kind":"read","session":"lab-A"},
  {"at_ms":600,"kind":"replicate","node":"south-replica","position":106,"delay_ms":20},
  {"at_ms":700,"kind":"read","session":"lab-A"}
]
```

- write：主库在线才成功；主库位点+1，记录该会话required_position和last_write_ms。失败写入不推进会话位点。
- read：若距最近成功写入 `< sticky_ms`，必须主库在线、同谱系且位点满足；否则拒绝。到达边界即结束粘滞。
- 粘滞期外：副本必须在线、同谱系、position≥会话required_position、delay_ms≤max_delay_ms，四条件同时成立。多个候选按延迟升序、位点降序、名称字典序选择。无副本达标则回主库；主库也失效则拒绝。
- replicate：只允许在线同谱系副本；位点不能回退、不能超过主库已提交位点。node_down/node_up修改模型状态。未知节点或非法复制在入队前422。
- 每个会话独立维护位点；从未成功写入的会话required=0，允许最终一致副本读取，不承诺全局强一致。低延迟绝不覆盖落后位点。

输出每事件目标/拒绝、会话所需位点、实际位点、各副本四项资格、最终节点、会话状态、汇总和SHA256。相同输入产生相同摘要；不依赖随机数或外部网络。拒绝不安全读是正常业务结果，不算任务失败。快照与结果保存在MySQL，Redis只传递任务ID。

## 主库断电恢复演练

第5个业务页把写入、提交、刷盘、断电与重启排成一条时序，核对哪些值留下、读请求何时放行、恢复中断后重试是否重复应用。

**选定策略：缓存满时让新写入等待事务结束，不刷未提交页腾空间。** 理由是不 steal 的缓存才能保证未提交值永不落数据文件，断电后只做 redo、无需 undo；代价是缓存被未提交页占满时写入延迟变大。页面上能直接看到每个写入等了多久、由哪个事务结束释放。

规则（`backend/src/services/crash_recovery.py`）：

- write：把值写进该 key 缓存页的未提交覆盖层，并追加 WAL 写记录；同键页被别的会话占据、或全部槽位都被未提交页占满时，写入排队，直到该 key 的所有者或任一持有槽位的事务提交/回滚。等待时长 = 实际进入缓存时刻 − 到达时刻。写值缺省时自动生成。
- commit：只追加并 fsync WAL 提交记录即确认，不要求刷数据页。若本会话还有写入在排队，提交记为 blocked（客户端还停在写入调用里，发不出提交）。
- abort：丢弃本会话覆盖层、取消本会话排队写入、释放槽位。
- flush：只刷「无未提交覆盖且位点比磁盘新」的页，其余跳过并保留在缓存；刷完的干净槽位释放给排队写入。
- read：本会话可读自己的未提交值；其他会话只能读到最近已提交值（没有就是 null），不会被未提交覆盖层污染。断电及恢复期间一律拒绝读取。
- crash：清空缓存与所有未提交覆盖层，WAL 截断到 durable_lsn，排队写入取消。只有已 fsync 的提交记录可以重放。
- restart：从 durable_lsn 起重放已提交事务的写记录；每条先比较目标页 page_lsn，已应用则跳过。带 `apply_limit` 时可只应用若干条后停下，主库进入 recovering，继续拒绝读取；再次 restart 续跑，不会重复应用。重放完毕才把读闸门打开。

恢复完成后页面汇总：确认写入次数、丢失的已确认写入（必须为0）、被读到的未提交值（必须为0）、重放重复应用条数（必须为0）、等待写入次数与最长等待、恢复尝试与中断次数、断电后拒读次数、首读放行时刻。键表并列「断电时已提交值 / 当前已提交值 / 断电时磁盘 / 恢复后磁盘 / 未提交值（应丢弃）」。

种子脚本附带 `QY-CRASH-A`（缓存2页排队 + 断电 + 恢复中断续跑）、`QY-CRASH-B`（已提交未刷盘页恢复）、`QY-CRASH-C`（回滚释放槽位后继续写入）三条时序。事件上限200条、缓存1–32页。

## 工程说明

|服务|技术|职责|
|---|---|---|
|frontend|原生HTML/CSS/JS + Nginx|5业务页、编辑表单、共享组件、/api代理|
|backend|Python3.11/FastAPI/SQLAlchemy/PyJWT|校验、权限、审计、快照与导出|
|worker|Python3.11|会话时序、路由算法与断电恢复算法|
|db|MySQL8.0|真实持久化业务和JSON证据|
|redis|Redis7|持久队列（路由与断电演练各一条）|

各实体贯穿 `backend/src/{models,schemas,services,routes}` 和 `frontend/src/{api,stores,pages}`；其余强制目录含controllers/middlewares/config/constants/utils、components/common/hooks/types/router。三组件table/badge/frame共用；hooks/session/feedback和utils/forms/html共用；request_id/errors双中间件。

共享枚举角色admin/operator/viewer与任务queued/running/completed/failed位于 `backend/src/constants/enums.py` / `frontend/src/constants/enums.js`，security/trials、frame/badge消费。节点角色primary/replica和状态healthy/down由node schema及前端表单定义，事件类型由request_set schema及路由算法定义；断电事件类型write/commit/abort/flush/read/crash/restart由crash schema与恢复算法定义。worker心跳仍写 `/tmp/worker-heartbeat`，产物目录可用 `ARTIFACT_DIR` 覆盖（默认 `/artifacts`）。

环境变量见.env.example：COMPOSE_PROJECT_NAME、三端口、DB_PASSWORD、JWT_SECRET、SEED_DEMO。默认密码仅本地演示、JWT8小时；seed自然键仅补缺失，不覆盖修改。schema由metadata.create_all初始建表，无在线升级引擎。5服务均健康检查，backend/worker依赖健康db/redis，frontend依赖backend；数据库、Redis、worker产物使用命名卷。

本地开发：Python3.11安装backend/requirements.txt、配置DATABASE_URL/REDIS_URL，在backend运行 `uvicorn src.main:app --reload` 和 `python -m src.worker`，前端经Nginx同源代理。停止清空：`docker compose down -v --remove-orphans`。

限制：单worker串行、无生产任务重试/崩溃续跑；不模拟网络随机抖动、自动选主或完整GTID集合。断电演练同样是确定性模型，不注入真实 MySQL 崩溃，也不模拟 doublewrite/redo 日志文件轮转。Google Fonts离线回退，业务不依赖字体网络。

## License

MIT。源码外 `../../test-artifacts/gb-4108/` 含真实测试、截图与结果。
