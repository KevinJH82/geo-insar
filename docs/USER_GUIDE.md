# geo-insar 产品使用说明与操作指南

> **服务地址:** http://localhost:8084(本机)/ http://192.168.112.18:8084(局域网)
> **当前状态:** Phase 1(ASF HyP3 云端)生产可用 · Phase 2/3 CLI 可用 · Phase 1.5 下游已对接

---

## 1. 这是什么?

**geo-insar** 是一个独立子系统,与 `geo-downloader`(光学/SAR 卫星原料下载)和 `geo-exploration`(矿产深部探测)平行,**专门做 InSAR(合成孔径雷达干涉测量)数据的获取与处理**。

### 1.1 解决什么问题

InSAR 不是"某颗卫星的产品",而是一种**处理技术**——把同一区域两次拍摄的雷达影像求差,得到地表毫米级形变。常见用途:

| 应用场景 | 典型形变量级 | geo-insar 价值 |
|---|---|---|
| 矿区地面沉降 | cm / 月 | **业务主线**(本项目核心) |
| 城市地下水/地铁施工沉降 | mm / 年 | 长时序监测 |
| 滑坡/边坡监测 | mm / 月 ~ cm / 月 | 配合 L 波段(ALOS-2)穿透植被 |
| 地震 / 火山同震形变 | dm ~ m | 突发事件应急 |
| 活动断层识别 | 速率差分场 | 矿产深部探测的"构造活动"证据层 |

### 1.2 三条技术路线

geo-insar 提供 **三种数据来源**,**输出格式完全统一**(commons/insar_schema.json 契约),可以混着用:

| 模式 | 何处计算 | 等待时间 | 磁盘需求 | 配额限制 | 适合场景 |
|---|---|---|---|---|---|
| **Cloud(HyP3)** | ASF 美国云端 | 30 min – 2 h | <10 GB/对 | 1000 job/月 | **默认推荐**,任意 AOI |
| **Local(SNAP)** | 部署主机本地 | 15-60 min/对 | ~50 GB/对 | 无 | 大批量、定制参数 |
| **LiCSAR** | 别人(NERC)算好的 | 0(直接下) | <500 MB/对 | 无 | 已发布区快速对照 |

---

## 2. 5 分钟快速上手(Cloud 模式)

### 步骤 1:打开浏览器
访问 http://localhost:8084(本机)或 http://192.168.112.18:8084(局域网)。

### 步骤 2:左侧"前置检查"页 → 跑一次自检
点右上**运行检查**,应看到 13 项全绿。任何 ❌ 都会给出修复建议(常见是 Python 依赖或 HyP3 授权)。

### 步骤 3:回到"任务控制台",上传 AOI
点 **AOI 区域文件** 选 KML(支持 `.kml` / `.ovkml` / `.kmz` 等扩展名)。系统立刻显示:
- AOI 名称
- 范围 bbox
- **面积 km²**
- **推荐后端**(< 600 km² 推荐 BURST,> 2500 km² 推荐 GAMMA)

### 步骤 4:设时间窗 + 参数
- **起始/结束日期**:Sentinel-1 自 2014 年起有数据,矿区监测常用最近半年
- **配对策略**:默认从 `config.yaml::pairing.strategy` 取(当前 = `closest_in_time`,相邻日期)
- **最大时间基线**:默认 **36 天**(矿区监测预调,捕获月级沉降信号)
- **最大垂直基线**:默认 **150 m**(收紧值,减少几何去相关)
- **最大对数**:默认 **120**(覆盖 3-6 个月监测,注意 HyP3 配额 1000/月)
- **极化**:默认 VV
- **后端**:已经按 AOI 自动选好,不必改

> **这些默认值都在 `config.yaml::pairing` 段里管,改一次全局生效**。CLI 和 Web 都遵循同一份默认,不需要在两处分别配。

### 步骤 5:**先点"查询能配出多少对"**(不花配额)
返回结果显示能配多少 SLC 对、每对的时间基线和轨道。如果数量异常(0 对或过多)就先调参数。

