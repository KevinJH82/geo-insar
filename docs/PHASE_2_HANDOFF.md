# Phase 2 本地 SLC + SNAP 干涉处理 交付清单

> Phase 2 给 geo-insar 加上"本地 SLC + SNAP 自处理"路线,与 Phase 1 的 HyP3 云端互补。
> CLI 通过 `--mode {cloud,local}` 一键切换,sensor ID 不变,前后产物完全对齐(metadata.json schema 一致)。

## 1. 改动总览

| 类型 | 文件 | 行数估算 | 说明 |
|---|---|---|---|
| 新建 | `geo-insar/downloader/sentinel1_slc.py` | ~165 | Sentinel-1 SLC 直下载,不调地理编码 |
| 新建 | `geo-insar/downloader/alos2_insar.py` | ~105 | ALOS-2 SLC 下载(复用 geo-downloader 的 ALOS2Downloader) |
| 新建 | `geo-insar/postprocess/insar_local.py` | ~290 | SNAP gpt 干涉处理 + 环境核查 |
| 新建 | `geo-insar/postprocess/snap_graphs/insar_pipeline.xml` | ~150 | SNAP gpt graph 模板(Sentinel-1 IW TOPS InSAR) |
| 改动 | `geo-insar/main.py` | +4 参数 + 2 函数 | `--mode/--sensor/--gpt/--snaphu` + `_run_local()` |

## 2. CLI 用法

### 云端(Phase 1,默认)
```bash
python3 main.py --kml AOI.kml --start 2024-06-01 --end 2024-08-01 \
                --backend INSAR_ISCE_BURST
# 等价于 --mode cloud
```

### 本地(Phase 2 新增)
```bash
# Sentinel-1 C 波段
python3 main.py --kml AOI.kml --start 2024-06-01 --end 2024-08-01 \
                --mode local --sensor sentinel1 \
                --gpt /opt/snap/bin/gpt --snaphu snaphu

# ALOS-2 L 波段(穿透植被强)
python3 main.py --kml AOI.kml --start 2024-06-01 --end 2024-08-01 \
                --mode local --sensor alos2
```

`_run_local()` 流程:
1. AOI 解析 + 面积评估
2. `check_environment()` 核查 SNAP/snaphu/pyroSAR/Java
3. 凭证载入(同 cloud)
4. SLC 搜索 + 下载(沿用 asf_search 认证流程)
5. 时间相邻配对 + 批量调 `insar_local.run_pair()` 跑 SNAP gpt
6. 输出归位到 §1.4 标准目录契约(与 HyP3 完全一致)

## 3. 启动前置(必须人工完成)

geo-insar Phase 2 环境**当前完全缺失**(本机扫描结果):

| 工具 | 状态 | 安装 |
|---|---|---|
| ESA SNAP(含 `gpt`) | ❌ 未装 | 见 §3.1 |
| snaphu(解缠器) | ❌ 未装 | 见 §3.2 |
| pyroSAR | ❌ 未装 | `pip install pyroSAR` |
| Java(SNAP 依赖) | ❌ 未装 | 见 §3.3 |

`/usr/sbin/gpt` 是 **macOS 的 GPT 分区工具**,**不是 SNAP gpt**,`check_environment()` 已自动过滤。

### 3.1 ESA SNAP 安装(包含 gpt 命令)
- **macOS:** 下载 https://step.esa.int/main/download/snap-download/ 选 macOS 安装包(~600 MB),装到 `/Applications/snap/`,gpt 在 `/Applications/snap/bin/gpt`
- **Linux:** 下载 `.sh` 安装脚本,默认装到 `/opt/snap/`,gpt 在 `/opt/snap/bin/gpt`
- 安装时勾选 "Sentinel-1 Toolbox"(InSAR 必备)
- 装完跑 `gpt -h` 应输出 SNAP 版本信息

### 3.2 snaphu 安装
- **macOS:** `brew install snaphu`
- **Ubuntu/Debian:** `apt install snaphu`
- **源码:** https://web.stanford.edu/group/radar/softwareandlinks/sw/snaphu/ 编译

### 3.3 Java 安装(SNAP 强依赖)
SNAP 支持 Java 8 / 11 / 17,推荐 **OpenJDK 11**:
- **macOS:** `brew install openjdk@11` 然后按 brew 提示加 PATH
- **Linux:** `apt install openjdk-11-jdk`

### 3.4 跑前置检查脚本验证
```bash
cd /opt/deepexplor-services/geo-insar
bash scripts/preflight_check.sh
# Phase 1 项目应该 ✅
# 然后:
python3 -c "
import sys; sys.path.insert(0, '.'); sys.path.insert(0, '..')
from postprocess.insar_local import check_environment
env = check_environment()
print('OK' if env['ok'] else 'MISSING:', env['missing'])
for k,v in env['details'].items(): print(f'  {k}: {v}')
"
```

预期看到 `OK` 且所有 4 项都有版本号/路径。

## 4. SNAP gpt graph 说明

