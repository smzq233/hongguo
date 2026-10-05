from __future__ import annotations

import requests
from bs4 import BeautifulSoup

from .models import Episode
from .utils import (
    parse_embedded_json,
    recursive_values,
)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/137 Safari/537.36"
)

BASE = "https://hongguoduanju.com"


class VideoParser:
    """
    公开视频探测。

    注意：
    APK 的真实下载链路不是把 ID 拼在一个固定媒体前缀后面。

    APK 是：
      episode.vid
        -> Ti(vid)
        -> multi_video_model/v1
        -> video_model
        -> fallback_api
        -> video_list

    当前版本不会复现会员绕过、CENC/DRM 解密或访问控制规避。

    为了修复 v0.2 的错误：
      ❌ /player/{series_id}/{vid}

    v0.4 只会对每集独立 ID 尝试：
      ✅ /player/{vid}

    这只是公开页面探测，不代表 APK 下载本身这么做。
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
            }
        )

    @staticmethod
    def _looks_protected(
        url: str,
        text: str = "",
    ) -> bool:
        content = (url + " " + text).lower()

        return any(
            word in content
            for word in (
                "license",
                "widevine",
                "playready",
                "fairplay",
                "drm",
                "cenc",
            )
        )

    @staticmethod
    def _looks_media(url: str) -> bool:
        lower = url.lower()

        return any(
            marker in lower
            for marker in (
                ".mp4",
                ".m4v",
                ".mov",
                "video/tos/",
                "mime_type=video",
            )
        )

    def _extract_public_candidates(
        self,
        html: str,
    ) -> list[str]:
        candidates: list[str] = []

        for marker in (
            "_ROUTER_DATA",
            "_SSR_DATA",
        ):
            obj = parse_embedded_json(
                html,
                marker,
            )

            if not obj:
                continue

            values = recursive_values(
                obj,
                (
                    "main_url",
                    "mainUrl",
                    "play_url",
                    "playUrl",
                    "video_url",
                    "videoUrl",
                    "src",
                ),
            )

            for value in values:
                if (
                    isinstance(value, str)
                    and value.startswith(
                        ("http://", "https://")
                    )
                ):
                    candidates.append(
                        value.replace(r"\u0026", "&")
                    )

                elif isinstance(value, dict):
                    for key in (
                        "url",
                        "main_url",
                        "mainUrl",
                    ):
                        u = value.get(key)

                        if (
                            isinstance(u, str)
                            and u.startswith(
                                ("http://", "https://")
                            )
                        ):
                            candidates.append(u)

        soup = BeautifulSoup(
            html,
            "html.parser",
        )

        for tag in soup.find_all(
            ("video", "source"),
        ):
            u = tag.get("src")

            if (
                u
                and u.startswith(
                    ("http://", "https://")
                )
            ):
                candidates.append(u)

        result: list[str] = []

        for url in candidates:
            if url not in result:
                result.append(url)

        return result

    def resolve_public_url(
        self,
        ep: Episode,
    ) -> str:
        if ep.direct_url:
            return ep.direct_url

        if not ep.vid:
            ep.status = "该集缺少 chapter_id/vid"
            raise RuntimeError(ep.status)

        # 修正：
        # 不再访问 /player/{series_id}/{vid}
        #
        # 只把单集 chapter_id/vid 作为独立 player 入口探测。
        player_url = (
            ep.player_url
            or f"{BASE}/player/{ep.vid}"
        )

        try:
            r = self.s.get(
                player_url,
                timeout=20,
                allow_redirects=True,
            )
        except requests.RequestException as e:
            ep.status = "单 ID 播放页请求失败"
            raise RuntimeError(
                f"{ep.status}：{e}"
            ) from e

        if r.status_code == 404:
            ep.status = "该 ID 没有独立公开 player 页面"
            raise RuntimeError(
                f"{ep.status}；"
                f"episode vid={ep.vid}。"
                "APK 对这种情况会进入 Ti(vid) 播放器 API，"
                "而不是继续拼网页 URL。"
            )

        r.raise_for_status()

        page_html = r.text or ""

        candidates = self._extract_public_candidates(
            page_html
        )

        for url in candidates:
            if self._looks_protected(
                url,
                page_html,
            ):
                continue

            if self._looks_media(url):
                ep.direct_url = url
                ep.accessible = True
                ep.status = "发现公开媒体直链"
                return url

        if candidates:
            ep.protected = True
            ep.status = (
                "页面有媒体信息，但没有公开直链"
            )
        else:
            ep.status = (
                "player 页面存在，但未暴露公开媒体直链"
            )

        raise RuntimeError(
            ep.status
            + f"；episode vid={ep.vid}。"
            + "APK 下一步是 Ti(vid) -> multi_video_model。"
        )
