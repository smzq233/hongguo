#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
JSON 媒体参数分析 / 明文媒体下载 GUI

用途：
1. 直接粘贴接口 JSON。
2. 自动识别常见结构：
   - safe_video_model.video_list
   - safe_fallback_response.video_info.data.video_list
   - video_info.data.video_list
   - data.video_list
   - 顶层 video_list
3. 自动整理清晰度、URL、加密标记、spade_a、kid、key_seed 等信息。
4. 对“未受保护 + HTTP(S) 直链”的条目提供直接下载。
5. 对 CENC / spade_a / encrypt=true 等受保护条目仅做参数分析与离线一致性检查，
   不自动执行内容保护绕过或解密。

仅处理你有权访问、下载或用于课程测试的数据。
"""

from __future__ import annotations

import json
import queue
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText


APP_TITLE = "JSON 媒体分析与下载验证工具"
DEFAULT_UA = "Mozilla/5.0 (compatible; CourseMediaAnalyzer/1.0)"


# -----------------------------------------------------------------------------
# 数据模型
# -----------------------------------------------------------------------------


@dataclass
class MediaEntry:
    quality: str = "未知"
    definition: str = ""
    width: Optional[int] = None
    height: Optional[int] = None
    bitrate: Optional[int] = None
    size_bytes: Optional[int] = None
    codec: str = ""
    file_id: str = ""

    # 可能是明文 URL，也可能是加密的字符串
    main_url: str = ""
    raw_main_url: str = ""
    backup_urls: List[str] = None

    # 加密相关元数据
    protected: bool = False
    encrypt: Optional[bool] = None
    encryption_method: str = ""
    spade_a: str = ""
    kid: str = ""

    # 数据来源
    source: str = ""

    # 可选：另一份结构里已经出现的明文 URL，用于课程 fixture 的离线一致性检查
    expected_plain_url: str = ""

    def __post_init__(self) -> None:
        if self.backup_urls is None:
            self.backup_urls = []

    @property
    def display_quality(self) -> str:
        if self.quality and self.quality != "未知":
            return self.quality
        if self.definition:
            return self.definition.upper()
        if self.height:
            return f"{self.height}P"
        return "未知"

    @property
    def resolution(self) -> str:
        if self.width and self.height:
            return f"{self.width}×{self.height}"
        if self.height:
            return f"{self.height}P"
        return ""

    @property
    def is_http_url(self) -> bool:
        return is_http_url(self.main_url)

    @property
    def downloadable(self) -> bool:
        # 自动下载只开放给明确未受保护的 HTTP(S) URL。
        return self.is_http_url and not self.protected


@dataclass
class AnalysisResult:
    episode: Optional[int]
    vid: str
    requested_quality: str
    status: str
    fallback_api: str
    key_seed: str
    entries: List[MediaEntry]
    root_summary: Dict[str, Any]
    warnings: List[str]


# -----------------------------------------------------------------------------
# 通用解析辅助
# -----------------------------------------------------------------------------


def is_http_url(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(("http://", "https://"))


def safe_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def first_nonempty(*values: Any, default: str = "") -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return default


def normalize_quality(value: Any) -> str:
    if value is None:
        return "未知"
    s = str(value).strip()
    if not s:
        return "未知"
    lower = s.lower()
    aliases = {
        "1080p": "1080P",
        "720p": "720P",
        "540p": "540P",
        "480p": "480P",
        "360p": "360P",
        "1080": "1080P",
        "720": "720P",
        "540": "540P",
        "480": "480P",
        "360": "360P",
    }
    return aliases.get(lower, s.upper())


def quality_rank(q: str) -> int:
    digits = "".join(ch for ch in q if ch.isdigit())
    try:
        return int(digits)
    except ValueError:
        return -1


def extract_backup_urls(obj: Dict[str, Any]) -> List[str]:
    result: List[str] = []

    value = obj.get("backup_url")
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str) and item:
                result.append(item)
    elif isinstance(value, str) and value:
        result.append(value)

    for key, value in obj.items():
        if key.startswith("backup_url_") and isinstance(value, str) and value:
            result.append(value)

    # 去重并保持顺序
    seen = set()
    deduped = []
    for item in result:
        if item not in seen:
            seen.add(item)
            deduped.append(item)
    return deduped


def extract_encrypt_fields(item: Dict[str, Any]) -> Tuple[Optional[bool], str, str, str]:
    encrypt_info = item.get("encrypt_info")
    if not isinstance(encrypt_info, dict):
        encrypt_info = {}

    encrypt = item.get("encrypt")
    if encrypt is None:
        encrypt = encrypt_info.get("encrypt")

    encryption_method = first_nonempty(
        item.get("encryption_method"),
        encrypt_info.get("encryption_method"),
    )

    spade_a = first_nonempty(
        item.get("spade_a"),
        encrypt_info.get("spade_a"),
    )

    kid = first_nonempty(
        item.get("kid"),
        encrypt_info.get("kid"),
    )

    if isinstance(encrypt, bool):
        encrypt_bool: Optional[bool] = encrypt
    elif encrypt in (1, "1", "true", "True"):
        encrypt_bool = True
    elif encrypt in (0, "0", "false", "False"):
        encrypt_bool = False
    else:
        encrypt_bool = None

    return encrypt_bool, encryption_method, spade_a, kid


def is_protected_item(
    encrypt: Optional[bool],
    encryption_method: str,
    spade_a: str,
    item: Dict[str, Any],
) -> bool:
    if encrypt is True:
        return True
    if spade_a:
        return True
    if encryption_method:
        lower = encryption_method.lower()
        if any(token in lower for token in ("cenc", "encrypt", "aes-ctr", "widevine", "playready")):
            return True
    stream_type = str(item.get("stream_type", "")).lower()
    if stream_type == "encrypt":
        return True
    return False


def build_entry_from_video_item(
    item: Dict[str, Any],
    source: str,
    forced_quality: str = "",
) -> MediaEntry:
    meta = item.get("video_meta")
    if not isinstance(meta, dict):
        meta = item

    definition = first_nonempty(
        meta.get("definition"),
        item.get("definition"),
    )

    height = safe_int(first_nonempty(
        str(meta.get("vheight", "")) if meta.get("vheight") is not None else "",
        str(item.get("vheight", "")) if item.get("vheight") is not None else "",
    ))

    quality = normalize_quality(
        forced_quality
        or definition
        or (f"{height}P" if height else item.get("quality_type"))
    )

    main_url = first_nonempty(item.get("main_url"))

    encrypt, method, spade_a, kid = extract_encrypt_fields(item)
    protected = is_protected_item(encrypt, method, spade_a, item)

    return MediaEntry(
        quality=quality,
        definition=definition,
        width=safe_int(meta.get("vwidth") or item.get("vwidth")),
        height=safe_int(meta.get("vheight") or item.get("vheight")),
        bitrate=safe_int(meta.get("bitrate") or item.get("bitrate")),
        size_bytes=safe_int(meta.get("size") or item.get("size")),
        codec=first_nonempty(meta.get("codec_type"), item.get("codec_type"), item.get("codec")),
        file_id=first_nonempty(meta.get("file_id"), item.get("file_id")),
        main_url=main_url,
        raw_main_url=main_url if not is_http_url(main_url) else "",
        backup_urls=extract_backup_urls(item),
        protected=protected,
        encrypt=encrypt,
        encryption_method=method,
        spade_a=spade_a,
        kid=kid,
        source=source,
    )


# -----------------------------------------------------------------------------
# 结构识别
# -----------------------------------------------------------------------------


def locate_payload(root: Dict[str, Any]) -> Dict[str, Any]:
    """兼容 video_info.data / data / 顶层。"""
    vi = root.get("video_info")
    if isinstance(vi, dict) and isinstance(vi.get("data"), dict):
        return vi["data"]
    if isinstance(root.get("data"), dict):
        return root["data"]
    return root


def parse_video_list_any(value: Any, source: str) -> List[MediaEntry]:
    entries: List[MediaEntry] = []

    if isinstance(value, list):
        for idx, item in enumerate(value):
            if not isinstance(item, dict):
                continue
            entries.append(build_entry_from_video_item(item, f"{source}[{idx}]"))

    elif isinstance(value, dict):
        key_to_quality = {
            "video_5": "1080P",
            "video_4": "720P",
            "video_3": "540P",
            "video_2": "480P",
            "video_1": "360P",
        }
        for key, item in value.items():
            if not isinstance(item, dict):
                continue
            entries.append(
                build_entry_from_video_item(
                    item,
                    f"{source}.{key}",
                    forced_quality=key_to_quality.get(str(key), ""),
                )
            )

    return entries


def merge_entries(entries: List[MediaEntry]) -> List[MediaEntry]:
    """
    将不同结构中相同清晰度的信息合并。

    典型课程 fixture：
    - safe_fallback_response 的 video_5 有加密 main_url + spade_a
    - safe_video_model 中 1080p 有已经出现的 HTTP(S) main_url

    为避免将它误认为未保护资源：只要任一来源显示 protected，就保留 protected=True。
    HTTP(S) URL 仅作为 expected_plain_url 用于离线一致性检查。
    """
    grouped: Dict[str, MediaEntry] = {}

    def merge_one(base: MediaEntry, other: MediaEntry) -> MediaEntry:
        # 受保护性取 OR
        protected = base.protected or other.protected

        # 如果一份是非 HTTP 的 raw 密文、另一份是 HTTP(S) URL，
        # 把后者作为 expected_plain_url，而不是改写“可自动下载”判断。
        raw_candidate = ""
        http_candidate = ""
        for value in (base.main_url, other.main_url):
            if value:
                if is_http_url(value):
                    http_candidate = http_candidate or value
                else:
                    raw_candidate = raw_candidate or value

        main_url = base.main_url or other.main_url
        raw_main_url = base.raw_main_url or other.raw_main_url or raw_candidate
        expected_plain_url = base.expected_plain_url or other.expected_plain_url

        if protected and raw_candidate and http_candidate:
            main_url = raw_candidate
            raw_main_url = raw_candidate
            expected_plain_url = expected_plain_url or http_candidate
        elif not protected and http_candidate:
            main_url = http_candidate

        backup_urls: List[str] = []
        seen = set()
        for u in (base.backup_urls or []) + (other.backup_urls or []):
            if u not in seen:
                seen.add(u)
                backup_urls.append(u)

        return MediaEntry(
            quality=base.quality if base.quality != "未知" else other.quality,
            definition=base.definition or other.definition,
            width=base.width or other.width,
            height=base.height or other.height,
            bitrate=base.bitrate or other.bitrate,
            size_bytes=base.size_bytes or other.size_bytes,
            codec=base.codec or other.codec,
            file_id=base.file_id or other.file_id,
            main_url=main_url,
            raw_main_url=raw_main_url,
            backup_urls=backup_urls,
            protected=protected,
            encrypt=True if (base.encrypt is True or other.encrypt is True) else (base.encrypt if base.encrypt is not None else other.encrypt),
            encryption_method=base.encryption_method or other.encryption_method,
            spade_a=base.spade_a or other.spade_a,
            kid=base.kid or other.kid,
            source=f"{base.source} + {other.source}",
            expected_plain_url=expected_plain_url,
        )

    for entry in entries:
        key = entry.display_quality
        if key not in grouped:
            grouped[key] = entry
        else:
            grouped[key] = merge_one(grouped[key], entry)

    result = list(grouped.values())
    result.sort(key=lambda x: quality_rank(x.display_quality), reverse=True)
    return result


def analyze_json(root: Any) -> AnalysisResult:
    if not isinstance(root, dict):
        raise ValueError("JSON 顶层必须是对象（{...}）。")

    warnings: List[str] = []
    entries: List[MediaEntry] = []

    episode = safe_int(root.get("episode"))
    vid = first_nonempty(root.get("vid"), root.get("video_id"))
    requested_quality = normalize_quality(root.get("quality"))
    status = first_nonempty(root.get("status"))
    fallback_api = first_nonempty(root.get("fallback_api"))

    # 1) safe_video_model.video_list
    svm = root.get("safe_video_model")
    if isinstance(svm, dict):
        fallback_api = fallback_api or first_nonempty(svm.get("fallback_api"))
        if isinstance(svm.get("video_list"), (list, dict)):
            entries.extend(parse_video_list_any(svm["video_list"], "safe_video_model.video_list"))

    # 2) safe_fallback_response -> video_info.data
    key_seed = ""
    sfr = root.get("safe_fallback_response")
    if isinstance(sfr, dict):
        payload = locate_payload(sfr)
        key_seed = first_nonempty(payload.get("key_seed"))
        if isinstance(payload.get("video_list"), (list, dict)):
            entries.extend(parse_video_list_any(payload["video_list"], "safe_fallback_response.video_info.data.video_list"))

    # 3) 顶层本身也可能就是响应
    payload = locate_payload(root)
    if payload is not root or isinstance(root.get("video_list"), (list, dict)):
        key_seed = key_seed or first_nonempty(payload.get("key_seed"))
        if isinstance(payload.get("video_list"), (list, dict)):
            entries.extend(parse_video_list_any(payload["video_list"], "video_info/data.video_list"))

    # 4) 摘要形式（没有 video_list，只给一个 media_url/raw_url）
    if not entries:
        raw_url = first_nonempty(root.get("main_url_raw"), root.get("raw_url"), root.get("media_url"), root.get("main_url"))
        if raw_url:
            has_spade = bool(root.get("has_spade_a"))
            protected = has_spade or "encrypt" in status.lower() or "受保护" in status
            entries.append(
                MediaEntry(
                    quality=requested_quality,
                    width=None,
                    height=None,
                    bitrate=safe_int(root.get("bitrate")),
                    size_bytes=safe_int(root.get("size_bytes")),
                    codec=first_nonempty(root.get("codec")),
                    main_url=raw_url,
                    raw_main_url=raw_url if not is_http_url(raw_url) else "",
                    protected=protected,
                    encrypt=True if protected else None,
                    source="top-level summary",
                )
            )

    entries = merge_entries(entries)

    if not entries:
        warnings.append("没有识别到 video_list 或可用的 main_url/media_url。")

    if root.get("key_seed_present") is True and not key_seed:
        warnings.append("数据声明 key_seed_present=true，但当前 JSON 中没有 key_seed 实际值。")

    if root.get("has_spade_a") is True and not any(e.spade_a for e in entries):
        warnings.append("数据声明 has_spade_a=true，但当前 JSON 中没有 spade_a 实际值。")

    if any(e.protected for e in entries):
        warnings.append("检测到受保护媒体（CENC/spade_a/encrypt）。工具只分析参数，不自动执行解密或绕过内容保护。")

    root_summary = {
        "episode": root.get("episode"),
        "vid": root.get("vid"),
        "quality": root.get("quality"),
        "status": root.get("status"),
        "downloadable": root.get("downloadable"),
        "has_spade_a": root.get("has_spade_a"),
        "key_seed_present": root.get("key_seed_present"),
        "address_type": root.get("address_type"),
        "resolution": root.get("resolution"),
        "codec": root.get("codec"),
        "size_bytes": root.get("size_bytes"),
    }

    return AnalysisResult(
        episode=episode,
        vid=vid,
        requested_quality=requested_quality,
        status=status,
        fallback_api=fallback_api,
        key_seed=key_seed,
        entries=entries,
        root_summary=root_summary,
        warnings=warnings,
    )


# -----------------------------------------------------------------------------
# 下载（仅未受保护 HTTP(S) URL）
# -----------------------------------------------------------------------------


def download_plain_media(
    url: str,
    destination: Path,
    user_agent: str,
    progress_cb,
    log_cb,
    stop_event: threading.Event,
) -> None:
    if not is_http_url(url):
        raise ValueError("所选条目不是 HTTP(S) URL。")

    destination.parent.mkdir(parents=True, exist_ok=True)

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept": "*/*",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            total_header = response.headers.get("Content-Length")
            total = safe_int(total_header)
            downloaded = 0
            started = time.time()

            log_cb(f"HTTP {getattr(response, 'status', 'OK')}")
            log_cb(f"Content-Type: {response.headers.get('Content-Type', '(unknown)')}")
            if total:
                log_cb(f"Content-Length: {total:,} bytes")

            with destination.open("wb") as f:
                while True:
                    if stop_event.is_set():
                        raise InterruptedError("下载已取消")

                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break

                    f.write(chunk)
                    downloaded += len(chunk)

                    elapsed = max(time.time() - started, 0.001)
                    speed = downloaded / elapsed

                    if total and total > 0:
                        percent = downloaded / total * 100.0
                    else:
                        percent = None

                    progress_cb(downloaded, total, percent, speed)

            progress_cb(downloaded, total, 100.0 if total else None, 0.0)

    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP 下载失败：{exc.code} {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"网络连接失败：{exc.reason}") from exc


# -----------------------------------------------------------------------------
# GUI
# -----------------------------------------------------------------------------


class MediaAnalyzerApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()

        self.title(APP_TITLE)
        self.geometry("1180x850")
        self.minsize(900, 650)

        self.result: Optional[AnalysisResult] = None
        self.entry_by_iid: Dict[str, MediaEntry] = {}
        self.selected_entry: Optional[MediaEntry] = None

        self.worker_queue: queue.Queue = queue.Queue()
        self.download_stop = threading.Event()
        self.download_thread: Optional[threading.Thread] = None

        self._build_ui()
        self.after(100, self._poll_worker_queue)

    # ------------------------------------------------------------------
    # UI 构建
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=10)
        root.pack(fill="both", expand=True)

        paned = ttk.Panedwindow(root, orient="vertical")
        paned.pack(fill="both", expand=True)

        upper = ttk.Frame(paned)
        lower = ttk.Frame(paned)
        paned.add(upper, weight=3)
        paned.add(lower, weight=2)

        # --------------------------------------------------------------
        # 输入区
        # --------------------------------------------------------------
        input_frame = ttk.LabelFrame(upper, text="1. 粘贴 JSON")
        input_frame.pack(fill="both", expand=True, pady=(0, 8))

        toolbar = ttk.Frame(input_frame)
        toolbar.pack(fill="x", padx=8, pady=(8, 4))

        ttk.Button(toolbar, text="分析粘贴内容", command=self.analyze_input).pack(side="left", padx=(0, 6))
        ttk.Button(toolbar, text="打开 JSON 文件", command=self.open_json_file).pack(side="left", padx=(0, 6))
        ttk.Button(toolbar, text="载入课程示例结构", command=self.load_example).pack(side="left", padx=(0, 6))
        ttk.Button(toolbar, text="清空", command=self.clear_all).pack(side="left", padx=(0, 6))

        self.input_text = ScrolledText(input_frame, wrap="none", height=13, font=("Consolas", 10))
        self.input_text.pack(fill="both", expand=True, padx=8, pady=(4, 8))

        # --------------------------------------------------------------
        # 分析结果表格
        # --------------------------------------------------------------
        result_frame = ttk.LabelFrame(upper, text="2. 自动识别的媒体条目")
        result_frame.pack(fill="both", expand=True)

        columns = (
            "quality",
            "resolution",
            "size",
            "codec",
            "protected",
            "method",
            "url_type",
            "source",
        )

        self.tree = ttk.Treeview(result_frame, columns=columns, show="headings", height=8)
        headings = {
            "quality": "画质",
            "resolution": "分辨率",
            "size": "大小",
            "codec": "编码",
            "protected": "保护",
            "method": "加密方式",
            "url_type": "地址类型",
            "source": "来源",
        }
        widths = {
            "quality": 75,
            "resolution": 95,
            "size": 105,
            "codec": 95,
            "protected": 75,
            "method": 120,
            "url_type": 105,
            "source": 260,
        }

        for col in columns:
            self.tree.heading(col, text=headings[col])
            self.tree.column(col, width=widths[col], anchor="w")

        yscroll = ttk.Scrollbar(result_frame, orient="vertical", command=self.tree.yview)
        xscroll = ttk.Scrollbar(result_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)

        self.tree.grid(row=0, column=0, sticky="nsew", padx=(8, 0), pady=8)
        yscroll.grid(row=0, column=1, sticky="ns", pady=8)
        xscroll.grid(row=1, column=0, sticky="ew", padx=(8, 0))

        result_frame.rowconfigure(0, weight=1)
        result_frame.columnconfigure(0, weight=1)

        self.tree.bind("<<TreeviewSelect>>", self.on_tree_select)

        # --------------------------------------------------------------
        # 下半区：详情 + 日志
        # --------------------------------------------------------------
        lower_paned = ttk.Panedwindow(lower, orient="horizontal")
        lower_paned.pack(fill="both", expand=True)

        detail_frame = ttk.LabelFrame(lower_paned, text="3. 当前条目详情")
        log_frame = ttk.LabelFrame(lower_paned, text="日志 / 警告")
        lower_paned.add(detail_frame, weight=3)
        lower_paned.add(log_frame, weight=2)

        detail_toolbar = ttk.Frame(detail_frame)
        detail_toolbar.pack(fill="x", padx=8, pady=(8, 4))

        self.download_button = ttk.Button(
            detail_toolbar,
            text="下载未受保护媒体",
            command=self.download_selected,
            state="disabled",
        )
        self.download_button.pack(side="left", padx=(0, 6))

        self.cancel_button = ttk.Button(
            detail_toolbar,
            text="取消下载",
            command=self.cancel_download,
            state="disabled",
        )
        self.cancel_button.pack(side="left", padx=(0, 6))

        ttk.Button(
            detail_toolbar,
            text="保存分析报告",
            command=self.save_report,
        ).pack(side="left", padx=(0, 6))

        self.detail_text = ScrolledText(detail_frame, wrap="word", font=("Consolas", 10))
        self.detail_text.pack(fill="both", expand=True, padx=8, pady=(4, 8))
        self.detail_text.configure(state="disabled")

        progress_frame = ttk.Frame(detail_frame)
        progress_frame.pack(fill="x", padx=8, pady=(0, 8))

        self.progress = ttk.Progressbar(progress_frame, mode="determinate", maximum=100)
        self.progress.pack(side="left", fill="x", expand=True)

        self.progress_label = ttk.Label(progress_frame, text="未开始")
        self.progress_label.pack(side="left", padx=(8, 0))

        self.log_text = ScrolledText(log_frame, wrap="word", font=("Consolas", 9))
        self.log_text.pack(fill="both", expand=True, padx=8, pady=8)
        self.log_text.configure(state="disabled")

        # 状态栏
        self.status_var = tk.StringVar(value="请粘贴 JSON，然后点击“分析粘贴内容”。")
        status = ttk.Label(root, textvariable=self.status_var, anchor="w")
        status.pack(fill="x", pady=(8, 0))

    # ------------------------------------------------------------------
    # 输入 / 分析
    # ------------------------------------------------------------------

    def set_log(self, text: str, append: bool = True) -> None:
        self.log_text.configure(state="normal")
        if not append:
            self.log_text.delete("1.0", "end")
        self.log_text.insert("end", text.rstrip() + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def set_detail(self, text: str) -> None:
        self.detail_text.configure(state="normal")
        self.detail_text.delete("1.0", "end")
        self.detail_text.insert("1.0", text)
        self.detail_text.configure(state="disabled")

    def clear_table(self) -> None:
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self.entry_by_iid.clear()
        self.selected_entry = None
        self.download_button.configure(state="disabled")

    def analyze_input(self) -> None:
        raw = self.input_text.get("1.0", "end").strip()
        if not raw:
            messagebox.showwarning("没有内容", "请先粘贴 JSON。")
            return

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            messagebox.showerror(
                "JSON 格式错误",
                f"第 {exc.lineno} 行，第 {exc.colno} 列：{exc.msg}",
            )
            return

        try:
            result = analyze_json(data)
        except Exception as exc:
            messagebox.showerror("分析失败", str(exc))
            return

        self.result = result
        self.clear_table()
        self.set_log("", append=False)

        self.set_log("JSON 分析完成。")
        self.set_log(f"识别到 {len(result.entries)} 个媒体条目。")

        if result.episode is not None:
            self.set_log(f"episode: {result.episode}")
        if result.vid:
            self.set_log(f"vid: {result.vid}")
        if result.key_seed:
            self.set_log(f"key_seed: 已找到（{len(result.key_seed)} 字符）")
        else:
            self.set_log("key_seed: 未找到")

        for warning in result.warnings:
            self.set_log(f"警告: {warning}")

        for index, entry in enumerate(result.entries):
            iid = f"m{index}"
            self.entry_by_iid[iid] = entry
            self.tree.insert(
                "",
                "end",
                iid=iid,
                values=(
                    entry.display_quality,
                    entry.resolution or "-",
                    format_size(entry.size_bytes),
                    entry.codec or "-",
                    "是" if entry.protected else "否",
                    entry.encryption_method or "-",
                    "HTTP(S)" if entry.is_http_url else "非直链/密文",
                    entry.source,
                ),
            )

        # 尽量选中请求画质；否则选最高画质
        target_iid = None
        if result.requested_quality and result.requested_quality != "未知":
            for iid, entry in self.entry_by_iid.items():
                if entry.display_quality == result.requested_quality:
                    target_iid = iid
                    break

        if target_iid is None and self.entry_by_iid:
            target_iid = next(iter(self.entry_by_iid.keys()))

        if target_iid:
            self.tree.selection_set(target_iid)
            self.tree.focus(target_iid)
            self.tree.see(target_iid)
            self.on_tree_select(None)
        else:
            self.set_detail(self._build_overview_text(result))

        self.status_var.set(f"分析完成：{len(result.entries)} 个媒体条目。")

    def open_json_file(self) -> None:
        path = filedialog.askopenfilename(
            title="选择 JSON 文件",
            filetypes=[
                ("JSON 文件", "*.json"),
                ("文本文件", "*.txt"),
                ("所有文件", "*.*"),
            ],
        )
        if not path:
            return

        try:
            text = Path(path).read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = Path(path).read_text(encoding="utf-8-sig")
        except Exception as exc:
            messagebox.showerror("读取失败", str(exc))
            return

        self.input_text.delete("1.0", "end")
        self.input_text.insert("1.0", text)
        self.analyze_input()

    def load_example(self) -> None:
        example = {
            "episode": 1,
            "vid": "teaching_example",
            "quality": "1080P",
            "status": "课程示例：未受保护直链",
            "safe_video_model": {
                "video_list": [
                    {
                        "main_url": "https://example.edu/course/sample.mp4",
                        "video_meta": {
                            "definition": "1080p",
                            "vwidth": 1920,
                            "vheight": 1080,
                            "bitrate": 2500000,
                            "size": 12345678,
                            "codec_type": "h264",
                        },
                        "encrypt_info": {
                            "encrypt": False,
                        },
                    },
                    {
                        "main_url": "https://example.edu/course/sample-720.mp4",
                        "video_meta": {
                            "definition": "720p",
                            "vwidth": 1280,
                            "vheight": 720,
                            "bitrate": 1400000,
                            "size": 7654321,
                            "codec_type": "h264",
                        },
                        "encrypt_info": {
                            "encrypt": False,
                        },
                    },
                ]
            },
        }
        self.input_text.delete("1.0", "end")
        self.input_text.insert("1.0", json.dumps(example, ensure_ascii=False, indent=2))
        self.analyze_input()

    def clear_all(self) -> None:
        if self.download_thread and self.download_thread.is_alive():
            messagebox.showwarning("正在下载", "请先取消或等待当前下载结束。")
            return
        self.input_text.delete("1.0", "end")
        self.clear_table()
        self.set_detail("")
        self.set_log("", append=False)
        self.progress["value"] = 0
        self.progress_label.configure(text="未开始")
        self.status_var.set("请粘贴 JSON，然后点击“分析粘贴内容”。")
        self.result = None

    # ------------------------------------------------------------------
    # 条目详情
    # ------------------------------------------------------------------

    def on_tree_select(self, _event) -> None:
        selection = self.tree.selection()
        if not selection:
            return

        entry = self.entry_by_iid.get(selection[0])
        if entry is None:
            return

        self.selected_entry = entry
        self.set_detail(self._build_entry_detail(entry))

        if entry.downloadable:
            self.download_button.configure(state="normal")
            self.status_var.set(f"{entry.display_quality}：未检测到内容保护，可直接下载 HTTP(S) URL。")
        else:
            self.download_button.configure(state="disabled")
            if entry.protected:
                self.status_var.set(
                    f"{entry.display_quality}：检测到受保护媒体；仅展示和验证参数，不自动解密下载。"
                )
            else:
                self.status_var.set(f"{entry.display_quality}：当前地址不是 HTTP(S) 直链。")

    def _build_overview_text(self, result: AnalysisResult) -> str:
        lines = [
            "===== JSON 概览 =====",
            f"episode: {result.episode if result.episode is not None else '-'}",
            f"vid: {result.vid or '-'}",
            f"请求画质: {result.requested_quality or '-'}",
            f"状态: {result.status or '-'}",
            f"fallback_api: {result.fallback_api or '-'}",
            f"key_seed: {result.key_seed or '(未找到)'}",
            "",
            "警告:",
        ]
        if result.warnings:
            lines.extend(f"- {w}" for w in result.warnings)
        else:
            lines.append("- 无")
        return "\n".join(lines)

    def _build_entry_detail(self, entry: MediaEntry) -> str:
        lines = [
            "===== 当前媒体条目 =====",
            f"画质: {entry.display_quality}",
            f"分辨率: {entry.resolution or '-'}",
            f"码率: {format_bitrate(entry.bitrate)}",
            f"大小: {format_size(entry.size_bytes)}",
            f"编码: {entry.codec or '-'}",
            f"file_id: {entry.file_id or '-'}",
            f"来源: {entry.source}",
            "",
            "===== 内容保护 =====",
            f"protected: {entry.protected}",
            f"encrypt: {entry.encrypt}",
            f"encryption_method: {entry.encryption_method or '-'}",
            f"kid: {entry.kid or '-'}",
            f"spade_a: {entry.spade_a or '-'}",
            "",
            "===== 地址 =====",
            f"main_url 类型: {'HTTP(S)' if entry.is_http_url else '非 HTTP(S) / 密文'}",
            f"main_url:\n{entry.main_url or '-'}",
        ]

        if entry.raw_main_url and entry.raw_main_url != entry.main_url:
            lines.extend(["", f"raw_main_url:\n{entry.raw_main_url}"])

        if entry.expected_plain_url:
            lines.extend([
                "",
                "课程 fixture 中同时发现了另一份 HTTP(S) URL：",
                entry.expected_plain_url,
                "",
                "说明：该 URL 仅作为结构/一致性对照显示；当前条目仍按受保护媒体处理。",
            ])

        if entry.backup_urls:
            lines.append("")
            lines.append("backup_urls:")
            lines.extend(f"  - {u}" for u in entry.backup_urls)

        if self.result:
            lines.extend([
                "",
                "===== 全局参数 =====",
                f"key_seed: {self.result.key_seed or '(未找到)'}",
                f"fallback_api: {self.result.fallback_api or '-'}",
            ])

        lines.append("")
        if entry.downloadable:
            lines.append("状态：未检测到内容保护，并且存在 HTTP(S) 直链，可使用“下载未受保护媒体”。")
        elif entry.protected:
            lines.append("状态：检测到 CENC/spade_a/encrypt 等保护标记；工具不会自动执行解密或绕过保护。")
        else:
            lines.append("状态：没有可直接下载的 HTTP(S) URL。")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 下载
    # ------------------------------------------------------------------

    def download_selected(self) -> None:
        entry = self.selected_entry
        if entry is None:
            messagebox.showwarning("未选择", "请先选择一个媒体条目。")
            return

        if entry.protected:
            messagebox.showwarning(
                "受保护媒体",
                "当前条目带有 CENC/spade_a/encrypt 等内容保护标记。\n\n"
                "工具可以展示和验证参数，但不会自动执行解密或绕过内容保护。",
            )
            return

        if not entry.is_http_url:
            messagebox.showwarning("不可下载", "当前条目不是 HTTP(S) 直链。")
            return

        if self.download_thread and self.download_thread.is_alive():
            messagebox.showwarning("下载中", "已有下载任务正在运行。")
            return

        default_name = build_default_filename(self.result, entry)
        path = filedialog.asksaveasfilename(
            title="保存媒体文件",
            initialfile=default_name,
            defaultextension=".mp4",
            filetypes=[
                ("MP4 视频", "*.mp4"),
                ("所有文件", "*.*"),
            ],
        )
        if not path:
            return

        self.download_stop.clear()
        self.progress["value"] = 0
        self.progress_label.configure(text="准备下载…")
        self.download_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.status_var.set("正在下载未受保护媒体…")
        self.set_log(f"开始下载: {entry.main_url}")
        self.set_log(f"保存到: {path}")

        url = entry.main_url
        destination = Path(path)

        def progress_cb(downloaded, total, percent, speed):
            self.worker_queue.put(("progress", downloaded, total, percent, speed))

        def log_cb(message):
            self.worker_queue.put(("log", message))

        def worker():
            try:
                download_plain_media(
                    url=url,
                    destination=destination,
                    user_agent=DEFAULT_UA,
                    progress_cb=progress_cb,
                    log_cb=log_cb,
                    stop_event=self.download_stop,
                )
                self.worker_queue.put(("done", str(destination)))
            except InterruptedError as exc:
                try:
                    if destination.exists():
                        destination.unlink()
                except OSError:
                    pass
                self.worker_queue.put(("cancelled", str(exc)))
            except Exception as exc:
                self.worker_queue.put(("error", str(exc)))

        self.download_thread = threading.Thread(target=worker, daemon=True)
        self.download_thread.start()

    def cancel_download(self) -> None:
        if self.download_thread and self.download_thread.is_alive():
            self.download_stop.set()
            self.status_var.set("正在取消下载…")
            self.set_log("已请求取消下载。")

    def _poll_worker_queue(self) -> None:
        try:
            while True:
                event = self.worker_queue.get_nowait()
                kind = event[0]

                if kind == "progress":
                    _, downloaded, total, percent, speed = event
                    if percent is not None:
                        self.progress.configure(mode="determinate")
                        self.progress["value"] = max(0.0, min(100.0, percent))
                        self.progress_label.configure(
                            text=f"{percent:5.1f}% · {format_size(downloaded)} · {format_speed(speed)}"
                        )
                    else:
                        self.progress.configure(mode="indeterminate")
                        if not self.progress.instate(["!disabled"]):
                            pass
                        self.progress_label.configure(
                            text=f"{format_size(downloaded)} · {format_speed(speed)}"
                        )

                elif kind == "log":
                    self.set_log(str(event[1]))

                elif kind == "done":
                    path = event[1]
                    self.progress.configure(mode="determinate")
                    self.progress["value"] = 100
                    self.progress_label.configure(text="100% · 完成")
                    self.cancel_button.configure(state="disabled")
                    if self.selected_entry and self.selected_entry.downloadable:
                        self.download_button.configure(state="normal")
                    self.status_var.set(f"下载完成：{path}")
                    self.set_log(f"下载完成: {path}")
                    messagebox.showinfo("下载完成", f"文件已保存：\n{path}")

                elif kind == "cancelled":
                    self.progress.configure(mode="determinate")
                    self.progress["value"] = 0
                    self.progress_label.configure(text="已取消")
                    self.cancel_button.configure(state="disabled")
                    if self.selected_entry and self.selected_entry.downloadable:
                        self.download_button.configure(state="normal")
                    self.status_var.set("下载已取消。")
                    self.set_log("下载已取消。")

                elif kind == "error":
                    error = event[1]
                    self.progress.configure(mode="determinate")
                    self.progress["value"] = 0
                    self.progress_label.configure(text="失败")
                    self.cancel_button.configure(state="disabled")
                    if self.selected_entry and self.selected_entry.downloadable:
                        self.download_button.configure(state="normal")
                    self.status_var.set("下载失败。")
                    self.set_log(f"下载失败: {error}")
                    messagebox.showerror("下载失败", str(error))

        except queue.Empty:
            pass
        finally:
            self.after(100, self._poll_worker_queue)

    # ------------------------------------------------------------------
    # 报告导出
    # ------------------------------------------------------------------

    def save_report(self) -> None:
        if self.result is None:
            messagebox.showwarning("没有分析结果", "请先分析 JSON。")
            return

        path = filedialog.asksaveasfilename(
            title="保存分析报告",
            initialfile="media_analysis_report.json",
            defaultextension=".json",
            filetypes=[("JSON 文件", "*.json"), ("所有文件", "*.*")],
        )
        if not path:
            return

        report = {
            "episode": self.result.episode,
            "vid": self.result.vid,
            "requested_quality": self.result.requested_quality,
            "status": self.result.status,
            "fallback_api": self.result.fallback_api,
            "key_seed": self.result.key_seed,
            "warnings": self.result.warnings,
            "entries": [asdict(e) for e in self.result.entries],
        }

        try:
            Path(path).write_text(
                json.dumps(report, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc))
            return

        self.set_log(f"分析报告已保存: {path}")
        messagebox.showinfo("保存完成", f"报告已保存：\n{path}")


# -----------------------------------------------------------------------------
# 格式化
# -----------------------------------------------------------------------------


def format_size(value: Optional[int]) -> str:
    if value is None:
        return "-"
    size = float(value)
    units = ["B", "KB", "MB", "GB", "TB"]
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size):,} {unit}"
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{value} B"


def format_bitrate(value: Optional[int]) -> str:
    if value is None:
        return "-"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f} Mbps"
    if value >= 1_000:
        return f"{value / 1_000:.1f} kbps"
    return f"{value} bps"


def format_speed(value: float) -> str:
    if not value or value <= 0:
        return "-"
    return f"{format_size(int(value))}/s"


def build_default_filename(result: Optional[AnalysisResult], entry: MediaEntry) -> str:
    parts: List[str] = []
    if result and result.episode is not None:
        parts.append(f"episode_{result.episode}")
    if result and result.vid:
        parts.append(result.vid)
    parts.append(entry.display_quality.lower())
    safe = "_".join(parts) or "video"
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in safe)
    return safe + ".mp4"


# -----------------------------------------------------------------------------
# 主入口
# -----------------------------------------------------------------------------


def main() -> None:
    app = MediaAnalyzerApp()
    app.mainloop()


if __name__ == "__main__":
    main()
