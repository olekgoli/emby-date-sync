import io
import json
import os
import sqlite3
import tempfile
import unittest
import urllib.error
import urllib.parse
from unittest import mock

from app import emby_date_sync as sync


class DateSyncTests(unittest.TestCase):
    def test_emby_datetime_normalizes_arr_timestamp(self):
        self.assertEqual(
            sync.emby_datetime("2026-05-20 16:43:00.2594338Z"),
            "2026-05-20T16:43:00.0000000Z",
        )

    def test_date_matches_with_small_tolerance(self):
        self.assertTrue(
            sync.date_matches(
                "2026-05-20T16:43:00.0000000Z",
                "2026-05-20T16:43:00Z",
                1,
            )
        )

    def test_first_import_dates_uses_oldest_import_event(self):
        with tempfile.TemporaryDirectory() as directory:
            connection = sqlite3.connect(os.path.join(directory, "radarr.db"))
            try:
                connection.execute(
                    "create table History (MovieId integer, Date text, EventType integer)"
                )
                connection.executemany(
                    "insert into History (MovieId, Date, EventType) values (?, ?, ?)",
                    [
                        (7, "2026-05-20 16:43:00.2594338Z", 3),
                        (7, "2026-03-31 00:18:51.1111111Z", 3),
                        (7, "2026-06-01 10:00:00.0000000Z", 6),
                        (8, "2026-01-01 00:00:00.0000000Z", 1),
                    ],
                )
                connection.commit()
            finally:
                connection.close()

            self.assertEqual(
                sync.first_import_dates(directory, "radarr.db", "MovieId"),
                {7: "2026-03-31T00:18:51.0000000Z"},
            )

    def test_movie_match_prefers_imdb(self):
        item = {
            "ProviderIds": {"Imdb": "tt123", "Tmdb": "456"},
            "Path": "/movies/example/file.mkv",
        }
        radarr = {
            "by_imdb": {"tt123": "2026-01-01T00:00:00.0000000Z"},
            "by_tmdb": {"456": "2026-02-01T00:00:00.0000000Z"},
            "by_path": {},
        }
        self.assertEqual(
            sync.movie_target(item, radarr),
            ("2026-01-01T00:00:00.0000000Z", "imdb:tt123"),
        )

    def test_episode_match_prefers_path(self):
        item = {
            "Path": "/tv/show/Season 01/show - s01e01.mkv",
            "SeriesId": "1",
            "ParentIndexNumber": 1,
            "IndexNumber": 1,
        }
        series_lookup = {"1": {"imdb": "ttseries", "tvdb": "123"}}
        sonarr = {
            "by_path": {"/tv/show/season 01/show - s01e01.mkv": "2026-03-01T00:00:00.0000000Z"},
            "by_imdb": {("ttseries", 1, 1): "2026-04-01T00:00:00.0000000Z"},
            "by_tvdb": {},
        }
        self.assertEqual(
            sync.episode_target(item, series_lookup, sonarr),
            ("2026-03-01T00:00:00.0000000Z", "path"),
        )

    def test_plan_update_records_target_date(self):
        movies = [
            {
                "Id": "m1",
                "Name": "Movie",
                "Type": "Movie",
                "DateCreated": "2026-01-01T00:00:00.0000000Z",
                "ProviderIds": {"Imdb": "tt123"},
            }
        ]
        planned, skipped, _ = sync.plan_updates(
            movies=movies,
            episodes=[],
            series_items=[],
            radarr={
                "by_imdb": {"tt123": "2026-01-02T00:00:00.0000000Z"},
                "by_tmdb": {},
                "by_path": {},
            },
            sonarr={"by_imdb": {}, "by_tvdb": {}, "by_path": {}},
            tolerance_seconds=1,
            log_examples=5,
        )
        self.assertEqual(skipped, {})
        self.assertEqual(len(planned), 1)
        self.assertNotIn("payload", planned[0])
        self.assertEqual(planned[0]["name"], "Movie")
        self.assertEqual(planned[0]["to"], "2026-01-02T00:00:00.0000000Z")

    def test_plan_update_records_series_target_date(self):
        series_items = [
            {
                "Id": "s1",
                "Name": "Series",
                "Type": "Series",
                "DateCreated": "2026-04-01T00:00:00.0000000Z",
                "ProviderIds": {"Tvdb": "123"},
            }
        ]
        planned, skipped, _ = sync.plan_updates(
            movies=[],
            episodes=[],
            series_items=series_items,
            radarr={"by_imdb": {}, "by_tmdb": {}, "by_path": {}},
            sonarr={
                "by_imdb": {},
                "by_tvdb": {},
                "by_path": {},
                "series_by_imdb": {},
                "series_by_tvdb": {"123": "2026-03-01T00:00:00.0000000Z"},
                "series_by_path": {},
            },
            tolerance_seconds=1,
            log_examples=5,
        )
        self.assertEqual(skipped, {})
        self.assertEqual(len(planned), 1)
        self.assertEqual(planned[0]["type"], "Series")
        self.assertEqual(planned[0]["to"], "2026-03-01T00:00:00.0000000Z")


