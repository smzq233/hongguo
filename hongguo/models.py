from __future__ import annotations
from dataclasses import dataclass, field, asdict

@dataclass
class ShareResult:
    original_url: str
    final_url: str
    content_id: str = ""
    series_id: str = ""
    video_id: str = ""
    vid: str = ""
    title: str = ""
    html: str = ""
    metadata: dict = field(default_factory=dict)

@dataclass
class Episode:
    index: int
    vid: str = ""
    chapter_id: str = ""
    title: str = ""
    player_url: str = ""
    direct_url: str = ""
    accessible: bool = False
    protected: bool = False
    status: str = "待检测"

@dataclass
class VideoOption:
    definition: str
    label: str
    value: str
    width: int = 0
    height: int = 0
    bitrate: int = 0
    size_bytes: int = 0
    codec: str = ""
    raw_url: str = ""
    direct_url: str = ""
    key_seed: str = ""
    spade_a: str = ""
    has_spade_a: bool = False
    downloadable: bool = False
    protected: bool = False
    status: str = ""

@dataclass
class VideoResolveResult:
    vid: str
    fallback_api: str = ""
    options: list[VideoOption] = field(default_factory=list)
    raw_video_model: dict = field(default_factory=dict)
    raw_fallback: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)

@dataclass
class SeriesInfo:
    series_id: str
    name: str
    total: int
    cover: str = ""
    intro: str = ""
    source_url: str = ""
    episodes: list[Episode] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)
