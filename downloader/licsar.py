"""
licsar.py — COMET LiCSAR 第三方 InSAR 成品抓取(Phase 3)

LiCSAR 是英国 NERC COMET 团队对全球 Sentinel-1 数据持续做 InSAR 处理后
公开发布的衍生品(干涉图、相干性、解缠相位、GACOS 大气校正等)。
适合"快速验证 + 不想自己跑处理"的场景,完美补充 Phase 1(HyP3)和 Phase 2(本地 SNAP)。

数据组织(基于已发表规范):
  https://data.ceda.ac.uk/neodc/comet/data/licsar_products/<track>/<frame_id>/
    ├── products/<refdate>_<secdate>/
    │   ├── <ref>_<sec>.geo.diff_pha.tif      # 干涉相位
    │   ├── <ref>_<sec>.geo.cc.tif            # 相干性
    │   ├── <ref>_<sec>.geo.unw.tif           # 解缠相位(主要产物)
    │   └── <ref>_<sec>.geo.unfiltered_pha.tif (可选)
    ├── epochs/<date>/                         # 单时相产品
    └── metadata/                              # frame 边界 KML

Frame ID 格式: <track 3 位>D_<frame 5 位>_<looks 6 位>
  例如: 022D_05411_131313  (track=022, descending, frame=05411, looks=13×13×13)
  A=ascending, D=descending

中国境内覆盖:
  - 部分区域覆盖(阿尔卑斯-喜马拉雅构造带优先)
  - 山东半岛、西藏、新疆部分有 frame
  - 东部沿海多数地区无覆盖
  - 检查覆盖率:用 search_frames_by_bbox() 看返回是否为空

认证:数据完全公开,无需登录(走 HTTP 即可)。
"""

import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

_COMMONS_PATH = Path("/opt/deepexplor-services")
if str(_COMMONS_PATH) not in sys.path:
    sys.path.insert(0, str(_COMMONS_PATH))

from commons.base_downloader import BaseDownloader
from commons.aoi import bbox_to_wkt
from commons.download import download_with_resume

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False


# LiCSAR 数据根目录(CEDA 存档)
LICSAR_BASE_URL = "https://data.ceda.ac.uk/neodc/comet/data/licsar_products/"

# 频域有效负载切换 URL(JASMIN 迁移期间的 fallback)
LICSAR_FALLBACK_URLS = [
    "https://gws-access.jasmin.ac.uk/public/nceo_geohazards/LiCSAR_products/",
    "https://data.ceda.ac.uk/neodc/comet/data/licsar_products.future/",
]


