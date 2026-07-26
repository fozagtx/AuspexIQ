"""AuspexIQ MCP server — live YouTube niche analysis.

Tools: scan_niche (niche saturation + outliers + ENTER/CROWDED/AVOID verdict)
and channel_outliers (which of a channel's videos overperformed its baseline).
"""

import asyncio
import hashlib
import json
import logging
import math
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlparse

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse
from x402.http import (
    OKXAuthConfig,
    OKXFacilitatorClient,
    OKXFacilitatorConfig,
    PaymentOption,
)
from x402.http.middleware.fastapi import PaymentMiddlewareASGI
from x402.http.types import RouteConfig
from x402.mechanisms.evm.deferred.server import AggrDeferredEvmScheme
from x402.mechanisms.evm.exact.server import ExactEvmScheme
from x402.server import x402ResourceServer

import config
from src import analysis, youtube
from src.youtube import RequestMeter, ToolFault

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("auspex")

if not youtube.api_key_present():
    log.critical(
        "%s is not set — every tool call will return MISSING_API_KEY until it is configured",
        config.YT_API_KEY_ENV,
    )

mcp = FastMCP(
    name="AuspexIQ",
    instructions=(
        "Live YouTube niche analysis for agents. scan_niche returns saturation, "
        "breakout outlier videos, and an ENTER/CROWDED/AVOID verdict for a niche "
        "keyword. channel_outliers reveals which of a channel's videos overperformed "
        "its own baseline. Every number comes from the YouTube Data API v3 at "
        "request time (or a short-TTL cache of a previous live response)."
    ),
)


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _watch_url(video_id):
    return "https://www.youtube.com/watch?" + urlencode({"v": video_id})


def _params_hash(values):
    return hashlib.sha256(repr(values).encode()).hexdigest()[:12]


def _meta(cache_state, fetched_at_iso, units_spent):
    return {
        "cache": cache_state,
        "fetched_at": fetched_at_iso,
        "quota_units_spent": units_spent,
        "quota_units_remaining_today": youtube.quota.remaining,
    }


def _log_request(tool, params_hash, cache_state, units, started, outcome):
    log.info(
        json.dumps(
            {
                "tool": tool,
                "params": params_hash,
                "cache": cache_state,
                "units": units,
                "latency_ms": round((time.monotonic() - started) * 1000),
                "outcome": outcome,
            }
        )
    )


def _dedupe(values):
    return list(dict.fromkeys(values))


def _chunks(values, size):
    for i in range(0, len(values), size):
        yield values[i : i + size]


async def _execute(tool, params_hash, cache_key, ttl_s, worst_case_units, run):
    """Shared request wrapper: cache lookup, quota precheck, pipeline, cache store,
    structured error conversion, one-line request log."""
    started = time.monotonic()
    meter = RequestMeter()
    try:
        cached = youtube.cache.get(cache_key)
        if cached is not None:
            payload, fetched_at = cached
            _log_request(tool, params_hash, "hit", 0, started, payload.get("verdict", "ok"))
            return {**payload, "meta": _meta("hit", fetched_at, 0)}
        youtube.quota.precheck(worst_case_units)
        payload = await run(meter)
        fetched_at = _iso(_now())
        youtube.cache.put(cache_key, payload, ttl_s, fetched_at)
        _log_request(tool, params_hash, "miss", meter.units, started, payload.get("verdict", "ok"))
        return {**payload, "meta": _meta("miss", fetched_at, meter.units)}
    except ToolFault as fault:
        _log_request(tool, params_hash, "miss", meter.units, started, fault.code)
        return fault.to_response()
    except Exception as exc:  # defensive: a tool must never raise to the client
        log.exception("unexpected failure in %s", tool)
        fault = ToolFault("YT_API_ERROR", f"Unexpected server error: {exc}. Retry may help.", True)
        _log_request(tool, params_hash, "miss", meter.units, started, fault.code)
        return fault.to_response()


# ---------------------------------------------------------------------------
# Tool 1: scan_niche
# ---------------------------------------------------------------------------


