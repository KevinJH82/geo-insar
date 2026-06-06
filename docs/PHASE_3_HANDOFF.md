# Phase 3 第三方 InSAR 成品(LiCSAR)交付清单

> Phase 3 把 COMET LiCSAR 全球 Sentinel-1 衍生品接入 geo-insar,作为 Phase 1(HyP3)和 Phase 2(本地 SNAP)之外的"零处理"路线。
> 不需要等 HyP3 处理(节省 30min-2h),也不需要本地装 SNAP/snaphu(节省 GB 级磁盘和环境配置)。

## 1. 改动总览

| 类型 | 文件 | 行数估算 | 说明 |
|---|---|---|---|
| 新建 | `downloader/licsar.py` | ~270 | LiCSAR 抓取器(HTTP 目录索引解析 + 下载) |
| 新建 | `postprocess/licsar_postprocess.py` | ~180 | LiCSAR → commons/insar_schema 标准化 |
| 改动 | `main.py` | +2 参数 + 1 函数 | `--mode licsar` + `--licsar-frame` + `_run_licsar()` |
| 改动 | `web/templates/index.html` | +25 行 | 任务列表下加 "其他数据源 (CLI)" 提示卡 |

## 2. LiCSAR 是什么

**COMET LiCSAR** = COMET Looking inside the Continents from Space Automated Routine。
- 由英国 NERC 资助,Leeds + Oxford + Reading 大学合办
- 持续对 Sentinel-1 全球数据自动跑 InSAR,产出**已解缠的形变图 + 相干性**
- 公开数据,无需登录(走 CEDA 存档 HTTP)
- 主要覆盖**阿尔卑斯-喜马拉雅构造带**和**全球火山区**

**数据组织:**
```
https://data.ceda.ac.uk/neodc/comet/data/licsar_products/<track>/<frame_id>/
├── products/<refdate>_<secdate>/
│   ├── <ref>_<sec>.geo.unw.tif         # 解缠相位(弧度,主产物)
│   ├── <ref>_<sec>.geo.cc.tif          # 相干性(0-1)
│   └── <ref>_<sec>.geo.diff_pha.tif    # 干涉相位
├── epochs/<date>/                       # 单时相产品
└── metadata/                            # frame 边界 KML
```

**Frame ID 格式:** `<track 3 位><A|D>_<frame 5 位>_<looks 6 位>`
例如 `022D_05411_131313` = track 022 / Descending / frame 05411 / 13×13 多视

## 3. CLI 用法

```bash
# 1. 到 LiCS Portal 查找覆盖研究区的 frame ID
#    https://comet.nerc.ac.uk/comet-lics-portal/
#    通常一个研究区可能被 2-4 个 frame 覆盖(升降轨各 1-2 个)

# 2. 用 frame ID 跑 LiCSAR 模式
python3 main.py --kml AOI.kml --start 2024-06-01 --end 2024-08-01 \
                --mode licsar \
                --licsar-frame 022D_05411_131313 \
                --max-pairs 20

# 3. 输出落到 downloads/<aoi>/sentinel1_insar/<pair>/(与 HyP3 / SNAP 完全一致的契约)
```

## 4. 数据契约对齐

LiCSAR 原生文件名 → geo-insar 标准产物名:

| LiCSAR 文件 | 标准名 | 说明 |
|---|---|---|
| `*.geo.unw.tif` | `unwrapped_phase.tif` | 直接拷贝(弧度) |
| `*.geo.cc.tif` | `coherence.tif` | 直接拷贝(0-1) |
| `*.geo.diff_pha.tif` | `wrapped_phase.tif` | 直接拷贝 |
| **(LiCSAR 不提供)** | `los_displacement.tif` | **从解缠相位反算**:`disp_mm = unw * λ/(4π) * 1000`,λ=55.466 mm |

`metadata.json` 字段:
- `source: "licsar"`
- `source_version: "COMET LiCSAR (NERC)"`
- `polarization: "VV"`(LiCSAR 都用 VV)
- `frame_id`:LiCSAR 原 frame ID
- `incidence_angle_mean: 38.0`(Sentinel-1 IW 默认,精确值需查 frame metadata,Phase 3+ 优化)
- `perp_baseline_m: null`(LiCSAR 元数据中通常没有)

## 5. 中国境内覆盖说明

LiCSAR 优先级最高的是**阿尔卑斯-喜马拉雅构造带**(包括中国西部),次优是全球火山区。

| 区域 | 覆盖情况 |
|---|---|
| 西藏、新疆、青海 | ✅ 较好覆盖(在构造带内) |
| 云南、四川西部 | ✅ 较好覆盖(地震带) |
| 内蒙古、华北 | ⚠️ 部分覆盖,需查 portal |
| 山东、东部沿海 | ⚠️ 不一定有,以 LiCS Portal 查询为准 |
| 海洋 | ❌ 不处理 |

