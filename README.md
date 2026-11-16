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
