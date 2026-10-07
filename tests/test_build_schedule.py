import copy
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path

from scripts.build_schedule import (
    ScheduleValidationError,
    build_schedule,
    publish_candidate,
    schedule_content,
    validate_schedule,
)


HEADERS = [
    "game_id",
    "season",
    "game_type",
    "week",
    "gameday",
    "weekday",
    "gametime",
    "away_team",
    "home_team",
]


def make_csv(rows, headers=None):
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=headers or HEADERS)
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def game(
    game_id="2026_01_DAL_PHI",
    season="2026",
    game_type="REG",
    week="1",
    gameday="2026-09-10",
    weekday="Thursday",
    gametime="20:20",
    away_team="DAL",
    home_team="PHI",
):
    return {
        "game_id": game_id,
        "season": season,
        "game_type": game_type,
        "week": week,
        "gameday": gameday,
        "weekday": weekday,
        "gametime": gametime,
        "away_team": away_team,
        "home_team": home_team,
    }


class BuildScheduleTests(unittest.TestCase):
    def test_filters_other_seasons_and_non_regular_games(self):
        rows = [
            game(),
            game(game_id="2025_01_DAL_PHI", season="2025"),
            game(game_id="2026_01_PRE_DAL_PHI", game_type="PRE"),
            game(game_id="2026_WC_DAL_PHI", game_type="WC", week="19"),
        ]

        result = build_schedule(
            make_csv(rows),
            season=2026,
            generated_at="2026-10-07T12:00:00Z",
        )

        self.assertEqual(1, len(result["weeks"]["1"]))
        self.assertEqual("2026_01_DAL_PHI", result["weeks"]["1"][0]["gameId"])

    def test_groups_by_week_and_sorts_by_kickoff(self):
        rows = [
            game(
                game_id="2026_02_LV_LAC",
                week="2",
                gameday="2026-09-20",
                gametime="16:25",
                away_team="LV",
                home_team="LAC",
            ),
            game(
                game_id="2026_02_NYG_DAL",
                week="2",
                gameday="2026-09-20",
                gametime="13:00",
                away_team="NYG",
                home_team="DAL",
            ),
        ]

        result = build_schedule(make_csv(rows), season=2026)

        self.assertEqual(
            ["2026_02_NYG_DAL", "2026_02_LV_LAC"],
            [item["gameId"] for item in result["weeks"]["2"]],
        )

    def test_includes_team_abbreviations_and_names(self):
        result = build_schedule(make_csv([game()]), season=2026)
        built_game = result["weeks"]["1"][0]

        self.assertEqual(
            {"abbreviation": "DAL", "name": "Dallas Cowboys"},
            built_game["away"],
        )
        self.assertEqual(
            {"abbreviation": "PHI", "name": "Philadelphia Eagles"},
            built_game["home"],
        )

    def test_rejects_unknown_team_abbreviation(self):
        with self.assertRaisesRegex(ScheduleValidationError, "Unknown NFL team"):
            build_schedule(
                make_csv([game(away_team="XYZ")]),
                season=2026,
            )

    def test_rejects_missing_required_csv_column(self):
        headers = [column for column in HEADERS if column != "gametime"]
        row = game()
        row.pop("gametime")

        with self.assertRaisesRegex(ScheduleValidationError, "gametime"):
            build_schedule(make_csv([row], headers=headers), season=2026)

    def test_rejects_duplicate_game_ids(self):
        duplicate = game()

        with self.assertRaisesRegex(ScheduleValidationError, "Duplicate game_id"):
            build_schedule(make_csv([duplicate, duplicate]), season=2026)

    def test_strict_validation_requires_complete_272_game_season(self):
        incomplete = build_schedule(make_csv([game()]), season=2026)

        with self.assertRaisesRegex(ScheduleValidationError, "272"):
            validate_schedule(incomplete, strict=True)


class PublicationTests(unittest.TestCase):
    def candidate(self):
        return build_schedule(
            make_csv([game()]),
            season=2026,
            generated_at="2026-10-07T12:00:00Z",
        )

    def test_generated_timestamp_is_not_part_of_schedule_comparison(self):
        first = self.candidate()
        second = copy.deepcopy(first)
        second["generatedAt"] = "2026-10-08T12:00:00Z"

        self.assertEqual(schedule_content(first), schedule_content(second))

    def test_unchanged_candidate_does_not_modify_current_or_backup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            current = root / "nfl-schedule-2026.json"
            previous = root / "nfl-schedule-2026.previous.json"
            existing = self.candidate()
            current.write_text(json.dumps(existing), encoding="utf-8")
            previous.write_text('{"sentinel":"previous"}', encoding="utf-8")

            changed = publish_candidate(existing, current, previous)

            self.assertFalse(changed)
            self.assertEqual(existing, json.loads(current.read_text(encoding="utf-8")))
            self.assertEqual(
                {"sentinel": "previous"},
                json.loads(previous.read_text(encoding="utf-8")),
            )

    def test_changed_candidate_rotates_exactly_one_backup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            current = root / "nfl-schedule-2026.json"
            previous = root / "nfl-schedule-2026.previous.json"
            existing = self.candidate()
            current.write_text(json.dumps(existing), encoding="utf-8")

            changed_candidate = copy.deepcopy(existing)
            changed_candidate["weeks"]["1"][0]["gametime"] = "20:30"

            changed = publish_candidate(changed_candidate, current, previous)

            self.assertTrue(changed)
            self.assertEqual(existing, json.loads(previous.read_text(encoding="utf-8")))
            self.assertEqual(
                changed_candidate,
                json.loads(current.read_text(encoding="utf-8")),
            )

    def test_first_publication_creates_current_without_fake_backup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            current = root / "nfl-schedule-2026.json"
            previous = root / "nfl-schedule-2026.previous.json"

            changed = publish_candidate(self.candidate(), current, previous)

            self.assertTrue(changed)
            self.assertTrue(current.exists())
            self.assertFalse(previous.exists())


if __name__ == "__main__":
    unittest.main()