def _validate_scan(query, region_code, recency_days, max_results):
    if not isinstance(query, str) or not (
        config.QUERY_MIN_LEN <= len(query.strip()) <= config.QUERY_MAX_LEN
    ):
        raise ToolFault(
            "INVALID_INPUT",
            f"'query' must be a string of {config.QUERY_MIN_LEN}-{config.QUERY_MAX_LEN} "
            f"characters, e.g. a niche keyword like a topic phrase.",
            False,
        )
    region = (region_code or config.DEFAULT_REGION_CODE).strip().upper()
    if len(region) != 2 or not region.isalpha():
        raise ToolFault(
            "INVALID_INPUT",
            "'region_code' must be an ISO 3166-1 alpha-2 code such as 'US' or 'GB'.",
            False,
        )
    if not (config.RECENCY_DAYS_MIN <= recency_days <= config.RECENCY_DAYS_MAX):
        raise ToolFault(
            "INVALID_INPUT",
            f"'recency_days' must be between {config.RECENCY_DAYS_MIN} and "
            f"{config.RECENCY_DAYS_MAX}.",
            False,
        )
    if not (config.MAX_RESULTS_MIN <= max_results <= config.MAX_RESULTS_MAX):
        raise ToolFault(
            "INVALID_INPUT",
            f"'max_results' must be between {config.MAX_RESULTS_MIN} and "
            f"{config.MAX_RESULTS_MAX}.",
            False,
        )
    return query.strip(), region, int(recency_days), int(max_results)


async def _deep_baseline(channel, now, meter):
    """Baseline from the channel's recent uploads; falls back to the lifetime
    average (real numbers from channels.list) when uploads can't qualify."""
    fallback = (
        analysis.lifetime_average(channel["view_count"], channel["video_count"]),
        "lifetime_avg",
    )
    if not channel["uploads"]:
        return fallback
    try:
        page = await youtube.playlist_page(
            channel["uploads"], config.BASELINE_RECENT_UPLOADS, None, meter
        )
        upload_ids = [
            item["contentDetails"]["videoId"]
            for item in page.get("items", [])
            if item.get("contentDetails", {}).get("videoId")
        ]
        if not upload_ids:
            return fallback
        data = await youtube.list_videos(upload_ids, meter)
        uploads = [youtube.normalize_video(item) for item in data.get("items", [])]
        baseline = analysis.recent_median_baseline(uploads, now)
        if baseline is None:
            return fallback
        return baseline, "recent_median"
    except ToolFault as fault:
        if fault.code in ("QUOTA_EXHAUSTED", "MISSING_API_KEY"):
            raise
        return fallback


def _format_outlier(record):
    method_label = (
        "recent median" if record["baseline_method"] == "recent_median" else "lifetime average"
    )
    return {
        "title": record["title"],
        "url": _watch_url(record["id"]),
        "channel": record["channel_title"],
        "channel_subs": record["subs"] if record["subs"] is not None else 0,
        "views": record["views"],
        "published_at": _iso(record["published_at"]),
        "channel_baseline": round(record["baseline"]),
        "baseline_method": record["baseline_method"],
        "outlier_multiple": round(record["multiple"], 2),
        "why": f"{record['multiple']:.1f}x this channel's {method_label}",
    }


