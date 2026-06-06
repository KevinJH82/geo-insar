# geo-insar Phase 1 交付手册

> 本文档列出 Phase 1 完成后用户(运维/开发)需要执行的接手操作。
> 代码骨架已写完,但环境配置和外部资源需要人工补齐。

## 1. 已交付内容

### 1.1 代码骨架
- `/opt/deepexplor-services/commons/` — 跨子系统公共库(8 个文件)
- `/opt/deepexplor-services/geo-insar/` — 独立子系统完整骨架
  - `main.py` — CLI 入口
  - `downloader/sentinel1_insar.py` — HyP3 客户端
  - `postprocess/insar.py` — HyP3 输出标准化
  - `postprocess/stack.py` — 时序堆栈索引
  - `task_store.py` — SQLite 任务持久化
  - `web/app.py` + 4 个模板 — Flask UI(端口 8084)
  - `scripts/preflight_check.sh` — 启动前置自检
  - `config.yaml` + `requirements.txt`

### 1.2 测试数据
- `/opt/deepexplor-services/geo-insar/test_data/zhaoyuan_miaoshan.kml` — 山东招远庙山金矿(已从外接盘复制 + 改扩展名)

### 1.3 二级门户卡片
- `docs/2nd_portal_card_snippet.html` — 需要在 192.168.112.18 的 2nd Page/index.html 远程粘贴(本地无远程门户副本)

## 2. 启动前必须完成的操作

### 步骤 A:装 Python 依赖
```bash
cd /opt/deepexplor-services/geo-insar
pip install -r requirements.txt
```
依赖清单(都缺失):`hyp3_sdk`、`asf_search`、`sqlalchemy`、`flask`、`rasterio`、`lxml`、`shapely`、`pyyaml`、`requests`、`tqdm`、`jsonschema`。

### 步骤 B:配置 Earthdata + HyP3 凭证

geo-insar 默认 fallback 到 geo-downloader 的 `config/credentials.yaml`(同机部署)。检查:
```bash
ls /opt/deepexplor-services/geo-downloader/config/credentials.yaml
```

若不存在,创建一份:
```bash
cp /opt/deepexplor-services/geo-downloader/credentials.yaml.example \
   /opt/deepexplor-services/geo-downloader/config/credentials.yaml
# 编辑文件,填 nasa_earthdata 段
```

需要的账号:
1. **NASA Earthdata**(必须):https://urs.earthdata.nasa.gov/(免费,1 分钟)
2. **HyP3 授权**(必须):用 Earthdata 账号登 https://hyp3-api.asf.alaska.edu/ui/,授权 HyP3 应用,确认默认 1000/月 配额

### 步骤 C:跑前置检查
```bash
bash /opt/deepexplor-services/geo-insar/scripts/preflight_check.sh
```
所有项应该是 ✅ pass。如果有 ❌,按照 preflight 页给的修复建议处理。

### 步骤 D:启动 Web 服务
```bash
cd /opt/deepexplor-services/geo-insar
python3 web/app.py
# 浏览器打开 http://localhost:8084
```

## 3. Phase 1 验收流程

1. 浏览器打开 `http://localhost:8084/preflight.html`,运行前置检查,**全绿**
2. 切到 `任务控制台`,上传 `test_data/zhaoyuan_miaoshan.kml`
3. 系统会自动提示 AOI 面积(~5 km²)和推荐后端(ISCE_BURST)
4. 设时间窗 2024-06-01 ~ 2024-08-01,默认配对策略 closest_in_time
5. 点 `查询能配出多少对` —— 应该能列出几对 Sentinel-1 SLC 配对(若 HyP3 端点连通)
6. 点 `Dry-run` —— 任务入库但不提交 HyP3,验证配对清单
7. 点 `提交 HyP3` —— 真正提交,任务列表会出现 #N,状态从 `running` 转为 `cloud_processing`
8. 等 30 min - 2 h,HyP3 处理完后产物会自动下载到 `downloads/zhaoyuan_miaoshan_4_974km2_/sentinel1_insar/<refdate>_<secdate>_VV/`
9. QGIS 打开 `los_displacement.tif`,核对庙山金矿采空区(120.44°E, 37.19°N)的形变信号

## 4. 远程操作(本地无法完成)

### 4.1 二级门户加卡片
- 文件:`docs/2nd_portal_card_snippet.html`
- 目标:远程主机 `192.168.112.18` 的 `2nd Page/index.html`
- 见 snippet 文件顶部注释的集成步骤

### 4.2 服务管理(建议)
geo-insar / geo-downloader / geo-exploration / geo-reporter 都是 Flask 服务,建议用 systemd / supervisor 统一拉起:

```ini
# /etc/systemd/system/geo-insar.service
[Unit]
Description=geo-insar Web Service
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/deepexplor-services/geo-insar
ExecStart=/usr/bin/python3 web/app.py
Restart=on-failure
User=ubuntu  # 或对应用户

[Install]
WantedBy=multi-user.target
```

## 5. 已知 Phase 1 限制 / 后续迭代

| 限制 | Phase | 备注 |
|---|---|---|
| HyP3 轮询逻辑未完整实现 | 1 收尾 | `web/app.py:_poll_once()` 是占位,需要完善 `hyp3.get_job_by_id()` + 完成下载触发 standardize_hyp3_output() |
| AOI 极小 (4.97 km²) 用 ISCE_BURST 后端 | Phase 1 | 测试 AOI 选 GAMMA 会浪费配额 |
| 二级门户卡片 | 远程粘贴 | 见 §4.1 |
| Phase 2 SNAP / snaphu 未装 | Phase 2 | 不影响 Phase 1 |
| 下游对接 (geo-exploration/reporter/analyser) | Phase 1.5 | Phase 1 验证通过后再做 |

## 6. 故障排查

| 现象 | 修复 |
|---|---|
| `ImportError: hyp3_sdk` | `pip install -r requirements.txt` |
| `CredentialsError: 找不到 credentials.yaml` | 按步骤 B 创建 |
| Web 启动 `Address already in use` | 端口 8084 被占,改 config.yaml 或释放进程 |
| `KMLParseError: 缺少依赖 lxml` | `pip install lxml shapely` |
| HyP3 返回 401 | Earthdata 凭证错误或未授权 HyP3 应用 |
| 任务卡在 cloud_processing 不更新 | 轮询线程未完善(见 §5),手动:在 https://hyp3-api.asf.alaska.edu/ui/ 看 Job 是否 SUCCEEDED |
