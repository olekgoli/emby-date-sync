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

    def test_plan_update_changes_only_datecreated_in_payload(self):
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
        self.assertEqual(planned[0]["payload"]["Name"], "Movie")
        self.assertEqual(planned[0]["payload"]["DateCreated"], "2026-01-02T00:00:00.0000000Z")


if __name__ == "__main__":
    unittest.main()