async def _scan_pipeline(query, region, recency_days, max_results, meter):
    now = _now()
    published_after = _iso(now - timedelta(days=recency_days))

    search = await youtube.search_videos(query, region, published_after, max_results, meter)
    video_ids = _dedupe(
        item["id"]["videoId"]
        for item in search.get("items", [])
        if item.get("id", {}).get("videoId")
    )

    videos = []
    if video_ids:
        data = await youtube.list_videos(video_ids, meter)
        for item in data.get("items", []):
            video = youtube.normalize_video(item)
            if video["views"] is None or video["published_at"] is None:
                continue  # hidden view count or missing metadata
            if analysis.is_short(video["seconds"]):
                continue
            if not video["channel_id"]:
                continue
            videos.append(video)

    channels_by_id = {}
    channel_ids = _dedupe(v["channel_id"] for v in videos)
    if channel_ids:
        data = await youtube.list_channels(channel_ids, meter)
        channels_by_id = {
            channel["id"]: channel
            for channel in (youtube.normalize_channel(item) for item in data.get("items", []))
        }
    videos = [v for v in videos if v["channel_id"] in channels_by_id]

    result_counts = Counter(v["channel_id"] for v in videos)
    views_in_set = defaultdict(int)
    for v in videos:
        views_in_set[v["channel_id"]] += v["views"]
    ranked = sorted(
        result_counts, key=lambda cid: (-result_counts[cid], -views_in_set[cid], cid)
    )
    deep_ids = ranked[: config.BASELINE_DEEP_CHANNELS]

    baselines = {}
    semaphore = asyncio.Semaphore(config.BASELINE_CONCURRENCY)

    async def deep(channel_id):
        async with semaphore:
            baselines[channel_id] = await _deep_baseline(channels_by_id[channel_id], now, meter)

    await asyncio.gather(*(deep(cid) for cid in deep_ids))
    for cid in result_counts:
        if cid not in baselines:
            channel = channels_by_id[cid]
            baselines[cid] = (
                analysis.lifetime_average(channel["view_count"], channel["video_count"]),
                "lifetime_avg",
            )

    outlier_records = []
    for v in videos:
        baseline, method = baselines[v["channel_id"]]
        multiple = analysis.outlier_multiple(v["views"], baseline)
        if multiple is not None and multiple >= config.SCAN_OUTLIER_MULTIPLE:
            outlier_records.append(
                {
                    **v,
                    "baseline": baseline,
                    "baseline_method": method,
                    "multiple": multiple,
                    "subs": channels_by_id[v["channel_id"]]["subs"],
                }
            )
    outlier_records.sort(key=lambda r: -r["multiple"])

    saturation, verdict, reasons, signals = analysis.assess_niche(videos, outlier_records, now)

    return {
        "ok": True,
        "query": query,
        "region_code": region,
        "analyzed": {
            "videos": len(videos),
            "channels": len(result_counts),
            "deep_baseline_channels": len(deep_ids),
        },
        "saturation_score": saturation,
        "verdict": verdict,
        "verdict_reasons": reasons,
        "signals": signals,
        "outliers": [
            _format_outlier(r) for r in outlier_records[: config.SCAN_OUTLIERS_MAX]
        ],
    }


async def _scan_request(query, region_code, recency_days, max_results):
    """Full scan_niche request: validation, cache, quota, pipeline. Shared by
    the MCP tool and the paid REST endpoint."""
    params_hash = _params_hash((query, region_code, recency_days, max_results))
    started = time.monotonic()
    try:
        q, region, days, n_results = _validate_scan(
            query, region_code, recency_days, max_results
        )
    except ToolFault as fault:
        _log_request("scan_niche", params_hash, "-", 0, started, fault.code)
        return fault.to_response()
    cache_key = ("scan_niche", q.lower(), region, days, n_results)
    return await _execute(
        "scan_niche",
        params_hash,
        cache_key,
        config.SCAN_CACHE_TTL_S,
        config.SCAN_WORST_CASE_UNITS,
        lambda meter: _scan_pipeline(q, region, days, n_results, meter),
    )


@mcp.tool
async def scan_niche(
    query: str,
    region_code: str = config.DEFAULT_REGION_CODE,
    recency_days: int = config.RECENCY_DAYS_DEFAULT,
    max_results: int = config.MAX_RESULTS_DEFAULT,
) -> dict:
    """Assess a YouTube niche keyword using live YouTube data: who ranks, how
    concentrated the niche is, which videos are outliers relative to their own
    channel's baseline, and an ENTER / CROWDED / AVOID verdict for a new entrant.

    query: the niche keyword (2-80 chars). region_code: ISO 3166-1 alpha-2
    (default US). recency_days: only consider videos published in this window
    (30-1825, default 365). max_results: search results to analyze (10-50).
    """
    return await _scan_request(query, region_code, recency_days, max_results)


# ---------------------------------------------------------------------------
# Tool 2: channel_outliers
# ---------------------------------------------------------------------------


