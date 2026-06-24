#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from typing import Any


EMBY_FIELDS = ",".join(
    [
        "Budget",
        "Chapters",
        "DateCreated",
        "Genres",
        "HomePageUrl",
        "IndexOptions",
        "MediaStreams",
        "Overview",
        "ParentId",
        "Path",
        "People",
        "ProviderIds",
        "PrimaryImageAspectRatio",
        "Revenue",
        "SortName",
        "Studios",
        "Taglines",
    ]
)


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def env_bool(name: str, default: bool = False) -> bool:
    value = env(name)
    if not value:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int = 0) -> int:
    value = env(name)
    if not value:
        return default
    return int(value)


def log(event: str, **data: Any) -> None:
    print(json.dumps({"event": event, **data}, ensure_ascii=False), flush=True)


def parse_datetime(value: Any) -> dt.datetime | None:
    if not value:
        return None
    text = str(value).strip().replace(" ", "T")
    text = re.sub(r"Z$", "+00:00", text)
    text = re.sub(r"(\.\d{6})\d+([+-]\d\d:\d\d)?$", r"\1\2", text)
    parsed = dt.datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc).replace(tzinfo=None)


def emby_datetime(value: Any) -> str | None:
    parsed = parse_datetime(value)
    if parsed is None:
        return None
    return parsed.strftime("%Y-%m-%dT%H:%M:%S.0000000Z")


def date_matches(left: Any, right: Any, tolerance_seconds: int) -> bool:
    left_dt = parse_datetime(left)
    right_dt = parse_datetime(right)
    if left_dt is None or right_dt is None:
        return left == right
    return abs((left_dt - right_dt).total_seconds()) <= tolerance_seconds


def normalize_path(path: Any) -> str | None:
    if not path:
        return None
    return re.sub(r"/+", "/", str(path).strip()).rstrip("/").casefold()


def provider_id(provider_ids: dict[str, Any] | None, name: str) -> str | None:
    if not provider_ids:
        return None
    wanted = name.casefold()
    for key, value in provider_ids.items():
        if key.casefold() == wanted and value:
            return str(value)
    return None


