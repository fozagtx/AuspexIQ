"""Baselines, saturation score, and verdict. Pure functions — no I/O, no API calls."""

import re
from collections import defaultdict
from datetime import datetime, timedelta
from statistics import median

import config

_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)


def duration_seconds(iso_duration):
    """Parse an ISO 8601 duration (e.g. PT4M13S) into seconds. Unparseable → 0."""
    if not iso_duration:
        return 0
    match = _DURATION_RE.match(iso_duration)
    if not match:
        return 0
    days, hours, minutes, seconds = (int(g) if g else 0 for g in match.groups())
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def parse_timestamp(value):
    """Parse an RFC3339 timestamp from the API into an aware datetime."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def is_short(seconds):
    return seconds <= config.SHORTS_MAX_SECONDS


def lifetime_average(view_count, video_count):
    if not video_count:
        return 0.0
    return view_count / video_count


def recent_median_baseline(uploads, now):
    """Median views of uploads older than BASELINE_MIN_AGE_DAYS and longer than the
    Shorts cutoff. Returns None when fewer than BASELINE_MIN_VIDEOS qualify (caller
    falls back to the channel's lifetime average)."""
    cutoff = now - timedelta(days=config.BASELINE_MIN_AGE_DAYS)
    qualifying = [
        v["views"]
        for v in uploads
        if v["views"] is not None
        and v["published_at"] is not None
        and v["published_at"] < cutoff
        and not is_short(v["seconds"])
    ]
    if len(qualifying) < config.BASELINE_MIN_VIDEOS:
        return None
    return float(median(qualifying))


def outlier_multiple(views, baseline):
    """views / baseline, or None when the baseline is below BASELINE_FLOOR
    (avoids absurd multiples on dead channels)."""
    if baseline < config.BASELINE_FLOOR:
        return None
    return views / baseline


def assess_niche(videos, outlier_records, now):
    """Compute signals, saturation score, verdict, and reasons over the analyzed set.

    videos: normalized video dicts (post Shorts/hidden-count filtering).
    outlier_records: dicts carrying at least 'subs' (None when hidden) per outlier.
    Returns (saturation, verdict, reasons, signals).
    """
    n = len(videos)
    unique_channels = {v["channel_id"] for v in videos}
    u = len(unique_channels)
    diversity = u / n if n else 0.0

    views_by_channel = defaultdict(int)
    for v in videos:
        views_by_channel[v["channel_id"]] += v["views"]
    total_views = sum(views_by_channel.values())
    top3_views = sum(sorted(views_by_channel.values(), reverse=True)[:3])
    c3 = top3_views / total_views if total_views else 0.0

    fresh_cutoff = now - timedelta(days=config.FRESH_WINDOW_DAYS)
    fresh_count = sum(1 for v in videos if v["published_at"] >= fresh_cutoff)
    f90 = fresh_count / n if n else 0.0

    sw = sum(
        1
        for r in outlier_records
        if r["subs"] is not None and r["subs"] < config.SMALL_CHANNEL_SUBS
    )

    openness = (
        config.OPENNESS_W_DIVERSITY * diversity
        + config.OPENNESS_W_CONCENTRATION * (1 - c3)
        + config.OPENNESS_W_FRESHNESS * f90
        + config.OPENNESS_W_SMALL_OUTLIERS
        * min(sw, config.SMALL_OUTLIERS_CAP)
        / config.SMALL_OUTLIERS_CAP
    )
    saturation = round(100 - openness)

    if (
        saturation <= config.VERDICT_ENTER_MAX_SATURATION
        and sw >= config.VERDICT_ENTER_MIN_SMALL_OUTLIERS
    ):
        verdict = "ENTER"
        lead = (
            f"saturation {saturation}/100 is at or below "
            f"{config.VERDICT_ENTER_MAX_SATURATION} with {sw} small-channel "
            f"breakout(s); there is room for a new entrant"
        )
    elif (
        saturation > config.VERDICT_AVOID_MIN_SATURATION
        or f90 < config.VERDICT_AVOID_MAX_FRESH_SHARE
    ):
        verdict = "AVOID"
        parts = []
        if saturation > config.VERDICT_AVOID_MIN_SATURATION:
            parts.append(
                f"saturation {saturation}/100 exceeds {config.VERDICT_AVOID_MIN_SATURATION}"
            )
        if f90 < config.VERDICT_AVOID_MAX_FRESH_SHARE:
            parts.append(
                f"only {round(f90 * 100)}% of analyzed videos are from the last "
                f"{config.FRESH_WINDOW_DAYS} days; the niche looks stale"
            )
        lead = " and ".join(parts)
    else:
        verdict = "CROWDED"
        lead = (
            f"saturation {saturation}/100 sits between "
            f"{config.VERDICT_ENTER_MAX_SATURATION} and "
            f"{config.VERDICT_AVOID_MIN_SATURATION}; established channels dominate "
            f"but the niche is not closed"
        )

    reasons = [lead]
    if n == 0:
        reasons.append(
            "no qualifying long-form videos were found for this query in the recency window"
        )
    reasons.extend(
        [
            f"{u} unique channels across {n} analyzed videos",
            f"top 3 channels hold {round(c3 * 100)}% of the views in the result set",
            f"{round(f90 * 100)}% of analyzed videos were published in the last "
            f"{config.FRESH_WINDOW_DAYS} days",
            f"{sw} outlier video(s) came from channels under "
            f"{config.SMALL_CHANNEL_SUBS:,} subscribers",
        ]
    )

    signals = {
        "channel_diversity": round(diversity, 3),
        "top3_view_concentration": round(c3, 3),
        "fresh_share_90d": round(f90, 3),
        "small_channel_outliers": sw,
    }
    return saturation, verdict, reasons, signals
