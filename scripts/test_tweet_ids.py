"""推文 Snowflake ID 数值比较回归测试。"""
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest import TestCase, main
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import run_backfill
from fetch import fetch_account, tweet_id_num


class FakeClient:
    def __init__(self, tweets):
        self.tweets = tweets

    def last_tweets(self, handle, cursor=""):
        return {"tweets": self.tweets, "has_next_page": False, "next_cursor": ""}


class TweetIdComparisonTests(TestCase):
    OLD_18_DIGIT = "999925396849115136"
    NEW_19_DIGIT = "2107865881766481933"

    def test_tweet_id_num_compares_cross_length_ids_numerically(self):
        self.assertGreater(tweet_id_num(self.NEW_19_DIGIT), tweet_id_num(self.OLD_18_DIGIT))

    def test_fetch_does_not_drop_19_digit_id_after_18_digit_cursor(self):
        tweets = [{
            "id": self.NEW_19_DIGIT,
            "createdAt": "Wed Oct 07 16:09:23 +0000 2026",
            "text": "What an amazing time, Abel",
            "author": {"userName": "jamm3rd"},
            "url": f"https://x.com/jamm3rd/status/{self.NEW_19_DIGIT}",
        }]
        items, newest = fetch_account(
            FakeClient(tweets),
            "jamm3rd",
            self.OLD_18_DIGIT,
            datetime(2026, 10, 7, tzinfo=timezone.utc),
        )
        self.assertEqual([item["id"] for item in items], [self.NEW_19_DIGIT])
        self.assertEqual(newest, self.NEW_19_DIGIT)

    def test_fetch_selects_numeric_max_id(self):
        tweets = [
            {
                "id": self.OLD_18_DIGIT,
                "createdAt": "Wed Oct 07 15:00:00 +0000 2026",
                "text": "older",
                "author": {"userName": "test"},
            },
            {
                "id": self.NEW_19_DIGIT,
                "createdAt": "Wed Oct 07 16:00:00 +0000 2026",
                "text": "newer",
                "author": {"userName": "test"},
            },
        ]
        _, newest = fetch_account(
            FakeClient(tweets),
            "test",
            None,
            datetime(2026, 10, 7, tzinfo=timezone.utc),
        )
        self.assertEqual(newest, self.NEW_19_DIGIT)

    def test_backfill_state_advances_across_digit_boundary(self):
        accounts = [{"handle": "jamm3rd", "name": "金亨泰"}]
        row = (
            datetime(2026, 10, 7, 16, 9, 23, tzinfo=timezone.utc),
            {
                "id": self.NEW_19_DIGIT,
                "kind": "post",
                "created_at": "Wed Oct 07 16:09:23 +0000 2026",
                "text": "What an amazing time, Abel",
            },
        )
        state = {"jamm3rd": {"last_seen_id": self.OLD_18_DIGIT}}
        written_state = {}

        with (
            patch.object(run_backfill, "load_accounts", return_value=accounts),
            patch.object(run_backfill, "TwitterApiClient", return_value=object()),
            patch.object(run_backfill, "fetch_all", return_value=[row]),
            patch.object(run_backfill, "interpret_daily", side_effect=lambda daily: daily),
            patch.object(run_backfill, "write_json"),
            patch.object(run_backfill, "read_json", return_value=None),
            patch.object(run_backfill, "render_daily"),
            patch.object(run_backfill, "load_state", return_value=state),
            patch.object(run_backfill, "save_state", side_effect=lambda value: written_state.update(value)),
        ):
            run_backfill.main(max_pages=1)

        self.assertEqual(written_state["jamm3rd"]["last_seen_id"], self.NEW_19_DIGIT)


if __name__ == "__main__":
    main()