`geo-insar/postprocess/snap_graphs/insar_pipeline.xml` 是 MVP 简化版,做 11 步:
Read × 2 → TOPSAR-Split × 2 → Apply-Orbit × 2 → Back-Geocoding → Interferogram → TOPSAR-Deburst → GoldsteinPhaseFiltering → SnaphuExport → SnaphuImport → PhaseToDisplacement → Terrain-Correction → Write。

### 已知简化项(生产升级方向)
- 默认子带 `IW2`(中心),如果 AOI 跨多个子带需要并行跑 IW1/IW3 再合并
- 没做 ESD(Enhanced Spectral Diversity)精配准 — 长基线场景会有方位向相位 ramps
- snaphu 是 1 个 tile 处理 — 大 AOI(>50×50 km)建议 `numberOfTileRows=4 cols=4`
- 当前只写 `unwrapped_phase.tif`,coherence / wrapped_phase / los_displacement 的导出节点需要补全(用 BandSelect + Write,或者拆成 4 个独立 graph)

替换 graph:`export GEO_INSAR_SNAP_GRAPH=/path/to/your_custom.xml` 或直接编辑 `insar_pipeline.xml`。

## 5. 端到端验证流程

环境装好后,用 Phase 1 同款的庙山金矿测试 AOI:

```bash
cd /opt/deepexplor-services/geo-insar

# 验证 1:用 cloud 跑同一对(基线对照)
python3 main.py --kml test_data/zhaoyuan_miaoshan.kml \
                --start 2024-06-01 --end 2024-07-15 \
                --mode cloud --backend INSAR_ISCE_BURST --max-pairs 2
# 等 HyP3 完成,记录 los_displacement.tif

# 验证 2:用 local 跑同一对(本地)
python3 main.py --kml test_data/zhaoyuan_miaoshan.kml \
                --start 2024-06-01 --end 2024-07-15 \
                --mode local --sensor sentinel1 --max-pairs 2
# 检查 downloads/<aoi>/sentinel1_insar/<pair>/ 是否生成 4 产物
```

**通过标准:** Phase 1 vs Phase 2 同一对 SLC 在矿区采空区位置的形变量差异应在 **cm 量级以内**。

## 6. 资源消耗预期

SNAP 单对 IW SLC 干涉:
- **磁盘:** 中间文件 ~30-50 GB(`/_work/` 目录,完成后保留供调试)
- **内存:** JVM heap 默认 8GB(`-c 8G`),最低 4GB
- **耗时:** 单 burst ~10-15 min;整景 IW(9 个 burst)~40-60 min
- **CPU:** SNAP 多线程,跑满 4-8 核

如果磁盘紧张,在 `insar_local.run_pair()` 完成后可以删除 `<pair_dir>/_work/`。

## 7. ALOS-2 特别注意

ASF 的 ALOS-2 数据**不在普通 Earthdata 授权范围内**,需要单独申请:
1. 通过 https://www.eorc.jaxa.jp/ALOS/en/dataset/palsar2_l11_e.htm 申请 RESTEC 账号
2. 在 NASA Earthdata 控制台关联 ALOS-2 access(可能要走 ASF 邮件审批)
3. 审批通过后才能用 `--sensor alos2`

C 波段(Sentinel-1)无审批需求,Phase 2 启动可以先只用 Sentinel-1。

## 8. 故障排查

| 现象 | 修复 |
|---|---|
| `check_environment` 报 SNAP gpt 未找到 | `which gpt` 应输出 SNAP 路径,不是 `/usr/sbin/gpt`(macOS 分区工具)。在 `--gpt` 参数显式指定:`--gpt /Applications/snap/bin/gpt` |
| `Java not found` | 装 OpenJDK 11,`export PATH=/opt/openjdk-11/bin:$PATH` |
| `gpt` 跑到一半 OOM | 加大 heap:`-c 16G`(改 insar_local.py 第 ~120 行)或 SNAP 的 `~/.snap/etc/snap.properties` |
| snaphu 卡住几小时 | tile 数过小,改 graph 里 `numberOfTileRows/cols` 到 4×4 |
| Sentinel-1 SLC 下载 401 | 确认 NASA Earthdata 账号已授权 ASF 应用:https://urs.earthdata.nasa.gov/approve_app?client_id=BO_n7nTIlMljdvU6kRRB3g |
| ALOS-2 SLC 403 | 走 JAXA RESTEC 申请流程(§7) |
| pyroSAR 报 SNAP 路径错误 | 装完 SNAP 后跑一次 `snap --nogui` 让 SNAP 自动写入 pyroSAR 兼容的配置 |

## 9. Phase 2 文件清单(总览)

```
geo-insar/
├── main.py                                     (改:+4 CLI 参数, +_run_local)
├── downloader/
│   ├── sentinel1_slc.py                        (新建,165 行)
│   └── alos2_insar.py                          (新建,105 行)
├── postprocess/
│   ├── insar_local.py                          (新建,290 行)
│   └── snap_graphs/
│       └── insar_pipeline.xml                  (新建,150 行)
└── docs/
    └── PHASE_2_HANDOFF.md                      (本文档)
```

Phase 1 / Phase 1.5 文件全部保持不变,**零回归**。`--mode cloud`(默认)走 Phase 1 路径完全不受影响。
