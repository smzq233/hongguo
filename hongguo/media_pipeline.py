from __future__ import annotations

import json
import re
import shutil
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from .models import Episode, SeriesInfo, VideoOption, VideoResolveResult
from .utils import safe_filename

# 复用用户原有 video_decoder_gui.py 中已经验证过的解码逻辑，
# 这里只负责把它接入主流程，不再弹出第二/第三个窗口。
from video_decoder_gui import (
    DEFAULT_UA as DECODER_UA,
    decrypt_main_url,
    derive_cenc_key,
    download_file,
    ffmpeg_decrypt,
)


class JsonArchive:
    """统一保存播放器解析 JSON 与最终选中画质参数。"""

    def __init__(self, root: Path):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def series_dir(self, series: SeriesInfo) -> Path:
        folder = self.root / f"{safe_filename(series.name)}_{series.series_id}"
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    @staticmethod
    def _write(path: Path, data: dict) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)
        return path

    def save_resolve_result(
        self,
        series: SeriesInfo,
        ep: Episode,
        result: VideoResolveResult,
    ) -> Path:
        payload = {
            "episode": ep.index,
            "vid": result.vid,
            "fallback_api": result.fallback_api,
            "options": [asdict(x) for x in result.options],
            "raw_video_model": result.raw_video_model,
            "raw_fallback": result.raw_fallback,
        }
        return self._write(
            self.series_dir(series) / f"episode_{ep.index:03d}_resolve.json",
            payload,
        )

    def save_selected_params(
        self,
        series: SeriesInfo,
        ep: Episode,
        option: VideoOption,
    ) -> Path:
        quality = re.sub(r"[^0-9A-Za-z_-]+", "_", option.label or option.value or "unknown")
        payload = {
            "episode": ep.index,
            "vid": ep.vid or ep.chapter_id,
            "quality": option.label,
            "definition": option.definition,
            "resolution": (
                f"{option.width}x{option.height}"
                if option.width and option.height
                else ""
            ),
            "bitrate": option.bitrate,
            "size_bytes": option.size_bytes,
            "codec": option.codec,
            "main_url": option.raw_url or option.direct_url,
            "key_seed": option.key_seed,
            "spade_a": option.spade_a,
            "protected": option.protected,
            "downloadable": option.downloadable,
        }
        return self._write(
            self.series_dir(series) / f"episode_{ep.index:03d}_{quality}_params.json",
            payload,
        )


class IntegratedMediaPipeline:
    """把“解析 JSON -> 提取参数 -> 下载/处理”接到主 GUI 的单一流程。"""

    def __init__(
        self,
        download_manager,
        archive: JsonArchive,
        *,
        ffmpeg: str = "ffmpeg",
        keep_encrypted: bool = False,
        user_agent: str = DECODER_UA,
    ):
        self.manager = download_manager
        self.archive = archive
        self.ffmpeg = ffmpeg or "ffmpeg"
        self.keep_encrypted = keep_encrypted
        self.user_agent = user_agent or DECODER_UA

    def set_manager(self, manager) -> None:
        self.manager = manager

    @staticmethod
    def choose_option(result: VideoResolveResult, preferred: str = "auto") -> VideoOption:
        options = list(result.options or [])
        if not options:
            raise ValueError("该集没有可用画质信息。")

        def score(x: VideoOption):
            return (
                max(0, x.width) * max(0, x.height),
                max(0, x.height),
                max(0, x.bitrate),
                max(0, x.size_bytes),
            )

        if preferred in ("", "auto", "best", "最高可用"):
            return max(options, key=score)

        normalized = preferred.lower()
        for option in options:
            if option.value.lower() == normalized or option.label.lower() == normalized:
                return option

        # 指定画质不存在时，按“优先向低一级回退”的方式寻找。
        order = ["1080p", "720p", "540p", "480p", "360p"]
        option_map = {x.value.lower(): x for x in options}
        try:
            idx = order.index(normalized)
        except ValueError:
            return max(options, key=score)

        for q in order[idx + 1:]:
            if q in option_map:
                return option_map[q]
        for q in reversed(order[:idx]):
            if q in option_map:
                return option_map[q]
        return max(options, key=score)

    def download(
        self,
        series: SeriesInfo,
        ep: Episode,
        option: VideoOption,
        *,
        progress: Callable[[int, int, float], None] | None = None,
        log: Callable[[str], None] | None = None,
    ) -> Path:
        """根据画质类型自动选择普通下载或用户已有的解码流程。"""
        self.archive.save_selected_params(series, ep, option)

        # 普通 HTTP(S) 未保护资源继续走项目原有 DownloadManager，保留断点续传/HLS/DASH。
        if option.downloadable and not option.protected and not option.has_spade_a:
            return self.manager.download(series, ep, option, progress=progress)

        raw_url = str(option.raw_url or option.direct_url or "").strip()
        if not raw_url:
            raise ValueError("所选画质缺少 main_url。")

        actual_url = raw_url
        if not raw_url.startswith(("http://", "https://")):
            if not option.key_seed:
                raise ValueError("该画质 main_url 为编码值，但缺少 key_seed。")
            decoded = decrypt_main_url(raw_url, option.key_seed)
            actual_url = str(decoded["url"] or "").strip()
            if log:
                log(f"第 {ep.index} 集：main_url 已解析为 HTTP(S) 地址。")

        if not actual_url.startswith(("http://", "https://")):
            raise ValueError(f"解析后的 main_url 不是 HTTP(S) 地址：{actual_url!r}")

        cenc = derive_cenc_key(option.spade_a) if option.spade_a else None
        folder = self.manager.series_dir(series)
        output = folder / f"{ep.index:03d}_第{ep.index:03d}集.mp4"
        if output.exists() and output.stat().st_size > 0:
            return output

        if not cenc:
            self._download_direct(actual_url, output, progress)
            return output

        temp_dir = Path(tempfile.mkdtemp(prefix=f"hongguo_ep{ep.index:03d}_"))
        encrypted = temp_dir / "encrypted_media.bin"
        try:
            self._download_direct(actual_url, encrypted, progress)
            if log:
                log(f"第 {ep.index} 集：媒体下载完成，开始调用 FFmpeg 处理。")
            ffmpeg_decrypt(
                encrypted,
                output,
                cenc["key"],
                self.ffmpeg,
                log_cb=log,
            )
            if self.keep_encrypted:
                kept = output.with_name(output.name + ".encrypted.bin")
                shutil.copy2(encrypted, kept)
                if log:
                    log(f"已保留原始媒体文件：{kept}")
            return output
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def _download_direct(
        self,
        url: str,
        destination: Path,
        progress: Callable[[int, int, float], None] | None,
    ) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        state = {"last": 0, "last_time": None}

        def cb(done: int, total: int):
            if getattr(self.manager, "stop", None) is not None and self.manager.stop.is_set():
                raise InterruptedError("下载已取消")
            if progress is None:
                return
            import time

            now = time.monotonic()
            last_time = state["last_time"]
            last_done = state["last"]
            speed = 0.0
            if last_time is not None and now > last_time:
                speed = max(0.0, done - last_done) / (now - last_time)
            state["last"] = done
            state["last_time"] = now
            progress(done, total, speed)

        download_file(url, destination, self.user_agent, cb)
