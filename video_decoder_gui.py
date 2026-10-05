#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import base64
import hashlib
import json
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import urllib.request
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

FIXED_64 = bytes([
    77, 212, 194, 230, 184, 49, 98, 9, 14, 82, 179, 199, 166, 115, 59, 164,
    28, 178, 70, 43, 130, 154, 181, 138, 25, 107, 57, 219, 87, 23, 117, 36,
    244, 155, 175, 127, 8, 232, 214, 141, 38, 167, 46, 55, 193, 169, 90, 47,
    31, 5, 165, 24, 146, 174, 242, 148, 151, 50, 182, 42, 56, 170, 221, 88,
])

DEFAULT_UA = "com.phoenix.read/71332"


def b64url_decode(value: str) -> bytes:
    value = (value or "").strip()
    if not value:
        return b""
    normalized = value.replace("-", "+").replace("_", "/")
    if len(normalized) % 4 == 1:
        raise ValueError(f"Base64URL 长度非法：{len(normalized)} mod 4 = 1")
    normalized += "=" * ((4 - len(normalized) % 4) % 4)
    try:
        return base64.b64decode(normalized, validate=True)
    except Exception as exc:
        raise ValueError(f"Base64URL 解码失败：{exc}") from exc


def derive_url_key_iv(key_seed: str):
    seed = b64url_decode(key_seed)
    h1 = hashlib.sha512(seed).digest()
    h2 = hashlib.sha512(h1 + FIXED_64).digest()
    return h2[:16], h2[16:32], seed, h1, h2


def decrypt_main_url(main_url: str, key_seed: str):
    key, iv, seed, h1, h2 = derive_url_key_iv(key_seed)
    raw = b64url_decode(main_url)
    if len(raw) <= 4:
        raise ValueError("main_url 解码后长度不足，无法移除 4 字节头部")

    header = raw[:4]
    ciphertext = raw[4:]
    if not ciphertext or len(ciphertext) % 16 != 0:
        raise ValueError(
            f"main_url 密文长度必须是 16 的整数倍，当前为 {len(ciphertext)} bytes"
        )

    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    plain_raw = decryptor.update(ciphertext) + decryptor.finalize()
    plain = plain_raw

    if plain:
        pad = plain[-1]
        if 1 <= pad <= 16 and pad <= len(plain):
            plain = plain[:-pad]
    plain = plain.rstrip(b"\x00")
    url = plain.decode("latin-1").strip()

    return {
        "url": url,
        "seed": seed,
        "sha512_1": h1,
        "sha512_2": h2,
        "aes_key": key,
        "aes_iv": iv,
        "header": header,
        "ciphertext": ciphertext,
        "plain_raw": plain_raw,
        "plain": plain,
    }


def js_parse_hex_prefix(value: str) -> int:
    m = re.match(r"^[0-9a-fA-F]+", value)
    return int(m.group(0), 16) if m else 0


def derive_cenc_key(spade_a: str):
    raw = b64url_decode(spade_a)
    if len(raw) < 3:
        raise ValueError("spade_a 解码后至少需要 3 bytes")

    n = raw[0] ^ raw[1] ^ raw[2]
    r = len(raw) - n + 47
    if r <= 0 or r > 2 * len(raw):
        r = len(raw) - 1

    buf = bytearray(raw[1:1 + r])
    even_state = 85
    odd_state = 246

    for index in range(r):
        current = buf[index] if index < len(buf) else 0
        if index & 1:
            previous = even_state
            even_state = current
        else:
            previous = odd_state
            odd_state = current
        # transformed = (-21 - index.bit_count() + (previous ^ current)) & 0xFF
        meimei = bin(index).count("1")
        transformed = (-21 - meimei + (previous ^ current)) & 0xFF
        if index < len(buf):
            buf[index] = transformed

    key_text_bytes = bytes(buf[1:33])
    key_text = key_text_bytes.decode("latin-1")
    key_bytes = bytearray(16)
    parts = []

    for index in range(16):
        part = key_text[index * 2:index * 2 + 2]
        value = js_parse_hex_prefix(part) & 0xFF
        key_bytes[index] = value
        parts.append((index, part, value))

    return {
        "raw": raw,
        "n": n,
        "r": r,
        "transformed": bytes(buf),
        "key_text": key_text,
        "key_text_bytes": key_text_bytes,
        "key": bytes(key_bytes).hex(),
        "parts": parts,
    }


def download_file(url: str, destination: Path, ua: str, progress_cb=None):
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=120) as response, destination.open("wb") as f:
        total_text = response.headers.get("Content-Length")
        total = int(total_text) if total_text and total_text.isdigit() else 0
        downloaded = 0
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)
            downloaded += len(chunk)
            if progress_cb:
                progress_cb(downloaded, total)


