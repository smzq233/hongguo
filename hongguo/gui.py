from __future__ import annotations

import concurrent.futures
import json
import os
import queue
import threading
import time
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import requests

from .share_parser import ShareParser
from .series_parser import SeriesParser
from .player_api import PlayerApiClient, PlayerApiError
from .downloader import DownloadManager
from .app_logging import setup_logging
from .media_pipeline import JsonArchive, IntegratedMediaPipeline


QUALITY_CHOICES = [
    ("auto", "自动最高可用", "自动选择该集实际返回的最高画质"),
    ("1080p", "1080P · 全高清", "优先 1080P，不存在时向较低画质回退"),
    ("720p", "720P · 高清", "优先 720P，不存在时向较低画质回退"),
    ("540p", "540P · 清晰", "优先 540P，不存在时向较低画质回退"),
    ("480p", "480P · 标清", "优先 480P，不存在时向较低画质回退"),
    ("360p", "360P · 流畅", "优先 360P"),
]

QUALITY_DISPLAY = {value: label for value, label, _ in QUALITY_CHOICES}


def quality_display(value: str) -> str:
    value = str(value or "").strip()
    low = value.lower()
    if low in QUALITY_DISPLAY:
        return QUALITY_DISPLAY[low]
    if low.endswith("p") and low[:-1].isdigit():
        return value.upper()
    return value or "未知画质"


