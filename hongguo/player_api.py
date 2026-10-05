from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import requests

from .models import VideoOption, VideoResolveResult

ENDPOINT = "https://api5-normal-sinfonlineb.fqnovel.com/novel/player/multi_video_model/v1/"
APP_UA = "com.phoenix.read/71332"

QUALITY_ORDER = [
    ("video_5", "1080P", "1080p", 1920, 1080),
    ("video_4", "720P", "720p", 1280, 720),
    ("video_3", "540P", "540p", 960, 540),
    ("video_2", "480P", "480p", 854, 480),
    ("video_1", "360P", "360p", 640, 360),
]


class PlayerApiError(RuntimeError):
    pass


def _to_int(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        m = __import__("re").search(r"\d+", value)
        if m:
            try:
                return int(m.group(0))
            except Exception:
                return 0
    return 0


class ApkSigner:
    """
    调用从用户提供 APK 中提取的请求签名兼容逻辑。

    只负责生成播放器 API 所需的请求头；不包含会员校验、
    CENC/DRM 密钥提取或媒体解密逻辑。
    """

    def __init__(self, signer_js: Path | None = None):
        self.signer_js = signer_js or Path(__file__).with_name("signer_runtime.js")
        self.node = self._find_node()

    @staticmethod
    def _find_node() -> str:
        env = os.environ.get("HONGGUO_NODE", "").strip()
        candidates = [
            env,
            shutil.which("node") or "",
            r"C:\java\nodejs\node.exe",
            r"C:\Program Files\nodejs\node.exe",
            r"C:\Program Files (x86)\nodejs\node.exe",
        ]
        for c in candidates:
            if c and Path(c).exists():
                return c
        raise PlayerApiError(
            "未找到 Node.js。播放器 API 签名需要 Node.js；"
            "可设置环境变量 HONGGUO_NODE 指向 node.exe。"
        )

    def sign(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.signer_js.exists():
            raise PlayerApiError(f"签名脚本不存在：{self.signer_js}")
        try:
            p = subprocess.run(
                [self.node, str(self.signer_js)],
                input=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                text=True,
                capture_output=True,
                timeout=15,
                encoding="utf-8",
                errors="replace",
                creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
            )
        except subprocess.TimeoutExpired as e:
            raise PlayerApiError("生成播放器 API 签名超时。") from e
        except OSError as e:
            raise PlayerApiError(f"启动 Node.js 失败：{e}") from e

        text = (p.stdout or "").strip()
        try:
            data = json.loads(text)
        except Exception as e:
            raise PlayerApiError(
                "签名脚本返回异常。\n"
                f"stdout={text[:300]}\n"
                f"stderr={(p.stderr or '')[:300]}"
            ) from e
        if not data.get("ok"):
            raise PlayerApiError(f"签名失败：{data.get('error') or 'unknown error'}")
        return data["result"]


class PlayerApiClient:
    """
    对应 APK 的 Ti(vid)：

      vid
        -> multi_video_model/v1
        -> video_model
        -> fallback_api
        -> video_info.data.video_list

    对 fallback 返回的地址采取保守策略：
    - HTTP(S) main_url 且没有 spade_a：作为可直接下载地址；
    - 带 spade_a、非 HTTP(S) 编码 main_url：只展示，标记为受保护；
    - 不执行 APK 后续 sr()/ar() 的受保护媒体解码和 CENC 解密。
    """

    def __init__(self, session: requests.Session | None = None):
        self.s = session or requests.Session()
        self.signer = ApkSigner()

        # APK Ti() 中的默认设备参数。仅用于兼容该公开播放器接口请求。
        self.device_id = "4368802900374539"
        self.iid = "4368802899821595"
        self.device_model = "25053RT47C"
        self.device_brand = "Redmi"

    def _params(self) -> dict[str, str]:
        return {
            "iid": self.iid,
            "device_id": self.device_id,
            "ac": "wifi",
            "channel": "update_64",
            "aid": "8662",
            "app_name": "novelread",
            "version_code": "71332",
            "version_name": "7.1.3.32",
            "device_platform": "android",
            "os": "android",
            "ssmix": "a",
            "device_type": self.device_model,
            "device_brand": self.device_brand,
            "language": "zh",
            "os_api": "36",
            "os_version": "16",
            "manifest_version_code": "71332",
            "resolution": "1280*2772",
            "dpi": "520",
            "update_version_code": "71332",
            "host_abi": "arm64-v8a",
            "dragon_device_type": "phone",
            "pv_player": "71332",
            "compliance_status": "0",
            "need_personal_recommend": "1",
            "player_so_load": "1",
            "is_android_pad_screen": "0",
        }

    @staticmethod
    def _body(vid: str) -> dict[str, Any]:
        return {
            "biz_param": {
                "detail_page_version": 0,
                "device_level": 3,
                "disable_digg_stat": False,
                "need_all_video_definition": True,
                "need_mp4_align": False,
                "use_os_player": False,
                "use_server_dns": False,
                "video_platform": 1024,
            },
            "mixed_video_id_map": {"1004": [str(vid)]},
        }

    def _base_headers(self) -> dict[str, str]:
        return {
            "User-Agent": APP_UA,
            "Accept": "application/json; charset=utf-8,application/x-protobuf",
            "Content-Type": "application/json; charset=UTF-8",
            "x-xs-from-web": "0",
            "x-ss-req-ticket": "0",  # signer/APK headers will overwrite where needed
            "x-tt-request-tag": "t=0;n=0",
            "sdk-version": "2",
            "passport-sdk-version": "50561",
            "x-vc-bdturing-sdk-version": "3.7.2.cn",
        }

    def _device_for_signer(self) -> dict[str, str]:
        return {
            "device_id": self.device_id,
            "iid": self.iid,
            "install_id": self.iid,
            "device_brand": self.device_brand,
            "device_model": self.device_model,
            "device_type": self.device_model,
            "device_manufacturer": self.device_brand,
            "os_version": "16",
            "version_name": "7.1.3.32",
            "ua": APP_UA,
        }

    @staticmethod
    def _fallback_url(video_model: dict[str, Any]) -> str:
        value = video_model.get("fallback_api")
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            nested = value.get("fallback_api") or value.get("url")
            return str(nested or "")
        return ""

    def resolve(self, vid: str) -> VideoResolveResult:
        vid = str(vid or "").strip()
        if not vid:
            raise PlayerApiError("缺少 vid。")

        params = self._params()
        body = self._body(vid)
        body_text = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        headers = self._base_headers()

        signed = self.signer.sign(
            {
                "url": ENDPOINT,
                "params": params,
                "device": self._device_for_signer(),
                "body": body,
                "headers": headers,
                "commonParams": None,
                "extra": "",
            }
        )
        sign_url = signed.get("signUrl") or ""
        signed_headers = signed.get("headers") or {}
        if not sign_url:
            raise PlayerApiError("签名结果缺少 signUrl。")

        # APK 在 Ti() 里会把 request ticket 设置成当前毫秒。
        # signer 生成其余签名后，这里保持一个真实毫秒值更贴近客户端行为。
        import time
        signed_headers["x-ss-req-ticket"] = str(int(time.time() * 1000))

        try:
            r = self.s.post(
                sign_url,
                data=body_text.encode("utf-8"),
                headers=signed_headers,
                timeout=(15, 25),
            )
        except requests.RequestException as e:
            raise PlayerApiError(f"multi_video_model 请求失败：{e}") from e

        if r.status_code < 200 or r.status_code >= 400:
            raise PlayerApiError(
                f"multi_video_model HTTP {r.status_code}：{r.text[:300]}"
            )
        try:
            root = r.json()
        except Exception as e:
            raise PlayerApiError(f"multi_video_model 返回非 JSON：{r.text[:300]}") from e

        if root.get("code") != 0:
            raise PlayerApiError(
                f"multi_video_model 响应异常：code={root.get('code')} "
                f"message={root.get('message') or root.get('msg') or ''}"
            )

        item = (root.get("data") or {}).get(vid)
        if not item:
            # 有些 JSON 解码器/后端可能把 key 变成其他字符串，做一次宽松查找。
            for k, v in (root.get("data") or {}).items():
                if str(k) == vid:
                    item = v
                    break
        if not isinstance(item, dict):
            raise PlayerApiError("API 未返回该 vid 的多画质信息。")

        video_model_raw = item.get("video_model")
        try:
            video_model = (
                json.loads(video_model_raw)
                if isinstance(video_model_raw, str)
                else (video_model_raw or {})
            )
        except Exception as e:
            raise PlayerApiError("video_model 解析失败。") from e

        fallback_api = self._fallback_url(video_model)
        if not fallback_api:
            raise PlayerApiError("video_model 中没有 fallback_api。")

        try:
            fr = self.s.get(
                fallback_api,
                headers={"User-Agent": APP_UA},
                timeout=(15, 25),
            )
        except requests.RequestException as e:
            raise PlayerApiError(f"fallback_api 请求失败：{e}") from e

        if fr.status_code < 200 or fr.status_code >= 400:
            raise PlayerApiError(f"fallback_api HTTP {fr.status_code}：{fr.text[:300]}")
        try:
            froot = fr.json()
        except Exception as e:
            raise PlayerApiError(f"fallback_api 返回非 JSON：{fr.text[:300]}") from e

        info = ((froot.get("video_info") or {}).get("data")) or froot.get("data") or froot
        if not isinstance(info, dict):
            info = {}
        video_list = info.get("video_list") or {}
        parent_key_seed = str(info.get("key_seed") or "")
        if not isinstance(video_list, dict) or not video_list:
            raise PlayerApiError("fallback_api 没有返回 video_list。")

        options: list[VideoOption] = []
        for key, label, value, width, height in QUALITY_ORDER:
            entry = video_list.get(key)
            if not isinstance(entry, dict):
                continue
            raw_url = str(entry.get("main_url") or "")
            if not raw_url:
                continue
            key_seed = str(entry.get("key_seed") or parent_key_seed or "")
            spade_a = str(entry.get("spade_a") or "")
            is_http = raw_url.startswith("http://") or raw_url.startswith("https://")
            has_spade = bool(spade_a)

            bitrate = _to_int(
                entry.get("bitrate")
                or entry.get("vbitrate")
                or entry.get("bit_rate")
                or entry.get("video_bitrate")
                or entry.get("data_rate")
            )
            size_bytes = _to_int(
                entry.get("file_size")
                or entry.get("size")
                or entry.get("video_size")
            )
            codec = str(
                entry.get("codec")
                or entry.get("vcodec")
                or entry.get("codec_type")
                or entry.get("format")
                or ""
            )

            # APK 后续若存在 spade_a，会生成 cencKey 并用 ffmpeg -decryption_key。
            # 本工具不执行该受保护媒体解密，所以这种情况只展示地址。
            protected = has_spade or not is_http
            downloadable = is_http and not has_spade
            direct_url = raw_url if downloadable else ""
            if downloadable:
                status = "可直接下载"
            elif has_spade:
                status = "受保护（存在 CENC/spade_a），仅展示原始地址"
            else:
                status = "编码/受保护地址，仅展示原始值"

            options.append(
                VideoOption(
                    definition=key,
                    label=label,
                    value=value,
                    width=width,
                    height=height,
                    bitrate=bitrate,
                    size_bytes=size_bytes,
                    codec=codec,
                    raw_url=raw_url,
                    direct_url=direct_url,
                    key_seed=key_seed,
                    spade_a=spade_a,
                    has_spade_a=has_spade,
                    downloadable=downloadable,
                    protected=protected,
                    status=status,
                )
            )

        if not options:
            raise PlayerApiError("video_list 中没有找到任何画质。")

        return VideoResolveResult(
            vid=vid,
            fallback_api=fallback_api,
            options=options,
            raw_video_model=video_model,
            raw_fallback=froot,
        )

    @staticmethod
    def choose_downloadable(
        result: VideoResolveResult,
        preferred: str = "auto",
    ) -> VideoOption:
        """
        选择当前接口返回的最高可直接访问画质。

        preferred:
          auto   -> 最高可下载（优先分辨率，其次码率）
          1080p / 720p / 540p / 480p / 360p
        """
        direct = [
            x
            for x in result.options
            if (
                x.downloadable
                and (x.direct_url or x.raw_url).startswith(
                    ("http://", "https://")
                )
                and not x.has_spade_a
                and not x.protected
            )
        ]

        if not direct:
            raise PlayerApiError(
                "该集没有未加密的 HTTP(S) 高清源；"
                "播放器返回的画质均为受保护/编码媒体。"
            )

        def score(x: VideoOption):
            pixels = max(0, x.width) * max(0, x.height)
            return (
                pixels,
                max(0, x.height),
                max(0, x.bitrate),
                max(0, x.size_bytes),
            )

        if preferred in ("", "auto", "best", "最高可下载"):
            return max(direct, key=score)

        for x in direct:
            if x.value == preferred:
                return x

        # 指定画质不可直接下载时：
        # 优先向低一级回退，再考虑更高一级的公开源。
        order = [
            "1080p",
            "720p",
            "540p",
            "480p",
            "360p",
        ]
        try:
            idx = order.index(preferred)
        except ValueError:
            return max(direct, key=score)

        direct_map = {x.value: x for x in direct}

        for q in order[idx + 1:]:
            if q in direct_map:
                return direct_map[q]

        for q in reversed(order[:idx]):
            if q in direct_map:
                return direct_map[q]

        return max(direct, key=score)