def _validate_channel_inputs(channel, lookback_videos, min_multiple):
    if not isinstance(channel, str) or not channel.strip():
        raise ToolFault(
            "INVALID_INPUT",
            "'channel' is required: a channel ID (UC...), an @handle, or a full "
            "youtube.com channel URL.",
            False,
        )
    if not (config.LOOKBACK_VIDEOS_MIN <= lookback_videos <= config.LOOKBACK_VIDEOS_MAX):
        raise ToolFault(
            "INVALID_INPUT",
            f"'lookback_videos' must be between {config.LOOKBACK_VIDEOS_MIN} and "
            f"{config.LOOKBACK_VIDEOS_MAX}.",
            False,
        )
    if not (config.MIN_MULTIPLE_MIN <= min_multiple <= config.MIN_MULTIPLE_MAX):
        raise ToolFault(
            "INVALID_INPUT",
            f"'min_multiple' must be between {config.MIN_MULTIPLE_MIN} and "
            f"{config.MIN_MULTIPLE_MAX}.",
            False,
        )
    return channel.strip(), int(lookback_videos), float(min_multiple)


def _not_found(reference):
    return ToolFault(
        "CHANNEL_NOT_FOUND",
        f"Could not resolve '{reference}' to a YouTube channel. Pass a channel ID "
        f"(UC...), an @handle, or a full youtube.com channel URL.",
        False,
    )


async def _resolve_channel(reference, meter):
    ref = reference.strip()
    channel_id = None
    handle = None
    if ref.startswith("UC") and len(ref) == 24 and " " not in ref:
        channel_id = ref
    elif ref.startswith(("http://", "https://")) or "youtube.com" in ref or "youtu.be" in ref:
        parsed = urlparse(ref if "://" in ref else "https://" + ref)
        segments = [s for s in parsed.path.split("/") if s]
        if segments and segments[0] == "channel" and len(segments) > 1:
            channel_id = segments[1]
        elif segments:
            at_segments = [s for s in segments if s.startswith("@")]
            handle = at_segments[0][1:] if at_segments else segments[-1]
        else:
            raise _not_found(reference)
    elif ref.startswith("@"):
        handle = ref[1:]
    else:
        handle = ref
    if channel_id:
        data = await youtube.list_channels([channel_id], meter)
    else:
        if not handle:
            raise _not_found(reference)
        data = await youtube.channel_by_handle(handle, meter)
    items = data.get("items") or []
    if not items:
        raise _not_found(reference)
    return youtube.normalize_channel(items[0])


async def _channel_pipeline(channel_ref, lookback, min_multiple, meter):
    now = _now()
    channel = await _resolve_channel(channel_ref, meter)

    upload_ids = []
    if channel["uploads"]:
        page_token = None
        while len(upload_ids) < lookback:
            page_size = min(50, lookback - len(upload_ids))
            try:
                page = await youtube.playlist_page(
                    channel["uploads"], page_size, page_token, meter
                )
            except ToolFault as fault:
                # A brand-new channel's uploads playlist can 404; that is an
                # honest zero-uploads answer, not a server failure.
                if fault.code == "YT_API_ERROR" and "playlistNotFound" in fault.message:
                    break
                raise
            upload_ids.extend(
                item["contentDetails"]["videoId"]
                for item in page.get("items", [])
                if item.get("contentDetails", {}).get("videoId")
            )
            page_token = page.get("nextPageToken")
            if not page_token:
                break
    upload_ids = _dedupe(upload_ids)[:lookback]

    videos = []
    for batch in _chunks(upload_ids, 50):
        data = await youtube.list_videos(batch, meter)
        for item in data.get("items", []):
            video = youtube.normalize_video(item)
            if video["views"] is None or video["published_at"] is None:
                continue  # hidden view count or missing metadata
            videos.append(video)

    baseline = analysis.recent_median_baseline(videos, now)
    if baseline is None:
        baseline = analysis.lifetime_average(channel["view_count"], channel["video_count"])
        method = "lifetime_avg"
    else:
        method = "recent_median"

    outlier_records = []
    for v in videos:
        multiple = analysis.outlier_multiple(v["views"], baseline)
        if multiple is not None and multiple >= min_multiple:
            outlier_records.append(
                {
                    **v,
                    "channel_title": channel["title"],
                    "baseline": baseline,
                    "baseline_method": method,
                    "multiple": multiple,
                    "subs": channel["subs"],
                }
            )
    outlier_records.sort(key=lambda r: -r["multiple"])

    return {
        "ok": True,
        "channel": {
            "id": channel["id"],
            "title": channel["title"],
            "subscribers": channel["subs"] if channel["subs"] is not None else 0,
            "url": "https://www.youtube.com/channel/" + channel["id"],
        },
        "baseline": round(baseline),
        "baseline_method": method,
        "videos_considered": len(videos),
        "outliers": [_format_outlier(r) for r in outlier_records],
    }