### 步骤 6:**Dry-run 试一遍**(不真正提交 HyP3)
任务会入库但只标记 dry-run,不消耗 HyP3 配额。可以看任务列表里 jobs 数量是否符合预期。

### 步骤 7:提交 HyP3
确认 dry-run 没问题后点 **提交 HyP3**。任务进入 cloud_processing 状态,**等 30 min – 2 h** 每个 job 跑完。

### 步骤 8:看输出
HyP3 完成后,产物自动下载到:
```
geo-insar/downloads/<AOI 名>/sentinel1_insar/<参考日期>_<次要日期>_VV/
├── unwrapped_phase.tif    # 解缠相位(弧度)
├── coherence.tif          # 相干性(0-1)
├── los_displacement.tif   # ★ LOS 形变(mm,主产物)
├── wrapped_phase.tif      # 干涉图
└── metadata.json          # 元数据
```

用 QGIS 打开 `los_displacement.tif`,**红蓝双色 colormap**:红=远离卫星(沉降),蓝=靠近卫星(抬升)。

---

## 3. 四个页面详解

### 3.1 任务控制台(/index.html)

**主操作页**,从上传 KML 到提交任务都在这。

| 区块 | 含义 |
|---|---|
| AOI 区域文件 | 上传 KML / OVKML / KMZ。系统自动算面积、给后端建议 |
| 时间窗 | Sentinel-1 SLC 的搜索时间范围 |
| 配对策略 | `closest_in_time`(相邻):**推荐**,生成最少对数<br>`fixed_master`(固定主):一个参考影像 vs 所有从影像<br>`cascade`(级联):所有可能组合,对数最多 |
| 最大时间基线 | 时间相隔太久的对相干性会差,默认 24 天 |
| 最大垂直基线 | 卫星轨道差异太大,几何不利于干涉,默认 200 m |
| 极化 | VV(默认)/ HH。Sentinel-1 IW 模式标配 VV+VH |
| HyP3 后端 | `INSAR_ISCE_BURST`:**单 burst,~25×25 km 内**,快,推荐小 AOI(系统会搜 `PRODUCT_TYPE.BURST` granule)<br>`INSAR_GAMMA`:**整景,~250 km**,慢,推荐大 AOI(系统会搜 `PRODUCT_TYPE.SLC`)<br>选错后端会找不到数据,所以前端有 AOI 大小自动建议 |
| 附带 DEM/水体掩膜/入射角图 | 可选附加产物,大部分场景不需要 |
| 配对预览 | **强烈推荐先点这个**,看能配多少对、基线分布,不花配额 |
| 任务列表 | 显示所有提交过的任务(从 SQLite 读),含状态进度 |
| 其他数据源 | Phase 2(SNAP)/ Phase 3(LiCSAR)的 CLI 用法提示 |

### 3.2 产品契约(/architecture.html)

**查看 InSAR 标准输出的目录结构和 metadata.json schema**。下游子系统(geo-exploration / geo-reporter / geo-analyser)都按这个契约消费 geo-insar 的输出,所以契约文档非常关键。

- 显示标准目录树
- 显示 metadata.json 的 18 个字段定义(从 `commons/insar_schema.json` 加载)
- 显示跨子系统数据流图

**主要用途:** 集成方/运维核对契约,不是日常操作页。

### 3.3 交付整理(/delivery.html)

**列出 downloads/ 下所有已完成的 InSAR 堆栈(按 AOI 聚合)**。

每个堆栈显示:
- AOI 名称
- 干涉对数量
- 日期跨度
- 极化 / 轨道方向
- 整体相干性均值(质量指标,> 0.5 为好)
- 本地路径

**主要用途:** 检查产出、给下游子系统提供 AOI 列表。

### 3.4 前置检查(/preflight.html)

**运维诊断面板**,跑 `scripts/preflight_check.sh` 看 13 项环境检查:
- HyP3 API 端点能否连通
- 端口 8084 是否被占
- Earthdata 凭证是否有效
- 8 个 Python 依赖
- OpenVPN 出口
- 测试 KML 是否就位

任何 ❌ 都给出修复建议。**不阻塞服务启动**——只是给运维提供诊断信息。

---

