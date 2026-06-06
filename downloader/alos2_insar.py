"""
alos2_insar.py — ALOS-2 PALSAR-2 SLC 下载(Phase 2,L 波段 InSAR)

geo-downloader/downloader/alos2.py 已经支持 processing_level='SLC',
本模块直接复用其搜索/下载逻辑,只在 ID 和后处理钩子上区分。

L 波段比 C 波段(Sentinel-1)穿透植被强,更适合:
- 植被覆盖密集的矿区(如热带/亚热带森林)
- 长基线干涉(L 波段去相干慢)
- 大范围地表抬升监测

注意:
- ASF 的 ALOS-2 数据需要单独通过 JAXA/RESTEC 申请权限,不是纯 Earthdata 即可
- 申请地址参考 https://www.eorc.jaxa.jp/ALOS/en/dataset/palsar2_l11_e.htm
"""

import importlib.util
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

_COMMONS_PATH = Path("/opt/deepexplor-services")
if str(_COMMONS_PATH) not in sys.path:
    sys.path.insert(0, str(_COMMONS_PATH))

from commons.base_downloader import BaseDownloader
from commons.aoi import bbox_to_wkt

# 关键设计:**不污染 sys.path** 来加载 geo-downloader 的 ALOS2Downloader。
# 原因同 commons/aoi.py — geo-insar 自己有顶层 downloader/ 和 postprocess/
# 包,如果把 geo-downloader 塞进 sys.path 会遮蔽 geo-insar 的同名包。
# 这里用 importlib 把 geo-downloader 的 downloader 包注册为独立命名空间
# "geodl_downloader",alos2 内部的 `from .base import BaseDownloader` 相对
# import 通过 sys.modules 注册的父包能正确解析。
_GEODL_DOWNLOADER_DIR = Path("/opt/deepexplor-services/geo-downloader/downloader")
_GEODL_NS = "geodl_downloader"


def _load_geodl_alos2():
    """从 geo-downloader 加载 ALOS2Downloader,不污染 sys.path。"""
    # 已加载过(模块缓存)
    if _GEODL_NS + ".alos2" in sys.modules:
        return sys.modules[_GEODL_NS + ".alos2"].ALOS2Downloader

    if not _GEODL_DOWNLOADER_DIR.exists():
        raise ImportError(
            f"找不到 {_GEODL_DOWNLOADER_DIR},无法复用 geo-downloader 的 ALOS2Downloader"
        )

    def _exec(name, file_path, is_package=False):
        kwargs = {}
        if is_package:
            kwargs["submodule_search_locations"] = [str(file_path.parent)]
        spec = importlib.util.spec_from_file_location(name, str(file_path), **kwargs)
        if spec is None or spec.loader is None:
            raise ImportError(f"无法创建 spec: {file_path}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod

    # 顺序:downloader/__init__.py(父包) → base.py(alos2 相对 import 依赖) → alos2.py
    _exec(_GEODL_NS, _GEODL_DOWNLOADER_DIR / "__init__.py", is_package=True)
    _exec(_GEODL_NS + ".base", _GEODL_DOWNLOADER_DIR / "base.py")
    alos2_mod = _exec(_GEODL_NS + ".alos2", _GEODL_DOWNLOADER_DIR / "alos2.py")
    return alos2_mod.ALOS2Downloader


try:
    import asf_search as asf
    HAS_ASF = True
except ImportError:
    HAS_ASF = False


class ALOS2InsarDownloader(BaseDownloader):
    """
    ALOS-2 SLC InSAR 下载器。

    设计上是 geo-downloader/ALOS2Downloader 的薄封装,只做两件事:
    1. processing_level 强制 'SLC'
    2. 下载完成后挂钩本地 SNAP 干涉处理(postprocess/insar_local.py)
    """

    PLATFORM_NAME = "alos2_insar"
    REQUIRES_AUTH = True

    def __init__(
        self,
        credentials: Dict[str, str],
        output_dir: str = "./downloads",
        **kwargs,
    ):
        super().__init__(credentials=credentials, output_dir=output_dir)
        self._inner = None

    def _check_deps(self):
        if not HAS_ASF:
            raise ImportError("缺少依赖: asf_search — pip install asf_search")

    def _get_inner(self):
        if self._inner is None:
            self._check_deps()
            ALOS2Downloader = _load_geodl_alos2()
            self._inner = ALOS2Downloader(
                credentials=self.credentials,
                output_dir=str(self.output_dir),
            )
        return self._inner

    def search(
        self,
        bbox: Tuple[float, float, float, float],
        start_date: str,
        end_date: str,
        beam_mode: str = "Fine",
        **kwargs,
    ) -> List[Any]:
        """
        搜索 ALOS-2 SLC 场景。

        beam_mode 选项(对齐 ASF):
          'Fine'      — 3m 全极化(InSAR 推荐)
          'Ultra-fine'— 1m 单极化
          'ScanSAR'   — 100m 宽幅(可用于大范围)
        """
        inner = self._get_inner()
        # 强制 SLC,无视调用方传的 processing_level
        kwargs["processing_level"] = "SLC"
        kwargs.setdefault("beam_mode", beam_mode)
        return inner.search(bbox, start_date, end_date, **kwargs)

    def download(
        self,
        search_results: List[Any],
        save_dir: Path,
        max_items: int = 5,
        **kwargs,
    ) -> List[Path]:
        """
        下载 ALOS-2 SLC zip 到 save_dir,返回路径列表。
        不在此处触发本地干涉处理(由 main.py mode=local 时统一调度)。
        """
        inner = self._get_inner()
        return inner.download(search_results, save_dir, max_items=max_items, **kwargs)
