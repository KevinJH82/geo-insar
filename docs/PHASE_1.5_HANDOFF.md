# Phase 1.5 下游对接交付清单

> Phase 1.5 把 geo-insar 标准输出接入了三个下游子系统:geo-reporter / geo-exploration / geo-analyser。
> 所有改动**零回归**:无 InSAR 数据时,三个子系统的现有行为完全不变。

## 1. 改动总览

| 子系统 | 改动文件数 | 改动量级 | 新增能力 |
|---|---|---|---|
| geo-reporter | 2 | 小(纯配置 + 1 个新函数) | 报告新增"InSAR 形变监测"章节,自动注入本地堆栈统计 |
| geo-exploration | 5 | 中 | SlowVars 第 8 类形变因素 + InSAR 数据自动加载 + 双向 cmap + UI 提示 |
| geo-analyser | 5(3 新建 + 2 改动) | 中-大 | InSAR 异常检测、时序分析、文件订阅、3 个新 API、新前端页 |

## 2. geo-reporter 改动

### 文件
- `reporter/categories.py` — 在第 8 个位置插入 `insar_deformation` SearchCategory(章节自动编号为"第 8 章")
- `reporter/data_sources.py` — 新增 `fetch_insar_local()` 函数,扫描 `/opt/deepexplor-services/geo-insar/downloads/` 下与研究区相交的 InSAR 堆栈,生成结构化文本注入到 raw_data;`SUPPORTED` 集合加入 `insar_deformation`;`fetch_direct` 加入 `insar_deformation` 分支

### 数据流
```
geo-insar 标准输出 → fetch_insar_local() → raw_data (Tavily 旁路) → Claude 提取 → 报告 InSAR 章节
```

### 验证
```bash
cd /opt/deepexplor-services/geo-reporter
python3 -c "from reporter.categories import get_all_categories; [print(c.id) for c in get_all_categories()]"
# 应该看到 9 个类别,insar_deformation 在 geophysics 之后、remote_sensing 之前
```

## 3. geo-exploration 改动

### 文件
- `Python_Project/python_version/detectors/slow_vars_detector.py` — 加 `'surface_deformation'` 因素调度 + `_calculate_surface_deformation()` + `_calculate_fracture()` 增强 InSAR 速率叠加
- `Python_Project/web_app/core/detectors/slow_vars_detector.py` — Matlab 版本算法,在 `fault_activity` 后叠加 InSAR 速率差分场,在 `b` 公式加入 `surface_deformation` 权重项(0.12)
- `Python_Project/web_app/core/mineral_engine.py` — 新增 `_load_insar()` 方法,自动识别 `los_velocity.tif`、`coherence.tif`、`sentinel1_insar/<pair>/los_displacement.tif`,注入 `data_context['insar_velocity']` 和 `data_context['insar_coherence']`
- `Python_Project/web_app/utils/visualizer.py` — 新增 `plot_insar_deformation()` 静态方法,RdBu_r 双向 colormap,对称 vmin/vmax 截断,输出 `04_InSAR形变速率.png`
- `Python_Project/newpage.html` — 在数据上传卡片下加紫色提示条,说明 InSAR 自动识别条件

### 关键设计:零回归
所有 InSAR 增强都在 `if insar_v is not None` 守卫下,缺失时回退到原版逻辑:
- `_calculate_fracture()` 只在有 InSAR 时叠加,否则纯 DEM
- `b` 公式有两条分支:无 InSAR 用原始 5 因素权重,有 InSAR 用 6 因素重新分配权重
- `_load_insar()` 找不到文件时返回 `(None, None, None)`,数据上下文照常构建

### 验证(需要先装依赖)
```bash
# 在有 InSAR 数据的目录(包含 los_velocity.tif 和 coherence.tif)上跑 SlowVars
# DetectorResult.debug_data 应包含 'surface_deformation' 和 'insar_enabled': True
```

## 4. geo-analyser 改动

### 新建文件
- `insar_analysis.py` — InSAR 异常检测核心(3 个函数 + 1 个 dataclass):
  - `coherence_to_stability()` — 相干性 → 稳定性得分
  - `los_velocity_clustering()` — 形变速率聚类(连通块标记)识别活跃区,无需 sklearn
  - `fusion_deformation_mineral()` — 形变 × 矿物异常联合,百分位归一化后加权融合
- `insar_timeseries.py` — 时序分析(3 个函数):
  - `load_insar_stack()` — 扫描 AOI 目录加载所有干涉对(可处理形状不一致的对齐)
  - `temporal_velocity_trend()` — 简化 SBAS:每对 disp/dt 后取像素级中位数,得到 mm/year 速率
  - `coherence_decay_model()` — 拟合 `coh ~ exp(-baseline/tau)`,得到去相干特征时间 τ(天)
