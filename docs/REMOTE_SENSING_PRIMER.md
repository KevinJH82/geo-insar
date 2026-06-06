# 遥感基础 · InSAR 原理 · HyP3 介绍

> 本文档为 geo-insar 平台的**配套科普**,目的是让运维和业务用户在不背 SAR 教科书的前提下,理解平台在做什么、各个模块为什么存在、产物为什么这样。读完后再看 `USER_GUIDE.md` 操作起来会更顺。

---

## 0. 一张图先放在最前面

```
┌─────────────────────────────────────────────────────────────────┐
│                  整体遥感数据工作流(本项目)                       │
└─────────────────────────────────────────────────────────────────┘

  外部数据源                  geo-downloader              四个下游子系统
  ───────────                 ─────────────                ─────────────
  Copernicus    ─────┐        ┌─ 原料下载(光学)
  Earthdata     ─────┤        │   Sentinel-2、Landsat、
  ASF DAAC      ─────┼───→    │   ASTER、ECOSTRESS、…
  USGS          ─────┘        │
                              ├─ 原料下载(SAR GRD)        ┌─→ geo-reporter  报告生成
                              │   Sentinel-1 GRD          │   (光学 + InSAR 章节)
                              │                           │
                              ↓                           ├─→ geo-exploration 矿产探测
                       postprocess(光学)                  │   (SlowVars 8 因素检测)
                         · LST                            │
                         · 温度梯度                       └─→ geo-analyser  分析平台
                         · 温度异常梯度                       (矿物识别 + 形变分析)
                         · OTCI
                              │
                              ↓
                          光学 4 衍生品交付
                              │
  ┌───────────────────────────┼───────────────────────────┐
  │  本项目 Phase 1-3 新增的支线:InSAR(geo-insar)        │
  └───────────────────────────┼───────────────────────────┘
                              ↓
              ┌───────────────┴───────────────┐
              │   geo-insar(独立子系统)        │
              │                                 │
              │  Phase 1: HyP3 云端              │←─ ASF 云端处理(本文 §8)
              │  Phase 2: 本地 SLC + SNAP        │←─ 自处理 SAR 原料
              │  Phase 3: LiCSAR 第三方成品      │←─ 别人发布好的衍生品
              │                                 │
              │  统一输出: 4 个 InSAR 产物       │
              │    · unwrapped_phase             │
              │    · coherence                   │
              │    · los_displacement            │
              │    · wrapped_phase               │
              └───────────────┬─────────────────┘
                              │
                              └──→ 三个下游子系统(同光学衍生品的消费路径)
```

记住一件事:**geo-downloader 处理"光学衍生品",geo-insar 处理"形变衍生品",二者在数据契约上并列、互不冲突**。

---

## 1. 遥感数据的两大类

遥感卫星按"传感器类型"分两大类,本平台两类都处理,但用不同方式:

### 1.1 光学遥感(被动式,**geo-downloader 主战场**)

- **原理**:卫星只是个"超级相机",**被动**接收太阳光经地面反射后回到太空的信号
- **代表**:Landsat、Sentinel-2、ASTER、MODIS
- **波段**:可见光 + 近红外 + 短波红外 + 热红外(对应 RGB → NDVI → 矿物吸收 → 温度)
- **特点**:
  - ✅ 直观,人眼可读(RGB 拼起来就是一张地表照片)
  - ✅ 波谱信息丰富(几百到几千个波段),擅长**物质识别**(植被/矿物/水)
  - ❌ **怕云、怕夜、怕雨**(没有太阳光就没数据)
  - ❌ **不能直接测形变**(像素移动几毫米肉眼看不出来)

### 1.2 主动遥感雷达 SAR(本平台 InSAR 的基础)

- **原理**:卫星自己**发射**微波信号到地面,接收**回波**。相当于带着手电筒拍夜景
- **代表**:Sentinel-1(本平台主用)、ALOS-2、TerraSAR-X
- **波长**:厘米级(C 波段 ~5.6 cm,L 波段 ~24 cm,X 波段 ~3 cm)
- **特点**:
  - ✅ **穿云、夜间、风雨**都能拍(微波不在乎)
  - ✅ 信号是**复数**(同时记录振幅 + 相位),相位是 InSAR 的关键
  - ✅ 每次成像精确到 mm 量级,**两次拍摄可算地面 mm 级形变**(InSAR)
  - ❌ 影像难读(几何畸变、相干噪声),不能"看图说话"
  - ❌ 算法重(需要专业 SAR 处理软件)

