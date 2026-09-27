"""YouTube transcript tool (youtube-transcript-api; no API key needed).

Only the video id is accepted (validated), never a URL, so the tool cannot be
pointed at another host.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from app.config import get_settings
from app.core.errors import InvalidInputError, ToolError, TransientError
from app.mcp.schemas import ToolContext

VIDEO_ID_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:[^\s#]*&)?v=|shorts/|embed/|live/)|youtu\.be/)([A-Za-z0-9_-]{11})",
    re.IGNORECASE,
)
MAX_SEGMENTS = 6_000


class TranscriptInput(BaseModel):
    video_id: str = Field(pattern=r"^[A-Za-z0-9_-]{11}$")
    languages: list[str] = Field(default_factory=list, max_length=5)


def available() -> str:
    if not get_settings().youtube_transcripts_enabled:
        return "not_configured"
    try:
        import youtube_transcript_api  # noqa: F401
    except ImportError:
        return "not_configured"
    return "available"


def extract_video_ids(text: str) -> list[str]:
    return list(dict.fromkeys(VIDEO_ID_RE.findall(text)))[:2]


def get_transcript(arguments: TranscriptInput, context: ToolContext) -> dict[str, Any]:
    if available() != "available":
        raise ToolError("YouTube transcripts are not available (youtube-transcript-api missing or disabled).", retryable=False)
    from youtube_transcript_api import YouTubeTranscriptApi
    from youtube_transcript_api import _errors as errors

    languages = [*arguments.languages, *get_settings().youtube_languages, "en"]
    try:
        fetched = YouTubeTranscriptApi().fetch(arguments.video_id, languages=list(dict.fromkeys(languages)))
    except (errors.TranscriptsDisabled, errors.NoTranscriptFound, errors.VideoUnavailable, errors.InvalidVideoId) as exc:
        raise InvalidInputError(f"No transcript is available for this video ({exc.__class__.__name__}).") from None
    except errors.CouldNotRetrieveTranscript as exc:  # blocked IP, age restriction, network, ...
        raise ToolError(f"YouTube did not return a transcript ({exc.__class__.__name__}).", retryable=False) from None
    except OSError:  # includes requests' connection errors and timeouts
        raise TransientError("YouTube could not be reached.") from None
    segments = [
        {"start": round(float(snippet.start), 2), "duration": round(float(snippet.duration), 2), "text": snippet.text}
        for snippet in list(fetched)[:MAX_SEGMENTS]
        if snippet.text.strip()
    ]
    return {
        "video_id": arguments.video_id,
        "language": getattr(fetched, "language_code", None),
        "generated": getattr(fetched, "is_generated", None),
        "segments": segments,
    }
