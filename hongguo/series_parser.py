from __future__ import annotations

import requests

from .models import SeriesInfo, Episode, ShareResult
from .utils import (
    parse_embedded_json,
    recursive_first,
    normalize_id_list,
    valid_id,
)

BASE = "https://hongguoduanju.com"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/137 Safari/537.36"
)


class SeriesParser:
    """
    复现 APK xi() 的剧集映射。

    最重要的行为：
      landing page.pageData.chapter_ids
        -> vidList
        -> 每一集直接用 vidList[index]

    不再：
      - 使用 video_id 推算后续集数
      - 使用 content_id 推算后续集数
      - 拼 /player/{series_id}/{vid}
    """

    def __init__(
        self,
        session: requests.Session | None = None,
    ):
        self.s = session or requests.Session()

        self.s.headers.update(
            {
                "User-Agent": UA,
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Accept": "text/html,application/xhtml+xml",
            }
        )

    @staticmethod
    def _player_data(html: str, fallback_id: str = "") -> dict:
        """
        对应 APK xi() 的 /player/{e} 兜底解析。
        """
        router = parse_embedded_json(html, "_ROUTER_DATA")

        if not isinstance(router, dict):
            return {}

        loader = router.get("loaderData")

        if not isinstance(loader, dict):
            return {}

        player = None

        for key, value in loader.items():
            if (
                isinstance(key, str)
                and key.startswith("player_")
                and isinstance(value, dict)
            ):
                player = value
                break

        if not isinstance(player, dict):
            return {}

        detail = player.get("seriesDetail")

        if not isinstance(detail, dict):
            return {}

        vids = detail.get("vid_list")

        if not isinstance(vids, list):
            vids = []

        vids = [
            str(x)
            for x in vids
            if valid_id(x)
        ]

        try:
            episode_cnt = int(
                detail.get("episode_cnt")
                or len(vids)
                or 0
            )
        except Exception:
            episode_cnt = len(vids)

        try:
            accessible_cnt = int(
                detail.get("accessible_episode_cnt")
                or 0
            )
        except Exception:
            accessible_cnt = 0

        return {
            "series_id": str(
                detail.get("series_id")
                or fallback_id
                or ""
            ),
            "name": detail.get("series_name") or "",
            "cover": detail.get("series_cover") or "",
            "intro": detail.get("series_intro") or "",
            "episode_cnt": episode_cnt,
            "accessible_cnt": accessible_cnt,
            "pay_type": detail.get("pay_type"),
            "vid_list": vids,
        }

    @staticmethod
    def _from_landing(share: ShareResult) -> SeriesInfo | None:
        """
        这是 APK 对 novelquickapp 分享链接的优先路径。

        APK 逻辑：
          pageData.series_data
          pageData.chapter_ids
          chapter_ids -> vidList
        """
        chapter_ids = (
            share.metadata.get("chapter_ids")
            or []
        )

        if not chapter_ids:
            return None

        chapter_ids = [
            str(x)
            for x in chapter_ids
            if valid_id(x)
        ]

        if not chapter_ids:
            return None

        page_data = (
            share.metadata.get("page_data")
            or {}
        )

        series_data = (
            page_data.get("series_data")
            if isinstance(page_data, dict)
            else {}
        )

        if not isinstance(series_data, dict):
            series_data = {}

        name = (
            series_data.get("title")
            or series_data.get("series_name")
            or share.title
            or f"红果短剧_{chapter_ids[0]}"
        )

        cover = (
            series_data.get("series_cover")
            or share.metadata.get("cover")
            or ""
        )

        intro = (
            series_data.get("series_intro")
            or share.metadata.get("intro")
            or ""
        )

        try:
            episode_cnt = int(
                series_data.get("serial_count")
                or share.metadata.get("serial_count")
                or len(chapter_ids)
            )
        except Exception:
            episode_cnt = len(chapter_ids)

        # APK 本身在 landing path 上就是这样：
        #   seriesId = t || chapter_ids[0]
        # Mi(t) 最终通常取到 chapter_ids[0]。
        entry_id = (
            share.metadata.get("apk_entry_id")
            or chapter_ids[0]
        )

        episodes: list[Episode] = []

        for pos in range(episode_cnt):
            episode_no = pos + 1
            vid = (
                chapter_ids[pos]
                if pos < len(chapter_ids)
                else ""
            )

            episodes.append(
                Episode(
                    index=episode_no,
                    vid=vid,
                    chapter_id=vid,
                    player_url=(
                        f"{BASE}/player/{vid}"
                        if vid
                        else ""
                    ),
                    accessible=False,
                    status=(
                        "APK chapter_id/vid 已解析"
                        if vid
                        else "缺少 episode id"
                    ),
                )
            )

        return SeriesInfo(
            series_id=str(entry_id),
            name=str(name),
            total=episode_cnt,
            cover=str(cover),
            intro=str(intro),
            source_url=share.final_url,
            episodes=episodes,
            raw={
                "source": "novelquickapp landing page",
                "apk_entry_id": entry_id,
                "share_video_id": share.video_id,
                "share_vid": share.vid,
                "share_content_id": share.content_id,
                "chapter_ids": chapter_ids,
                "mapping": (
                    "episode N = chapter_ids[N-1]"
                ),
                "note": (
                    "player_url here is only a public-page probe "
                    "using /player/{chapter_id}; "
                    "APK download itself calls Ti(vid) instead."
                ),
            },
        )

    def from_share(
        self,
        share: ShareResult,
    ) -> SeriesInfo:
        # 1. APK 对分享落地页优先直接使用 chapter_ids。
        landing = self._from_landing(share)

        if landing is not None:
            return landing

        # 2. 非分享落地页时，按 APK xi() 的 /player/{e} 兜底。
        candidates: list[str] = []

        for value in (
            share.series_id,
            share.metadata.get("apk_entry_id"),
            share.vid,
            share.content_id,
            share.video_id,
        ):
            if valid_id(value) and str(value) not in candidates:
                candidates.append(str(value))

        errors: list[str] = []

        for candidate in candidates:
            url = f"{BASE}/player/{candidate}"

            try:
                r = self.s.get(
                    url,
                    timeout=20,
                    allow_redirects=True,
                )

                if r.status_code >= 400:
                    errors.append(
                        f"{url}: HTTP {r.status_code}"
                    )
                    continue

                data = self._player_data(
                    r.text,
                    candidate,
                )

                vids = data.get("vid_list") or []

                if not vids:
                    continue

                n = max(
                    int(data.get("episode_cnt") or 0),
                    len(vids),
                )

                episodes = []

                for pos in range(n):
                    episode_no = pos + 1
                    vid = (
                        vids[pos]
                        if pos < len(vids)
                        else ""
                    )

                    episodes.append(
                        Episode(
                            index=episode_no,
                            vid=vid,
                            chapter_id=vid,
                            player_url=(
                                f"{BASE}/player/{vid}"
                                if vid
                                else ""
                            ),
                            accessible=False,
                            status=(
                                "APK vid_list 已解析"
                                if vid
                                else "缺少 episode id"
                            ),
                        )
                    )

                return SeriesInfo(
                    series_id=(
                        data.get("series_id")
                        or candidate
                    ),
                    name=(
                        data.get("name")
                        or share.title
                        or f"红果短剧_{candidate}"
                    ),
                    total=n,
                    cover=data.get("cover") or "",
                    intro=data.get("intro") or "",
                    source_url=r.url,
                    episodes=episodes,
                    raw={
                        "source": "hongguoduanju player page",
                        "vid_list": vids,
                        "accessible_cnt": data.get(
                            "accessible_cnt",
                            0,
                        ),
                        "mapping": (
                            "episode N = vid_list[N-1]"
                        ),
                    },
                )

            except Exception as e:
                errors.append(f"{url}: {e}")

        raise RuntimeError(
            "没有解析出 APK 所需的 chapter_ids / vid_list。\n"
            + "\n".join(errors[-5:])
        )