## 4. 关键概念解释

### 4.1 InSAR 基本术语

| 术语 | 含义 |
|---|---|
| **SLC** | Single Look Complex,单视复数。InSAR 的**原料**,保留了相位信息(GRD 不行) |
| **干涉对** | 两幅同区域 SLC 影像组成的对(主+从) |
| **干涉图** | 两影像相位求差,形变信息编码在条纹里 |
| **解缠** | 相位是 [-π, π] 的"卷绕"值,解缠把它变成实际累计相位 |
| **相干性** | 0-1 值,每个像素的可靠度;<0.3 一般不可信(水体、植被、剧烈形变) |
| **LOS 形变** | Line-of-Sight,卫星视线方向。负=远离卫星(沉降),正=靠近卫星(抬升) |
| **时间基线** | 两次拍摄相隔的天数。短=相干性好,长=能看慢变信号但去相干风险高 |
| **垂直基线** | 两次卫星轨道在垂直方向的距离(米)。太大几何不利,< 200 m 较好 |

### 4.2 三种 HyP3 后端

- **INSAR_GAMMA**:GAMMA 软件,处理整个 Sentinel-1 IW 影像(~250×250 km),即使 AOI 小也是整景处理。出图全且元数据齐
- **INSAR_ISCE_BURST**:ISCE 软件,**单 burst 模式**(~25×25 km),配额节省、速度快。小 AOI 强烈推荐

geo-insar 会根据 AOI 面积自动建议。除非有特殊需求,跟随建议即可。

### 4.3 4 个标准产物的物理含义

| 文件 | 单位 | 物理意义 |
|---|---|---|
| `unwrapped_phase.tif` | 弧度 | 解缠相位,2π 对应 ~28 mm LOS 形变(Sentinel-1 C 波段) |
| `coherence.tif` | 0-1 | 像素级数据质量。0.5+ 算好,<0.3 当噪声 |
| `los_displacement.tif` | **mm** | **LOS 方向形变量**,**最常用的最终产物** |
| `wrapped_phase.tif` | [-π, π] | 干涉条纹图,可视化用 |

---

## 5. 参数详解(操作时遇到的每个字段都讲透)

任务控制台每个参数从 **是什么 / 为什么有 / 怎么选** 三个角度讲清楚。

### 5.1 配对策略 `pair_strategy`

InSAR 至少需要两幅 SLC 影像配成"对"。同一时间窗内通常有 N 幅可用影像,**怎么配对**就是策略问题。

#### closest_in_time(相邻策略)— ★ 默认推荐

按时间排序后**只让相邻的两幅配对**:

```
时间轴 →   影像1 ─→ 影像2 ─→ 影像3 ─→ 影像4 ─→ 影像5
对清单:         [1-2]    [2-3]    [3-4]    [4-5]      共 N-1 对
```

- ✅ 时间基线最短,相干性最好
- ✅ 对数少,节约 HyP3 配额
- ✅ 标准时序分析输入(SBAS / MintPy 都欢迎)
- ❌ 速率反演样本量比 cascade 少
- **推荐场景**:几乎所有矿区/城市监测的默认

#### fixed_master(固定主影像)

选一幅作"主",其余都配它:

```
时间轴 →   影像1(主) ─→ 影像2 ─→ 影像3 ─→ 影像4 ─→ 影像5
对清单:        [1-2]       [1-3]    [1-4]    [1-5]    共 N-1 对
```

- ✅ 所有对共用同一主影像,几何参考一致
- ❌ 时间跨度越大,后期对相干性越差(往后越不可用)
- ❌ 对垂直基线敏感
- **推荐场景**:**PS-InSAR 永久散射体**分析,必须固定 master

#### cascade(级联策略)

所有可能的两两组合都配:

```
时间轴 →   影像1 ─→ 影像2 ─→ 影像3 ─→ 影像4 ─→ 影像5
对清单:  [1-2] [1-3] [1-4] [1-5]
         [2-3] [2-4] [2-5]
         [3-4] [3-5]
         [4-5]                                共 C(N,2) = 10 对
```