**为什么 InSAR 单独做一个子系统?**因为 SAR 数据从"原料"到"可用形变图"中间隔了 7-15 个处理步骤,跟光学的"波段算术"完全不是同一回事。geo-downloader 负责把 SAR GRD(已经做完地理编码的强度图)拿到本地,但要拿到形变图必须额外的干涉处理流程——这就是 geo-insar 存在的意义。

---

## 2. SAR 工作原理 30 秒讲完

```
卫星(同一颗,沿轨道前进)
   │
   │  ① 发射微波脉冲(波长 λ ≈ 5.6 cm,Sentinel-1)
   ├─────────────────────────→ 地面
   │                            │
   │  ② 接收回波(2-way 距离 R) │
   │←─────────────────────────  │
   ↓
记录每个像素的两个数:
  · 振幅 A  — 地表对微波的反射强度(可视化为灰度图)
  · 相位 φ  — 信号往返的精确相位(0~2π),与距离 R 直接相关
              φ = -4π R / λ  +  地表散射相位(随机但可重复)
```

**关键事实**:**单次** SAR 影像的相位 φ 看起来是随机噪声(因为地表散射本身是统计随机的),没什么用。但**两次** SAR 影像在同一地点的相位差 Δφ,**绝大部分随机项会抵消**,剩下的就是这两次之间地面动了多少 ——这就是 InSAR 的全部哲学。

---

## 3. InSAR 一句话推导

两次 SAR 影像的相位差:

```
Δφ = φ_1 - φ_2
   = (-4π R_1 / λ) - (-4π R_2 / λ) + (噪声基本抵消)
   = -4π × ΔR / λ
```

其中 `ΔR = R_2 - R_1` 是这两次拍摄之间**地面像素到卫星的距离变化**——也就是 LOS(Line of Sight,视线方向)形变量!

反过来:

```
ΔR (mm) = -Δφ × λ / (4π) × 1000
```

代入 Sentinel-1 C 波段 λ = 55.466 mm:

```
ΔR (mm) ≈ -Δφ × 4.41
```

**一个 2π 的相位差 ≈ 28 mm LOS 形变**(Sentinel-1)。

> LiCSAR 模式之所以能"从解缠相位反算 LOS 形变"也就是这个公式(见 `postprocess/licsar_postprocess.py::_convert_phase_to_displacement`)。

---

## 4. 但事情没这么简单——InSAR 的 5 大挑战

刚才的推导假设"两次拍摄之间只有地面动了",现实是相位差里还混了一堆其他东西。**理解这些挑战,就理解了为什么 InSAR 算法这么复杂、为什么 HyP3 / SNAP / LiCSAR 这些工具存在**。

### 4.1 相位卷绕(Phase Wrapping)

相位天生在 [-π, π] 区间,**累计形变超过 28 mm 就会"卷绕"回去**。比如真实形变是 50 mm,相位差看起来只是 50 - 28 = 22 mm。

**解决**:**解缠(unwrapping)** 算法,基于空间连续性恢复真实的累计相位。snaphu 是最常用的解缠器(本平台 Phase 2 / LiCSAR 都用它)。这也是为什么标准输出区分:
- `wrapped_phase.tif`(原始,带卷绕)
- `unwrapped_phase.tif`(解缠后,真实相位)
- `los_displacement.tif`(unwrap × λ/(4π),最终 mm 形变)

### 4.2 时间去相干(Temporal Decorrelation)

地表的散射特性会随时间变化(植被长、土壤湿、积雪化),**两次拍摄间隔越久,散射相位就越不"相干"**,InSAR 信号被淹没。

**衡量**:**相干性 coherence(0-1)**。每个像素一个值,coherence < 0.3 基本是噪声。

**实际影响**:
- 城市/裸岩区:coherence 高(0.6+),信号稳定
- 农田/森林:coherence 低(常 < 0.3),InSAR 几乎不可用
- 雪覆盖:每场雪都会让 coherence 重置

