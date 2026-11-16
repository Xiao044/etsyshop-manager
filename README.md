# etsyshop-manager

Etsy 店铺管理、公开指标采集、评价同步和数据看板项目。

本仓库于 **2026-11-16** 从现有服务器部署首次导入，仅保留正在使用的网站和采集版本。旧版本目录及其中的模拟数据生成脚本已从项目目录移除，不纳入本次 Git 提交。

## 当前生产入口

| 用途 | 版本 / 路径 | 服务 |
| --- | --- | --- |
| Flask 网站、店铺管理、数据看板 | `releases/v22` | `etsy-tools.service` |
| Chromium 指标采集与评价同步 | `releases/v11/collector.py`、`review_sync.py` | `etsy-collector.service` |
| 每日采集计划 | 每天 18:15，`Asia/Shanghai` | `etsy-collector.timer` |

**网站与采集程序使用不同版本。v11 是当前采集版本，不能因版本号较低而删除；修改 v22 不会自动修改采集入口。**

服务器项目根目录为 `/opt/etsy-tools`。网站由 Gunicorn 监听 `127.0.0.1:8765`，经 Nginx 提供 HTTPS；采集任务使用 `flock`、`xvfb-run` 和 Chromium。实际启用的 systemd 配置位于 `/etc/systemd/system/`。

## 代码结构

```text
releases/
  v11/
    collector.py        当前采集入口
    review_sync.py      当前评价同步实现
    ...                 该部署目录内保留的配套文件
  v22/
    app.py              当前 Flask 网站入口
    counter_metrics.py  累计指标转自然日增量及图表刻度
    finance_metrics.py  财务指标换算
    templates/          页面模板
    static/             CSS / JavaScript
```

- `app.py`：店铺维护、看板查询、评价读取和路由。
- `collector.py`：读取店铺公开页面，保存采集批次与快照。
- `review_sync.py`：评价翻页、去重、内容及图片同步。
- v11 目录中保留了原发布包的配套网站文件和服务模板，但**当前网站只运行 v22**。不要直接用 v11 的网站服务模板覆盖生产配置。
- 当前生产数据库中原有的模拟记录不在本次清理范围内，也不上传；涉及财务数据时仍需区分模拟与真实记录，不能将演示指标当作真实账务结果。

## 环境与数据

当前部署环境为 Ubuntu 24.04、Python 3，主要运行组件包括 Flask、Werkzeug、Gunicorn、websocket-client、SQLite、Chromium、Xvfb 和 Nginx。本次源码导入不安装或升级依赖，不执行应用初始化、采集、迁移或数据生成脚本。

数据目录由 `ETSY_TOOLS_DATA_DIR` 指定，默认 `/var/lib/etsy-tools`：

- `shops.sqlite3`：店铺、采集快照、评价等运行数据。
- `review_images/`：下载的评价图片。
- `session_secret`：网站会话密钥。
- `backups/`：数据库备份。

**这些数据、浏览器登录资料、环境变量文件、私钥、完整分享链接和访问令牌均不属于源码，不应提交到仓库。** 不要将本机的服务器登录凭据文件复制进本项目。

`.gitignore` 已过滤数据库及旁文件、评价图片、备份、秘密配置、密钥、缓存、日志、macOS 元数据、模拟数据生成脚本，并将可纳入版本控制的发布目录限制为 v11 与 v22。实际运行数据原本位于项目目录之外，本次不移动、不删除或修改数据库。

## 版本控制

- 主分支为 `main`，保留 GitHub 仓库已有的初始提交。
- 远程地址：`https://github.com/Xiao044/etsyshop-manager.git`。
- 旧发布版本在删除前保留了仓库外的回滚压缩包；压缩包不上传。
- 本次不修改当前版本业务代码、不重启服务、不更改采集计划。
- Git 仓库不是生产数据库备份，恢复环境仍需单独恢复运行配置、秘密信息和数据。
- 后续提交前应检查 `git status`、`git diff` 及暂存内容，避免将真实令牌或运行数据加入公开仓库。

## Windows 本地开发

本地开发使用 Python 3.12、Chrome（找不到时尝试 Edge）和 SQLite，不需要 MySQL。
本地启动器固定使用仓库下 `data/shops.sqlite3`，不会继承服务器的数据目录变量；
数据库、会话密钥、日志和独立浏览器资料都在 Git 忽略范围内。
本地网站仅监听 `127.0.0.1`，不部署或更改服务器任务。

在仓库根目录打开 PowerShell：

