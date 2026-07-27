"""YouTube Data API v3 client: quota guard, TTL cache, typed faults.

Every number the server returns originates from a live API response made here
at request time, or from the TTL cache of a previous live response. Nothing is
fabricated; failures surface as structured errors.
"""

import os
import time
from collections import OrderedDict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

import config
from src import analysis

API_BASE = "https://www.googleapis.com/youtube/v3"
_PACIFIC = ZoneInfo("America/Los_Angeles")

_QUOTA_UPSTREAM_REASONS = {"quotaExceeded", "dailyLimitExceeded"}


class ToolFault(Exception):
    """Structured tool failure, surfaced to the client as {"ok": false, "error": {...}}."""

    def __init__(self, code, message, retryable):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable

    def to_response(self):
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "retryable": self.retryable,
            },
        }


class RequestMeter:
    """Counts quota units spent by a single tool invocation (reported in meta)."""

    def __init__(self):
        self.units = 0


class QuotaGuard:
    """Tracks units spent per Google quota day (resets at midnight Pacific)."""

    def __init__(self, budget):
        self.budget = budget
        self._day = None
        self._spent = 0

    def _roll(self):
        today = datetime.now(_PACIFIC).date()
        if today != self._day:
            self._day = today
            self._spent = 0

    @property
    def remaining(self):
        self._roll()
        return max(0, self.budget - self._spent)

    def next_reset_iso(self):
        now = datetime.now(_PACIFIC)
        reset = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return reset.isoformat()

    def precheck(self, worst_case_units):
        self._roll()
        if worst_case_units > self.remaining:
            raise ToolFault(
                "QUOTA_EXHAUSTED",
                f"Daily YouTube quota budget is spent ({self._spent}/{self.budget} units "
                f"used today; this call needs up to {worst_case_units}). The budget "
                f"resets at midnight Pacific: {self.next_reset_iso()}. Retry after that.",
                True,
            )

    def spend(self, units, meter):
        self._roll()
        self._spent += units
        meter.units += units


quota = QuotaGuard(config.DAILY_UNIT_BUDGET)


class TTLCache:
    """In-memory TTL cache keyed on normalized inputs. Stores only real fetched
    responses (caching is not seeding). Capped; evicts oldest entries first."""

    def __init__(self, max_entries):
        self.max_entries = max_entries
        self._entries = OrderedDict()  # key -> (expires_at, payload, fetched_at_iso)

    def get(self, key):
        entry = self._entries.get(key)
        if entry is None:
            return None
        expires_at, payload, fetched_at = entry
        if time.monotonic() >= expires_at:
            del self._entries[key]
            return None
        return payload, fetched_at

    def put(self, key, payload, ttl_s, fetched_at):
        self._entries[key] = (time.monotonic() + ttl_s, payload, fetched_at)
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)


cache = TTLCache(config.CACHE_MAX_ENTRIES)

_client = httpx.AsyncClient(timeout=config.UPSTREAM_TIMEOUT_S)


def api_key_present():
    return bool(os.environ.get(config.YT_API_KEY_ENV, "").strip())


def _api_key():
    key = os.environ.get(config.YT_API_KEY_ENV, "").strip()
    if not key:
        raise ToolFault(
            "MISSING_API_KEY",
            f"{config.YT_API_KEY_ENV} is not configured on the server, so no live "
            f"YouTube data can be fetched. The operator must set it; retrying will "
            f"not help until then.",
            False,
        )
    return key


def _sanitize(text, key):
    return text.replace(key, "***") if key else text


def _error_details(response):
    try:
        err = response.json().get("error", {})
        errors = err.get("errors") or [{}]
        return errors[0].get("reason", "unknown"), err.get("message", response.text[:200])
    except Exception:
        return "unknown", response.text[:200]