**应对策略**:用时间基线短的对(本平台默认 12-36 天),用 L 波段(ALOS-2)穿透植被。

### 4.3 几何去相干(Geometric/Baseline Decorrelation)

两次卫星轨道并非完全重合,**垂直基线 B⊥**(两次轨道在垂直方向的距离)如果太大,从两个角度看同一地物会产生几何性相位错乱。

**经验阈值**:Sentinel-1 IW 模式 B⊥ < 200 m 较好。本平台默认 150 m(矿区监测预调值)。

### 4.4 大气延迟

电离层和对流层中的水汽会延迟微波传播,**几 cm 量级**的"虚假形变"。山区尤其严重。

**应对**:
- HyP3 / LiCSAR 可选 GACOS 大气校正
- 时序 InSAR(PS/SBAS)能在多对统计中消除大气随机性
- 本平台 MVP 暂不集成 GACOS,后续 Phase 3.x 可加

### 4.5 地形畸变

陡峭地形导致 SAR 影像出现 layover(叠掩)、shadow(阴影)、foreshortening(前向压缩)。

**应对**:Terrain Correction(地形校正,Range-Doppler)用 DEM 投影到地理坐标,本平台所有输出都已做了 TC,直接是 EPSG:4326 GeoTIFF。

---

## 5. Sentinel-1 平台

本平台**主用**的 SAR 卫星,理解它的特性能帮你设参数。

### 5.1 基本参数

| 项 | 值 | 影响 |
|---|---|---|
| 运营方 | ESA(欧洲航天局) | 数据公开免费 |
| 在轨星 | S1A(2014-)、S1B(2016-2022 故障)、S1C(2024-) | 重访周期变化 |
| 波长 | 5.6 cm(C 波段) | 28 mm/2π 形变灵敏度 |
| 重访周期 | **理论 6 天**(双星)/ **实际 12 天**(单星) | 时间基线最小步长 |
| 主要模式 | **IW**(Interferometric Wide swath) | 标准 InSAR 模式 |
| IW 幅宽 | 250 km | 大区域监测 |
| 像素分辨率 | 5×20 m(IW) | 干涉处理后约 30 m |
| 极化 | VV / VH(陆地标配)/ HH / HV | 矿区监测一般 VV |
| 轨道方向 | Ascending(升轨,白天) / Descending(降轨,夜间) | 升降轨联合可分解 E/N/U 三分量形变 |

### 5.2 IW 模式的内部结构(关键)

IW 模式不是连续成像,而是把 250 km 幅宽**切成 3 个 sub-swath(IW1/IW2/IW3)**,每个 sub-swath 又切成多个 **burst**(约 25 km × 80 km)。

```
        Sentinel-1 IW 整景(~250 × 250 km)
        ┌────────────────────────────────┐
        │   IW1   │   IW2   │   IW3      │ ← 3 个子带
        ├─────────┼─────────┼────────────┤
        │ burst 1 │ burst 1 │ burst 1    │
        ├─────────┼─────────┼────────────┤
        │ burst 2 │ burst 2 │ burst 2    │
        ├─────────┼─────────┼────────────┤
        │   ...   │   ...   │   ...      │ ← 每子带 ~9 个 burst
        └─────────┴─────────┴────────────┘
```

**为什么重要**:
- 小 AOI(< 25 km × 25 km)只覆盖 1 个 burst,**完全没必要处理整景**——这就是 HyP3 提供 **ISCE_BURST 后端**的原因(只处理单 burst,省时省配额)
- 大 AOI 跨多 burst 时用 **GAMMA 整景**模式更合适
- 平台前端会按 AOI 面积自动建议(< 600 km² → BURST,> 2500 km² → GAMMA)

### 5.3 数据产品级别

ASF 提供 Sentinel-1 数据有多种产品级别,本平台用到的:

| 级别 | 全称 | 含相位 | 几何 | 适合 |
|---|---|---|---|---|
| **SLC** | Single Look Complex | ✅ | radar geometry | InSAR 原料(GAMMA 后端) |
| **BURST** | 单 burst 切出来的 SLC | ✅ | radar geometry | InSAR 原料(ISCE_BURST 后端) |
| GRD | Ground Range Detected | ❌(只有振幅) | 地理编码后 | 海洋/地物分类,**不能干涉** |

