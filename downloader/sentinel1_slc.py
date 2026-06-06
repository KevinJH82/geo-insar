"""
sentinel1_slc.py — Sentinel-1 SLC 直下载(Phase 2)

与 sentinel1_insar.py(走云端 HyP3)的区别:
- 直接下原始 SLC ZIP 到本地
- **不调用** pyroSAR 地理编码(SLC 必须保留原始复相位用于干涉)
- 下载完成后由 postprocess/insar_local.py 接管,跑 SNAP gpt 干涉流程

参考实现:geo-downloader/downloader/sentinel1.py 的 search() 和 download(),
但 processingLevel 改为 SLC,download() 后续不走 _geocode_s1。
"""

import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_COMMONS_PATH = Path("/opt/deepexplor-services")
if str(_COMMONS_PATH) not in sys.path:
    sys.path.insert(0, str(_COMMONS_PATH))

from commons.base_downloader import BaseDownloader
from commons.aoi import bbox_to_wkt
from commons.download import download_with_resume

try:
    import asf_search as asf
    HAS_ASF = True
except ImportError:
    HAS_ASF = False


class Sentinel1SLCDownloader(BaseDownloader):
    """Sentinel-1 SLC 下载器(Phase 2 本地干涉流程用)。"""

    PLATFORM_NAME = "sentinel1_slc"
    REQUIRES_AUTH = True

    def __init__(
        self,
        credentials: Dict[str, str],
        output_dir: str = "./downloads",
        **kwargs,
    ):
        super().__init__(credentials=credentials, output_dir=output_dir)
        self._asf_session = None

    def _check_deps(self):
        if not HAS_ASF:
            raise ImportError(
                "缺少依赖: asf_search\n请运行: pip install asf_search earthaccess"
            )

    def _get_session(self):
        """认证(优先 token,fallback username/password)。"""
        if self._asf_session is not None:
            return self._asf_session
        self._check_deps()
        token = self.credentials.get("token") or ""
        username = self.credentials.get("username", "")
        password = self.credentials.get("password", "")

        # 优先 token,如果空尝试 earthaccess 动态获取
        if token:
            self._asf_session = asf.ASFSession().auth_with_token(token)
            return self._asf_session

        # 用 username/password + earthaccess 动态拿 EDL token(对齐 sentinel1.py)
        try:
            import earthaccess
            os.environ["EARTHDATA_USERNAME"] = username
            os.environ["EARTHDATA_PASSWORD"] = password
            earthaccess.login(strategy="environment")
            tok_dict = earthaccess.get_edl_token() or {}
            tok = tok_dict.get("access_token", "")
            if tok:
                self._asf_session = asf.ASFSession().auth_with_token(tok)
                return self._asf_session
        except Exception:
            pass

        # 最终 fallback
        self._asf_session = asf.ASFSession().auth_with_creds(username, password)
        return self._asf_session

    def search(
        self,
        bbox: Tuple[float, float, float, float],
        start_date: str,
        end_date: str,
        beam_mode: str = "IW",
        polarization: str = "VV+VH",
        max_results: int = 200,
        **kwargs,
    ) -> List[Any]:
        """搜索 Sentinel-1 SLC 场景。"""
        self._check_deps()
        self._validate_date(start_date)
        self._validate_date(end_date)

        aoi_wkt = bbox_to_wkt(bbox)
        session = self._get_session()

        results = asf.search(
            platform=[asf.PLATFORM.SENTINEL1],
            processingLevel=[asf.PRODUCT_TYPE.SLC],
            beamMode=[beam_mode],
            intersectsWith=aoi_wkt,
            start=f"{start_date}T00:00:00Z",
            end=f"{end_date}T23:59:59Z",
            maxResults=max_results,
            opts=asf.ASFSearchOptions(session=session),
        )
        scenes = list(results)
        print(f"    [Sentinel-1 SLC] 找到 {len(scenes)} 景(本地干涉流程用)")
        for s in scenes[:5]:
            p = s.properties
            print(f"      {p.get('startTime','?')[:10]}  "
                  f"path={p.get('pathNumber','?')}  "
                  f"frame={p.get('frameNumber','?')}  "
                  f"pol={p.get('polarization','?')}  "
                  f"{p.get('sceneName','?')}")
        return scenes

    def download(
        self,
        search_results: List[Any],
        save_dir: Path,
        max_items: int = 5,
        **kwargs,
    ) -> List[Path]:
        """
        下载 SLC ZIP 到 save_dir。
        **不调** pyroSAR 地理编码(SLC 复相位必须保留)。

        返回 zip 路径列表,供 postprocess/insar_local.py 消费。
        """
        self._check_deps()
        save_dir = Path(save_dir)
        save_dir.mkdir(parents=True, exist_ok=True)
        session = self._get_session()

        to_download = list(search_results)[:max_items]
        downloaded: List[Path] = []

        import requests
        for i, scene in enumerate(to_download, 1):
            try:
                props = scene.properties
                url = props.get("url") or scene.geojson().get("properties", {}).get("url")
                if not url:
                    print(f"    [{i}/{len(to_download)}] 无下载 URL: {props.get('sceneName')}")
                    continue
                name = props.get("sceneName") or props.get("fileName") or f"scene_{i}"
                fname = name if name.lower().endswith(".zip") else f"{name}.zip"
                dest = save_dir / fname
                if dest.exists():
                    print(f"    [{i}/{len(to_download)}] 已存在,跳过: {fname}")
                    downloaded.append(dest)
                    continue

                # 用 asf session 下载(自带认证 cookies)
                # session 是 asf.ASFSession,继承 requests.Session,可以直接用
                req_session = session if hasattr(session, "get") else requests.Session()
                print(f"    [{i}/{len(to_download)}] 下载 {fname}")
                download_with_resume(req_session, url, dest, desc=fname[:50])
                downloaded.append(dest)
            except Exception as e:
                print(f"    [{i}/{len(to_download)}] 下载失败 {scene.properties.get('sceneName')}: {e}")

        print(f"    [Sentinel-1 SLC] 下载完成 {len(downloaded)}/{len(to_download)} 景")
        return downloaded