```powershell
# 首次准备空开发库；依赖在独立 .venv 中安装
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/setup-windows.ps1

# 或首次用已有本地一致性快照创建开发副本（开发库已存在时拒绝覆盖）
# powershell -NoProfile -ExecutionPolicy Bypass -File scripts/setup-windows.ps1 -Snapshot '本地快照绝对路径'

# 已配置环境之后：
.\.venv\Scripts\python.exe scripts/local.py db-info
.\.venv\Scripts\python.exe scripts/local.py web --port 8765

# 可选：仅显式诊断时提交单店任务；页面和标准启动流程不触发此命令
.\.venv\Scripts\python.exe scripts/local.py collect --shop-id 1

# 只读查询本地数据库，不允许写入
.\.venv\Scripts\python.exe scripts/local.py sql --query 'SELECT id,name FROM shops WHERE deleted_at IS NULL'

# 离线回归测试：不打开浏览器、不请求 Etsy、不修改开发数据
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

- 独立浏览器资料：`data/browser-profile`，不读取或复用日常浏览器的登录资料。
- 可通过 `ETSY_BROWSER_PATH` 指定已安装的 Chrome/Edge 路径；详见 `.env.example`。
- `.env.example` 仅是配置说明，不会被自动载入。
- 原快照中的评价图片只有文件名引用；图片文件本身不在 SQLite 中，未复制图片时部分历史图片无法展示。
- 现有 `finance_demo` 内容属于历史演示数据，不代表真实财务数据。

### Etsy 显示临时访问限制时

`Access is temporarily restricted` 是 Etsy 返回的设备或网络访问限制页面，
不是 SQLite、Windows 安装或店铺已删除的错误；该页面不一定提供验证码。
它列出的网络、自动化、JavaScript、开发工具等都只是可能原因，不能据此判定具体原因。

采集器识别到此页后会立即停止本批剩余店铺和评价访问，返回非零退出码，
并将批次标记为 `blocked`。为保持原有批次计数兼容，未访问的店铺会记录为
`failed`，但错误信息明确写出“未访问”，指标保持 `NULL`，不会伪造为 0 或成功结果。
评价翻页遇到同样限制也会停止，不进行自动重试、切换代理或绕过验证。

遇到限制请暂停采集和连续刷新，等待正常访问恢复。由用户使用普通浏览器手动
核对同一店铺是否可正常访问；如仍受限，按页面提供的入口联系 Etsy 支持。
不要将反复启动采集器当作验证恢复的方法。单元测试通过只代表保护逻辑有效，
不代表真实 Etsy 采集已经成功。

## 按日定期采集（无前台手动刷新）

前台已移除全部刷新、单店刷新、刷新进度和记录面板，不再轮询采集队列。
原 `/api/collection/*` 网页接口也已删除，旧页面不能再提交或恢复采集任务。
已有历史任务表、诊断日志和真实采集数据不因此清理；永久删除店铺及数据库级联清理功能保留。

服务器继续沿用原有 `etsy-collector.timer`，每天北京时间 **18:15** 触发
`etsy-collector.service`，以 `flock`、`xvfb-run` 启动 v11 `collector.py`，
遍历全部未删除店铺并同步公开指标与评价。没有新增或修改服务器定时器。

Windows 本地仅作为开发预览：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/start-windows.ps1
# http://127.0.0.1:8765
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/stop-windows.ps1
```

本地标准启动脚本只启动网站，不再自动启动手动刷新 Worker，也不会创建 Windows 计划任务。
旧版启动脚本管理的 Worker 会在停止脚本中兼容退出。`scripts/local.py` 的采集/队列命令
仅留作显式的开发诊断工具，不与网站启动绑定；未经显式调用不执行采集。
网站浏览不改变采集计划，页面重新打开时展示已保存的最新数据。

## 商品数量：All 与 On sale

- 采集侧同时读取 Etsy 店铺侧栏 `All` 与 `On sale`：`shop_snapshots.item_count` 保留原有的 All 商品总数，新增可空整数 `on_sale_count` 保存促销商品数量。On sale 是 All 的子集，不是另一个商品总数。
- 网站和采集器启动时会幂等补齐新字段；已有 All 值不变，历史未采集的 On sale 保持 `NULL`，不会填 0 或复制 All。批次审计表 `collection_results` 也保留两项值。
- 多店看板的“在售商品”页与单店数据的原商品卡片，都在同一卡片中展示两项数量、各自的截至日期、当日净增和同轴双线走势；每日表格在同一商品列中分别列出 All / On sale。
- 两条净增序列独立计算，只有相邻两个北京自然日均有该项数据才产生增量；缺失值显示 `—`、不跨缺失日连线。On sale 没有显示时记为未知，采集结果为部分成功；明确显示 0 时才保存 0。
- 无需重采历史数据，正常后续采集即可补充新的 On sale 记录。此改动未连接或部署生产服务器。

## 采集数据按日更新

- `shop_snapshots` 按 **店铺 + 北京时间自然日（Asia/Shanghai）** 唯一保存。相同日期再次采集时更新原记录（保留记录 ID），新的一天才新增记录；不是按 UTC 日期或“最近 24 小时”判断。
- 同一天多次成功采集以最新值为准；过期的较早结果不会回滚新值。部分成功时保留该日同来源的已知字段，明确的 `0` 仍是有效数值；真实数据替换模拟数据时不继承缺失的模拟字段。
- 本次失败或遇到访问限制会写入独立的批次结果，不用空值覆盖当天已有的有效指标。`collection_results` 保留每次批次的原始指标、状态和错误；Worker 和批次计数从这张审计表读取，避免把旧的成功记录误认为本次成功。
- 评价依旧按 `(shop_id, review_id)` 增量同步，不会把同一天的多条评价相互覆盖。财务演示表原有 `(shop_id, day)` 主键保持不变。
- 采集器首次启动会迁移旧结构：先在 `data/backups/` 建立一致性 SQLite 备份，再将旧快照完整归档到 `collection_results`，合并同日重复快照并建立唯一索引。保留有效数据优先、真实优先、同来源最新优先；店铺、评价和采集批次均不删除。后续启动幂等执行，不重复整理。

也可在本地队列无排队或运行任务时手动整理；这个命令不会打开浏览器或访问 Etsy：

```powershell
.\.venv\Scripts\python.exe scripts/migrate-daily-snapshots.py
```

默认仅处理当前仓库 `data/shops.sqlite3`。命令输出备份路径、整理前后快照数量和完整性检查结果。上述改动未连接、迁移或部署生产服务器。

## 空数据与模拟记录清理

- 没有店铺时展示空名单和添加入口，不自动创建示例店铺或采集数据。
- 没有有效指标、缺少相邻日基线时显示 `—`，不填充为 0，不跨缺失日期计算日增量。
- 未同步评价时差评数量未知；完成同步后确认没有已收录差评才展示 0。
- 没有有效财务记录时展示“尚未接入”，不会生成模拟金额。空财务字段不会参与算术。
- 采集页没有任何可解析指标时记为失败，缺失字段保留 NULL；失败尝试不覆盖同日已有有效值。

清理仅针对指定本地开发库中 `is_simulated=1` 的日快照、批次结果和财务记录。
默认预览；实际清理前须停止本地 Worker、等待任务结束。会自动在 `data/backups/` 生成一致性备份，
在同一事务中删除并校验全部非模拟记录未变；不会删除历史备份、店铺、评价或真实采集记录。

```powershell
.\.venv\Scripts\python.exe scripts/local.py clean-simulated
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/stop-windows.ps1
.\.venv\Scripts\python.exe scripts/local.py clean-simulated --apply
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/start-windows.ps1
```

### 历史采集记录与店铺删除

- 原手动刷新功能的历史记录保留在 `collection_jobs`，请求与逐店任务的关联在 `collection_job_tasks`；
  逐店状态保存在 `collection_tasks`。原始采集批次、指标结果和评价同步结果分别保存在
  `collection_runs`、`collection_results` / `shop_snapshots`、`review_sync_runs`。
- 采集器详细输出独立写入 `data/logs/collection-task-<id>.log`，Worker 输出位于同目录下的
  `worker.stdout.log`、`worker.stderr.log`，浏览器日志位于 `data/collector-chromium.log`。
- 前台不再显示或删除刷新任务记录；对应网页接口已移除。历史数据库记录和本地诊断文件保留。
- 店铺管理、看板卡片和单店数据页都有永久删除入口。确认后，后端以 `BEGIN IMMEDIATE` 和采集资源锁
  在同一事务中清除店铺、日快照、批次结果、评价、评价同步记录、财务数据和关联刷新任务。
  独占采集批次和空刷新请求一并删除；共享批次保留其他店铺数据并重算计数，共享请求仅移除本店任务。
- 永久删除不可撤销，重新添加相同链接会创建新的店铺记录，不能恢复历史。新表单必须提交
  `confirm_permanent=1`；前端仅在明确确认后设置，旧页面/未确认表单不会误触发永久删除。
- 采集执行中暂不允许永久删除，避免子进程回写；出现未知关联表或外键检查异常会整体回滚。
  本操作只清理当前数据库，不自动删除离线备份、磁盘诊断日志或可能共享的评价图片缓存。