class LiCSARDownloader(BaseDownloader):
    """COMET LiCSAR 第三方 InSAR 产品下载器。"""

    PLATFORM_NAME = "licsar"
    REQUIRES_AUTH = False  # 公开数据,无需凭证

    def __init__(
        self,
        credentials: Optional[Dict[str, str]] = None,
        output_dir: str = "./downloads",
        base_url: str = LICSAR_BASE_URL,
        **kwargs,
    ):
        super().__init__(credentials=credentials or {}, output_dir=output_dir)
        self.base_url = base_url.rstrip("/") + "/"
        self._session = None

    def _check_deps(self):
        if not HAS_REQUESTS:
            raise ImportError("缺少依赖: requests — pip install requests")

    def _get_session(self):
        if self._session is None:
            self._check_deps()
            self._session = requests.Session()
            # LiCSAR 数据公开,但服务器对 User-Agent 敏感
            self._session.headers.update({
                "User-Agent": "geo-insar/0.1 (research/non-commercial)",
                "Accept": "text/html,*/*",
            })
        return self._session

    # ─────────────────────────────────────────────────────────
    # Frame 查找
    # ─────────────────────────────────────────────────────────

    def search_frames_by_bbox(
        self,
        bbox: Tuple[float, float, float, float],
        max_frames: int = 5,
    ) -> List[str]:
        """
        基于 AOI bbox 找候选 LiCSAR frame。

        LiCSAR 的 frame 索引没有官方 API,有两种实现路径:
        1. 离线 frame 几何索引(本仓库 licsar_frame_index.geojson,需用户首次下载)
        2. 通过 COMET-LiCS Portal 的查询接口(JSON 端点,但稳定性差)

        MVP 实现:**用户显式提供 frame ID**(最稳定),自动查找留给 Phase 3+ 升级。
        本函数当前返回空列表 + 提示,真实使用通过 search_frames(frame_ids=[...])。

        Returns
        -------
        []  # MVP: 总是空,提示用户用 frame_ids 显式指定
        """
        print(f"    [LiCSAR] 自动 frame 查找需要 frame 几何索引")
        print(f"    [LiCSAR] AOI bbox: {bbox}")
        print(f"    [LiCSAR] 临时方案:请到 https://comet.nerc.ac.uk/comet-lics-portal/")
        print(f"             查找覆盖本研究区的 frame ID,然后用 --licsar-frame 显式指定")
        return []

    def _list_url(self, url: str) -> List[str]:
        """
        请求一个 LiCSAR HTTP 目录索引,返回该层下所有子链接。

        CEDA 用 HTML 目录列表,链接形如 <a href="022D/">022D/</a>
        """
        sess = self._get_session()
        try:
            r = sess.get(url, timeout=30)
            r.raise_for_status()
        except Exception as e:
            print(f"    [LiCSAR] 列表失败 {url}: {e}")
            return []
        # 简单 anchor 提取,不依赖 BeautifulSoup
        hrefs = re.findall(r'href="([^"?#][^"]*)"', r.text)
        # 过滤 "../" 和外链
        result = []
        for h in hrefs:
            if h.startswith("?") or h.startswith("/") or "://" in h:
                continue
            if h in ("..", "../"):
                continue
            result.append(h)
        return result

    # ─────────────────────────────────────────────────────────
    # 按 frame 列产品 + 时间窗筛选
    # ─────────────────────────────────────────────────────────

    def search(
        self,
        bbox: Tuple[float, float, float, float],
        start_date: str,
        end_date: str,
        frame_ids: Optional[List[str]] = None,
        max_pairs_per_frame: int = 50,
        **kwargs,
    ) -> List[Dict]:
        """
        搜索 LiCSAR 干涉对。

        Parameters
        ----------
        bbox : 用于自动查找 frame(MVP 未实现,需 frame_ids 显式指定)
        start_date / end_date : YYYY-MM-DD
        frame_ids : 显式提供的 LiCSAR frame ID 列表,如 ['022D_05411_131313']
        max_pairs_per_frame : 每个 frame 最多返回的干涉对数

        Returns
        -------
        list of dict, 每个 dict 描述一个干涉对:
          {
            "frame_id": "022D_05411_131313",
            "ref_date": "2024-06-01",
            "sec_date": "2024-06-13",
            "track": "022",
            "orbit": "DESCENDING",
            "product_dir_url": "https://.../products/20240601_20240613/",
            "files": {"unw": "...", "cc": "...", "diff_pha": "..."}
          }
        """
        self._check_deps()
        self._validate_date(start_date)
        self._validate_date(end_date)

        # MVP: 必须用户提供 frame_ids
        if not frame_ids:
            auto = self.search_frames_by_bbox(bbox)
            if not auto:
                print(f"    [LiCSAR] 未提供 frame_ids 且自动查找未实现,无法搜索")
                return []
            frame_ids = auto

        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")

        all_pairs: List[Dict] = []
        for fid in frame_ids:
            track_dir = self._extract_track(fid)
            if not track_dir:
                print(f"    [LiCSAR] frame_id 格式不对: {fid}(应如 022D_05411_131313)")
                continue

            products_url = f"{self.base_url}{track_dir}/{fid}/products/"
            print(f"    [LiCSAR] 列 {products_url}")
            pair_dirs = self._list_url(products_url)
            count = 0
            for pd in pair_dirs:
                m = re.match(r"^(\d{8})_(\d{8})/?$", pd)
                if not m:
                    continue
                ref_d, sec_d = m.group(1), m.group(2)
                try:
                    ref_dt = datetime.strptime(ref_d, "%Y%m%d")
                    sec_dt = datetime.strptime(sec_d, "%Y%m%d")
                except ValueError:
                    continue
                # 用 ref_date 作为对的代表日期
                if not (start_dt <= ref_dt <= end_dt):
                    continue
                pair_url = urljoin(products_url, pd if pd.endswith("/") else pd + "/")
                files = self._list_url(pair_url)
                # 找标准产物
                file_map = {}
                for f in files:
                    if f.endswith(".geo.unw.tif"):
                        file_map["unw"] = pair_url + f
                    elif f.endswith(".geo.cc.tif"):
                        file_map["cc"] = pair_url + f
                    elif f.endswith(".geo.diff_pha.tif"):
                        file_map["diff_pha"] = pair_url + f

                if not file_map.get("unw"):
                    continue  # 解缠相位是必备品,没有就跳过

                all_pairs.append({
                    "frame_id": fid,
                    "ref_date": ref_dt.strftime("%Y-%m-%d"),
                    "sec_date": sec_dt.strftime("%Y-%m-%d"),
                    "track": track_dir,
                    "orbit": "DESCENDING" if "D" in fid.split("_")[0] else "ASCENDING",
                    "product_dir_url": pair_url,
                    "files": file_map,
                })
                count += 1
                if count >= max_pairs_per_frame:
                    break
        print(f"    [LiCSAR] 共找到 {len(all_pairs)} 个匹配的干涉对(frames={frame_ids})")
        return all_pairs

    @staticmethod
    def _extract_track(frame_id: str) -> Optional[str]:
        """frame_id '022D_05411_131313' → '022D'(目录索引用)。"""
        m = re.match(r"^(\d{3}[AD])_", frame_id)
        return m.group(1) if m else None

    # ─────────────────────────────────────────────────────────
    # 下载
    # ─────────────────────────────────────────────────────────

    def download(
        self,
        search_results: List[Dict],
        save_dir: Path,
        max_items: int = 20,
        **kwargs,
    ) -> List[Path]:
        """
        下载 LiCSAR 干涉对 GeoTIFF。
        返回每个对的本地目录路径(供 postprocess/licsar_postprocess 标准化)。
        """
        self._check_deps()
        save_dir = Path(save_dir)
        save_dir.mkdir(parents=True, exist_ok=True)
        sess = self._get_session()

        to_download = search_results[:max_items]
        out_dirs: List[Path] = []
        for i, p in enumerate(to_download, 1):
            ref = p["ref_date"].replace("-", "")
            sec = p["sec_date"].replace("-", "")
            pair_dir = save_dir / f"{p['frame_id']}_{ref}_{sec}"
            pair_dir.mkdir(parents=True, exist_ok=True)
            print(f"    [{i}/{len(to_download)}] {p['frame_id']} {ref}-{sec}")
            try:
                for kind, url in p["files"].items():
                    fname = url.rsplit("/", 1)[-1]
                    dest = pair_dir / fname
                    download_with_resume(sess, url, dest, desc=fname[:50])
                out_dirs.append(pair_dir)
            except Exception as e:
                print(f"      下载失败: {e}")
        print(f"    [LiCSAR] 下载完成 {len(out_dirs)}/{len(to_download)}")
        return out_dirs
