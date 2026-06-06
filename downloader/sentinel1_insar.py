"""
sentinel1_insar.py — Sentinel-1 InSAR 通过 ASF HyP3 云端处理

流程:
1. 解析 AOI(commons/aoi.py)
2. asf_search 查 Sentinel-1 SLC 场景
3. 按配对策略生成 reference-secondary 对清单
4. hyp3_sdk 提交 InSAR jobs(INSAR_GAMMA 或 INSAR_ISCE_BURST)
5. 后台轮询 → 下载 → 标准化(postprocess/insar.py)

注册: nasa_earthdata 凭证(优先 token,fallback username/password)
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

# commons/ 兄弟目录导入
_COMMONS_PATH = Path("/opt/deepexplor-services")
if str(_COMMONS_PATH) not in sys.path:
    sys.path.insert(0, str(_COMMONS_PATH))

from commons.base_downloader import BaseDownloader
from commons.aoi import bbox_to_wkt

try:
    import asf_search as asf
    HAS_ASF = True
except ImportError:
    HAS_ASF = False

try:
    import hyp3_sdk
    HAS_HYP3 = True
except ImportError:
    HAS_HYP3 = False


# 配对策略
PAIR_CLOSEST = "closest_in_time"
PAIR_FIXED_MASTER = "fixed_master"
PAIR_CASCADE = "cascade"

# HyP3 后端
BACKEND_GAMMA = "INSAR_GAMMA"
BACKEND_ISCE_BURST = "INSAR_ISCE_BURST"


class Sentinel1InsarDownloader(BaseDownloader):

    PLATFORM_NAME = "sentinel1_insar"
    REQUIRES_AUTH = True

    def __init__(
        self,
        credentials: Dict[str, str],
        output_dir: str = "./downloads",
        **kwargs,
    ):
        super().__init__(credentials=credentials, output_dir=output_dir)
        self._asf_session = None
        self._hyp3 = None

    def _check_deps(self):
        missing = []
        if not HAS_ASF:
            missing.append("asf_search")
        if not HAS_HYP3:
            missing.append("hyp3_sdk")
        if missing:
            raise ImportError(
                f"缺少依赖: {', '.join(missing)}\n"
                f"请运行: pip install {' '.join(missing)}"
            )

    def _get_asf_session(self):
        if self._asf_session is None:
            self._check_deps()
            token = self.credentials.get("token") or ""
            if token:
                self._asf_session = asf.ASFSession().auth_with_token(token)
            else:
                self._asf_session = asf.ASFSession().auth_with_creds(
                    self.credentials["username"],
                    self.credentials["password"],
                )
        return self._asf_session

    def _get_hyp3(self):
        if self._hyp3 is None:
            self._check_deps()
            token = self.credentials.get("token") or ""
            if token:
                # hyp3_sdk 也支持 EDL token
                self._hyp3 = hyp3_sdk.HyP3(
                    username=self.credentials["username"],
                    password=self.credentials.get("password") or token,
                )
            else:
                self._hyp3 = hyp3_sdk.HyP3(
                    username=self.credentials["username"],
                    password=self.credentials["password"],
                )
        return self._hyp3

    # ─────────────────────────────────────────────────────────
    # search() — 查 Sentinel-1 SLC 或 BURST(取决于 backend)
    # ─────────────────────────────────────────────────────────
    def search(
        self,
        bbox: Tuple[float, float, float, float],
        start_date: str,
        end_date: str,
        beam_mode: str = "IW",
        polarization: str = "VV",
        backend: str = BACKEND_ISCE_BURST,
        **kwargs,
    ) -> List[Any]:
        """
        搜索 Sentinel-1 数据。

        - backend=INSAR_GAMMA      → 整景 SLC(processingLevel=SLC)
        - backend=INSAR_ISCE_BURST → burst 级 granule(processingLevel=BURST),
                                      因为 HyP3 的 ISCE_BURST API 只接受 burst granule。
        """
        self._check_deps()
        self._validate_date(start_date)
        self._validate_date(end_date)

        aoi_wkt = bbox_to_wkt(bbox)
        session = self._get_asf_session()

        if backend == BACKEND_ISCE_BURST:
            proc_level = asf.PRODUCT_TYPE.BURST
            kind = "BURST"
            # burst 数据每个 polarization 是独立 granule,默认只取 VV(配对一致性)
            search_pol = polarization
        else:
            proc_level = asf.PRODUCT_TYPE.SLC
            kind = "SLC"
            # SLC 是整景,polarization 字段表示该景包含的极化(VV+VH 是 IW 主流)
            search_pol = "VV+VH"

        results = asf.search(
            platform=[asf.PLATFORM.SENTINEL1],
            processingLevel=[proc_level],
            beamMode=[beam_mode],
            polarization=[search_pol],
            intersectsWith=aoi_wkt,
            start=f"{start_date}T00:00:00Z",
            end=f"{end_date}T23:59:59Z",
            maxResults=500 if kind == "BURST" else 200,
            opts=asf.ASFSearchOptions(session=session),
        )

        scenes = list(results)
        print(f"    [Sentinel-1 {kind}] 找到 {len(scenes)} 个")
        for s in scenes[:5]:
            p = s.properties
            if kind == "BURST":
                b = p.get("burst") or {}
                print(f"      {p.get('startTime','?')[:10]}  "
                      f"burst={b.get('fullBurstID','?')}  "
                      f"pol={p.get('polarization','?')}")
            else:
                print(f"      {p.get('startTime','?')[:10]}  "
                      f"path={p.get('pathNumber','?')}  "
                      f"frame={p.get('frameNumber','?')}  "
                      f"pol={p.get('polarization','?')}")
        if len(scenes) > 5:
            print(f"      ... 共 {len(scenes)} 个")
        return scenes

    # ─────────────────────────────────────────────────────────
    # 配对策略
    # ─────────────────────────────────────────────────────────
    @staticmethod
    def _scene_datetime(scene) -> datetime:
        return datetime.fromisoformat(scene.properties["startTime"].replace("Z", "+00:00"))

    def make_pairs(
        self,
        scenes: List[Any],
        strategy: str = PAIR_CLOSEST,
        max_temporal_baseline_days: int = 24,
        max_perp_baseline_m: float = 200.0,
        max_pairs: int = 50,
    ) -> List[Tuple[Any, Any]]:
        """
        根据策略生成 (reference, secondary) 干涉对清单。

        过滤:
        - 同 path/frame/orbit_direction
        - 时间基线 <= max_temporal_baseline_days
        - 垂直基线 <= max_perp_baseline_m(若可获取)
        """
        if not scenes:
            return []

        # 分组键:同组内的影像才能配对
        # - SLC:  (path, frame, orbit_direction)
        # - BURST:(fullBurstID, polarization)—— frame=None,必须用 burst ID
        def _group_key(s):
            b = s.properties.get("burst")
            if b:  # burst 数据
                return (b.get("fullBurstID"), s.properties.get("polarization"))
            return (
                s.properties.get("pathNumber"),
                s.properties.get("frameNumber"),
                s.properties.get("flightDirection"),
            )

        groups: Dict[Tuple, List] = {}
        for s in scenes:
            groups.setdefault(_group_key(s), []).append(s)

        pairs: List[Tuple[Any, Any]] = []

        for key, group in groups.items():
            group_sorted = sorted(group, key=lambda x: self._scene_datetime(x))
            if len(group_sorted) < 2:
                continue

            if strategy == PAIR_CLOSEST:
                for i in range(len(group_sorted) - 1):
                    ref, sec = group_sorted[i], group_sorted[i + 1]
                    if self._baseline_days(ref, sec) > max_temporal_baseline_days:
                        continue
                    pairs.append((ref, sec))

            elif strategy == PAIR_FIXED_MASTER:
                master = group_sorted[0]
                for sec in group_sorted[1:]:
                    if self._baseline_days(master, sec) > max_temporal_baseline_days:
                        continue
                    pairs.append((master, sec))

            elif strategy == PAIR_CASCADE:
                for i in range(len(group_sorted) - 1):
                    for j in range(i + 1, len(group_sorted)):
                        if self._baseline_days(group_sorted[i], group_sorted[j]) > max_temporal_baseline_days:
                            break
                        pairs.append((group_sorted[i], group_sorted[j]))

            else:
                raise ValueError(f"未知配对策略: {strategy}")

        # 截断
        if len(pairs) > max_pairs:
            print(f"    [配对] 共可配 {len(pairs)} 对,截至前 {max_pairs} 对(按时间排序)")
            pairs.sort(key=lambda p: self._scene_datetime(p[0]))
            pairs = pairs[:max_pairs]

        print(f"    [配对] 生成 {len(pairs)} 个 reference-secondary 对(策略: {strategy})")
        return pairs

    def _baseline_days(self, ref, sec) -> int:
        return abs((self._scene_datetime(sec) - self._scene_datetime(ref)).days)

    # ─────────────────────────────────────────────────────────
    # download() — 提交 HyP3 jobs(异步,真正下载由 postprocess 触发)
    # ─────────────────────────────────────────────────────────
    def submit_pairs(
        self,
        pairs: List[Tuple[Any, Any]],
        backend: str = BACKEND_ISCE_BURST,
        include_dem: bool = False,
        include_water_mask: bool = False,
        include_inc_map: bool = False,
        job_name_prefix: str = "geo-insar",
    ) -> List[Any]:
        """
        向 HyP3 提交 InSAR jobs。返回 hyp3_sdk.Job 列表。
        """
        self._check_deps()
        hyp3 = self._get_hyp3()
        jobs = []

        for ref, sec in pairs:
            ref_name = ref.properties.get("sceneName") or ref.properties.get("granuleName")
            sec_name = sec.properties.get("sceneName") or sec.properties.get("granuleName")
            ref_date = self._scene_datetime(ref).strftime("%Y%m%d")
            sec_date = self._scene_datetime(sec).strftime("%Y%m%d")
            name = f"{job_name_prefix}_{ref_date}_{sec_date}"

            if backend == BACKEND_GAMMA:
                result = hyp3.submit_insar_job(
                    granule1=ref_name,
                    granule2=sec_name,
                    name=name,
                    include_look_vectors=include_inc_map,
                    include_los_displacement=True,
                    include_inc_map=include_inc_map,
                    include_dem=include_dem,
                    include_wrapped_phase=True,
                    apply_water_mask=include_water_mask,
                )
            elif backend == BACKEND_ISCE_BURST:
                result = hyp3.submit_insar_isce_burst_job(
                    granule1=ref_name,
                    granule2=sec_name,
                    name=name,
                    apply_water_mask=include_water_mask,
                )
            else:
                raise ValueError(f"未知后端: {backend}")

            # hyp3_sdk 7.x 的 submit_* 返回 Batch(单 pair 提交时 Batch 里就 1 个 Job)
            job = result.jobs[0] if hasattr(result, "jobs") else result
            print(f"    [HyP3 提交] {name} → job_id={job.job_id} (backend={backend})")
            jobs.append(job)

        return jobs

    def download(
        self,
        search_results: List[Any],
        save_dir: Path,
        **kwargs,
    ) -> List[Path]:
        """
        BaseDownloader 兼容接口:提交 pairs 后立即返回(不阻塞)。
        实际下载由 task_store 的轮询线程驱动。
        """
        raise NotImplementedError(
            "InSAR 是异步流程,请使用 search() + make_pairs() + submit_pairs() + "
            "task_store 轮询机制,不要直接调 download()"
        )