class FakeResponse:
    def __init__(self, body, status=200):
        self.status = status
        self._body = body if isinstance(body, bytes) else json.dumps(body).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(code, body=b"boom"):
    return urllib.error.HTTPError("http://x", code, "error", {}, io.BytesIO(body))


@mock.patch("app.emby_date_sync.time.sleep")
class HttpTests(unittest.TestCase):
    def test_client_errors_are_not_retried(self, _sleep):
        with mock.patch("urllib.request.urlopen", side_effect=http_error(401)) as urlopen:
            with self.assertRaises(urllib.error.HTTPError):
                sync.http_json("http://x")
        self.assertEqual(urlopen.call_count, 1)

    def test_server_errors_and_timeouts_are_retried(self, _sleep):
        responses = [http_error(503), TimeoutError("read timed out"), FakeResponse({"ok": 1})]
        with mock.patch("urllib.request.urlopen", side_effect=responses) as urlopen:
            self.assertEqual(sync.http_json("http://x"), {"ok": 1})
        self.assertEqual(urlopen.call_count, 3)

    def test_retries_are_bounded(self, _sleep):
        with mock.patch("urllib.request.urlopen", side_effect=TimeoutError()) as urlopen:
            with self.assertRaises(TimeoutError):
                sync.http_json("http://x")
        self.assertEqual(urlopen.call_count, 3)


class FetchEmbyItemsTests(unittest.TestCase):
    def test_pages_until_total_is_reached(self):
        all_items = [{"Id": str(i)} for i in range(5)]
        starts = []

        def fake_http_json(url, headers):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            start = int(query["StartIndex"][0])
            limit = int(query["Limit"][0])
            starts.append(start)
            return {"Items": all_items[start:start + limit], "TotalRecordCount": len(all_items)}

        with mock.patch.object(sync, "http_json", side_effect=fake_http_json):
            items, total = sync.fetch_emby_items("http://emby", "t", "Episode", page_size=2)
        self.assertEqual(items, all_items)
        self.assertEqual(total, 5)
        self.assertEqual(starts, [0, 2, 4])

    def test_stops_on_empty_page(self):
        with mock.patch.object(sync, "http_json", return_value={"Items": []}) as http_json:
            items, total = sync.fetch_emby_items("http://emby", "t", "Movie")
        self.assertEqual((items, total), ([], None))
        self.assertEqual(http_json.call_count, 1)


@mock.patch("app.emby_date_sync.time.sleep")
class ApplyUpdatesTests(unittest.TestCase):
    planned = [
        {"id": "gone", "type": "Movie", "name": "Deleted", "to": "2026-01-01T00:00:00.0000000Z"},
        {"id": "ok", "type": "Movie", "name": "Present", "to": "2026-01-01T00:00:00.0000000Z"},
    ]

    def test_fetch_failure_is_recorded_per_item(self, _sleep):
        def fake_fetch(base_url, token, item_id):
            if item_id == "gone":
                raise RuntimeError("Expected one Emby item gone, got 0")
            return {"Id": item_id, "DateCreated": "2026-05-01T00:00:00.0000000Z"}

        with mock.patch.object(sync, "fetch_emby_item", side_effect=fake_fetch), \
                mock.patch.object(sync, "post_json", return_value=204) as post_json:
            updated, errors = sync.apply_updates("http://emby", "t", self.planned)
        self.assertEqual(updated, 1)
        self.assertEqual([error["id"] for error in errors], ["gone"])
        post_json.assert_called_once()
        self.assertEqual(post_json.call_args.args[1]["DateCreated"], "2026-01-01T00:00:00.0000000Z")

    def test_post_http_error_is_attempted_once(self, _sleep):
        with mock.patch.object(sync, "fetch_emby_item", return_value={"DateCreated": None}), \
                mock.patch("urllib.request.urlopen", side_effect=http_error(400, b"bad payload")) as urlopen:
            updated, errors = sync.apply_updates("http://emby", "t", self.planned[1:])
        self.assertEqual(updated, 0)
        self.assertEqual(errors[0]["error"], "http 400: bad payload")
        self.assertEqual(urlopen.call_count, 1)


if __name__ == "__main__":
    unittest.main()
