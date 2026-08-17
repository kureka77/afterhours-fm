from datetime import datetime
from typing import Optional
from sqlalchemy.orm import Mapped, mapped_column
from database import Base


class PlayedTrack(Base):
    __tablename__ = "played_tracks"

    id:              Mapped[int]           = mapped_column(primary_key=True)
    title:           Mapped[str]
    artist:          Mapped[str]
    album:           Mapped[Optional[str]]
    station:         Mapped[Optional[str]] = mapped_column(default=None)
    started_at:      Mapped[datetime]
    spotify_url:     Mapped[Optional[str]]
    apple_music_url: Mapped[Optional[str]]
    deezer_url:      Mapped[Optional[str]]
    rating:          Mapped[Optional[int]] = mapped_column(default=None)  # 1=👍  -1=👎
