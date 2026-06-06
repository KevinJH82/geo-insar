# Case Study — 山东招远庙山金矿(2026-05-27)

> AOI: 山东招远庙山金矿 4.974 km² ・ Sentinel-1 InSAR ・ HyP3 ISCE_BURST ・ 矿区沉降监测可行性验证

## 1. 这次建立的可复用工程模板

```
geo-insar/
├── scripts/
│   ├── preflight_check.sh          # 启动前置检查(已能识别"自己的服务",不会误报端口冲突)
│   ├── postprocess_task.py         # 批量解压 + 标准化 + 建栈索引
│   ├── quicklook_pairs.py          # 渲染代表对的 LOS 形变 + 相干性 PNG
│   ├── sbas_invert.py              # 简化 SBAS 时序反演(纯 numpy)
│   └── align_ifgrams_for_mintpy.py # HyP3 输出 size 对齐(MintPy 必须)
├── config.yaml                     # pairing 段(矿区/滑坡/油气场景预设默认值)
├── mintpy_work/<burst>/
│   ├── smallbaselineApp.cfg        # MintPy 配置模板(可直接套到新 AOI)
│   └── ifgrams_aligned/            # 对齐到统一 grid 的 ifgram 目录
└── downloads/<aoi>/
    ├── sentinel1_insar/            # 标准化产物(每个 pair 一个目录)
    ├── stack_index.json            # 时序栈索引
    ├── sbas/<burst>/               # 自己 SBAS 输出(速率图 + 时序栈 + 点位曲线)
    └── quicklooks/                 # 快视图 PNG
```

## 2. 新 AOI 一键复现流程

```bash
# 1. 前端提 task(走 config.yaml::pairing 矿区默认:36d / 150m / 120 pairs)
#    → 任务异步执行 → HyP3 处理 → 自动下载到 downloads/task_<id>/

# 2. 批量解压标准化 + 建栈
python3 scripts/postprocess_task.py <task_id>

# 3. 快视图扫一眼数据质量(每 burst 挑 2 张)
python3 scripts/quicklook_pairs.py <task_id> --per-burst 2

# 4. 简化 SBAS 反演(自动选 pair 数最多的 burst)
python3 scripts/sbas_invert.py <task_id>

# 5. 如需上 MintPy
python3 scripts/align_ifgrams_for_mintpy.py \
    mintpy_work/<burst>/ifgrams \
    mintpy_work/<burst>/ifgrams_aligned

cd mintpy_work/<burst>
source ~/miniforge3/etc/profile.d/conda.sh
conda activate mintpy
smallbaselineApp.py smallbaselineApp.cfg
```

## 3. 招远庙山金矿实证结论

| 指标 | 值 |
|---|---|
| 时间范围 | 2018-11-25 ~ 2019-12-13(约 1 年) |
| 干涉对数 | 120(4 burst:IW1×25, IW2×53, IW3×21+21) |
| 平均相干性 | 0.90(矿区裸露地表,C-band 表现极佳) |
| LOS 速率(MintPy P5~P95) | -18 ~ +30 mm/yr,std 14.9 mm/yr |
| **形变判断** | **AOI 内无 ≥ 10 mm/年的沉降信号**;实际形变可能在 ±5 mm/yr 大气噪声地板之下 |

**为什么没看到强信号(合理推测)**:招远庙山是 1990s 开始开采的成熟矿区,可能闭坑后稳定;或主采空区不在这 5 km² 范围内。要拿到"区分 0 vs 3 mm/年缓慢沉降"级别的精度,**必须接 GACOS 大气校正**(预期能把 std 从 15 mm/yr 压到 ~5 mm/yr)。

### 自己 SBAS vs MintPy(无 GACOS)对比

| 指标 | 自己 SBAS | MintPy | 改善 |
|---|---|---|---|
| 速率极值 | ±200 mm/yr | ±150 mm/yr | -25%(unwrap 错误检测) |
| 标准差 | 17.58 mm/yr | 14.89 mm/yr | -15% |
| P5~P95 区间宽度 | 53 mm/yr | 48 mm/yr | -10% |
| 有效像素 | 40.6% | 39.5% | 相近 |

**结论**:不接 GACOS 时,MintPy 改善有限(标准差 -15%)。真正决定噪声地板的是大气校正。

## 4. 这轮踩过的真实坑(下次复用时记得)