- ✅ 样本最多,统计反演鲁棒
- ✅ SBAS 时序分析的标准输入
- ❌ 对数爆炸:5 幅 → 10 对,10 幅 → 45 对,**HyP3 配额烧得快**
- ❌ 加上 `max_temporal_baseline` 限制后长基线对会剔除
- **推荐场景**:已确认有形变,要用 SBAS 精细反演速率场

#### 决策表

| 场景 | 推荐 |
|---|---|
| 矿区/城市常规监测 | **closest_in_time** |
| PS-InSAR 永久散射体 | fixed_master |
| SBAS 时序速率反演 | cascade(配合 max-pairs 限流) |
| 不确定 | **closest_in_time** |

---

### 5.2 最大时间基线 `max_temporal_baseline_days`

**是什么**:同一对的两幅影像,**拍摄间隔不能超过多少天**。

**为什么有**:时间间隔越长,地表散射特性变化越大(植被、土壤湿度、积雪),**相干性 coherence 衰减**——超过某阈值就完全是噪声了。

**C 波段 Sentinel-1 的经验衰减**(每种地表类型 coherence γ ≈):

| 时间基线 | 12 天 | 24 天 | 36 天 | 72 天 | 180 天 |
|---|---|---|---|---|---|
| 城市 / 裸地 | 0.8 | 0.7 | 0.6 | 0.4 | 0.2 |
| 矿区裸岩 | 0.7 | 0.6 | 0.5 | 0.3 | 0.1 |
| 农田 | 0.5 | 0.3 | 0.2 | 噪声 | — |
| 森林 | 0.3 | 噪声 | — | — | — |
| 雪覆盖 | 0.2 | 每场雪相干性重置 | | | |

**本项目默认 36 天**(矿区裸岩场景预调,见 `config.yaml::pairing.max_temporal_baseline_days`)。

**实战经验值**:
| AOI 类型 | 推荐 |
|---|---|
| 城市 / 矿区裸岩 | **24-48 天** |
| 急速形变(地震/火山)| **12 天**(避免相位卷绕 ambiguity) |
| 植被覆盖密集 | C 波段 24 天已吃力,改 ALOS-2 L 波段(Phase 2) |
| 雪季监测 | **不推荐 InSAR**,等雪化 |

---

### 5.3 最大垂直基线 `max_perp_baseline_m`

**是什么**:同一对的两次卫星轨道,**在垂直于飞行方向上的距离差**不能超过多少米。

**为什么有**:从不同角度看同一地物会产生**几何相位错乱(空间去相干)**,即使时间间隔短也会失效。

```
卫星轨道几何(截面图):
   轨道 1 ◇          B⊥ = 垂直基线 (米)
          \  ◇ 轨道 2
           \ │
            \│ 视线
             \
        ───── ● 地面像素
```

**经验阈值**(Sentinel-1 IW):
- B⊥ < 200 m → 去相干基本可忽略
- B⊥ > 500 m → 相干性显著下降
- **本项目默认 150 m**(收紧,优先质量)

**通常不需要改**:Sentinel-1 轨道控制非常稳定,**多数对 B⊥ 都在 50-150 m**。如果配出 0 对,可以放宽到 200。

---

### 5.4 最大对数 `max_pairs`

**是什么**:单任务最多提交几个干涉对。

**为什么有**:HyP3 配额 **1000 job/月**,一对一 job。不限流容易一次任务吃光月配额。

**本项目默认 120**(`config.yaml::pairing.max_pairs`):
- 1 个 AOI 一年 ≈ 12 个 acquisition × 1 frame ≈ 12 对(closest)~ 30 对(cascade 24 天阈值)
- 120 对够覆盖 **3-6 个月多 frame 监测** 或 **1 年单 frame 时序**

**调参建议**:提交前先看一眼 https://hyp3-api.asf.alaska.edu/ui/ 的剩余配额。

---

### 5.5 极化 `polarization`

SAR 卫星发射和接收的微波都是**有方向的偏振**。**第一个字母 = 发射,第二个字母 = 接收**:

