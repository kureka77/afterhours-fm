from datetime import datetime
from typing import Optional
from sqlalchemy import Index
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

    # Both indexes serve reads that were full table scans before.
    #
    # started_at: /api/track-history is ORDER BY started_at DESC LIMIT 50, which
    # without an index sorts the whole table to return 50 rows. Declared
    # ascending on purpose — a btree can be walked in either direction, so
    # Postgres and SQLite both satisfy a DESC ORDER BY from it with a backward
    # scan. A DESC index would only earn its keep for a mixed-direction
    # multi-column sort, which nothing here does.
    #
    # (artist, title): the "heard this before" lookup counts plays of one exact
    # track. Column order matters — this index also serves a query filtering on
    # artist alone (leftmost prefix), but not one filtering on title alone.
    __table_args__ = (
        Index("ix_played_tracks_started_at", "started_at"),
        Index("ix_played_tracks_artist_title", "artist", "title"),
    )