**操作建议:** 在用 `--mode licsar` 前,先到 [LiCS Portal](https://comet.nerc.ac.uk/comet-lics-portal/) 用研究区 bbox 查可用 frame。**没有覆盖时降级到 Phase 1(HyP3)或 Phase 2(SNAP)。**

测试 AOI(山东招远庙山金矿)经验:portal 显示该位置**有部分覆盖**,但 frame ID 需要在 portal 上确认(本仓库没有内置 frame 几何索引)。

## 6. 与 Phase 1 / Phase 2 的对比

| 维度 | Phase 1 (HyP3) | Phase 2 (本地 SNAP) | Phase 3 (LiCSAR) |
|---|---|---|---|
| 计算位置 | ASF 云端 | 部署主机 | 已发布(NERC 跑过) |
| 等待时间 | 30 min - 2 h | 单对 ~15-60 min | 0(直接下) |
| 磁盘需求 | <10 GB/对 | ~50 GB/对 | <500 MB/对 |
| 配额 | 1000 job/月 | 无 | 无 |
| AOI 灵活度 | 任意 AOI | 任意 AOI | 受 frame 边界限制 |
| 覆盖区域 | 全球 | 全球 | 仅 LiCSAR 已发布区 |
| 元数据完整度 | 高(perp_baseline 等齐全) | 高(本地直接算) | 中(部分字段缺失) |
| 适合场景 | 灵活研究、新区域 | 不想用云端、有完整 SAR 知识 | 已发布区快速对照、跨阶段交叉验证 |

**典型用法是三者结合**:Phase 1 跑研究区主体,Phase 3 在覆盖区作为基线对照,Phase 2 在长时序或特殊参数时补足。

## 7. 自动 frame 查找(MVP 未实现,Phase 3+ 升级方向)

当前 `search_frames_by_bbox()` 只是占位,**用户必须显式提供 `--licsar-frame`**。

完整实现需要:
1. **离线 frame 几何索引**:从 https://comet.nerc.ac.uk/comet-lics-portal/data/frames-geometry.geojson(假设的端点)下载,缓存到 `geo-insar/data/licsar_frame_index.geojson`
2. **几何相交查询**:用 shapely 在线计算 AOI bbox ∩ frame 几何
3. **降级提示**:无相交时输出"该区域无 LiCSAR 覆盖,建议改用 --mode cloud"

留作 Phase 3.1 增强(等 plan 确认是否值得做)。

## 8. 端到端验证(需要先有 LiCSAR 覆盖的 AOI)

如果用户研究区在山东招远(LiCSAR 覆盖待验证):
```bash
# 假设 portal 上查到覆盖该位置的 frame 是 040A_05111_131313
python3 main.py --kml test_data/zhaoyuan_miaoshan.kml \
                --start 2024-06-01 --end 2024-08-01 \
                --mode licsar --licsar-frame 040A_05111_131313 \
                --max-pairs 5

# 检查 downloads/<aoi>/sentinel1_insar/<pair>/los_displacement.tif
# 与 Phase 1 HyP3 输出做对比(应该量级一致,差异 <1 cm)
```

如果用户研究区是西藏/喜马拉雅地区(LiCSAR 覆盖好),覆盖通常 100%。

## 9. 故障排查

| 现象 | 修复 |
|---|---|
| `--mode licsar` 但没传 `--licsar-frame` | 必须显式指定,见 §3 |
| `LiCSAR 列表失败 ... 503` | CEDA 偶尔维护,过一会再试;或 `--licsar-base-url` 改用 JASMIN fallback |
| 找到 0 对 | 时间窗内该 frame 可能无数据;或日期格式错 |
| 下载快但解压后 los_displacement.tif 缺失 | rasterio 没装或 unw → disp 转换失败,装 rasterio 后重跑(已下载的会跳过) |
| coherence.tif 显示全 0 | LiCSAR 有时延迟更新,检查 epoch 日期 |

## 10. Phase 3 文件清单

```
geo-insar/
├── main.py                                  (改:+2 CLI 参数 + _run_licsar)
├── downloader/
│   └── licsar.py                            (新建,~270 行)
├── postprocess/
│   └── licsar_postprocess.py                (新建,~180 行)
├── web/templates/
│   └── index.html                           (改:加"其他数据源"提示卡)
└── docs/
    └── PHASE_3_HANDOFF.md                   (本文档)
```

Phase 1 / Phase 1.5 / Phase 2 文件完全不变,**零回归**。`--mode cloud`(默认)继续走 HyP3 路径。

## 11. 三阶段交付文档体系

- `HANDOFF.md` — Phase 1 总览(HyP3 云端)
- `PHASE_1.5_HANDOFF.md` — 三个下游对接(geo-reporter / geo-exploration / geo-analyser)
- `PHASE_2_HANDOFF.md` — 本地 SNAP 干涉
- `PHASE_3_HANDOFF.md` — LiCSAR 第三方成品(本文)

geo-insar 的整体方案至此覆盖完毕(对照 plan 文件 `/Users/demacmini/.claude/plans/insar-insar-functional-origami.md` 的 Phase 1/1.5/2/3 全部章节)。