**关键**:Phase 1 / 2 的 InSAR 流程一定要 SLC 或 BURST,GRD 不行。geo-downloader 的 sentinel1.py 默认下 GRD(因为它做光学分析的衍生品),geo-insar 的 sentinel1_insar.py 才下 SLC/BURST。这是两套独立流程。

---

## 6. ASF HyP3 详解(Phase 1 主线)

### 6.1 HyP3 是什么

**HyP3** = Hybrid Pluggable Processing Pipeline,**ASF(Alaska Satellite Facility)运营的免费 SAR 云端处理平台**。

- **网址**:https://hyp3-api.asf.alaska.edu/ui/
- **登录**:用 NASA Earthdata 账号(就是 geo-downloader 配的同一个 `kevinjh`)
- **价格**:免费,每账号默认配额 **1000 job/月**
- **运行地点**:AWS 美国云上,你只是远程触发处理
- **来源**:ASF 是 NASA 资助的 4 个 SAR 数据中心之一,本身就托管 Sentinel-1 / ALOS-2 / NISAR(即将)的原始数据

### 6.2 HyP3 能做什么

提交一个 "job",HyP3 在云端跑完整的 SAR 处理流程,产物可下载:

| Job 类型 | 输入 | 输出 | 用途 |
|---|---|---|---|
| **RTC_GAMMA** | 1 个 GRD | 辐射地形校正影像 | SAR 地物分类 |
| **INSAR_GAMMA** | 2 个 SLC | 干涉对 4 产物 + 元数据 | **本平台 GAMMA 模式** |
| **INSAR_ISCE_BURST** | 2 个 BURST | 单 burst 干涉对 | **本平台 BURST 模式** |
| autoRIFT | 2 个 SLC | 流速场 | 冰川、滑坡 |

**本平台只用 INSAR_GAMMA 和 INSAR_ISCE_BURST**。

### 6.3 HyP3 的内部流程(以 INSAR_ISCE_BURST 为例)

提交 job 后,HyP3 在云端执行:

```
   你的请求(主+从 burst granule ID)
            │
            ↓
   1. 拉取原始 BURST 数据(从 ASF 自己的归档)
   2. Apply Precise Orbit(精轨道,~3 周后才有)
   3. ISCE 配准(reference ↔ secondary)
   4. 计算干涉图 + 相干性
   5. 去平地相位 + 去地形相位(用 GLO-30 DEM)
   6. Goldstein 相位滤波
   7. SNAPHU 解缠
   8. Range-Doppler Terrain Correction → WGS84 GeoTIFF
   9. 打包 ZIP 上传 S3
            │
            ↓
   你的程序轮询拿到 download URL → 下载
            │
            ↓
   geo-insar 标准化(unwrap → mm,改名,写 metadata.json)
```

**单 job 耗时**:30 min – 2 h,主要看队列长度和 burst 大小。

### 6.4 HyP3 的提交方式

HyP3 提供官方 Python SDK:`hyp3_sdk`。本平台 `downloader/sentinel1_insar.py::submit_pairs()` 就是这个 SDK 的封装。

```python
import hyp3_sdk

hyp3 = hyp3_sdk.HyP3(username, password)  # 或 EDL token

# ISCE_BURST(本平台默认)
job = hyp3.submit_insar_isce_burst_job(
    granule1="<burst_id_ref>",
    granule2="<burst_id_sec>",
    name="my-pair-name",
    apply_water_mask=False,
)
# job.job_id 拿来后续查状态

# GAMMA(整景)
job = hyp3.submit_insar_job(
    granule1="<scene_name_ref>",
    granule2="<scene_name_sec>",
    name="my-pair-name",
    include_los_displacement=True,
    include_wrapped_phase=True,
    apply_water_mask=False,
)
```

### 6.5 与 geo-insar 的对接

```
geo-insar Web UI(8084)
    │ POST /api/run
    ↓
sentinel1_insar.py
  · search()            ← asf_search 查 SLC/BURST
  · make_pairs()        ← 按 closest_in_time / fixed_master 配对
  · submit_pairs()      ← hyp3_sdk 提交
    │
    ↓
task_store(SQLite)
    · 每对一条 job 记录,状态 submitted → cloud_processing → ready → downloaded
    │
    ↓
后台轮询线程(5 min/轮)
    · hyp3.get_job_by_id() 查状态
    · SUCCEEDED 时下载 ZIP
    · 调 postprocess/insar.py 标准化(改名 + 元数据)
```