| 极化码 | 全称 | 物理含义 | 用途 |
|---|---|---|---|
| **VV** | 发垂直,收垂直 | 同极化,**地表回波最强** | ★ Sentinel-1 InSAR 默认 |
| HH | 发水平,收水平 | 同极化,对人工目标(楼/桥/船)敏感 | ALOS-2 默认 / 极地冰川 |
| VH | 发垂直,收水平 | 交叉极化,体散射敏感(植被),信号弱 | 植被分类辅助 |
| HV | 发水平,收垂直 | 交叉极化,与 VH 对偶 | 同 VH |

**Sentinel-1 IW 标配双极化 VV+VH**,**InSAR 只用 VV**(信号最强、相干性最好)。VH 主要做地物分类(植被),InSAR 不用。

**一句话决策**:
- Sentinel-1 → **VV 永远是首选**,不要改
- ALOS-2 → HH 是默认(L 波段穿透植被时 H 更强)

---

### 5.6 后端 `backend`

详见 §3.1 任务控制台表 和 `REMOTE_SENSING_PRIMER.md` §5.2/§5.3。

| 后端 | 处理单元 | 适合 AOI | 数据源 |
|---|---|---|---|
| **INSAR_ISCE_BURST** | 单 burst(~25×25 km) | < 600 km² | ASF BURST granule |
| **INSAR_GAMMA** | 整景 SLC(~250×250 km) | > 2500 km² | ASF SLC scene |

**前端会按 AOI 面积自动建议**,跟着选就行。选错后端会找不到数据(BURST 不能用 SLC 的 ID,反之亦然)。

---

### 5.7 附加产物开关

任务控制台底部三个 checkbox,默认都不勾。

#### 附带 DEM(`--include-dem`)

- **是什么**:HyP3 处理时用的数字高程模型(GLO-30 或 SRTM,~30 m)的本地副本
- **什么时候勾**:想自己核对地形校正用的高程 / 做坡度坡向分析(矿区滑坡评估)
- **代价**:ZIP 多 ~30 MB

#### 水体掩膜(`--include-water-mask`)

- **是什么**:海洋 / 河流 / 湖泊的二值掩膜(0/1)
- **什么时候勾**:AOI 含大片水体。水面相干性极低、容易在解缠时引入假相位,**有掩膜可以在后处理直接 mask 掉**
- **庙山金矿这种内陆裸地 AOI 不需要**

#### 入射角图(`--include-inc-map`)

- **是什么**:每像素卫星视线与地面法线的夹角(度),Sentinel-1 IW 通常 30°-45°
- **什么时候勾**:
  - **LOS 形变 → 垂直分量**反算:`v_vert = v_los / cos(θ)`
  - 升降轨联合反演 E/N/U 三分量形变
- **代价**:多 ~30 MB

#### 决策

| 用途 | 推荐 |
|---|---|
| 第一次跑 / 矿区基础监测 | 都不勾(ZIP 最小) |
| 精确形变量级分析 | ★ 勾入射角图 |
| 含水体 AOI | ★ 勾水体掩膜 |
| 地形分析 | ★ 勾 DEM |

---

### 5.8 一张参数关系图

```
                ┌─ pair_strategy ─→ 决定对的拓扑结构(链式/星形/全连接)
                │
配对参数(影响哪些
对会被生成)   ─┼─ max_temporal_baseline_days ─→ 时间过滤(相干性)
                │
                └─ max_perp_baseline_m ─→ 几何过滤(几何去相干)
                
                ┌─ max_pairs ─→ HyP3 配额限流
节流参数  ─────┤
                └─ backend ─→ 决定搜哪种产品(BURST / SLC)
                
                ┌─ polarization ─→ 决定用哪个极化(VV 推荐)
处理参数  ─────┤
                └─ include-{dem,water-mask,inc-map} ─→ 附加产物

```

---

## 6. 命令行用法(高级)

Web UI 操作的所有事 CLI 都能做。CLI 还**多了 Phase 2(本地 SNAP)和 Phase 3(LiCSAR)模式**,这两个目前不在 Web UI 里。

### 5.1 Cloud 模式(等价于 Web UI)