def ffmpeg_decrypt(src: Path, dst: Path, cenc_key: str, ffmpeg: str, log_cb=None):
    if not re.fullmatch(r"[0-9a-fA-F]{32}", cenc_key):
        raise ValueError("CENC key 必须是 32 个十六进制字符")

    ffmpeg_path = shutil.which(ffmpeg) or (ffmpeg if Path(ffmpeg).exists() else None)
    if not ffmpeg_path:
        raise FileNotFoundError("找不到 FFmpeg，请安装后确认 ffmpeg -version 可执行")

    cmd = [
        str(ffmpeg_path), "-y", "-decryption_key", cenc_key,
        "-i", str(src), "-c", "copy", "-movflags", "+faststart", str(dst),
    ]
    if log_cb:
        log_cb("FFmpeg 命令：\n" + " ".join(cmd))

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        if log_cb:
            log_cb(line.rstrip())
    code = proc.wait()
    if code != 0:
        raise RuntimeError(f"FFmpeg 执行失败，退出码 {code}")


class DecoderGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("教学项目视频解码验证工具")
        self.geometry("1050x820")
        self.minsize(820, 680)

        self.msg_queue = queue.Queue()
        self.worker = None
        self.last_decode = None
        self.last_spade = None

        self.main_url_var = tk.StringVar()
        self.key_seed_var = tk.StringVar()
        self.spade_a_var = tk.StringVar()
        self.output_var = tk.StringVar(value=str(Path.cwd() / "output.mp4"))
        self.ffmpeg_var = tk.StringVar(value="ffmpeg")
        self.ua_var = tk.StringVar(value=DEFAULT_UA)
        self.keep_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="就绪")
        self.progress_var = tk.DoubleVar(value=0.0)

        self._build_ui()
        self.after(100, self._poll_queue)

    def _build_ui(self):
        root = ttk.Frame(self, padding=12)
        root.pack(fill="both", expand=True)
        root.columnconfigure(1, weight=1)
        root.rowconfigure(8, weight=1)

        ttk.Label(root, text="main_url").grid(row=0, column=0, sticky="nw", padx=(0, 8), pady=4)
        self.main_text = ScrolledText(root, height=3, wrap="word")
        self.main_text.grid(row=0, column=1, columnspan=3, sticky="nsew", pady=4)

        ttk.Label(root, text="key_seed").grid(row=1, column=0, sticky="nw", padx=(0, 8), pady=4)
        self.seed_text = ScrolledText(root, height=2, wrap="word")
        self.seed_text.grid(row=1, column=1, columnspan=3, sticky="ew", pady=4)

        ttk.Label(root, text="spade_a").grid(row=2, column=0, sticky="nw", padx=(0, 8), pady=4)
        self.spade_text = ScrolledText(root, height=2, wrap="word")
        self.spade_text.grid(row=2, column=1, columnspan=3, sticky="ew", pady=4)

        ttk.Label(root, text="输出文件").grid(row=3, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(root, textvariable=self.output_var).grid(row=3, column=1, sticky="ew", pady=4)
        ttk.Button(root, text="选择…", command=self.choose_output).grid(row=3, column=2, padx=6)
        ttk.Checkbutton(root, text="保留加密原文件", variable=self.keep_var).grid(row=3, column=3, sticky="w")

        ttk.Label(root, text="FFmpeg").grid(row=4, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(root, textvariable=self.ffmpeg_var).grid(row=4, column=1, sticky="ew", pady=4)
        ttk.Label(root, text="User-Agent").grid(row=4, column=2, sticky="e", padx=(8, 6))
        ttk.Entry(root, textvariable=self.ua_var, width=26).grid(row=4, column=3, sticky="ew")

        action_bar = ttk.Frame(root)
        action_bar.grid(row=5, column=0, columnspan=4, sticky="ew", pady=(8, 6))
        self.load_btn = ttk.Button(action_bar, text="导入 JSON", command=self.load_json)
        self.load_btn.pack(side="left", padx=(0, 6))
        self.demo_btn = ttk.Button(action_bar, text="填入演示数据", command=self.fill_demo)
        self.demo_btn.pack(side="left", padx=6)
        self.decode_btn = ttk.Button(action_bar, text="① 只验证解码", command=self.start_decode)
        self.decode_btn.pack(side="left", padx=6)
        self.run_btn = ttk.Button(action_bar, text="② 下载并解密", command=self.start_download)
        self.run_btn.pack(side="left", padx=6)
        ttk.Button(action_bar, text="清空日志", command=self.clear_log).pack(side="right")

        result = ttk.LabelFrame(root, text="解析结果", padding=8)
        result.grid(row=6, column=0, columnspan=4, sticky="ew", pady=6)
        result.columnconfigure(1, weight=1)

        self.real_url_var = tk.StringVar(value="-")
        self.aes_key_var = tk.StringVar(value="-")
        self.aes_iv_var = tk.StringVar(value="-")
        self.cenc_key_var = tk.StringVar(value="-")

        labels = [
            ("真实 URL", self.real_url_var),
            ("AES KEY", self.aes_key_var),
            ("AES IV", self.aes_iv_var),
            ("CENC KEY", self.cenc_key_var),
        ]
        for row, (name, var) in enumerate(labels):
            ttk.Label(result, text=name).grid(row=row, column=0, sticky="nw", padx=(0, 8), pady=2)
            entry = ttk.Entry(result, textvariable=var, state="readonly")
            entry.grid(row=row, column=1, sticky="ew", pady=2)

        progress_frame = ttk.Frame(root)
        progress_frame.grid(row=7, column=0, columnspan=4, sticky="ew", pady=(4, 6))
        progress_frame.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(progress_frame, variable=self.progress_var, maximum=100)
        self.progress.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ttk.Label(progress_frame, textvariable=self.status_var).grid(row=0, column=1, sticky="e")

        log_frame = ttk.LabelFrame(root, text="详细日志 / 中间值", padding=6)
        log_frame.grid(row=8, column=0, columnspan=4, sticky="nsew")
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)
        self.log = ScrolledText(log_frame, wrap="word", font=("TkFixedFont", 10))
        self.log.grid(row=0, column=0, sticky="nsew")

    def get_params(self):
        return (
            self.main_text.get("1.0", "end").strip(),
            self.seed_text.get("1.0", "end").strip(),
            self.spade_text.get("1.0", "end").strip(),
        )

    def set_text(self, widget, value):
        widget.delete("1.0", "end")
        widget.insert("1.0", value or "")

    def load_json(self):
        path = filedialog.askopenfilename(
            title="选择参数 JSON", filetypes=[("JSON", "*.json"), ("所有文件", "*.*")]
        )
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            self.set_text(self.main_text, data.get("main_url", ""))
            self.set_text(self.seed_text, data.get("key_seed", ""))
            self.set_text(self.spade_text, data.get("spade_a", ""))
            self._log(f"已加载：{path}")
        except Exception as exc:
            messagebox.showerror("加载失败", str(exc))

    def fill_demo(self):
        self.set_text(self.main_text, "qAABDEMOuF7x9KJ3mN5pR8sT2vW4yZ6aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789ABCDEF==")
        self.set_text(self.seed_text, "demo_key_seed_7f3a9c2e_2026_teaching_only")
        self.set_text(self.spade_text, "U1BBREVfREVNT19DRU5DX01FVEFEQVRBX05PVF9SRUFM")
        self._log("已填入演示占位数据。它用于测试错误提示，不代表可成功解密的真实测试向量。")

    def choose_output(self):
        path = filedialog.asksaveasfilename(
            title="选择输出 MP4", defaultextension=".mp4", filetypes=[("MP4 视频", "*.mp4"), ("所有文件", "*.*")]
        )
        if path:
            self.output_var.set(path)

    def clear_log(self):
        self.log.delete("1.0", "end")

    def _log(self, text):
        self.log.insert("end", str(text) + "\n")
        self.log.see("end")

    def post(self, kind, payload=None):
        self.msg_queue.put((kind, payload))

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.msg_queue.get_nowait()
                if kind == "log":
                    self._log(payload)
                elif kind == "status":
                    self.status_var.set(payload)
                elif kind == "progress":
                    self.progress_var.set(payload)
                elif kind == "result":
                    d, s = payload
                    self.real_url_var.set(d["url"])
                    self.aes_key_var.set(d["aes_key"].hex())
                    self.aes_iv_var.set(d["aes_iv"].hex())
                    self.cenc_key_var.set(s["key"] if s else "(无 spade_a)")
                    self.last_decode, self.last_spade = d, s
                elif kind == "error":
                    messagebox.showerror("执行失败", payload)
                elif kind == "done":
                    self._set_busy(False)
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _set_busy(self, busy: bool):
        state = "disabled" if busy else "normal"
        for btn in (self.load_btn, self.demo_btn, self.decode_btn, self.run_btn):
            btn.configure(state=state)
        if not busy:
            self.status_var.set("就绪")

    def _snapshot(self):
        main_url, key_seed, spade_a = self.get_params()
        return {
            "main_url": main_url,
            "key_seed": key_seed,
            "spade_a": spade_a,
            "output": self.output_var.get(),
            "ffmpeg": self.ffmpeg_var.get(),
            "ua": self.ua_var.get(),
            "keep": self.keep_var.get(),
        }

    def start_decode(self):
        if self.worker and self.worker.is_alive():
            return
        params = self._snapshot()
        self._set_busy(True)
        self.progress_var.set(0)
        self.worker = threading.Thread(target=self._decode_worker, args=(params,), daemon=True)
        self.worker.start()

    def start_download(self):
        if self.worker and self.worker.is_alive():
            return
        params = self._snapshot()
        self._set_busy(True)
        self.progress_var.set(0)
        self.worker = threading.Thread(target=self._download_worker, args=(params,), daemon=True)
        self.worker.start()

    def _decode_all(self, params):
        main_url = params["main_url"]
        key_seed = params["key_seed"]
        spade_a = params["spade_a"]
        if not main_url or not key_seed:
            raise ValueError("main_url 和 key_seed 不能为空")

        self.post("status", "正在解码参数…")
        d = decrypt_main_url(main_url, key_seed)
        s = derive_cenc_key(spade_a) if spade_a else None

        self.post("log", "\n========== main_url 解码 ==========")
        self.post("log", f"key_seed decode: {d['seed'].hex()}")
        self.post("log", f"SHA512 #1: {d['sha512_1'].hex()}")
        self.post("log", f"SHA512 #2: {d['sha512_2'].hex()}")
        self.post("log", f"AES KEY: {d['aes_key'].hex()}")
        self.post("log", f"AES IV : {d['aes_iv'].hex()}")
        self.post("log", f"4-byte header: {d['header'].hex()}")
        self.post("log", f"ciphertext bytes: {len(d['ciphertext'])}")
        self.post("log", f"plain raw hex: {d['plain_raw'].hex()}")
        self.post("log", f"真实 URL: {d['url']}")

        if s:
            self.post("log", "\n========== spade_a 解码 ==========")
            self.post("log", f"decoded bytes: {s['raw'].hex()}")
            self.post("log", f"decoded text : {s['raw'].decode('latin-1')!r}")
            self.post("log", f"n = {s['n']}, r = {s['r']}")
            self.post("log", f"transformed: {s['transformed'].hex()}")
            self.post("log", f"key text: {s['key_text']!r}")
            for idx, part, value in s["parts"]:
                self.post("log", f"key[{idx:02d}] <- {part!r:6s} = 0x{value:02x}")
            self.post("log", f"最终 CENC KEY: {s['key']}")
        else:
            self.post("log", "未提供 spade_a：跳过 CENC key 派生。")

        self.post("result", (d, s))
        return d, s

    def _decode_worker(self, params):
        try:
            self._decode_all(params)
            self.post("progress", 100)
            self.post("status", "参数验证完成")
        except Exception as exc:
            self.post("error", f"{type(exc).__name__}: {exc}")
            self.post("status", "失败")
        finally:
            self.post("done")

    def _download_worker(self, params):
        temp_dir = None
        try:
            d, s = self._decode_all(params)
            url = d["url"]
            if not url.startswith(("http://", "https://")):
                raise ValueError(f"main_url 解出的结果不是 HTTP(S) URL：{url!r}")

            output = Path(params["output"]).expanduser().resolve()
            output.parent.mkdir(parents=True, exist_ok=True)
            ua = params["ua"].strip() or DEFAULT_UA

            if not s:
                self.post("status", "正在直接下载…")
                download_file(url, output, ua, self._progress_download)
                self.post("log", f"完成：{output}")
                self.post("progress", 100)
                return

            temp_dir = Path(tempfile.mkdtemp(prefix="video_decoder_gui_"))
            encrypted = temp_dir / "encrypted.bin"
            self.post("status", "正在下载加密视频…")
            download_file(url, encrypted, ua, self._progress_download)
            self.post("log", f"下载完成：{encrypted} ({encrypted.stat().st_size} bytes)")

            self.post("status", "正在调用 FFmpeg 解密…")
            self.post("progress", 0)
            ffmpeg_decrypt(
                encrypted, output, s["key"], params["ffmpeg"].strip() or "ffmpeg",
                lambda line: self.post("log", line),
            )

            if params["keep"]:
                kept = output.with_name(output.name + ".encrypted.bin")
                shutil.copy2(encrypted, kept)
                self.post("log", f"已保留加密原文件：{kept}")

            self.post("progress", 100)
            self.post("status", "完成")
            self.post("log", f"输出视频：{output}")
        except Exception as exc:
            self.post("error", f"{type(exc).__name__}: {exc}")
            self.post("status", "失败")
        finally:
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)
            self.post("done")

    def _progress_download(self, downloaded: int, total: int):
        if total > 0:
            pct = min(100.0, downloaded * 100.0 / total)
            self.post("progress", pct)
            self.post("status", f"下载中 {pct:.1f}% ({downloaded}/{total} bytes)")
        else:
            self.post("status", f"已下载 {downloaded / 1024 / 1024:.2f} MB")


def main():
    parser = argparse.ArgumentParser(description="教学项目视频解码 GUI")
    parser.parse_args()
    app = DecoderGUI()
    app.mainloop()


if __name__ == "__main__":
    main()