async def _get(resource, params, cost, meter):
    key = _api_key()
    quota.spend(cost, meter)
    try:
        response = await _client.get(
            f"{API_BASE}/{resource}", params={**params, "key": key}
        )
    except httpx.TimeoutException:
        raise ToolFault(
            "UPSTREAM_TIMEOUT",
            f"YouTube API '{resource}' call exceeded {config.UPSTREAM_TIMEOUT_S}s. "
            f"Retry shortly.",
            True,
        )
    except httpx.HTTPError as exc:
        raise ToolFault(
            "YT_API_ERROR",
            _sanitize(f"YouTube API '{resource}' request failed: {exc}. Retry may help.", key),
            True,
        )
    if response.status_code == 200:
        return response.json()
    reason, message = _error_details(response)
    if reason in _QUOTA_UPSTREAM_REASONS:
        raise ToolFault(
            "QUOTA_EXHAUSTED",
            f"YouTube reports the API key's daily quota is exhausted. Quota resets "
            f"at midnight Pacific: {quota.next_reset_iso()}. Retry after that.",
            True,
        )
    raise ToolFault(
        "YT_API_ERROR",
        _sanitize(
            f"YouTube API '{resource}' returned HTTP {response.status_code} "
            f"({reason}): {message}. Retry may help.",
            key,
        ),
        True,
    )


async def search_videos(query, region_code, published_after_iso, max_results, meter):
    return await _get(
        "search",
        {
            "part": "snippet",
            "type": "video",
            "q": query,
            "order": "relevance",
            "maxResults": max_results,
            "regionCode": region_code,
            "publishedAfter": published_after_iso,
        },
        config.COST_SEARCH,
        meter,
    )


async def list_videos(video_ids, meter):
    return await _get(
        "videos",
        {
            "part": "snippet,statistics,contentDetails,liveStreamingDetails",
            "id": ",".join(video_ids),
        },
        config.COST_LIST,
        meter,
    )


async def list_channels(channel_ids, meter):
    return await _get(
        "channels",
        {"part": "snippet,statistics,contentDetails", "id": ",".join(channel_ids)},
        config.COST_LIST,
        meter,
    )


async def channel_by_handle(handle, meter):
    return await _get(
        "channels",
        {"part": "snippet,statistics,contentDetails", "forHandle": handle},
        config.COST_LIST,
        meter,
    )


async def playlist_page(playlist_id, max_results, page_token, meter):
    params = {
        "part": "contentDetails",
        "playlistId": playlist_id,
        "maxResults": max_results,
    }
    if page_token:
        params["pageToken"] = page_token
    return await _get("playlistItems", params, config.COST_LIST, meter)


def normalize_video(item):
    """Flatten a videos.list item. views is None when the count is hidden.
    stream is True for live, upcoming, and past live broadcasts."""
    snippet = item.get("snippet", {})
    stats = item.get("statistics", {})
    seconds = analysis.duration_seconds(item.get("contentDetails", {}).get("duration"))
    published = snippet.get("publishedAt")
    return {
        "id": item.get("id", ""),
        "title": snippet.get("title", "").strip(),
        "channel_id": snippet.get("channelId", ""),
        "channel_title": snippet.get("channelTitle", "").strip(),
        "published_at": analysis.parse_timestamp(published) if published else None,
        "views": int(stats["viewCount"]) if "viewCount" in stats else None,
        "seconds": seconds,
        "stream": snippet.get("liveBroadcastContent") in ("live", "upcoming")
        or "liveStreamingDetails" in item,
    }


def normalize_channel(item):
    """Flatten a channels.list item. subs is None when the count is hidden."""
    snippet = item.get("snippet", {})
    stats = item.get("statistics", {})
    hidden = stats.get("hiddenSubscriberCount", False)
    return {
        "id": item.get("id", ""),
        "title": snippet.get("title", "").strip(),
        "subs": None if hidden else int(stats.get("subscriberCount", 0)),
        "view_count": int(stats.get("viewCount", 0)),
        "video_count": int(stats.get("videoCount", 0)),
        "uploads": item.get("contentDetails", {})
        .get("relatedPlaylists", {})
        .get("uploads"),
    }