最简(全部默认从 `config.yaml::pairing` 读,矿区监测预调):
```bash
cd /opt/deepexplor-services/geo-insar
python3 main.py --kml test_data/zhaoyuan_miaoshan.kml \
                --start 2024-06-01 --end 2024-08-01 \
                --backend INSAR_ISCE_BURST
```

临时覆盖配对参数(滑坡场景,要更长基线):
```bash
python3 main.py --kml landslide.kml --start 2024-01-01 --end 2024-12-31 \
                --max-temporal-baseline 60 --max-perp-baseline 250 --max-pairs 200
```

加 `--dry-run` 试运行不提交 HyP3。

### 5.2 Local 模式(本地 SNAP)
```bash
# 前提:先装 ESA SNAP + snaphu + pyroSAR + Java,详见 PHASE_2_HANDOFF.md
python3 main.py --kml test_data/zhaoyuan_miaoshan.kml \
                --start 2024-06-01 --end 2024-08-01 \
                --mode local --sensor sentinel1 \
                --gpt /opt/snap/bin/gpt
```

### 5.3 LiCSAR 模式(第三方成品)
```bash
# 前提:先到 https://comet.nerc.ac.uk/comet-lics-portal/ 查覆盖本研究区的 frame ID
python3 main.py --kml test_data/zhaoyuan_miaoshan.kml \
                --start 2024-06-01 --end 2024-08-01 \
                --mode licsar --licsar-frame 022D_05411_131313 \
                --max-pairs 10
```

完整参数列表:`python3 main.py --help`(22 个参数)

---

## 7. 输出去哪儿了?下游怎么消费?

geo-insar 的输出 `downloads/<AOI>/sentinel1_insar/<pair>/` 被**三个下游子系统**自动订阅:

### 6.1 geo-reporter(报告生成,8081)
- 新增"InSAR 形变监测资料"章节(第 8 章)
- 自动从 geo-insar 输出读统计,注入到报告生成 prompt
- 与 Tavily WebSearch 找到的外部文献融合

### 6.2 geo-exploration(矿产深部探测,8083)
- **SlowVars 检测器**新增第 8 类构造因素 `surface_deformation`
- LOS 速率绝对值作为活跃形变信号
- 用速率差分场增强现有"断裂识别"
- 提交任务时如果上传包内含 `los_velocity.tif` 和 `coherence.tif`,自动启用

### 6.3 geo-analyser(分析平台,5001)
- 新增"InSAR 形变分析"导航页(NewPage/deformation.html)
- 自动扫描 geo-insar 输出目录,列出可分析的 AOI
- 提供 3 种分析模式:
  - **活跃形变区聚类** — 识别超过阈值的连通块
  - **相干性稳定性** — 把相干性转成稳定性得分
  - **形变-矿物联合** — 形变 × 蚀变矿物异常融合,识别"既动又有矿"的区域
- 还有时序速率反演 + 相干性衰减建模

数据流总览:
```
geo-insar/downloads/<aoi>/sentinel1_insar/<pair>/
    │
    ├──→ geo-exploration : 定时扫描 → SlowVars 第 8 类因素 → 矿产预测图叠加
    │
    ├──→ geo-reporter    : insar_reader 读统计 → 报告 InSAR 章节
    │
    └──→ geo-analyser    : insar_broker 扫描 → API 分析 → 前端展示
```

---

## 8. 常见问题速查

| 现象 | 修复 |
|---|---|
| 上传 KML 后不显示信息 | 检查文件 < 10 MB,扩展名是 .kml/.ovkml/.kmz 之一 |
| "查询能配出多少对" 返回 0 | 时间窗太短或 Sentinel-1 在该区域无覆盖;放宽到 6 个月再试 |
| 提交后任务列表立刻 error | 多半是 HyP3 授权未到位:登 https://hyp3-api.asf.alaska.edu/ui/ 用 Earthdata 账号点一下应用授权 |
| 任务卡在 cloud_processing 不更新 | 后台轮询线程 5 min 检查一次;或到 HyP3 portal 手动看 job 状态 |
| `los_displacement.tif` 全是噪声 | 检查相干性图,可能是植被区或时间基线太长 |
| 矿区位置没看到沉降信号 | 时间窗内可能开采暂停;或形变小于检测灵敏度(C 波段 ~ 5 mm) |
| 浏览器访问 8084 连接被拒 | 服务挂了,SSH 到主机重启:`cd /opt/deepexplor-services/geo-insar && python3 web/app.py` |
| 想停服务 | `lsof -nP -iTCP:8084 -sTCP:LISTEN -t \| xargs kill`(preflight 页能识别"是 geo-insar 自己在跑",显示 PID 提示) |
| 配对默认值不合适 | 改 `config.yaml::pairing` 段(时间基线/垂直基线/最大对数/策略),CLI 和 Web 都跟随 |