class QualityPickerDialog(tk.Toplevel):
    """下载前的一次性画质选择窗口。"""

    def __init__(self, master, current="auto"):
        super().__init__(master)
        self.title("选择下载画质")
        self.resizable(False, False)
        self.transient(master)
        self.result = None
        self.var = tk.StringVar(value=current if current in QUALITY_DISPLAY else "auto")

        box = ttk.Frame(self, padding=16)
        box.pack(fill="both", expand=True)
        ttk.Label(
            box,
            text="请选择本次下载使用的画质",
            font=("Microsoft YaHei UI", 11, "bold"),
        ).pack(anchor="w", pady=(0, 10))

        for value, label, desc in QUALITY_CHOICES:
            card = ttk.Frame(box, padding=(8, 5))
            card.pack(fill="x")
            ttk.Radiobutton(
                card,
                text=label,
                value=value,
                variable=self.var,
            ).pack(side="left")
            ttk.Label(card, text=desc).pack(side="left", padx=(14, 0))

        ttk.Separator(box).pack(fill="x", pady=10)
        buttons = ttk.Frame(box)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="取消", command=self._cancel).pack(side="right")
        ttk.Button(buttons, text="确定下载", command=self._ok).pack(side="right", padx=(0, 8))

        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.bind("<Return>", lambda _e: self._ok())
        self.bind("<Escape>", lambda _e: self._cancel())
        self.update_idletasks()
        x = master.winfo_rootx() + max(0, (master.winfo_width() - self.winfo_width()) // 2)
        y = master.winfo_rooty() + max(0, (master.winfo_height() - self.winfo_height()) // 3)
        self.geometry(f"+{x}+{y}")
        self.grab_set()
        self.focus_set()

    def _ok(self):
        self.result = self.var.get()
        self.destroy()

    def _cancel(self):
        self.result = None
        self.destroy()

    @classmethod
    def ask(cls, master, current="auto"):
        dialog = cls(master, current=current)
        master.wait_window(dialog)
        return dialog.result


class EpisodeSquareSelector(ttk.Frame):
    """用真正的正方形单元格选择剧集；单击切换选中/取消。"""

    CELL_SIZE = 58
    GAP = 8
    PADDING = 10

    def __init__(self, master, on_change=None):
        super().__init__(master)
        self.on_change = on_change
        self.episodes = []
        self.selected = set()

        self.canvas = tk.Canvas(
            self,
            highlightthickness=0,
            borderwidth=0,
            background="#ffffff",
        )
        self.scrollbar = ttk.Scrollbar(
            self,
            orient="vertical",
            command=self.canvas.yview,
        )
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")

        self.canvas.bind("<Configure>", lambda _e: self._redraw())
        self.canvas.bind("<MouseWheel>", self._on_mousewheel)
        self.canvas.bind("<Button-4>", lambda _e: self.canvas.yview_scroll(-1, "units"))
        self.canvas.bind("<Button-5>", lambda _e: self.canvas.yview_scroll(1, "units"))

    def set_episodes(self, episodes):
        self.episodes = list(episodes or [])
        valid = {ep.index for ep in self.episodes}
        self.selected.intersection_update(valid)
        self._redraw()
        self._notify()

    def get_selected_indices(self):
        return set(self.selected)

    def select_all(self):
        self.selected = {ep.index for ep in self.episodes}
        self._redraw()
        self._notify()

    def clear_selection(self):
        self.selected.clear()
        self._redraw()
        self._notify()

    def invert_selection(self):
        all_indices = {ep.index for ep in self.episodes}
        self.selected = all_indices - self.selected
        self._redraw()
        self._notify()

    def _notify(self):
        if self.on_change:
            try:
                self.on_change(len(self.selected), len(self.episodes))
            except Exception:
                pass

    def _on_mousewheel(self, event):
        delta = getattr(event, "delta", 0)
        if delta:
            self.canvas.yview_scroll(int(-delta / 120), "units")

    def _toggle(self, episode_index):
        if episode_index in self.selected:
            self.selected.remove(episode_index)
        else:
            self.selected.add(episode_index)
        self._redraw()
        self._notify()

    def _redraw(self):
        self.canvas.delete("all")
        if not self.episodes:
            self.canvas.create_text(
                20,
                20,
                anchor="nw",
                text="解析分享链接后，这里会显示每一集的方块选择框。",
                fill="#666666",
                font=("Microsoft YaHei UI", 10),
            )
            self.canvas.configure(scrollregion=(0, 0, 1, 80))
            return

        width = max(self.canvas.winfo_width(), self.CELL_SIZE + self.PADDING * 2)
        usable = max(1, width - self.PADDING * 2)
        cols = max(1, (usable + self.GAP) // (self.CELL_SIZE + self.GAP))

        for pos, ep in enumerate(self.episodes):
            row, col = divmod(pos, cols)
            x1 = self.PADDING + col * (self.CELL_SIZE + self.GAP)
            y1 = self.PADDING + row * (self.CELL_SIZE + self.GAP)
            x2 = x1 + self.CELL_SIZE
            y2 = y1 + self.CELL_SIZE
            chosen = ep.index in self.selected
            fill = "#2563eb" if chosen else "#f7f7f7"
            outline = "#1d4ed8" if chosen else "#b7b7b7"
            text_color = "#ffffff" if chosen else "#222222"
            tag = f"episode_{ep.index}"

            self.canvas.create_rectangle(
                x1,
                y1,
                x2,
                y2,
                fill=fill,
                outline=outline,
                width=2 if chosen else 1,
                tags=(tag, "episode_cell"),
            )
            label = f"✓\n{ep.index:03d}" if chosen else f"{ep.index:03d}"
            self.canvas.create_text(
                (x1 + x2) / 2,
                (y1 + y2) / 2,
                text=label,
                fill=text_color,
                font=("Microsoft YaHei UI", 10, "bold" if chosen else "normal"),
                justify="center",
                tags=(tag, "episode_cell"),
            )
            self.canvas.tag_bind(
                tag,
                "<Button-1>",
                lambda _e, idx=ep.index: self._toggle(idx),
            )

        rows = (len(self.episodes) + cols - 1) // cols
        total_height = self.PADDING * 2 + rows * self.CELL_SIZE + max(0, rows - 1) * self.GAP
        self.canvas.configure(scrollregion=(0, 0, width, total_height))


class HongguoApp(tk.Tk):
    def __init__(
        self,
        download_dir: Path | str | None = None,
        log_dir: Path | str | None = None,
        json_dir: Path | str | None = None,
        ffmpeg: str = "ffmpeg",
        keep_encrypted: bool = False,
    ):
        super().__init__()
        self.title("红果短剧一体化解析下载器 v1.1")
        self.geometry("1240x860")
        self.minsize(1020, 720)

        self.session = requests.Session()
        # 某些 v2rayN/Clash 环境会给 requests 注入失效代理。
        # 设置 HONGGUO_IGNORE_PROXY=1 可让本工具忽略 HTTP_PROXY/HTTPS_PROXY。
        if os.environ.get("HONGGUO_IGNORE_PROXY", "").strip() == "1":
            self.session.trust_env = False
        self.share_parser = ShareParser(self.session)
        self.series_parser = SeriesParser(self.session)
        self.player_api = None

        base_dir = Path.cwd()
        self.root_dir = Path(download_dir or (base_dir / "downloads")).expanduser().resolve()
        self.log_dir = Path(log_dir or (base_dir / "runtime" / "logs")).expanduser().resolve()
        self.json_dir = Path(json_dir or (base_dir / "runtime" / "json")).expanduser().resolve()
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.json_dir.mkdir(parents=True, exist_ok=True)

        self.logger, self.log_path = setup_logging(self.log_dir)
        self.manager = DownloadManager(self.root_dir, self.session)
        self.archive = JsonArchive(self.json_dir)
        self.pipeline = IntegratedMediaPipeline(
            self.manager,
            self.archive,
            ffmpeg=ffmpeg,
            keep_encrypted=keep_encrypted,
        )
        self.series = None
        self.q = queue.Queue()
        self.address_rows = {}
        self.address_objects = {}
        self.quality_nodes = {}
        self.selected_quality = "auto"

        self._build()
        self.log(f"日志文件：{self.log_path}")
        self.log(f"JSON 归档目录：{self.json_dir}")
        self.after(100, self._poll)

    def _get_player_api(self):
        if self.player_api is None:
            self.player_api = PlayerApiClient(self.session)
        return self.player_api

    def _build(self):
        root = ttk.Frame(self, padding=12)
        root.pack(fill="both", expand=True)

        inp = ttk.LabelFrame(root, text="1. 分享链接", padding=10)
        inp.pack(fill="x")
        self.text = tk.Text(inp, height=3, wrap="word")
        self.text.pack(fill="x")

        row = ttk.Frame(inp)
        row.pack(fill="x", pady=(8, 0))
        self.an_btn = ttk.Button(row, text="解析链接", command=self.start_analyze)
        self.an_btn.pack(side="left")
        ttk.Button(row, text="清空", command=lambda: self.text.delete("1.0", "end")).pack(side="left", padx=8)
        self.dir_label = ttk.Label(row, text=f"下载目录：{self.root_dir}")
        self.dir_label.pack(side="left", padx=12, fill="x", expand=True)
        ttk.Button(row, text="更改目录", command=self.choose_dir).pack(side="right")

        info = ttk.LabelFrame(root, text="2. 解析结果", padding=10)
        info.pack(fill="x", pady=(10, 0))
        self.info1 = tk.StringVar(value="剧名：-")
        self.info2 = tk.StringVar(value="ID / 集数：-")
        ttk.Label(info, textvariable=self.info1).pack(anchor="w")
        ttk.Label(info, textvariable=self.info2).pack(anchor="w", pady=(4, 0))

        self.nb = ttk.Notebook(root)
        self.nb.pack(fill="both", expand=True, pady=(10, 0))

        self.tab_download = ttk.Frame(self.nb, padding=10)
        self.tab_grid = ttk.Frame(self.nb, padding=10)
        self.tab_urls = ttk.Frame(self.nb, padding=10)
        self.nb.add(self.tab_download, text="剧集与下载")
        self.nb.add(self.tab_grid, text="方块选集")
        self.nb.add(self.tab_urls, text="视频地址")

        self._build_download_tab()
        self._build_grid_tab()
        self._build_url_tab()

        logs = ttk.LabelFrame(root, text="日志", padding=8)
        logs.pack(fill="x", pady=(10, 0))
        self.logbox = tk.Text(logs, height=7, state="disabled", wrap="word")
        self.logbox.pack(fill="x")

    def _build_download_tab(self):
        tools = ttk.Frame(self.tab_download)
        tools.pack(fill="x", pady=(0, 8))
        ttk.Button(tools, text="全选", command=self.select_all).pack(side="left")
        ttk.Button(tools, text="取消选择", command=lambda: self.lst.selection_clear(0, "end")).pack(side="left", padx=6)
        ttk.Label(tools, text="当前画质：").pack(side="left", padx=(18, 4))
        self.quality_label = tk.StringVar(value=quality_display(self.selected_quality))
        ttk.Label(tools, textvariable=self.quality_label, width=18).pack(side="left")
        ttk.Button(tools, text="选择画质…", command=self.pick_quality_only).pack(side="left", padx=(4, 0))
        ttk.Label(tools, text="解析、参数提取和下载已合并为一个流程。 ").pack(side="right")

        lf = ttk.Frame(self.tab_download)
        lf.pack(fill="both", expand=True)
        self.lst = tk.Listbox(
            lf,
            selectmode=tk.EXTENDED,
            exportselection=False,
            font=("Microsoft YaHei UI", 10),
        )
        self.lst.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(lf, command=self.lst.yview)
        sb.pack(side="right", fill="y")
        self.lst.config(yscrollcommand=sb.set)

        act = ttk.Frame(self.tab_download)
        act.pack(fill="x", pady=(10, 0))
        self.down_btn = ttk.Button(act, text="下载选中集数", state="disabled", command=self.start_download)
        self.down_btn.pack(side="left")
        self.cancel_btn = ttk.Button(act, text="取消下载", state="disabled", command=self.cancel)
        self.cancel_btn.pack(side="left", padx=8)
        ttk.Button(act, text="把选中集解析到“视频地址”页", command=self.resolve_selected_to_url_tab).pack(side="left", padx=(10, 0))
        self.pb = ttk.Progressbar(act, maximum=100)
        self.pb.pack(side="left", fill="x", expand=True, padx=12)
        self.pct = tk.StringVar(value="0%")
        ttk.Label(act, textvariable=self.pct, width=8).pack(side="right")

    def _build_grid_tab(self):
        tools = ttk.Frame(self.tab_grid)
        tools.pack(fill="x", pady=(0, 8))

        ttk.Button(
            tools,
            text="全选",
            command=lambda: self.grid_selector.select_all(),
        ).pack(side="left")
        ttk.Button(
            tools,
            text="取消全选",
            command=lambda: self.grid_selector.clear_selection(),
        ).pack(side="left", padx=6)
        ttk.Button(
            tools,
            text="反选",
            command=lambda: self.grid_selector.invert_selection(),
        ).pack(side="left")

        ttk.Label(tools, text="当前画质：").pack(side="left", padx=(18, 4))
        ttk.Label(tools, textvariable=self.quality_label, width=18).pack(side="left")
        ttk.Button(
            tools,
            text="选择画质…",
            command=self.pick_quality_only,
        ).pack(side="left", padx=(4, 0))

        self.grid_count = tk.StringVar(value="已选 0 / 0 集")
        ttk.Label(
            tools,
            textvariable=self.grid_count,
            font=("Microsoft YaHei UI", 10, "bold"),
        ).pack(side="right")

        hint = ttk.Label(
            self.tab_grid,
            text="单击方块选中该集，再单击一次即可取消。蓝色带 ✓ 的方块表示已选中。",
        )
        hint.pack(fill="x", pady=(0, 8))

        box = ttk.LabelFrame(self.tab_grid, text="集数选择", padding=6)
        box.pack(fill="both", expand=True)
        self.grid_selector = EpisodeSquareSelector(
            box,
            on_change=self._on_grid_selection_changed,
        )
        self.grid_selector.pack(fill="both", expand=True)

        actions = ttk.Frame(self.tab_grid)
        actions.pack(fill="x", pady=(10, 0))
        ttk.Button(
            actions,
            text="下载方块选中的集数",
            command=self.start_grid_download,
        ).pack(side="left")
        ttk.Button(
            actions,
            text="把方块选中的集数解析到“视频地址”页",
            command=self.resolve_grid_selected_to_url_tab,
        ).pack(side="left", padx=8)
        ttk.Button(
            actions,
            text="取消下载",
            command=self.cancel,
        ).pack(side="left")

    def _build_url_tab(self):
        tools = ttk.Frame(self.tab_urls)
        tools.pack(fill="x", pady=(0, 8))
        ttk.Button(
            tools,
            text="解析剧集页中选中的集数",
            command=self.resolve_selected_to_url_tab,
        ).pack(side="left")
        ttk.Button(
            tools,
            text="解析全部集数",
            command=self.resolve_all_urls,
        ).pack(side="left", padx=6)
        ttk.Button(
            tools,
            text="清空地址列表",
            command=self.clear_url_rows,
        ).pack(side="left", padx=6)
        ttk.Button(
            tools,
            text="全选当前画质",
            command=self.select_all_current_url_quality,
        ).pack(side="left", padx=6)
        ttk.Button(
            tools,
            text="取消地址选择",
            command=self.clear_url_selection,
        ).pack(side="left", padx=6)
        ttk.Button(
            tools,
            text="下载已选画质",
            command=self.download_current_url_quality,
        ).pack(side="left", padx=6)
        ttk.Label(
            tools,
            text=(
                "main_url 若不是 http(s) 而是 qAAB... 之类编码值，"
                "会标为“受保护字段”；fallback_api 是元数据接口，不是视频文件。"
            ),
        ).pack(side="right")

        tf = ttk.Frame(self.tab_urls)
        tf.pack(fill="both", expand=True)

        columns = (
            "episode",
            "vid",
            "quality",
            "kind",
            "state",
        )
        self.url_tree = ttk.Treeview(
            tf,
            columns=columns,
            show="tree headings",
            selectmode="extended",
            height=12,
        )
        self.url_tree.heading("#0", text="画质分类")
        self.url_tree.column("#0", width=145, anchor="w")
        self.url_tree.heading("episode", text="集数")
        self.url_tree.heading("vid", text="vid")
        self.url_tree.heading("quality", text="画质")
        self.url_tree.heading("kind", text="地址类型")
        self.url_tree.heading("state", text="状态")

        self.url_tree.column("episode", width=65, anchor="center")
        self.url_tree.column("vid", width=195)
        self.url_tree.column("quality", width=75, anchor="center")
        self.url_tree.column("kind", width=120, anchor="center")
        self.url_tree.column("state", width=360)

        self.url_tree.pack(side="left", fill="both", expand=True)

        tsb = ttk.Scrollbar(tf, command=self.url_tree.yview)
        tsb.pack(side="right", fill="y")
        self.url_tree.config(yscrollcommand=tsb.set)
        self.url_tree.bind("<<TreeviewSelect>>", self.on_url_row_select)
        self.url_tree.bind("<Double-1>", lambda e: self.copy_media_url())

        detail = ttk.LabelFrame(
            self.tab_urls,
            text="选中地址详情",
            padding=8,
        )
        detail.pack(fill="x", pady=(10, 0))

        r0 = ttk.Frame(detail)
        r0.pack(fill="x")
        ttk.Label(r0, text="地址类型：", width=14).pack(side="left")
        self.url_kind = tk.StringVar(value="")
        ttk.Label(r0, textvariable=self.url_kind).pack(
            side="left",
            fill="x",
            expand=True,
        )

        r1 = ttk.Frame(detail)
        r1.pack(fill="x", pady=(6, 0))
        ttk.Label(r1, text="原始 main_url：", width=14).pack(side="left")
        self.media_url = tk.StringVar(value="")
        ttk.Entry(
            r1,
            textvariable=self.media_url,
        ).pack(side="left", fill="x", expand=True)
        ttk.Button(
            r1,
            text="复制原始值",
            command=self.copy_media_url,
        ).pack(side="left", padx=(6, 0))

        r2 = ttk.Frame(detail)
        r2.pack(fill="x", pady=(6, 0))
        ttk.Label(r2, text="fallback_api：", width=14).pack(side="left")
        self.fallback_url = tk.StringVar(value="")
        ttk.Entry(
            r2,
            textvariable=self.fallback_url,
        ).pack(side="left", fill="x", expand=True)
        ttk.Button(
            r2,
            text="复制 fallback",
            command=self.copy_fallback_url,
        ).pack(side="left", padx=(6, 0))

        r3 = ttk.Frame(detail)
        r3.pack(fill="x", pady=(6, 0))
        ttk.Label(r3, text="安全诊断信息：", width=14).pack(
            side="left",
            anchor="n",
        )
        self.url_detail = tk.Text(
            r3,
            height=7,
            wrap="word",
        )
        self.url_detail.pack(
            side="left",
            fill="x",
            expand=True,
        )
        self.url_detail.configure(state="disabled")

        buttons = ttk.Frame(detail)
        buttons.pack(fill="x", pady=(6, 0))
        ttk.Button(
            buttons,
            text="复制安全诊断 JSON",
            command=self.copy_safe_diagnostic,
        ).pack(side="left")
        ttk.Button(
            buttons,
            text="导出安全诊断 JSON",
            command=self.export_safe_diagnostic,
        ).pack(side="left", padx=6)
        ttk.Label(
            buttons,
            text="下方可查看 fallback_api 响应；完整解析结果会自动归档到 JSON 目录。",
        ).pack(side="right")

        fallback_box = ttk.LabelFrame(
            self.tab_urls,
            text="fallback_api 测试响应 JSON",
            padding=8,
        )
        fallback_box.pack(fill="both", expand=False, pady=(10, 0))

        fb_tools = ttk.Frame(fallback_box)
        fb_tools.pack(fill="x", pady=(0, 6))
        ttk.Button(
            fb_tools,
            text="复制 fallback JSON",
            command=self.copy_fallback_json,
        ).pack(side="left")
        ttk.Button(
            fb_tools,
            text="导出 fallback JSON",
            command=self.export_fallback_json,
        ).pack(side="left", padx=6)
        ttk.Label(
            fb_tools,
            text="展示接口原始响应结构；解析结果同时会自动写入 JSON 归档目录。",
        ).pack(side="right")

        fb_text_frame = ttk.Frame(fallback_box)
        fb_text_frame.pack(fill="both", expand=True)

        self.fallback_json_text = tk.Text(
            fb_text_frame,
            height=14,
            wrap="none",
        )
        self.fallback_json_text.pack(side="left", fill="both", expand=True)

        fb_scroll_y = ttk.Scrollbar(
            fb_text_frame,
            orient="vertical",
            command=self.fallback_json_text.yview,
        )
        fb_scroll_y.pack(side="right", fill="y")

        fb_scroll_x = ttk.Scrollbar(
            fallback_box,
            orient="horizontal",
            command=self.fallback_json_text.xview,
        )
        fb_scroll_x.pack(fill="x")

        self.fallback_json_text.configure(
            yscrollcommand=fb_scroll_y.set,
            xscrollcommand=fb_scroll_x.set,
            state="disabled",
        )

    def choose_dir(self):
        d = filedialog.askdirectory(initialdir=str(self.root_dir))
        if d:
            self.root_dir = Path(d).expanduser().resolve()
            self.manager = DownloadManager(self.root_dir, self.session)
            self.pipeline.set_manager(self.manager)
            self.dir_label.config(text=f"下载目录：{self.root_dir}")
            self.log(f"视频保存目录已修改：{self.root_dir}")

    def log(self, s):
        text = str(s)
        try:
            self.logger.info(text)
        except Exception:
            pass
        self.logbox.config(state="normal")
        self.logbox.insert("end", f"[{time.strftime('%H:%M:%S')}] {text}\n")
        self.logbox.see("end")
        self.logbox.config(state="disabled")

    def post(self, k, v=None):
        self.q.put((k, v))

    def _poll(self):
        try:
            while True:
                k, v = self.q.get_nowait()
                if k == "log":
                    self.log(v)
                elif k == "series":
                    self.show_series(v)
                elif k == "error":
                    self.log("错误：" + str(v))
                    messagebox.showerror("错误", str(v))
                    self.an_btn.config(state="normal")
                    self.down_btn.config(state="normal" if self.series else "disabled")
                elif k == "analysis_done":
                    self.an_btn.config(state="normal")
                elif k == "progress":
                    self.pb["value"] = v
                    self.pct.set(f"{v:.1f}%")
                elif k == "download_done":
                    self.down_btn.config(state="normal")
                    self.cancel_btn.config(state="disabled")
                    messagebox.showinfo("完成", v)
                elif k == "url_result":
                    ep, result = v
                    self.add_url_result(ep, result)
                elif k == "url_error":
                    ep, err = v
                    self.add_url_error(ep, err)
                elif k == "url_batch_done":
                    self.log(v)
                    self.nb.select(self.tab_urls)
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def start_analyze(self):
        text = self.text.get("1.0", "end").strip()
        if not text:
            return messagebox.showwarning("提示", "请粘贴分享链接。")
        self.an_btn.config(state="disabled")
        self.down_btn.config(state="disabled")
        self.log("开始解析分享链接……")
        threading.Thread(target=self._analyze, args=(text,), daemon=True).start()

    def _analyze(self, text):
        try:
            share = self.share_parser.resolve(text)
            self.post("log", f"分享页跳转：{share.final_url}")
            chapter_ids = share.metadata.get("chapter_ids") or []
            if chapter_ids:
                self.post(
                    "log",
                    f"APK chapter_ids 已解析：共 {len(chapter_ids)} 个；"
                    f"第1集={chapter_ids[0]}"
                    + (f"，第4集={chapter_ids[3]}" if len(chapter_ids) >= 4 else ""),
                )
            series = self.series_parser.from_share(share)
            self.manager.save_info(series)
            self.post("series", series)
            self.post("log", f"解析完成：{series.name}，共 {series.total} 集。")
        except Exception as e:
            self.logger.exception("分享链接解析失败")
            self.post("error", e)
        finally:
            self.post("analysis_done")

    def show_series(self, s):
        self.series = s
        self.info1.set(f"剧名：{s.name}")
        self.info2.set(f"APK入口ID：{s.series_id}    总集数：{s.total}")
        self.lst.delete(0, "end")
        for ep in s.episodes:
            ident = ep.vid or ep.chapter_id or "-"
            self.lst.insert("end", f"第 {ep.index:03d} 集    [{ep.status}]    vid={ident}")
        self.grid_selector.set_episodes(s.episodes)
        self.down_btn.config(state="normal")
        self.clear_url_rows()

    def select_all(self):
        if self.series:
            self.lst.selection_set(0, "end")

    def cancel(self):
        self.manager.stop.set()
        self.log("正在取消下载……")

    def _selected_episodes(self):
        if not self.series:
            return []
        return [self.series.episodes[i] for i in self.lst.curselection()]

    def pick_quality_only(self):
        selected = QualityPickerDialog.ask(self, self.selected_quality)
        if selected:
            self.selected_quality = selected
            self.quality_label.set(quality_display(selected))

    def _ask_download_quality(self):
        selected = QualityPickerDialog.ask(self, self.selected_quality)
        if selected:
            self.selected_quality = selected
            self.quality_label.set(quality_display(selected))
        return selected

    def _begin_download(self, eps):
        if not self.series:
            return
        eps = list(eps or [])
        if not eps:
            return messagebox.showwarning("提示", "请选择集数。")
        preferred = self._ask_download_quality()
        if not preferred:
            return
        self.manager.stop.clear()
        self.down_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self.pb["value"] = 0
        self.pct.set("0%")
        threading.Thread(target=self._download, args=(eps, preferred), daemon=True).start()

    def start_download(self):
        self._begin_download(self._selected_episodes())

    def _on_grid_selection_changed(self, selected_count, total_count):
        if hasattr(self, "grid_count"):
            self.grid_count.set(f"已选 {selected_count} / {total_count} 集")

    def _grid_selected_episodes(self):
        if not self.series or not hasattr(self, "grid_selector"):
            return []
        selected = self.grid_selector.get_selected_indices()
        return [ep for ep in self.series.episodes if ep.index in selected]

    def start_grid_download(self):
        eps = self._grid_selected_episodes()
        if not eps:
            return messagebox.showwarning("提示", "请在“方块选集”页点击方块选择需要下载的集数。")
        self._begin_download(eps)

    def resolve_grid_selected_to_url_tab(self):
        eps = self._grid_selected_episodes()
        if not eps:
            return messagebox.showwarning("提示", "请在“方块选集”页点击方块选择需要解析的集数。")
        self.nb.select(self.tab_urls)
        threading.Thread(target=self._resolve_url_batch, args=(eps,), daemon=True).start()

    def _download(self, eps, preferred):
        total = len(eps)
        finished = ok = fail = 0
        lock = threading.Lock()

        def one(ep):
            if self.manager.stop.is_set():
                return False, "已取消"
            vid = ep.vid or ep.chapter_id
            if not vid:
                return False, f"第 {ep.index} 集缺少 vid"
            try:
                self.post("log", f"第 {ep.index} 集：Ti(vid={vid}) 获取多画质地址……")
                result = self._get_player_api().resolve(vid)
                json_path = self.archive.save_resolve_result(self.series, ep, result)
                self.post("url_result", (ep, result))
                self.post("log", f"第 {ep.index} 集解析 JSON 已保存：{json_path}")
                opt = self.pipeline.choose_option(result, preferred)
                ep.direct_url = opt.direct_url or opt.raw_url
                ep.accessible = True
                ep.protected = opt.protected
                ep.status = f"下载中 {opt.label}"
                bitrate_text = (
                    f"，{opt.bitrate / 1000:.0f} kbps"
                    if getattr(opt, "bitrate", 0)
                    else ""
                )
                self.post(
                    "log",
                    f"第 {ep.index} 集：选择 {opt.label}{bitrate_text}，"
                    "开始检测媒体类型并下载。"
                )
                last_ui = [0.0]

                def on_progress(done, total_bytes, speed):
                    now = time.monotonic()
                    # 限制日志频率，避免 GUI 被刷爆。
                    if now - last_ui[0] < 1.0:
                        return
                    last_ui[0] = now

                    if total_bytes > 0:
                        pct = done * 100 / total_bytes
                        size_text = (
                            f"{done / 1024 / 1024:.1f}/"
                            f"{total_bytes / 1024 / 1024:.1f} MB"
                        )
                        self.post(
                            "log",
                            f"第 {ep.index} 集：{pct:.1f}% "
                            f"({size_text}) "
                            f"{speed / 1024 / 1024:.2f} MB/s"
                        )
                    else:
                        self.post(
                            "log",
                            f"第 {ep.index} 集："
                            f"{done / 1024 / 1024:.1f} MB，"
                            f"{speed / 1024 / 1024:.2f} MB/s"
                        )

                path = self.pipeline.download(
                    self.series,
                    ep,
                    opt,
                    progress=on_progress,
                    log=lambda line: self.post("log", f"第 {ep.index} 集：{line}"),
                )
                ep.status = "已完成"
                return True, f"第 {ep.index} 集完成：{path.name}"
            except Exception as e:
                self.logger.exception("第 %s 集下载失败", ep.index)
                text = str(e)
                ep.status = "失败"
                return False, f"第 {ep.index} 集失败：{text}"

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
            fs = [ex.submit(one, e) for e in eps]
            for f in concurrent.futures.as_completed(fs):
                if self.manager.stop.is_set():
                    break
                yes, msg = f.result()
                self.post("log", msg)
                with lock:
                    finished += 1
                    if yes:
                        ok += 1
                    else:
                        fail += 1
                    self.post("progress", finished * 100 / total)

        self.manager.save_info(self.series)
        self.post(
            "download_done",
            f"任务完成。成功 {ok} 集，失败 {fail} 集。\n保存目录：{self.manager.series_dir(self.series)}",
        )

    def resolve_selected_to_url_tab(self):
        eps = self._selected_episodes()
        if not eps:
            return messagebox.showwarning("提示", "请先在“剧集与下载”页选择需要解析的集数。")
        self.nb.select(self.tab_urls)
        threading.Thread(target=self._resolve_url_batch, args=(eps,), daemon=True).start()

    def resolve_all_urls(self):
        if not self.series:
            return messagebox.showwarning("提示", "请先解析短剧。")
        threading.Thread(target=self._resolve_url_batch, args=(list(self.series.episodes),), daemon=True).start()

    def _resolve_url_batch(self, eps):
        self.post("log", f"开始解析 {len(eps)} 集播放器地址……")
        ok = fail = 0
        # 地址解析也限制 3 并发，和 APK 下载槽位一致。
        def one(ep):
            vid = ep.vid or ep.chapter_id
            if not vid:
                return ep, None, "缺少 vid"
            try:
                result = self._get_player_api().resolve(vid)
                return ep, result, None
            except Exception as e:
                return ep, None, str(e)

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
            fs = [ex.submit(one, ep) for ep in eps]
            for f in concurrent.futures.as_completed(fs):
                ep, result, err = f.result()
                if err:
                    fail += 1
                    self.post("url_error", (ep, err))
                    self.post("log", f"第 {ep.index} 集地址解析失败：{err}")
                else:
                    ok += 1
                    json_path = self.archive.save_resolve_result(self.series, ep, result)
                    self.post("url_result", (ep, result))
                    self.post("log", f"第 {ep.index} 集：解析到 {len(result.options)} 个画质；JSON：{json_path}")
        self.post("url_batch_done", f"视频地址解析完成：成功 {ok} 集，失败 {fail} 集。")

    def clear_url_rows(self):
        self.address_rows = {}
        self.address_objects = {}
        self.quality_nodes = {}
        if hasattr(self, "url_tree"):
            for item in self.url_tree.get_children():
                self.url_tree.delete(item)
        if hasattr(self, "media_url"):
            self.media_url.set("")
            self.fallback_url.set("")
            if hasattr(self, "url_kind"):
                self.url_kind.set("")
            if hasattr(self, "url_detail"):
                self.url_detail.configure(state="normal")
                self.url_detail.delete("1.0", "end")
                self.url_detail.configure(state="disabled")
            if hasattr(self, "fallback_json_text"):
                self.fallback_json_text.configure(state="normal")
                self.fallback_json_text.delete("1.0", "end")
                self.fallback_json_text.configure(state="disabled")

    @staticmethod
    def _sanitize_diagnostic(value):
        """
        保留结构和普通元数据，但不输出用于受保护媒体解密的材料值。
        """
        sensitive = {
            "key_seed",
            "spade_a",
            "pssh",
            "license",
            "license_url",
            "decryption_key",
            "decrypt_key",
            "cenc_key",
        }

        if isinstance(value, dict):
            out = {}
            for k, v in value.items():
                lk = str(k).lower()
                out[k] = HongguoApp._sanitize_diagnostic(v)
                # if lk in sensitive:
                #     out[k] = "<存在，值已隐藏>" if v else ""
                # else:
                #     out[k] = HongguoApp._sanitize_diagnostic(v)
            return out

        if isinstance(value, list):
            return [
                HongguoApp._sanitize_diagnostic(x)
                for x in value
            ]

        return value

    @staticmethod
    def _address_kind(opt):
        raw = str(opt.raw_url or "")

        if opt.downloadable and raw.startswith(("http://", "https://")):
            return "HTTP直链"

        if opt.has_spade_a:
            return "受保护字段"

        if raw and not raw.startswith(("http://", "https://")):
            return "编码字段"

        return "未知"

    def add_url_result(self, ep, result):
        # 同一集重复解析时，删除旧结果。
        for item, data in list(self.address_rows.items()):
            if data.get("episode") == ep.index:
                try:
                    self.url_tree.delete(item)
                except Exception:
                    pass
                self.address_rows.pop(item, None)
                self.address_objects.pop(item, None)

        safe_fallback = self._sanitize_diagnostic(
            result.raw_fallback
        )
        safe_model = self._sanitize_diagnostic(
            result.raw_video_model
        )

        for opt in result.options:
            display_url = opt.direct_url or opt.raw_url
            kind = self._address_kind(opt)

            group_label = quality_display(opt.value or opt.label)
            parent = self.quality_nodes.get(group_label)
            if not parent or not self.url_tree.exists(parent):
                parent = self.url_tree.insert(
                    "",
                    "end",
                    text=group_label,
                    open=True,
                    values=("", "", "", "", ""),
                )
                self.quality_nodes[group_label] = parent

            iid = self.url_tree.insert(
                parent,
                "end",
                text="",
                values=(
                    ep.index,
                    result.vid,
                    opt.label,
                    kind,
                    opt.status,
                ),
            )

            row = {
                "episode": ep.index,
                "vid": result.vid,
                "quality": opt.label,
                "address_type": kind,
                "status": opt.status,
                "resolution": (
                    f"{opt.width}x{opt.height}"
                    if opt.width and opt.height
                    else ""
                ),
                "bitrate": getattr(opt, "bitrate", 0),
                "size_bytes": getattr(opt, "size_bytes", 0),
                "codec": getattr(opt, "codec", ""),
                "main_url_raw": display_url,
                "fallback_api": result.fallback_api,
                "downloadable": opt.downloadable,
                "is_http_url": str(opt.raw_url or "").startswith(
                    ("http://", "https://")
                ),
                "has_spade_a": opt.has_spade_a,
                "key_seed_present": bool(opt.key_seed),
                "safe_video_model": safe_model,
                "safe_fallback_response": safe_fallback,
            }
            self.address_rows[iid] = row
            self.address_objects[iid] = (ep, result, opt)

    def _selected_url_leaf_ids(self):
        """把当前选中的分类/子项展开为真正的视频画质记录。"""
        selected = list(self.url_tree.selection())
        leaf_ids = []
        seen = set()
        for iid in selected:
            candidates = []
            if iid in self.address_objects:
                candidates = [iid]
            else:
                candidates = list(self.url_tree.get_children(iid))
            for child in candidates:
                if child in self.address_objects and child not in seen:
                    seen.add(child)
                    leaf_ids.append(child)
        return leaf_ids

    def clear_url_selection(self):
        selected = self.url_tree.selection()
        if selected:
            self.url_tree.selection_remove(*selected)

    def select_all_current_url_quality(self):
        """
        选中某个画质分类或该分类中的任意一集后，
        一键选中该画质下已经解析出的全部集数。
        """
        selected = list(self.url_tree.selection())
        if not selected:
            return messagebox.showwarning(
                "提示",
                "请先点一下某个画质分类（例如 1080P），或该画质下的任意一集。",
            )

        parents = []
        seen = set()
        for iid in selected:
            if iid in self.address_objects:
                parent = self.url_tree.parent(iid)
            else:
                parent = iid if self.url_tree.get_children(iid) else ""
            if parent and parent not in seen:
                seen.add(parent)
                parents.append(parent)

        children = []
        for parent in parents:
            children.extend(
                child
                for child in self.url_tree.get_children(parent)
                if child in self.address_objects
            )

        if not children:
            return messagebox.showwarning("提示", "当前画质分类下没有可选择的视频记录。")

        self.url_tree.selection_set(children)
        self.url_tree.focus(children[0])
        self.url_tree.see(children[0])
        labels = [self.url_tree.item(parent, "text") for parent in parents]
        self.log(f"已全选画质 {'、'.join(labels)}：共 {len(children)} 条视频记录。")

    def download_current_url_quality(self):
        leaf_ids = self._selected_url_leaf_ids()
        if not leaf_ids:
            return messagebox.showwarning(
                "提示",
                "请选择一个画质分类，或按 Ctrl/Shift 多选需要下载的视频记录。",
            )
        if not self.series:
            return

        items = [self.address_objects[iid] for iid in leaf_ids]
        self.manager.stop.clear()
        self.cancel_btn.config(state="normal")
        self.pb["value"] = 0
        self.pct.set("0%")
        threading.Thread(
            target=self._download_exact_options_batch,
            args=(items,),
            daemon=True,
        ).start()

    def _download_exact_options_batch(self, items):
        total = len(items)
        ok = fail = finished = 0
        lock = threading.Lock()

        def one(item):
            ep, result, opt = item
            if self.manager.stop.is_set():
                return False, f"第 {ep.index} 集已取消"
            try:
                self.post("log", f"第 {ep.index} 集：开始下载指定画质 {opt.label}。")
                self.archive.save_resolve_result(self.series, ep, result)
                last_ui = [0.0]

                def on_progress(done, total_bytes, speed):
                    if self.manager.stop.is_set():
                        raise InterruptedError("下载已取消")
                    now = time.monotonic()
                    if now - last_ui[0] < 1.2:
                        return
                    last_ui[0] = now
                    if total_bytes > 0:
                        pct = done * 100 / total_bytes
                        self.post(
                            "log",
                            f"第 {ep.index} 集 {opt.label}：{pct:.1f}% "
                            f"{speed / 1024 / 1024:.2f} MB/s",
                        )

                path = self.pipeline.download(
                    self.series,
                    ep,
                    opt,
                    progress=on_progress,
                    log=lambda line: self.post("log", f"第 {ep.index} 集：{line}"),
                )
                ep.status = "已完成"
                return True, f"第 {ep.index} 集 {opt.label} 下载完成：{path.name}"
            except Exception as exc:
                self.logger.exception(
                    "指定画质下载失败：episode=%s quality=%s",
                    ep.index,
                    opt.label,
                )
                ep.status = "失败"
                return False, f"第 {ep.index} 集 {opt.label} 失败：{exc}"

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
            futures = [ex.submit(one, item) for item in items]
            for f in concurrent.futures.as_completed(futures):
                yes, msg = f.result()
                self.post("log", msg)
                with lock:
                    finished += 1
                    if yes:
                        ok += 1
                    else:
                        fail += 1
                    self.post("progress", finished * 100 / total)

        self.manager.save_info(self.series)
        self.post(
            "download_done",
            f"指定画质批量下载完成。成功 {ok} 个，失败 {fail} 个。\n"
            f"保存目录：{self.manager.series_dir(self.series)}",
        )

    def add_url_error(self, ep, err):
        iid = self.url_tree.insert(
            "",
            "end",
            values=(
                ep.index,
                ep.vid or ep.chapter_id,
                "-",
                "失败",
                f"失败：{err}",
            ),
        )
        self.address_rows[iid] = {
            "episode": ep.index,
            "vid": ep.vid or ep.chapter_id,
            "quality": "-",
            "address_type": "失败",
            "status": f"失败：{err}",
            "main_url_raw": "",
            "fallback_api": "",
            "downloadable": False,
        }

    def on_url_row_select(self, _event=None):
        sel = self.url_tree.selection()
        if not sel:
            return

        focus = self.url_tree.focus()
        current = focus if focus in sel else sel[-1]
        data = self.address_rows.get(current, {})

        self.media_url.set(
            data.get("main_url_raw", "")
        )
        self.fallback_url.set(
            data.get("fallback_api", "")
        )
        self.url_kind.set(
            data.get("address_type", "")
        )

        detail = json.dumps(
            data,
            ensure_ascii=False,
            indent=2,
        )

        self.url_detail.configure(state="normal")
        self.url_detail.delete("1.0", "end")
        self.url_detail.insert("1.0", detail)
        self.url_detail.configure(state="disabled")

        fallback_obj = data.get("safe_fallback_response", {})
        fallback_text = json.dumps(
            fallback_obj,
            ensure_ascii=False,
            indent=2,
        )
        if hasattr(self, "fallback_json_text"):
            self.fallback_json_text.configure(state="normal")
            self.fallback_json_text.delete("1.0", "end")
            self.fallback_json_text.insert("1.0", fallback_text)
            self.fallback_json_text.configure(state="disabled")

    def _current_safe_diagnostic(self):
        sel = self.url_tree.selection()
        if not sel:
            return None
        focus = self.url_tree.focus()
        current = focus if focus in sel else sel[-1]
        return self.address_rows.get(current)

    def copy_safe_diagnostic(self):
        data = self._current_safe_diagnostic()
        if not data:
            return messagebox.showwarning(
                "提示",
                "请先选择一条视频地址记录。",
            )

        self._copy(
            json.dumps(
                data,
                ensure_ascii=False,
                indent=2,
            ),
            "安全诊断 JSON",
        )

    def export_safe_diagnostic(self):
        data = self._current_safe_diagnostic()
        if not data:
            return messagebox.showwarning(
                "提示",
                "请先选择一条视频地址记录。",
            )

        default_name = (
            f"episode_{data.get('episode', 'unknown')}_"
            f"{data.get('quality', 'unknown')}_diagnostic.json"
        )

        path = filedialog.asksaveasfilename(
            title="导出安全诊断 JSON",
            defaultextension=".json",
            initialfile=default_name,
            filetypes=[("JSON", "*.json")],
        )

        if not path:
            return

        Path(path).write_text(
            json.dumps(
                data,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        self.log(f"安全诊断 JSON 已导出：{path}")

    def _current_fallback_json(self):
        data = self._current_safe_diagnostic()
        if not data:
            return None
        return data.get("safe_fallback_response", {})

    def copy_fallback_json(self):
        obj = self._current_fallback_json()
        if obj is None:
            return messagebox.showwarning(
                "提示",
                "请先选择一条视频地址记录。",
            )
        self._copy(
            json.dumps(
                obj,
                ensure_ascii=False,
                indent=2,
            ),
            "fallback_api 测试响应 JSON",
        )

    def export_fallback_json(self):
        obj = self._current_fallback_json()
        data = self._current_safe_diagnostic()
        if obj is None or not data:
            return messagebox.showwarning(
                "提示",
                "请先选择一条视频地址记录。",
            )

        default_name = (
            f"episode_{data.get('episode', 'unknown')}_"
            f"{data.get('quality', 'unknown')}_fallback_response.json"
        )

        path = filedialog.asksaveasfilename(
            title="导出 fallback_api 测试响应 JSON",
            defaultextension=".json",
            initialfile=default_name,
            filetypes=[("JSON", "*.json")],
        )
        if not path:
            return

        Path(path).write_text(
            json.dumps(
                obj,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        self.log(f"fallback_api 测试响应 JSON 已导出：{path}")

    def _copy(self, value, label):
        if not value:
            return messagebox.showwarning("提示", f"当前没有可复制的{label}。")
        self.clipboard_clear()
        self.clipboard_append(value)
        self.update()
        self.log(f"已复制{label}。")

    def copy_media_url(self):
        self._copy(self.media_url.get().strip(), "原始 main_url")

    def copy_fallback_url(self):
        self._copy(self.fallback_url.get().strip(), "fallback_api")
