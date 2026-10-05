# Hongguo Downloader 一体化版

当前版本把原来的三个操作窗口整合到了 `main.py` 启动的主界面中，并增加了批量画质选择与方块选集视图。

## 现在的使用流程

1. 运行 `python main.py`。
2. 粘贴分享地址并解析剧集。
3. 可以在“剧集与下载”列表页选择集数，也可以切换到“方块选集”页，用正方形集数块点选需要的集数；再次点击同一方块即可取消。
4. 点击下载按钮，在弹出的画质窗口中选择 1080P / 720P / 540P / 480P / 360P 或“自动最高可用”。
5. 程序自动完成播放器地址解析、JSON 归档、参数提取和后续下载流程，不再需要手工启动 `json_media_analyzer_gui.py` 与 `video_decoder_gui.py`。

“视频地址”页支持多选：可以按 Ctrl / Shift 选择多条记录；也可以先点某个画质分类（例如 1080P），再点“全选当前画质”，一次选中该画质下所有已经解析出的集数。直接选中画质分类后点击“下载已选画质”，也会自动把该分类下全部视频作为一个批量下载任务。

## 方块选集视图

主界面新增“方块选集”页签：

- 每一集显示为一个真正的正方形选择块；
- 单击一次选中，选中后显示蓝色和 `✓`；
- 再点一次取消；
- 支持“全选 / 取消全选 / 反选”；
- 可以直接下载方块选中的集数；
- 也可以把方块选中的集数批量解析到“视频地址”页。

## 视频地址批量画质下载

“视频地址”页的列表现在使用多选模式。典型操作：

1. 解析全部集数的视频地址；
2. 点一下 `1080P · 全高清` 分类；
3. 可直接点击“下载已选画质”，程序会下载该分类下全部 1080P 视频；
4. 或点击“全选当前画质”，把这个画质下所有视频全部选中后，再按 Ctrl / Shift 继续调整需要的集数；
5. 最后点击“下载已选画质”进行批量下载。

## 修改视频保存位置

只需要修改 `main.py` 顶部：

```python
VIDEO_SAVE_DIR = BASE_DIR / "downloads"
```

例如 Windows：

```python
VIDEO_SAVE_DIR = Path(r"D:\Video\Hongguo")
```

FFmpeg 没有加入 PATH 时，可以同时修改：

```python
FFMPEG_PATH = r"D:\ffmpeg\bin\ffmpeg.exe"
```

## 文件目录

```text
hongguo_downloader/
├── main.py
├── runtime/
│   ├── logs/
│   │   └── hongguo_YYYYMMDD_HHMMSS.log
│   └── json/
│       └── 剧名_seriesId/
│           ├── episode_001_resolve.json
│           ├── episode_001_1080P_params.json
│           └── ...
└── downloads/
    └── 剧名_seriesId/
        ├── series_info.json
        ├── download_state.json
        ├── download_report.json
        ├── 001_第001集.mp4
        └── ...
```

其中 `series_info.json` 继续保留在每部剧的视频目录中；播放器接口返回的完整 JSON 和最终选择画质的参数 JSON 则统一进入 `runtime/json`。

## 安装

```powershell
pip install -r requirements.txt
python main.py
```

另外需要：

- Node.js：项目现有播放器 API 签名脚本需要使用；
- FFmpeg：媒体处理阶段使用。