async def _channel_request(channel, lookback_videos, min_multiple):
    """Full channel_outliers request: validation, cache, quota, pipeline.
    Shared by the MCP tool and the paid REST endpoint."""
    params_hash = _params_hash((channel, lookback_videos, min_multiple))
    started = time.monotonic()
    try:
        ref, lookback, multiple = _validate_channel_inputs(
            channel, lookback_videos, min_multiple
        )
    except ToolFault as fault:
        _log_request("channel_outliers", params_hash, "-", 0, started, fault.code)
        return fault.to_response()
    cache_key = ("channel_outliers", ref.lower(), lookback, multiple)
    worst_case = config.COST_LIST + 2 * math.ceil(lookback / 50) * config.COST_LIST
    return await _execute(
        "channel_outliers",
        params_hash,
        cache_key,
        config.CHANNEL_CACHE_TTL_S,
        worst_case,
        lambda meter: _channel_pipeline(ref, lookback, multiple, meter),
    )


@mcp.tool
async def channel_outliers(
    channel: str,
    lookback_videos: int = config.LOOKBACK_VIDEOS_DEFAULT,
    min_multiple: float = config.MIN_MULTIPLE_DEFAULT,
) -> dict:
    """Reveal which of a YouTube channel's recent videos overperformed the
    channel's own baseline (median views of recent long-form uploads), using
    live YouTube data.

    channel: a channel ID (UC...), an @handle, or a full youtube.com channel
    URL. lookback_videos: how many recent uploads to analyze (10-100, default
    30). min_multiple: views/baseline threshold to count as an outlier
    (1.5-10, default 2.5).
    """
    return await _channel_request(channel, lookback_videos, min_multiple)


# ---------------------------------------------------------------------------
# Paid REST endpoints (x402-gated; same pipelines, same no-mock rules)
# ---------------------------------------------------------------------------

# "enforced" (x402 middleware installed) | "free" (explicitly disabled, serve
# results directly) | "unconfigured" (payments expected but credentials absent)
_payment_mode = "unconfigured"


def _payment_gate_error():
    return JSONResponse(
        {
            "ok": False,
            "error": {
                "code": "PAYMENT_NOT_CONFIGURED",
                "message": "This paid endpoint is not accepting calls yet: the "
                "operator has not configured payment credentials. Retry after "
                "the operator completes setup.",
                "retryable": True,
            },
        },
        status_code=503,
    )


def _int_param(params, name, default):
    raw = params.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise ToolFault("INVALID_INPUT", f"'{name}' must be an integer.", False)


def _float_param(params, name, default):
    raw = params.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        raise ToolFault("INVALID_INPUT", f"'{name}' must be a number.", False)


@mcp.custom_route(config.PAID_SCAN_PATH, methods=["GET"])
async def paid_scan_niche(request: Request) -> JSONResponse:
    if _payment_mode == "unconfigured":
        return _payment_gate_error()
    params = request.query_params
    try:
        recency_days = _int_param(params, "recency_days", config.RECENCY_DAYS_DEFAULT)
        max_results = _int_param(params, "max_results", config.MAX_RESULTS_DEFAULT)
    except ToolFault as fault:
        return JSONResponse(fault.to_response())
    result = await _scan_request(
        params.get("query", ""),
        params.get("region_code", config.DEFAULT_REGION_CODE),
        recency_days,
        max_results,
    )
    return JSONResponse(result)


