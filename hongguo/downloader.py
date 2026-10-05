from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlparse

import requests

from .models import SeriesInfo, Episode, VideoOption
from .utils import safe_filename


CHUNK = 1024 * 1024
APP_UA = "com.phoenix.read/71332"
MAX_RETRIES = 3


class DownloadError(RuntimeError):
    pass


class ProtectedMediaError(DownloadError):
    pass


class DirectMediaDownloader:
    """
    高清公开源下载器。

    支持：
    1. 普通 HTTP(S) 媒体文件：Range 断点续传；
    2. 未加密 HLS：FFmpeg -c copy 无损封装；
    3. 未加密 DASH：FFmpeg -c copy 无损封装。

    不支持：
    - spade_a / CENC / DRM；
    - 带 EXT-X-KEY / SAMPLE-AES 的 HLS；
    - 带 ContentProtection / PSSH 的 DASH。
    """

    def __init__(
        self,
        root: Path,
        session: requests.Session | None = None,
    ):
        self.root = Path(root)
        self.s = session or requests.Session()
        self.stop = threading.Event()
        self._state_lock = threading.Lock()

    def series_dir(self, series: SeriesInfo) -> Path:
        p = self.root / f"{safe_filename(series.name)}_{series.series_id}"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _state_file(self, series: SeriesInfo) -> Path:
        return self.series_dir(series) / "download_state.json"

    def _report_file(self, series: SeriesInfo) -> Path:
        return self.series_dir(series) / "download_report.json"

    def save_info(self, series: SeriesInfo) -> Path:
        p = self.series_dir(series) / "series_info.json"
        p.write_text(
            json.dumps(
                series.to_dict(),
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return p

    @staticmethod
    def _read_json(path: Path) -> dict:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    @staticmethod
    def _write_json_atomic(path: Path, data: dict) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)

    def record_state(
        self,
        series: SeriesInfo,
        ep: Episode,
        *,
        status: str,
        quality: str = "",
        url: str = "",
        downloaded: int = 0,
        total: int = 0,
        error: str = "",
        file_path: str = "",
        media_kind: str = "",
    ) -> None:
        with self._state_lock:
            path = self._state_file(series)
            state = self._read_json(path)
            episodes = state.setdefault("episodes", {})
            episodes[str(ep.index)] = {
                "episode": ep.index,
                "vid": ep.vid or ep.chapter_id,
                "status": status,
                "quality": quality,
                "url": url,
                "downloaded": downloaded,
                "total": total,
                "error": error,
                "file_path": file_path,
                "media_kind": media_kind,
                "updated_at": int(time.time()),
            }
            state["series_id"] = series.series_id
            state["series_name"] = series.name
            self._write_json_atomic(path, state)

    def record_report(
        self,
        series: SeriesInfo,
        ep: Episode,
        *,
        success: bool,
        status: str,
        quality: str = "",
        error: str = "",
        file_path: str = "",
        media_kind: str = "",
    ) -> None:
        with self._state_lock:
            path = self._report_file(series)
            report = self._read_json(path)
            report.setdefault("results", []).append(
                {
                    "episode": ep.index,
                    "vid": ep.vid or ep.chapter_id,
                    "success": success,
                    "status": status,
                    "quality": quality,
                    "error": error,
                    "file_path": file_path,
                    "media_kind": media_kind,
                    "timestamp": int(time.time()),
                }
            )
            report["series_id"] = series.series_id
            report["series_name"] = series.name
            self._write_json_atomic(path, report)

    @staticmethod
    def validate_option(option: VideoOption) -> str:
        if not option:
            raise DownloadError("没有视频画质信息。")

        if option.has_spade_a or option.protected:
            raise ProtectedMediaError(
                "该画质属于受保护媒体，不能作为普通高清源下载。"
            )

        if not option.downloadable:
            raise ProtectedMediaError("该画质未被标记为可直接访问。")

        url = str(option.direct_url or option.raw_url or "")
        if not url.startswith(("http://", "https://")):
            raise ProtectedMediaError("main_url 不是 HTTP(S) 地址。")

        return url

    @staticmethod
    def _headers(series: SeriesInfo, start: int = 0, etag: str = "", last_modified: str = "") -> dict[str, str]:
        headers = {
            "User-Agent": APP_UA,
            "Accept": "*/*",
            "Referer": series.source_url or "https://hongguoduanju.com/",
            "Connection": "keep-alive",
        }
        if start > 0:
            headers["Range"] = f"bytes={start}-"
            if etag:
                headers["If-Range"] = etag
            elif last_modified:
                headers["If-Range"] = last_modified
        return headers

    @staticmethod
    def _find_ffmpeg() -> str:
        env = os.environ.get("HONGGUO_FFMPEG", "").strip()
        candidates = [
            env,
            shutil.which("ffmpeg") or "",
            r"C:\ffmpeg\bin\ffmpeg.exe",
            r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        ]
        for c in candidates:
            if c and Path(c).exists():
                return c
        raise DownloadError(
            "检测到未加密 HLS/DASH，但没有找到 FFmpeg。"
            "请安装 FFmpeg 并加入 PATH，或设置 HONGGUO_FFMPEG。"
        )

    @staticmethod
    def _kind_from_url(url: str) -> str:
        lower = url.lower().split("?", 1)[0]
        if lower.endswith(".m3u8"):
            return "hls"
        if lower.endswith(".mpd"):
            return "dash"
        return "file"

    def _fetch_text(self, url: str, series: SeriesInfo) -> tuple[str, str, str]:
        try:
            r = self.s.get(
                url,
                headers=self._headers(series),
                timeout=(12, 25),
                allow_redirects=True,
            )
            r.raise_for_status()
        except requests.RequestException as e:
            raise DownloadError(f"清单请求失败：{e}") from e
        ctype = (r.headers.get("Content-Type") or "").lower()
        return r.text, r.url, ctype

    @staticmethod
    def _hls_is_encrypted(text: str) -> bool:
        upper = text.upper()
        if "#EXT-X-SESSION-KEY" in upper or "SAMPLE-AES" in upper:
            return True
        for line in text.splitlines():
            line = line.strip().upper()
            if not line.startswith("#EXT-X-KEY:"):
                continue
            m = re.search(r"METHOD=([^,]+)", line)
            if m and m.group(1).strip() != "NONE":
                return True
        return False

    @staticmethod
    def _hls_variants(text: str, base_url: str) -> list[dict]:
        lines = [x.strip() for x in text.splitlines()]
        out = []
        for i, line in enumerate(lines):
            if not line.startswith("#EXT-X-STREAM-INF:"):
                continue
            attrs = line.split(":", 1)[1]
            bandwidth = 0
            width = height = 0
            m = re.search(r"BANDWIDTH=(\d+)", attrs, re.I)
            if m:
                bandwidth = int(m.group(1))
            m = re.search(r"RESOLUTION=(\d+)x(\d+)", attrs, re.I)
            if m:
                width, height = int(m.group(1)), int(m.group(2))
            j = i + 1
            while j < len(lines) and (not lines[j] or lines[j].startswith("#")):
                j += 1
            if j < len(lines):
                out.append(
                    {
                        "url": urljoin(base_url, lines[j]),
                        "bandwidth": bandwidth,
                        "width": width,
                        "height": height,
                    }
                )
        return out

    def _resolve_clear_hls(
        self,
        url: str,
        series: SeriesInfo,
        target_height: int = 0,
    ) -> str:
        """
        检查 HLS master/media playlist，若是 master 则选不高于目标高度
        的最高变体；确认没有 EXT-X-KEY/SAMPLE-AES 后返回最终 playlist。
        """
        current = url
        for _ in range(3):
            text, final_url, _ctype = self._fetch_text(current, series)
            if self._hls_is_encrypted(text):
                raise ProtectedMediaError(
                    "HLS 清单包含 EXT-X-KEY/SAMPLE-AES，属于加密媒体。"
                )
            variants = self._hls_variants(text, final_url)
            if not variants:
                return final_url

            def score(v):
                return (v["height"], v["width"], v["bandwidth"])

            eligible = variants
            if target_height > 0:
                under = [v for v in variants if 0 < v["height"] <= target_height]
                if under:
                    eligible = under
            current = max(eligible, key=score)["url"]

        raise DownloadError("HLS master playlist 层级过深。")

    def _resolve_clear_dash(
        self,
        url: str,
        series: SeriesInfo,
    ) -> str:
        text, final_url, _ctype = self._fetch_text(url, series)
        low = text.lower()
        if (
            "<contentprotection" in low
            or "cenc:" in low
            or "<cenc:pssh" in low
            or "widevine" in low
            or "playready" in low
        ):
            raise ProtectedMediaError(
                "DASH MPD 包含 ContentProtection/PSSH，属于受保护媒体。"
            )
        return final_url

    def probe(
        self,
        series: SeriesInfo,
        option: VideoOption,
    ) -> dict:
        url = self.validate_option(option)
        hint = self._kind_from_url(url)

        if hint == "hls":
            clear = self._resolve_clear_hls(url, series, option.height)
            return {
                "kind": "hls",
                "url": clear,
                "content_type": "application/vnd.apple.mpegurl",
                "content_length": 0,
                "etag": "",
                "last_modified": "",
            }

        if hint == "dash":
            clear = self._resolve_clear_dash(url, series)
            return {
                "kind": "dash",
                "url": clear,
                "content_type": "application/dash+xml",
                "content_length": 0,
                "etag": "",
                "last_modified": "",
            }

        headers = self._headers(series)
        headers["Range"] = "bytes=0-0"
        try:
            r = self.s.get(
                url,
                headers=headers,
                stream=True,
                timeout=(12, 25),
                allow_redirects=True,
            )
        except requests.RequestException as e:
            raise DownloadError(f"媒体地址探测失败：{e}") from e

        try:
            if r.status_code not in (200, 206):
                raise DownloadError(f"媒体地址 HTTP {r.status_code}")

            ctype = (r.headers.get("Content-Type") or "").lower()
            final = r.url
            combined = (ctype + " " + final.lower())

            if "mpegurl" in combined or ".m3u8" in combined:
                r.close()
                clear = self._resolve_clear_hls(final, series, option.height)
                return {
                    "kind": "hls",
                    "url": clear,
                    "content_type": ctype,
                    "content_length": 0,
                    "etag": "",
                    "last_modified": "",
                }

            if "dash+xml" in combined or ".mpd" in combined:
                r.close()
                clear = self._resolve_clear_dash(final, series)
                return {
                    "kind": "dash",
                    "url": clear,
                    "content_type": ctype,
                    "content_length": 0,
                    "etag": "",
                    "last_modified": "",
                }

            if (
                "text/html" in ctype
                or "application/json" in ctype
                or "text/plain" in ctype
            ):
                raise DownloadError(
                    "下载地址没有返回媒体文件："
                    f"Content-Type={ctype or '<空>'}"
                )

            content_range = r.headers.get("Content-Range") or ""
            total = 0
            if "/" in content_range:
                tail = content_range.rsplit("/", 1)[-1]
                if tail.isdigit():
                    total = int(tail)
            if not total:
                try:
                    total = int(r.headers.get("Content-Length") or 0)
                except Exception:
                    total = 0

            return {
                "kind": "file",
                "url": final,
                "status_code": r.status_code,
                "content_type": ctype,
                "content_length": total,
                "accept_ranges": r.headers.get("Accept-Ranges") or "",
                "etag": r.headers.get("ETag") or "",
                "last_modified": r.headers.get("Last-Modified") or "",
            }
        finally:
            try:
                r.close()
            except Exception:
                pass

    def _download_manifest(
        self,
        series: SeriesInfo,
        ep: Episode,
        option: VideoOption,
        manifest_url: str,
        kind: str,
        dest: Path,
        progress: Callable[[int, int, float], None] | None = None,
    ) -> Path:
        ffmpeg = self._find_ffmpeg()
        tmp = dest.with_suffix(".ffmpeg.part.mp4")
        tmp.unlink(missing_ok=True)

        header_blob = (
            f"User-Agent: {APP_UA}\r\n"
            f"Referer: {series.source_url or 'https://hongguoduanju.com/'}\r\n"
        )

        cmd = [
            ffmpeg,
            "-y",
            "-loglevel",
            "warning",
            "-headers",
            header_blob,
            "-i",
            manifest_url,
            "-map",
            "0",
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(tmp),
        ]

        self.record_state(
            series,
            ep,
            status=f"ffmpeg-{kind}",
            quality=option.label,
            url=manifest_url,
            media_kind=kind,
        )

        creationflags = (
            subprocess.CREATE_NO_WINDOW
            if os.name == "nt"
            else 0
        )
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creationflags,
            )
        except OSError as e:
            raise DownloadError(f"启动 FFmpeg 失败：{e}") from e

        last_notice = 0.0
        stderr_lines = []

        while proc.poll() is None:
            if self.stop.is_set():
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except Exception:
                    proc.kill()
                tmp.unlink(missing_ok=True)
                raise DownloadError("用户取消下载。")

            now = time.monotonic()
            if progress and now - last_notice >= 2:
                last_notice = now
                current = tmp.stat().st_size if tmp.exists() else 0
                progress(current, 0, 0.0)
            time.sleep(0.25)

        if proc.stderr:
            stderr_lines = proc.stderr.read().splitlines()[-20:]

        if proc.returncode != 0:
            tmp.unlink(missing_ok=True)
            raise DownloadError(
                "FFmpeg 无损封装失败："
                + (" | ".join(stderr_lines[-5:]) or f"exit={proc.returncode}")
            )

        if not tmp.exists() or tmp.stat().st_size <= 0:
            raise DownloadError("FFmpeg 未生成有效 MP4 文件。")

        if dest.exists():
            dest.unlink()
        tmp.replace(dest)

        self.record_state(
            series,
            ep,
            status="completed",
            quality=option.label,
            url=manifest_url,
            downloaded=dest.stat().st_size,
            total=dest.stat().st_size,
            file_path=str(dest),
            media_kind=kind,
        )
        self.record_report(
            series,
            ep,
            success=True,
            status="completed",
            quality=option.label,
            file_path=str(dest),
            media_kind=kind,
        )
        return dest

    def _download_file(
        self,
        series: SeriesInfo,
        ep: Episode,
        option: VideoOption,
        probe: dict,
        dest: Path,
        progress: Callable[[int, int, float], None] | None = None,
    ) -> Path:
        url = probe["url"]
        part = dest.with_suffix(dest.suffix + ".part")
        meta_path = part.with_suffix(part.suffix + ".json")

        old_meta = self._read_json(meta_path)
        remote_etag = str(probe.get("etag") or "")
        remote_lm = str(probe.get("last_modified") or "")

        start = part.stat().st_size if part.exists() else 0
        if start:
            changed = False
            if old_meta.get("url") and old_meta.get("url") != url:
                changed = True
            elif old_meta.get("etag") and remote_etag and old_meta.get("etag") != remote_etag:
                changed = True
            elif old_meta.get("last_modified") and remote_lm and old_meta.get("last_modified") != remote_lm:
                changed = True
            if changed:
                part.unlink(missing_ok=True)
                meta_path.unlink(missing_ok=True)
                start = 0

        self._write_json_atomic(
            meta_path,
            {
                "url": url,
                "etag": remote_etag,
                "last_modified": remote_lm,
                "content_type": probe.get("content_type", ""),
                "content_length": probe.get("content_length", 0),
                "quality": option.label,
                "episode": ep.index,
                "vid": ep.vid or ep.chapter_id,
            },
        )

        last_error = None
        for attempt in range(1, MAX_RETRIES + 1):
            if self.stop.is_set():
                raise DownloadError("用户取消下载。")

            start = part.stat().st_size if part.exists() else 0
            headers = self._headers(
                series,
                start=start,
                etag=remote_etag,
                last_modified=remote_lm,
            )

            self.record_state(
                series,
                ep,
                status="downloading",
                quality=option.label,
                url=url,
                downloaded=start,
                total=int(probe.get("content_length") or 0),
                media_kind="file",
            )

            try:
                with self.s.get(
                    url,
                    headers=headers,
                    stream=True,
                    timeout=(15, 90),
                    allow_redirects=True,
                ) as r:
                    if start > 0 and r.status_code == 200:
                        start = 0
                        part.unlink(missing_ok=True)

                    if r.status_code not in (200, 206):
                        raise DownloadError(f"下载 HTTP {r.status_code}")

                    ctype = (r.headers.get("Content-Type") or "").lower()
                    if any(x in ctype for x in ("text/html", "application/json", "text/plain")):
                        raise DownloadError(
                            f"地址没有返回媒体文件：Content-Type={ctype}"
                        )

                    try:
                        remaining = int(r.headers.get("Content-Length") or 0)
                    except Exception:
                        remaining = 0
                    total = (
                        start + remaining
                        if remaining
                        else int(probe.get("content_length") or 0)
                    )

                    done = start
                    begin = time.monotonic()
                    mode = "ab" if start else "wb"

                    with open(part, mode) as f:
                        for chunk in r.iter_content(chunk_size=CHUNK):
                            if self.stop.is_set():
                                raise DownloadError("用户取消下载。")
                            if not chunk:
                                continue
                            f.write(chunk)
                            done += len(chunk)
                            if progress:
                                elapsed = max(time.monotonic() - begin, 0.001)
                                progress(done, total, (done - start) / elapsed)

                expected = int(probe.get("content_length") or 0)
                final_size = part.stat().st_size if part.exists() else 0
                if expected > 0 and final_size < expected:
                    raise DownloadError(
                        f"文件大小不足：{final_size}/{expected} bytes"
                    )

                if dest.exists():
                    dest.unlink()
                part.replace(dest)
                meta_path.unlink(missing_ok=True)

                self.record_state(
                    series,
                    ep,
                    status="completed",
                    quality=option.label,
                    url=url,
                    downloaded=dest.stat().st_size,
                    total=expected or dest.stat().st_size,
                    file_path=str(dest),
                    media_kind="file",
                )
                self.record_report(
                    series,
                    ep,
                    success=True,
                    status="completed",
                    quality=option.label,
                    file_path=str(dest),
                    media_kind="file",
                )
                return dest

            except Exception as e:
                last_error = e
                if attempt < MAX_RETRIES:
                    time.sleep(min(1.5 * (2 ** (attempt - 1)), 6))

        text = str(last_error or "未知下载错误")
        self.record_state(
            series,
            ep,
            status="failed",
            quality=option.label,
            url=url,
            downloaded=part.stat().st_size if part.exists() else 0,
            total=int(probe.get("content_length") or 0),
            error=text,
            media_kind="file",
        )
        self.record_report(
            series,
            ep,
            success=False,
            status="failed",
            quality=option.label,
            error=text,
            media_kind="file",
        )
        raise DownloadError(text)

    def download(
        self,
        series: SeriesInfo,
        ep: Episode,
        option: VideoOption,
        progress: Callable[[int, int, float], None] | None = None,
    ) -> Path:
        self.validate_option(option)

        folder = self.series_dir(series)
        dest = folder / f"{ep.index:03d}_第{ep.index:03d}集.mp4"

        if dest.exists() and dest.stat().st_size > 0:
            return dest

        probe = self.probe(series, option)
        kind = probe.get("kind") or "file"

        if kind in ("hls", "dash"):
            return self._download_manifest(
                series,
                ep,
                option,
                probe["url"],
                kind,
                dest,
                progress,
            )

        return self._download_file(
            series,
            ep,
            option,
            probe,
            dest,
            progress,
        )


DownloadManager = DirectMediaDownloader
