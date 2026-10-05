from __future__ import annotations

import json
import re
from urllib.parse import urlparse, parse_qs

import requests

from .models import ShareResult
from .utils import (
    first_url,
    decode_many,
    valid_id,
    parse_embedded_json,
    recursive_first,
    normalize_id_list,
)

MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 16; 25053RT47C) "
    "AppleWebKit/537.36 Chrome/137 Mobile Safari/537.36"
)


class ShareParser:
    """
    按 APK 中 hongguo.js 的 Ai / Ci / Di / Mi 思路解析分享链接。

    关键修正：
    1. novelquickapp 分享页最终 URL 里的 video_id / vid 只是页面参数，
       不能把它们当成“全集每一集”的 ID。
    2. APK 真正用于全集的数组是：
         _ROUTER_DATA
           -> loaderData
           -> video-animation-share_page
           -> pageData
           -> chapter_ids
    3. APK 随后把 chapter_ids 直接作为 vidList 使用：
         第 N 集 = chapter_ids[N - 1]
    """

    def __init__(self, session: requests.Session | None = None):
        self.s = session or requests.Session()
        self.s.headers.update(
            {
                "User-Agent": MOBILE_UA,
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Accept": "text/html,application/xhtml+xml",
            }
        )

    def _query_ids(self, url: str) -> dict:
        out = {
            "series_id": "",
            "video_id": "",
            "vid": "",
            "content_id": "",
        }

        try:
            q = parse_qs(urlparse(url).query)
        except Exception:
            return out

        aliases = {
            "series_id": ("series_id", "seriesId"),
            "video_id": ("video_id", "videoId"),
            "vid": ("vid",),
            "content_id": ("content_id", "contentId"),
        }

        for dst, names in aliases.items():
            for name in names:
                values = q.get(name)
                if values and valid_id(values[0]):
                    out[dst] = str(values[0])
                    break

        for special in ("zlink", "report_params"):
            values = q.get(special)
            if not values:
                continue

            parsed = self._decode_special(special, values[0])

            for key, value in parsed.items():
                if value and not out.get(key):
                    out[key] = value

        return out

    def _decode_special(self, kind: str, value: str) -> dict:
        """
        对应 APK Ci() 的核心行为。

        zlink:
          -> decode
          -> zlink query.schemeParams
          -> JSON
          -> vid / content_id / video_id

        report_params:
          -> JSON
          -> content_id / vid / video_id
        """
        out = {
            "series_id": "",
            "video_id": "",
            "vid": "",
            "content_id": "",
        }

        value = decode_many(value)

        try:
            if kind == "zlink":
                inner_q = parse_qs(urlparse(value).query)
                scheme = inner_q.get("schemeParams", [""])[0]
                data = json.loads(decode_many(scheme)) if scheme else {}
            else:
                data = json.loads(value)
        except Exception:
            data = {}

        for key in (
            "series_id",
            "seriesId",
            "video_id",
            "videoId",
            "vid",
            "content_id",
            "contentId",
        ):
            v = data.get(key)

            if not valid_id(v):
                continue

            if key in ("series_id", "seriesId"):
                out["series_id"] = str(v)
            elif key in ("video_id", "videoId"):
                out["video_id"] = str(v)
            elif key == "vid":
                out["vid"] = str(v)
            else:
                out["content_id"] = str(v)

        return out

    @staticmethod
    def _router_page_data(text: str) -> tuple[dict, dict]:
        """
        精确获取 APK xi() 使用的数据结构，而不是递归猜字段。

        返回：
          router_data, page_data
        """
        router = parse_embedded_json(text, "_ROUTER_DATA")

        if not isinstance(router, dict):
            return {}, {}

        loader = router.get("loaderData")

        if not isinstance(loader, dict):
            return router, {}

        # APK 优先使用 video-animation-share_page
        item = loader.get("video-animation-share_page")

        if isinstance(item, dict) and isinstance(item.get("pageData"), dict):
            return router, item["pageData"]

        # 兼容 player_* 页面
        for key, value in loader.items():
            if (
                isinstance(key, str)
                and key.startswith("player_")
                and isinstance(value, dict)
                and isinstance(value.get("pageData"), dict)
            ):
                return router, value["pageData"]

        return router, {}

    def _html_data(self, text: str) -> dict:
        out = {
            "series_id": "",
            "video_id": "",
            "vid": "",
            "content_id": "",
            "chapter_ids": [],
            "title": "",
            "cover": "",
            "intro": "",
            "serial_count": 0,
            "pay_type": None,
            "page_data": {},
        }

        # 1. 严格按 APK 的 landing page 结构读取
        router, page_data = self._router_page_data(text)

        if page_data:
            out["page_data"] = page_data

            chapter_ids = page_data.get("chapter_ids")

            if isinstance(chapter_ids, list):
                out["chapter_ids"] = [
                    str(x)
                    for x in chapter_ids
                    if valid_id(x)
                ]

            series_data = page_data.get("series_data")

            if isinstance(series_data, dict):
                title = (
                    series_data.get("title")
                    or series_data.get("series_name")
                    or ""
                )

                if isinstance(title, str):
                    out["title"] = title.strip()

                cover = (
                    series_data.get("series_cover")
                    or series_data.get("cover")
                    or ""
                )

                if isinstance(cover, str):
                    out["cover"] = cover

                intro = (
                    series_data.get("series_intro")
                    or series_data.get("intro")
                    or ""
                )

                if isinstance(intro, str):
                    out["intro"] = intro

                try:
                    out["serial_count"] = int(
                        series_data.get("serial_count")
                        or len(out["chapter_ids"])
                        or 0
                    )
                except Exception:
                    out["serial_count"] = len(out["chapter_ids"])

                out["pay_type"] = series_data.get("pay_type")

        # 2. SSR_DATA 里可能存 zlink/report_params
        ssr = parse_embedded_json(text, "_SSR_DATA")

        if isinstance(ssr, dict):
            query = (
                ssr.get("context", {})
                .get("request", {})
                .get("query", {})
            )

            if isinstance(query, dict):
                for special in ("zlink", "report_params"):
                    value = query.get(special)

                    if not value:
                        continue

                    parsed = self._decode_special(
                        special,
                        str(value),
                    )

                    for key, v in parsed.items():
                        if v and not out.get(key):
                            out[key] = v

        # 3. 只作为兜底，不覆盖精确 pageData 结果
        for marker in ("_ROUTER_DATA", "_SSR_DATA"):
            obj = parse_embedded_json(text, marker)

            if not obj:
                continue

            for key, names in {
                "series_id": ("series_id", "seriesId"),
                "video_id": ("video_id", "videoId"),
                "vid": ("vid",),
                "content_id": ("content_id", "contentId"),
            }.items():
                value = recursive_first(obj, names)

                if valid_id(value) and not out[key]:
                    out[key] = str(value)

            if not out["chapter_ids"]:
                value = recursive_first(
                    obj,
                    ("chapter_ids", "chapterIds", "vid_list", "vidList"),
                )
                ids = normalize_id_list(value)

                if ids:
                    out["chapter_ids"] = ids

            if not out["title"]:
                value = recursive_first(
                    obj,
                    (
                        "series_name",
                        "seriesName",
                        "title",
                        "book_name",
                        "bookName",
                    ),
                )

                if isinstance(value, str) and value.strip():
                    out["title"] = value.strip()

        # 4. APK Di() 还有 "vid" regex 兜底
        patterns = {
            "series_id": r'"series_id"\s*:\s*"?(\d{15,20})',
            "video_id": r'"video_id"\s*:\s*"?(\d{15,20})',
            "vid": r'"vid"\s*:\s*"?(\d{15,20})',
            "content_id": r'"content_id"\s*:\s*"?(\d{15,20})',
        }

        for key, pattern in patterns.items():
            match = re.search(pattern, text)

            if match and not out[key]:
                out[key] = match.group(1)

        # 5. title 兜底
        if not out["title"]:
            match = re.search(
                r"<title[^>]*>(.*?)</title>",
                text,
                re.S | re.I,
            )

            if match:
                out["title"] = re.sub(
                    r"<[^>]+>",
                    "",
                    match.group(1),
                ).strip()

        return out

    def resolve(self, text: str) -> ShareResult:
        url = first_url(text)

        if not url:
            raise ValueError("没有识别到分享链接。")

        initial = self._query_ids(url)

        try:
            response = self.s.get(
                url,
                timeout=20,
                allow_redirects=True,
            )
            response.raise_for_status()
        except requests.RequestException as e:
            raise RuntimeError(
                f"分享链接请求失败：{e}"
            ) from e

        final_url = response.url
        html = response.text or ""

        final_ids = self._query_ids(final_url)
        html_data = self._html_data(html)

        merged = {}

        for key in (
            "series_id",
            "video_id",
            "vid",
            "content_id",
        ):
            merged[key] = (
                html_data.get(key)
                or final_ids.get(key)
                or initial.get(key)
                or ""
            )

        chapter_ids = html_data.get("chapter_ids") or []

        # APK Mi() 在分享落地页中最重要的结果实际上是 chapter_ids[0]。
        # 它随后把完整 chapter_ids 作为 vidList。
        apk_entry_id = chapter_ids[0] if chapter_ids else ""

        # 保留 content_id，但不把 video_id / vid 错当成全集 episode id。
        content_id = (
            merged["content_id"]
            or merged["video_id"]
            or merged["vid"]
            or apk_entry_id
        )

        metadata = {
            "apk_entry_id": apk_entry_id,
            "chapter_ids": chapter_ids,
            "serial_count": html_data.get("serial_count", 0),
            "cover": html_data.get("cover", ""),
            "intro": html_data.get("intro", ""),
            "pay_type": html_data.get("pay_type"),
            "page_data": html_data.get("page_data", {}),
            "query_initial": initial,
            "query_final": final_ids,
            "id_mapping": (
                "episode N -> chapter_ids[N-1] "
                "(matches APK xi() vidList behavior)"
            ),
        }

        return ShareResult(
            original_url=url,
            final_url=final_url,
            content_id=content_id,
            series_id=merged["series_id"],
            video_id=merged["video_id"],
            vid=merged["vid"],
            title=html_data.get("title", ""),
            html=html,
            metadata=metadata,
        )