- `insar_broker.py` — geo-insar 输出目录订阅:
  - `scan_available_aois()` — 列出所有可分析堆栈(优先读 stack_index.json)
  - `get_stack_path()` — 名字 → 路径

### 改动文件
- `app.py` — 在文件末尾(`if __name__` 之前)追加:
  - `/api/insar/stacks` — GET,列堆栈
  - `/api/insar/analyze` — POST,跑 coherence_stability / velocity_cluster / fusion_mineral
  - `/api/insar/timeseries` — POST,跑 temporal_velocity_trend + coherence_decay_model
  - `_render_insar_array()` — 共享的 PNG 渲染辅助(RdBu_r 双向 cmap 自动检测)
- `NewPage/遥感分析平台浅色风格_v5/index.html` — 顶部导航加 "InSAR 形变分析" 链接
- `NewPage/遥感分析平台浅色风格_v5/deformation.html`(新建)— 完整的 InSAR 分析前端页:AOI 列表 + 参数表单 + 结果展示

### 数据流
```
geo-insar/downloads/<aoi>/sentinel1_insar/<pair>/
        ↓ insar_broker.scan_available_aois()
deformation.html UI 列堆栈
        ↓ 用户点 "运行分析"
/api/insar/analyze 或 /api/insar/timeseries
        ↓ insar_timeseries.load_insar_stack()
        ↓ insar_analysis.coherence_to_stability() / los_velocity_clustering() / ...
PNG base64 + stats JSON → 前端展示
```

### 验证
```bash
cd /opt/deepexplor-services/geo-analyser
# 检查 app.py 路由(干跑,无需启动服务)
python3 -c "
import ast
with open('app.py') as f:
    tree = ast.parse(f.read())
routes = [a.value for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
          for d in n.decorator_list if hasattr(d, 'func') and getattr(d.func, 'attr', '') == 'route'
          for a in d.args if hasattr(a, 'value')]
print([r for r in routes if 'insar' in r])
"
# 应该看到 ['/api/insar/stacks', '/api/insar/analyze', '/api/insar/timeseries']
```

## 5. 联调路径(端到端验证)

按以下顺序验证 Phase 1 + Phase 1.5 完整流程:

```
1. (Phase 1) geo-insar 跑 HyP3 → 输出落到 geo-insar/downloads/<aoi>/sentinel1_insar/<pair>/

2. (Phase 1.5 geo-reporter) 上传同个 AOI 的 KML 到 geo-reporter (端口 8081)
   → 报告生成时,第 8 章 InSAR 应自动包含本地堆栈的统计

3. (Phase 1.5 geo-exploration) 把 InSAR 文件复制到 geo-exploration 任务输入目录
   → SlowVars 输出应该包含 surface_deformation factor,debug_data['insar_enabled'] = True

4. (Phase 1.5 geo-analyser) 打开 http://localhost:5001/NewPage/.../deformation.html
   → 应该能看到 geo-insar 输出的 AOI 列表
   → 选 AOI + 点"运行分析",看到形变速率图和聚类结果
```

## 6. 已知限制 / 后续优化

- **insar_timeseries.temporal_velocity_trend** 是简化 SBAS(像素级中位数),不是完整最小二乘反演 — 适合 MVP,严肃应用应升级到 MintPy
- **fusion_deformation_mineral** 假设矿物异常图与速率图同 CRS+对齐,如果不同需要用户先做几何对齐
- **geo-reporter fetch_insar_local** 用 bbox 相交判断 AOI,如果用户的研究区在 KML 上是非矩形可能漏匹配 — Phase 2 升级到 shapely 几何相交
- **deformation.html 文件位置**:在 `NewPage/遥感分析平台浅色风格_v5/` 目录下,**实际访问路径要看 Flask 的静态文件配置**,可能需要在 app.py 加路由把它服务出去

## 7. Phase 1.5 改动文件全清单(10 个)

```
geo-reporter/
  reporter/categories.py            (改:加 SearchCategory)
  reporter/data_sources.py          (改:加 fetch_insar_local)

geo-exploration/Python_Project/
  python_version/detectors/slow_vars_detector.py   (改:第 8 类因素 + fracture 增强)
  web_app/core/detectors/slow_vars_detector.py     (改:Matlab 版 InSAR 叠加)
  web_app/core/mineral_engine.py                   (改:_load_insar)
  web_app/utils/visualizer.py                      (改:plot_insar_deformation)
  newpage.html                                     (改:InSAR 提示条)

geo-analyser/
  insar_analysis.py                 (新建,~250 行)
  insar_timeseries.py               (新建,~180 行)
  insar_broker.py                   (新建,~80 行)
  app.py                            (改:3 个新 API + 渲染辅助)
  NewPage/遥感分析平台浅色风格_v5/index.html       (改:加导航链接)
  NewPage/遥感分析平台浅色风格_v5/deformation.html (新建,完整 InSAR 分析页)
```