### 6.6 配额管理

- 默认 **1000 job/月**,过了次月 1 号重置
- 矿区监测一个 AOI 一年 12 个对,**够监测 80 个 AOI/月**
- 配额用尽前 HyP3 没主动报警,要靠 portal 自己看:https://hyp3-api.asf.alaska.edu/ui/
- 如果需要扩容,联系 ASF support(uso@asf.alaska.edu)

### 6.7 何时**不用** HyP3

| 场景 | 替代方案 |
|---|---|
| 配额用完了 | 切 Phase 2 本地 SNAP(无配额) |
| 不信任美国云、要数据本地化 | Phase 2 本地 SNAP |
| 想要 ALOS-2 L 波段(穿透植被) | Phase 2 本地 SNAP(HyP3 不直接支持 ALOS-2 InSAR) |
| 想要时序 InSAR(PS/SBAS) | Phase 3 LiCSAR,或者自己跑 MintPy |
| 研究区已被 LiCSAR 处理过 | Phase 3 LiCSAR(0 配额,直接下) |

---

## 7. 三条处理路线的本质对比

### 7.1 处理位置 / 数据流

```
Phase 1 (HyP3):
  AOI → 提交 burst IDs → AWS 美国云端处理 → 下 ZIP → 标准化
  你只下"最终产物",中间数据不落本地

Phase 2 (本地 SNAP):
  AOI → 搜 SLC → 下原始 SLC ZIP(~4 GB/景)→ pyroSAR 调 gpt 跑 11 步 → snaphu 解缠 → 标准化
  你需要本地有 ~50 GB 中间文件 + Java + SNAP + snaphu

Phase 3 (LiCSAR):
  Frame ID → CEDA HTTP 抓已发布的 .geo.unw.tif 等 → 标准化(unwrap → mm 反算)
  你只下别人处理好的成品,**0 处理**
```

### 7.2 算法上的细微差别

| 步骤 | HyP3 | 本地 SNAP | LiCSAR |
|---|---|---|---|
| 配准 | ISCE 或 GAMMA | SNAP Back-Geocoding | GAMMA(NERC 内部) |
| 解缠 | SNAPHU | SNAPHU | SNAPHU |
| TC | GLO-30 DEM | SRTM 1Sec | NASA DEM |
| 大气校正 | 可选 GACOS | 默认无 | 自动 GACOS |
| 多视 | 自动 | 默认 5×1 | 13×13 |

**结论**:三条路在矿区量级的形变上**应该差异 < 1 cm**,完美一致不太可能(算法和 DEM 差异)。建议**至少跑两条作交叉验证**——这也是 plan 中"联调验证"那一节的设计初衷。

---

## 8. 矿区沉降的物理图景

业务场景的核心就是这个,顺便讲下。

### 8.1 沉降机制

- **采空区上方** 塌陷 → 地表下沉
- **地下水位下降** → 土层压缩 → 大范围沉降
- **尾矿坝形变** → 危险预警

### 8.2 量级和时间尺度

| 矿种 | 典型形变速率 | 形变模式 | 探测建议 |
|---|---|---|---|
| 浅煤矿(露天/浅井) | **dm/月**(显著) | 局部漏斗形 | 12 天对就能看 |
| 深煤矿(井工) | **cm/月** | 大面积沉降 | 短期对(12-24 天) |
| 金属矿(金/铜/铅锌) | **mm/年 ~ cm/年** | 集中在采空区 | 长时序(6+ 个月) |
| 城市地下水 | **mm/年 ~ cm/年** | 区域性 | 多年时序 |

### 8.3 庙山金矿测试 AOI 的预期

招远庙山金矿是**深井金矿**,形变较温和:
- 量级:可能 mm/年 到 cm/年
- C 波段 Sentinel-1 灵敏度 ~5 mm,**单对干涉可能看不到清晰信号**
- 需要 **3-6 个月的多对堆栈** 才能看出趋势——这也是为什么默认 `max_pairs: 120`

