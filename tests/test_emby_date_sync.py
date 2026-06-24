import os
import sqlite3
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()
