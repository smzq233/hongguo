# -*- coding: utf-8 -*-
"""
只解析分享链接并打印 APK 实际使用的 chapter_ids。

运行：
    python inspect_episode_ids.py "https://novelquickapp.com/s/xxxx/"
"""

import sys
import requests

from hongguo.share_parser import ShareParser
from hongguo.series_parser import SeriesParser


def main():
    if len(sys.argv) < 2:
        print("用法：python inspect_episode_ids.py <分享链接>")
        raise SystemExit(2)

    link = sys.argv[1]

    session = requests.Session()

    share = ShareParser(session).resolve(link)
    series = SeriesParser(session).from_share(share)

    print("剧名：", series.name)
    print("APK入口ID：", series.series_id)
    print("分享页 video_id：", share.video_id or "-")
    print("分享页 vid：", share.vid or "-")
    print("总集数：", series.total)
    print()

    for ep in series.episodes:
        print(
            f"第 {ep.index:03d} 集 -> "
            f"chapter_id/vid = {ep.vid or ep.chapter_id or '-'}"
        )


if __name__ == "__main__":
    main()
