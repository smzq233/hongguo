from pathlib import Path

from hongguo.gui import HongguoApp


# =============================================================================
# 用户常用配置：以后主要改这里即可
# =============================================================================
BASE_DIR = Path(__file__).resolve().parent

# 视频最终保存根目录。
# Windows 也可以直接写成：Path(r"D:\Video\Hongguo")
VIDEO_SAVE_DIR = BASE_DIR / "downloads"

# 运行数据目录：日志和播放器 JSON 都统一放到这里，不与视频混在一起。
RUNTIME_DIR = BASE_DIR / "runtime"
LOG_DIR = RUNTIME_DIR / "logs"
JSON_DIR = RUNTIME_DIR / "json"

# FFmpeg：已经加入 PATH 时保持 "ffmpeg" 即可；否则填写 ffmpeg.exe 完整路径。
FFMPEG_PATH = "ffmpeg"

# 是否保留处理中产生的原始媒体文件。
KEEP_ENCRYPTED_SOURCE = False


if __name__ == "__main__":
    HongguoApp(
        download_dir=VIDEO_SAVE_DIR,
        log_dir=LOG_DIR,
        json_dir=JSON_DIR,
        ffmpeg=FFMPEG_PATH,
        keep_encrypted=KEEP_ENCRYPTED_SOURCE,
    ).mainloop()