| # | 坑 | 解决 |
|---|---|---|
| 1 | `INSAR_ISCE_BURST` 后端必须用 burst granule(`S1_005150_IW3_..._BURST`),不是整景 SLC(`S1A_IW_SLC__1SDV_...`),否则 HyP3 schema 校验失败 | `search()` 按 backend 切 `processingLevel`,burst 模式用 `(fullBurstID, polarization)` 分组 |
| 2 | hyp3_sdk 7.x 的 `submit_insar_*` 返回 `Batch` 而非 `Job`,直接取 `.job_id` 会 AttributeError | 改用 `result.jobs[0]` |
| 3 | hyp3_sdk 7.x 把 GAMMA 后端的 `include_inc_angle` 参数改名为 `include_inc_map` | 调用处同步改名 |
| 4 | HyP3 ISCE_BURST 同 burst 不同对的 raster size 差几像素(10 种 size,53 对里只有 11 对同 shape) | 自己 SBAS 用 rasterio reproject;MintPy 必须先用 `align_ifgrams_for_mintpy.py` 全部 gdal Warp 到参考网格 |
| 5 | MintPy 1.11 不兼容 numpy 2.x,反演阶段 `inv_quality[idx] = inv_quali` 抛 ValueError | 新装 env 后 `mamba install -n mintpy "numpy<2"` 降到 1.26.4 |
| 6 | `pair_id` 格式 `{ref}_{sec}_{pol}` 不含 burst,同日期不同 burst 在标准化时互相覆盖(120 → 98 数据丢失) | 加 `burst_id` 前缀,新格式 `{burst_id}_{ref}_{sec}_{pol}`,顺便填到 metadata.frame_id |
| 7 | Web 服务的 `_poll_hyp3_loop` 原本是 placeholder,导致 HyP3 那边 SUCCEEDED 后本地永远显示 0/N 完成 | 实现完整闭环:`get_job_by_id` 拉状态 → SUCCEEDED 立刻 `download_files()` 落地 → DOWNLOADED |
| 8 | matplotlib 默认字体不含 CJK,标题中文显示方框 | 加 `rcParams["font.sans-serif"] = ["PingFang SC", ...]` 字体回退链 |
| 9 | `preflight_check.sh` 把"自己的 Flask 服务"误报为端口被占用 | 检查 PID 的 cwd / 命令行是否在 geo-insar 项目内 |

## 5. 配置文件关键修改记录

### `config.yaml::pairing`(矿区场景默认)
```yaml
pairing:
  strategy: closest_in_time
  max_temporal_baseline_days: 36   # 矿区 24~36 天捕获月级沉降
  max_perp_baseline_m: 150         # 收紧减少 DEM 误差
  max_pairs: 120                   # 100~150 可覆盖 3~6 个月监测
```
其他场景参考:
- 滑坡/边坡:36 / 150 / 100~150
- 油气/地热:48~72 / 150 / 200~300(strategy=cascade)
- 构造形变:96 / 150 / 300+(配合 PS-InSAR)

### `web/app.py` 调整摘要
- 模块顶部加载 `config.yaml::pairing`,4 个 endpoint 参数 fallback 改从 config 读
- `search()` 调用全部传 `backend`(让 ISCE_BURST 走 burst 搜索)
- `_poll_once()` 从 placeholder 改为完整闭环实现
- 新增 `_maybe_finalize_task()`:所有 jobs 终态时自动把 task 标 `done`

### `mintpy_work/365365_IW2/smallbaselineApp.cfg` 关键字段
```
mintpy.load.processor          = hyp3
mintpy.load.unwFile            = ./ifgrams_aligned/*/*_unw_phase.tif
mintpy.load.corFile            = ./ifgrams_aligned/*/*_corr.tif
mintpy.load.connCompFile       = ./ifgrams_aligned/*/*_conncomp.tif
mintpy.load.demFile            = ./ifgrams_aligned/*/*_dem.tif
mintpy.load.incAngleFile       = ./ifgrams_aligned/*/*_lv_theta.tif
mintpy.load.azAngleFile        = ./ifgrams_aligned/*/*_lv_phi.tif
mintpy.network.coherenceBased  = no       # 高相干区不需要自动剔
mintpy.unwrapError.method      = bridging # closure-phase 修 unwrap 错误
mintpy.troposphericDelay.method = no      # 未接 GACOS;接通后改 gacos
mintpy.deramp                  = linear   # 简化大气校正
mintpy.topographicResidual     = yes
```

## 6. 下一步可选方向(若继续)

1. **接 GACOS 大气校正**:http://www.gacos.net 注册 → 申请 KML AOI 的 ZTD 数据 → 改 cfg `troposphericDelay.method = gacos`。预期 std 从 15 → ~5 mm/yr,真实毫米级形变才能浮出来。
2. **重提 cascade 策略任务**:M/(N-1) 从当前 1.0x 升到 3-5x,单对 unwrap 错误会被冗余平均掉,出图更干净。
3. **换更活跃的 AOI** 重做整条流水线:本次走通的 `API 配对 → HyP3 → 下载 → 标准化 → quicklook → SBAS → MintPy` 模板可直接套用。