---

## 9. 经典工作流案例:招远庙山金矿监测

测试 AOI `test_data/zhaoyuan_miaoshan.kml`(山东招远庙山金矿,~6 km²)的典型操作:

1. **任务控制台** → 上传 `zhaoyuan_miaoshan.kml` → 系统提示 5.96 km² + 推荐 BURST 后端
2. 时间窗设 2024-06-01 ~ 2024-08-01(夏季,植被覆盖会影响相干性,可能要往冬季调)
3. 配对策略 `closest_in_time`,基线 24 天,极化 VV,后端跟建议
4. 点 **查询能配出多少对** — 应该能配 4-6 对
5. 点 **Dry-run** 看入库正确
6. 点 **提交 HyP3** — 等 1-2 h
7. **交付整理页**看到 zhaoyuan_miaoshan AOI 出现,相干性均值大致 0.3-0.6
8. QGIS 打开 `downloads/zhaoyuan_miaoshan/sentinel1_insar/<pair>/los_displacement.tif`
9. 在 120.44°E, 37.19°N 附近找形变信号(深井金矿沉降通常 mm/年,可能不太明显;邻近的露天矿可能更显著)
10. **联调下游:**到 geo-analyser(端口 5001)的"InSAR 形变分析"页,选择 zhaoyuan_miaoshan,跑"活跃形变区聚类"——看是不是把金矿位置标记为活跃区

---

## 10. 参考文档

| 文档 | 受众 | 内容 |
|---|---|---|
| **`USER_GUIDE.md`**(本文档) | 业务用户 / 运维 | 操作指南、四个页面解释、CLI 用法 |
| **`REMOTE_SENSING_PRIMER.md`** ★ | 想理解原理的人 | 遥感基础、SAR/InSAR 原理、HyP3 详解、术语表 |
| **`REPORT_INTERPRETATION.md`** ★ | 看报告 / 出报告的人 | 速率图/相干性图/P1-P3 时序点位选定逻辑、可信度评估、踩坑指南 |
| **`HANDOFF.md`** | 开发 / 运维 | Phase 1(HyP3 云端)的交接细节 |
| **`PHASE_1.5_HANDOFF.md`** | 跨子系统集成方 | 三个下游(geo-reporter / geo-exploration / geo-analyser)接入说明 |
| **`PHASE_2_HANDOFF.md`** | 想用本地处理的人 | 本地 SNAP 干涉(需先装 SNAP/snaphu/Java) |
| **`PHASE_3_HANDOFF.md`** | 想用 LiCSAR 的人 | COMET LiCSAR 第三方成品下载 |
| **`2nd_portal_card_snippet.html`** | 前端 / 运维 | 二级门户卡片片段(贴到 192.168.112.18 的 index.html) |

> **如果你刚接手不懂 InSAR**,推荐先读 **`REMOTE_SENSING_PRIMER.md`**(40 分钟),再回到本文档操作。

## 11. 项目位置

```
/opt/deepexplor-services/
├── geo-insar/         ← 本子系统
├── geo-downloader/    ← 原料下载(Sentinel-1/Landsat/ASTER 等)
├── geo-exploration/   ← 矿产深部探测(端口 8083)
├── geo-reporter/      ← 报告生成(端口 8081)
├── geo-analyser/      ← 分析平台(端口 5001)
└── commons/           ← 跨子系统公共库
```

**版本:** geo-insar v0.1.0-phase1 · 文档更新于 2026-05-27