### 8.4 形变和构造的关联(geo-exploration 用法)

矿区周边的 InSAR LOS 速率差分场,可以揭示:
- **活动断层**:沿断层带的速率突变 → SlowVars 的 `fault_activity` 增强(Phase 1.5)
- **采空区**:局部沉降漏斗 → SlowVars 的 `surface_deformation` 新因素
- **应力转移**:大范围渐变速率 → 与地质构造图叠加

---

## 9. 常用术语对照表

| 中文 | 英文 | 缩写 | 释义 |
|---|---|---|---|
| 合成孔径雷达 | Synthetic Aperture Radar | SAR | 主动微波遥感 |
| 干涉测量 | Interferometry | — | 多次成像求相位差 |
| 单视复数 | Single Look Complex | SLC | 保留相位的 SAR 影像 |
| 地距探测 | Ground Range Detected | GRD | 只有振幅,无相位 |
| 视线方向 | Line of Sight | LOS | 卫星与地面像素连线 |
| 干涉对 | Interferometric Pair | — | 两幅 SLC 用于干涉 |
| 主影像 | Reference / Master | — | 配准基准 |
| 从影像 | Secondary / Slave | — | 配准到 master |
| 时间基线 | Temporal Baseline | — | 两次拍摄间隔(天) |
| 垂直基线 | Perpendicular Baseline | B⊥ | 轨道几何分离(米) |
| 相干性 | Coherence | γ | 0-1,数据质量 |
| 相位卷绕 | Phase Wrapping | — | 相位天然在 [-π,π] |
| 相位解缠 | Phase Unwrapping | — | 恢复真实累计相位 |
| 地形校正 | Terrain Correction | TC | 投影到地理坐标 |
| 距离-多普勒 | Range-Doppler | RDTC | TC 算法之一 |
| 永久散射体 | Persistent Scatterer | PS | 时序 InSAR 方法 |
| 短基线子集 | Small Baseline Subset | SBAS | 时序 InSAR 方法 |
| 重轨 | Repeat-Pass | — | 同卫星反复经过同位置 |

---

## 10. 推荐进一步阅读

- **入门书**:Ferretti 等 《InSAR Principles》(ESA 免费 PDF),最系统的 200 页
- **HyP3 文档**:https://hyp3-docs.asf.alaska.edu/
- **ASF 数据搜索教程**:https://asf.alaska.edu/asf-tutorials/
- **Sentinel-1 用户手册**:https://sentinels.copernicus.eu/web/sentinel/user-guides/sentinel-1-sar
- **LiCSAR 论文**:Lazecký et al. (2020) "LiCSAR: An Automatic InSAR Tool for Measuring and Monitoring Tectonic and Volcanic Activity", Remote Sensing 12(15)
- **中文教程**:中国矿业大学 / 武汉大学 InSAR 课程讲义,搜"InSAR 中文 PPT"
- **代码学习**:阅读 MintPy 项目(https://github.com/insarlab/MintPy),最权威的 Python InSAR 库

---

## 11. 整个系统逻辑的一句话总结

> **遥感卫星拍下的不是图,是数据;数据里有光学信号(看物质)和雷达信号(看几何)两类;光学走 geo-downloader 出 4 个常规衍生品;雷达 + 时间维度 = InSAR,走 geo-insar 出 4 个形变衍生品;两类衍生品最后都喂给 geo-exploration / geo-reporter / geo-analyser 三个下游做矿产探测、报告、深度分析。**

读完本文,你应该能解释:
- **为什么 geo-insar 要单独建一个子系统**(SAR 处理链跟光学完全不同)
- **为什么 Phase 1 默认走 HyP3**(免费、云端、配额够用)
- **为什么前端会提示 BURST vs GAMMA 后端**(AOI 大小决定数据产品级别)
- **为什么相干性这么重要**(coherence < 0.3 等于信号没了)
- **为什么矿区监测需要至少 3-6 个月的多对堆栈**(单对灵敏度只到 cm)
- **为什么测试 AOI 选了招远金矿**(胶东金矿带,深井开采,正好考验 mm/年 灵敏度极限)

**版本:** 文档对应 geo-insar v0.1.0-phase1 · 更新于 2026-05-27