@mcp.custom_route(config.PAID_CHANNEL_PATH, methods=["GET"])
async def paid_channel_outliers(request: Request) -> JSONResponse:
    if _payment_mode == "unconfigured":
        return _payment_gate_error()
    params = request.query_params
    try:
        lookback = _int_param(params, "lookback_videos", config.LOOKBACK_VIDEOS_DEFAULT)
        multiple = _float_param(params, "min_multiple", config.MIN_MULTIPLE_DEFAULT)
    except ToolFault as fault:
        return JSONResponse(fault.to_response())
    result = await _channel_request(params.get("channel", ""), lookback, multiple)
    return JSONResponse(result)


# ---------------------------------------------------------------------------
# Health check + ASGI app + entrypoint
# ---------------------------------------------------------------------------


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(request: Request) -> JSONResponse:
    return JSONResponse({"ok": True})


def _build_app():
    """Build the ASGI app; install the x402 payment middleware when the
    payment credentials are fully configured."""
    global _payment_mode
    application = mcp.http_app()

    if os.environ.get(config.PAYMENTS_ENABLED_ENV, "true").strip().lower() in (
        "false",
        "0",
        "no",
    ):
        _payment_mode = "free"
        log.info(
            "payments disabled via %s — the /paid endpoints serve results directly",
            config.PAYMENTS_ENABLED_ENV,
        )
        return application

    creds = {
        name: os.environ.get(name, "").strip()
        for name in (
            config.OKX_API_KEY_ENV,
            config.OKX_SECRET_KEY_ENV,
            config.OKX_PASSPHRASE_ENV,
            config.PAY_TO_ADDRESS_ENV,
        )
    }
    missing = [name for name, value in creds.items() if not value]
    if missing:
        log.critical(
            "x402 payment credentials missing (%s) — paid endpoints return "
            "PAYMENT_NOT_CONFIGURED until they are set",
            ", ".join(missing),
        )
        return application

    facilitator = OKXFacilitatorClient(
        OKXFacilitatorConfig(
            auth=OKXAuthConfig(
                api_key=creds[config.OKX_API_KEY_ENV],
                secret_key=creds[config.OKX_SECRET_KEY_ENV],
                passphrase=creds[config.OKX_PASSPHRASE_ENV],
            ),
            base_url=os.environ.get(config.OKX_BASE_URL_ENV, "").strip(),
        )
    )
    x402_server = x402ResourceServer(facilitator)
    x402_server.register(config.X402_NETWORK, ExactEvmScheme())
    x402_server.register(config.X402_NETWORK, AggrDeferredEvmScheme())

    pay_to = creds[config.PAY_TO_ADDRESS_ENV]
    price = f"${config.PAID_PRICE_USDT}"

    def options():
        return [
            PaymentOption(
                scheme="exact", price=price, network=config.X402_NETWORK, pay_to=pay_to
            ),
            PaymentOption(
                scheme="aggr_deferred",
                price=price,
                network=config.X402_NETWORK,
                pay_to=pay_to,
            ),
        ]

    routes = {
        f"GET {config.PAID_SCAN_PATH}": RouteConfig(
            accepts=options(),
            description="Live YouTube niche scan: saturation, outliers, ENTER/CROWDED/AVOID verdict",
            mime_type="application/json",
        ),
        f"GET {config.PAID_CHANNEL_PATH}": RouteConfig(
            accepts=options(),
            description="Live YouTube channel outlier audit vs the channel's own baseline",
            mime_type="application/json",
        ),
    }
    application.add_middleware(PaymentMiddlewareASGI, routes=routes, server=x402_server)
    _payment_mode = "enforced"
    log.info(
        "x402 payments enabled: %s per call on %s, pay-to %s",
        price,
        config.X402_NETWORK,
        pay_to,
    )
    return application


app = _build_app()

if __name__ == "__main__":
    if "--stdio" in sys.argv:
        mcp.run(transport="stdio")
    else:
        mcp.run(
            transport="http",
            host="0.0.0.0",
            port=int(os.environ.get("PORT", "8000")),
        )
