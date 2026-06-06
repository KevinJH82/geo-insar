"""
geo-insar — InSAR 数据下载与处理子系统

独立子系统,与 geo-downloader / geo-exploration / geo-reporter / geo-analyser
平行运行,通过 commons/ 共享公共基础设施。

主要功能:
- 提交 ASF HyP3 云端 InSAR Job(Phase 1)
- 本地 SLC + SNAP 干涉处理(Phase 2)
- 第三方 InSAR 成品下载(LiCSAR/EGMS,Phase 3)

标准输出契约见 commons/insar_schema.json。
"""

__version__ = "0.1.0-phase1"