def http_json(url: str, headers: dict[str, str] | None = None, timeout: int = 90) -> Any:
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def post_json(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str] | None = None,
    timeout: int = 90,
) -> int:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={**(headers or {}), "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        response.read()
        return response.status


def api_key(config_dir: str) -> str:
    config_path = os.path.join(config_dir, "config.xml")
    root = ET.parse(config_path).getroot()
    key = root.findtext("ApiKey")
    if not key:
        raise RuntimeError(f"Missing ApiKey in {config_path}")
    return key


def emby_token(config_dir: str, user_id: str) -> str:
    db_path = os.path.join(config_dir, "data", "authentication.db")
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cursor = connection.cursor()
        if user_id:
            cursor.execute(
                """
                select AccessToken
                  from Tokens_2
                 where IsActive = 1 and UserId = ?
                 order by DateLastActivityInt desc
                 limit 1
                """,
                (int(user_id),),
            )
            row = cursor.fetchone()
            if row:
                return str(row[0])
        cursor.execute(
            """
            select AccessToken
              from Tokens_2
             where IsActive = 1 and UserId is not null
             order by DateLastActivityInt desc
             limit 1
            """
        )
        row = cursor.fetchone()
        if not row:
            raise RuntimeError("No active Emby user token found")
        return str(row[0])
    finally:
        connection.close()


def arr_headers(key: str) -> dict[str, str]:
    return {"X-Api-Key": key, "Accept": "application/json"}


def emby_headers(token: str) -> dict[str, str]:
    return {"X-Emby-Token": token, "Accept": "application/json"}


def fetch_radarr_sources(base_url: str, key: str) -> dict[str, Any]:
    movies = http_json(base_url.rstrip("/") + "/api/v3/movie", arr_headers(key))
    by_imdb: dict[str, str] = {}
    by_tmdb: dict[str, str] = {}
    by_path: dict[str, str] = {}
    skipped: Counter[str] = Counter()
    for movie in movies:
        movie_file = movie.get("movieFile") or {}
        target = emby_datetime(movie_file.get("dateAdded"))
        if not target:
            skipped["missing_file_date"] += 1
            continue
        imdb = movie.get("imdbId")
        tmdb = movie.get("tmdbId")
        if imdb:
            by_imdb[str(imdb).casefold()] = target
        if tmdb:
            by_tmdb[str(tmdb)] = target
        path = normalize_path(movie_file.get("path"))
        if path:
            by_path[path] = target
    return {
        "by_imdb": by_imdb,
        "by_tmdb": by_tmdb,
        "by_path": by_path,
        "raw_count": len(movies),
        "skipped": dict(skipped),
    }


def fetch_sonarr_sources(base_url: str, key: str) -> dict[str, Any]:
    base = base_url.rstrip("/")
    headers = arr_headers(key)
    series_items = http_json(base + "/api/v3/series", headers)
    by_imdb: dict[tuple[str, int, int], str] = {}
    by_tvdb: dict[tuple[str, int, int], str] = {}
    by_path: dict[str, str] = {}
    skipped: Counter[str] = Counter()
    for series in series_items:
        series_id = series.get("id")
        if not series_id:
            skipped["series_missing_id"] += 1
            continue
        params = urllib.parse.urlencode(
            {"seriesId": series_id, "includeEpisodeFile": "true"}
        )
        episodes = http_json(base + "/api/v3/episode?" + params, headers)
        imdb = str(series.get("imdbId") or "").casefold() or None
        tvdb = str(series.get("tvdbId") or "") or None
        for episode in episodes:
            episode_file = episode.get("episodeFile") or {}
            target = emby_datetime(episode_file.get("dateAdded"))
            if not target:
                skipped["missing_file_date"] += 1
                continue
            season = episode.get("seasonNumber")
            number = episode.get("episodeNumber")
            if season is None or number is None:
                skipped["missing_episode_numbers"] += 1
                continue
            if imdb:
                by_imdb[(imdb, int(season), int(number))] = target
            if tvdb:
                by_tvdb[(tvdb, int(season), int(number))] = target
            path = normalize_path(episode_file.get("path"))
            if path:
                by_path[path] = target
    return {
        "by_imdb": by_imdb,
        "by_tvdb": by_tvdb,
        "by_path": by_path,
        "raw_series_count": len(series_items),
        "skipped": dict(skipped),
    }


def fetch_emby_items(base_url: str, token: str, item_type: str) -> tuple[list[dict[str, Any]], int | None]:
    params = urllib.parse.urlencode(
        {
            "Recursive": "true",
            "IncludeItemTypes": item_type,
            "Limit": "10000",
            "Fields": EMBY_FIELDS,
        }
    )
    response = http_json(
        base_url.rstrip("/") + "/Items?" + params,
        emby_headers(token),
    )
    return response.get("Items", []), response.get("TotalRecordCount")


def movie_target(item: dict[str, Any], radarr: dict[str, Any]) -> tuple[str | None, str | None]:
    imdb = provider_id(item.get("ProviderIds"), "imdb")
    if imdb:
        target = radarr["by_imdb"].get(imdb.casefold())
        if target:
            return target, f"imdb:{imdb}"
    tmdb = provider_id(item.get("ProviderIds"), "tmdb")
    if tmdb:
        target = radarr["by_tmdb"].get(str(tmdb))
        if target:
            return target, f"tmdb:{tmdb}"
    path = normalize_path(item.get("Path"))
    if path:
        target = radarr["by_path"].get(path)
        if target:
            return target, "path"
    return None, None


def episode_target(
    item: dict[str, Any],
    series_lookup: dict[str, dict[str, str | None]],
    sonarr: dict[str, Any],
) -> tuple[str | None, str | None]:
    path = normalize_path(item.get("Path"))
    if path:
        target = sonarr["by_path"].get(path)
        if target:
            return target, "path"

    series = series_lookup.get(str(item.get("SeriesId")))
    season = item.get("ParentIndexNumber")
    number = item.get("IndexNumber")
    if not series or season is None or number is None:
        return None, None

    imdb = series.get("imdb")
    if imdb:
        target = sonarr["by_imdb"].get((imdb.casefold(), int(season), int(number)))
        if target:
            return target, f"series-imdb:{imdb}"

    tvdb = series.get("tvdb")
    if tvdb:
        target = sonarr["by_tvdb"].get((tvdb, int(season), int(number)))
        if target:
            return target, f"series-tvdb:{tvdb}"

    return None, None


def plan_updates(
    movies: list[dict[str, Any]],
    episodes: list[dict[str, Any]],
    series_items: list[dict[str, Any]],
    radarr: dict[str, Any],
    sonarr: dict[str, Any],
    tolerance_seconds: int,
    log_examples: int,
) -> tuple[list[dict[str, Any]], Counter[str], dict[str, list[dict[str, Any]]]]:
    series_lookup = {}
    for series in series_items:
        series_lookup[str(series.get("Id"))] = {
            "imdb": provider_id(series.get("ProviderIds"), "imdb"),
            "tvdb": provider_id(series.get("ProviderIds"), "tvdb"),
        }

    planned = []
    skipped: Counter[str] = Counter()
    examples: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for item in movies:
        target, source = movie_target(item, radarr)
        if not target:
            skipped["movie_no_arr_match"] += 1
            if len(examples["movie_no_arr_match"]) < log_examples:
                examples["movie_no_arr_match"].append(
                    {"id": item.get("Id"), "name": item.get("Name")}
                )
            continue
        if date_matches(item.get("DateCreated"), target, tolerance_seconds):
            skipped["movie_already_ok"] += 1
            continue
        planned.append(
            {
                "id": item.get("Id"),
                "type": "Movie",
                "name": item.get("Name"),
                "from": item.get("DateCreated"),
                "to": target,
                "source": source,
                "payload": {**item, "DateCreated": target},
            }
        )

    for item in episodes:
        target, source = episode_target(item, series_lookup, sonarr)
        if not target:
            skipped["episode_no_arr_match"] += 1
            if len(examples["episode_no_arr_match"]) < log_examples:
                examples["episode_no_arr_match"].append(
                    {
                        "id": item.get("Id"),
                        "name": item.get("Name"),
                        "series": item.get("SeriesName"),
                        "season": item.get("ParentIndexNumber"),
                        "episode": item.get("IndexNumber"),
                    }
                )
            continue
        if date_matches(item.get("DateCreated"), target, tolerance_seconds):
            skipped["episode_already_ok"] += 1
            continue
        planned.append(
            {
                "id": item.get("Id"),
                "type": "Episode",
                "name": item.get("Name"),
                "series": item.get("SeriesName"),
                "from": item.get("DateCreated"),
                "to": target,
                "source": source,
                "payload": {**item, "DateCreated": target},
            }
        )

    return planned, skipped, examples


def apply_updates(base_url: str, token: str, planned: list[dict[str, Any]]) -> tuple[int, list[dict[str, Any]]]:
    headers = emby_headers(token)
    errors = []
    updated = 0
    for item in planned:
        url = base_url.rstrip("/") + "/Items/" + urllib.parse.quote(str(item["id"]))
        last_error = None
        for attempt in range(1, 4):
            try:
                status = post_json(url, item["payload"], headers)
                if status in {200, 204}:
                    updated += 1
                    last_error = None
                    break
                last_error = f"unexpected status {status}"
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", "replace")[:200]
                last_error = f"http {exc.code}: {body}"
            except Exception as exc:
                last_error = repr(exc)
            time.sleep(min(attempt * 2, 5))

        if last_error:
            errors.append(
                {
                    "id": item["id"],
                    "type": item["type"],
                    "name": item["name"],
                    "error": last_error,
                }
            )
            if len(errors) >= 10:
                break

        if updated and updated % 100 == 0:
            log("apply_progress", updated=updated, planned=len(planned), errors=len(errors))

    return updated, errors


def main() -> None:
    dry_run = env_bool("SYNC_DRY_RUN", False)
    max_updates = env_int("SYNC_MAX_UPDATES", 0)
    tolerance_seconds = env_int("SYNC_TOLERANCE_SECONDS", 1)
    log_examples = env_int("SYNC_LOG_EXAMPLES", 5)

    emby_base = env("EMBY_URL", "http://emby:8096/emby")
    radarr_base = env("RADARR_URL", "http://radarr:7878")
    sonarr_base = env("SONARR_URL", "http://sonarr:8989")

    radarr_key = api_key(env("RADARR_CONFIG_DIR", "/radarr-config"))
    sonarr_key = api_key(env("SONARR_CONFIG_DIR", "/sonarr-config"))
    token = emby_token(env("EMBY_CONFIG_DIR", "/emby-config"), env("EMBY_TOKEN_USER_ID", "1"))

    log("fetch_sources")
    radarr = fetch_radarr_sources(radarr_base, radarr_key)
    sonarr = fetch_sonarr_sources(sonarr_base, sonarr_key)
    log(
        "sources_ready",
        radarr_movies=radarr["raw_count"],
        radarr_matches=len(radarr["by_imdb"]) + len(radarr["by_path"]),
        radarr_skipped=radarr["skipped"],
        sonarr_series=sonarr["raw_series_count"],
        sonarr_matches=len(sonarr["by_path"]),
        sonarr_skipped=sonarr["skipped"],
    )

    if not radarr["by_imdb"] and not radarr["by_path"]:
        raise RuntimeError("Radarr returned no dated movie files")
    if not sonarr["by_imdb"] and not sonarr["by_path"]:
        raise RuntimeError("Sonarr returned no dated episode files")

    movies, movie_total = fetch_emby_items(emby_base, token, "Movie")
    episodes, episode_total = fetch_emby_items(emby_base, token, "Episode")
    series_items, series_total = fetch_emby_items(emby_base, token, "Series")
    log(
        "emby_ready",
        movies=len(movies),
        movie_total=movie_total,
        episodes=len(episodes),
        episode_total=episode_total,
        series=len(series_items),
        series_total=series_total,
    )

    planned, skipped, examples = plan_updates(
        movies,
        episodes,
        series_items,
        radarr,
        sonarr,
        tolerance_seconds,
        log_examples,
    )
    log(
        "plan",
        dry_run=dry_run,
        planned=len(planned),
        skipped=dict(skipped),
        examples={key: value for key, value in examples.items()},
        sample=[
            {
                key: item.get(key)
                for key in ("id", "type", "name", "series", "from", "to", "source")
            }
            for item in planned[:log_examples]
        ],
    )

    if max_updates > 0 and len(planned) > max_updates:
        raise RuntimeError(f"Planned {len(planned)} updates, above SYNC_MAX_UPDATES={max_updates}")

    if dry_run or not planned:
        return

    updated, errors = apply_updates(emby_base, token, planned)
    log("apply_done", updated=updated, planned=len(planned), errors=errors[:10])
    if errors:
        raise RuntimeError(f"Failed to update {len(errors)} Emby items")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("fatal", error=str(exc))
        sys.exit(1)
