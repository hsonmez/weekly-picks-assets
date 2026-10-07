#!/usr/bin/env python3
"""Build, validate, and safely publish the FF40 NFL schedule JSON."""

from __future__ import annotations

import argparse
import csv
import io
import json
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


SCHEMA_VERSION = 1
FIRST_WEEK = 1
LAST_WEEK = 18
EXPECTED_GAMES = 272
EXPECTED_GAMES_PER_TEAM = 17

REQUIRED_COLUMNS = {
    "game_id",
    "season",
    "game_type",
    "week",
    "gameday",
    "weekday",
    "gametime",
    "away_team",
    "home_team",
}

NFL_TEAMS = {
    "ARI": "Arizona Cardinals",
    "ATL": "Atlanta Falcons",
    "BAL": "Baltimore Ravens",
    "BUF": "Buffalo Bills",
    "CAR": "Carolina Panthers",
    "CHI": "Chicago Bears",
    "CIN": "Cincinnati Bengals",
    "CLE": "Cleveland Browns",
    "DAL": "Dallas Cowboys",
    "DEN": "Denver Broncos",
    "DET": "Detroit Lions",
    "GB": "Green Bay Packers",
    "HOU": "Houston Texans",
    "IND": "Indianapolis Colts",
    "JAX": "Jacksonville Jaguars",
    "KC": "Kansas City Chiefs",
    "LA": "Los Angeles Rams",
    "LAC": "Los Angeles Chargers",
    "LV": "Las Vegas Raiders",
    "MIA": "Miami Dolphins",
    "MIN": "Minnesota Vikings",
    "NE": "New England Patriots",
    "NO": "New Orleans Saints",
    "NYG": "New York Giants",
    "NYJ": "New York Jets",
    "PHI": "Philadelphia Eagles",
    "PIT": "Pittsburgh Steelers",
    "SEA": "Seattle Seahawks",
    "SF": "San Francisco 49ers",
    "TB": "Tampa Bay Buccaneers",
    "TEN": "Tennessee Titans",
    "WAS": "Washington Commanders",
}


class ScheduleValidationError(ValueError):
    """Raised when upstream schedule data is unsafe to publish."""


def utc_now_text() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def kickoff_timestamp(gameday: str, gametime: str) -> int:
    try:
        local = datetime.strptime(
            f"{gameday} {gametime}", "%Y-%m-%d %H:%M"
        ).replace(tzinfo=ZoneInfo("America/New_York"))
    except (TypeError, ValueError) as error:
        raise ScheduleValidationError(
            f"Invalid NFL kickoff date/time: {gameday!r} {gametime!r}."
        ) from error

    return int(local.timestamp() * 1000)


def team(abbreviation: str) -> dict:
    abbreviation = str(abbreviation or "").strip()
    if abbreviation not in NFL_TEAMS:
        raise ScheduleValidationError(
            f"Unknown NFL team abbreviation: {abbreviation or '(blank)'}."
        )

    return {
        "abbreviation": abbreviation,
        "name": NFL_TEAMS[abbreviation],
    }


def build_schedule(
    csv_text: str,
    season: int,
    generated_at: str | None = None,
) -> dict:
    reader = csv.DictReader(io.StringIO(csv_text))
    columns = set(reader.fieldnames or [])
    missing = sorted(REQUIRED_COLUMNS - columns)

    if missing:
        raise ScheduleValidationError(
            "nflverse CSV is missing required column(s): " + ", ".join(missing)
        )

    weeks: dict[str, list[dict]] = {}
    seen_game_ids: set[str] = set()

    for row in reader:
        try:
            row_season = int(row["season"])
            week = int(row["week"])
        except (TypeError, ValueError) as error:
            raise ScheduleValidationError(
                "nflverse CSV contains a non-numeric season or week."
            ) from error

        if (
            row_season != int(season)
            or row["game_type"].strip() != "REG"
            or week < FIRST_WEEK
            or week > LAST_WEEK
        ):
            continue

        game_id = row["game_id"].strip()

        if not game_id:
            raise ScheduleValidationError(
                "nflverse CSV contains a blank game_id."
            )

        if game_id in seen_game_ids:
            raise ScheduleValidationError(
                f"Duplicate game_id: {game_id}."
            )

        seen_game_ids.add(game_id)

        gameday = row["gameday"].strip()
        gametime = row["gametime"].strip()

        built_game = {
            "gameId": game_id,
            "away": team(row["away_team"]),
            "home": team(row["home_team"]),
            "gameday": gameday,
            "gametime": gametime,
            "weekday": row["weekday"].strip(),
            "kickoffTimestamp": kickoff_timestamp(
                gameday,
                gametime,
            ),
        }

        weeks.setdefault(
            str(week),
            [],
        ).append(built_game)

    for games in weeks.values():
        games.sort(
            key=lambda item: (
                item["kickoffTimestamp"],
                item["gameId"],
            )
        )

    schedule = {
        "schemaVersion": SCHEMA_VERSION,
        "season": int(season),
        "source": "nflverse",
        "generatedAt": generated_at or utc_now_text(),
        "weeks": dict(
            sorted(
                weeks.items(),
                key=lambda item: int(item[0]),
            )
        ),
    }

    validate_schedule(
        schedule,
        strict=False,
    )

    return schedule


def validate_schedule(
    schedule: dict,
    strict: bool,
) -> None:
    if not isinstance(schedule, dict):
        raise ScheduleValidationError(
            "Schedule must be a JSON object."
        )

    if schedule.get("schemaVersion") != SCHEMA_VERSION:
        raise ScheduleValidationError(
            "Unexpected schedule schemaVersion."
        )

    if not isinstance(schedule.get("season"), int):
        raise ScheduleValidationError(
            "Schedule season must be an integer."
        )

    weeks = schedule.get("weeks")

    if not isinstance(weeks, dict) or not weeks:
        raise ScheduleValidationError(
            "Schedule contains no regular-season games."
        )

    game_ids: set[str] = set()
    team_game_counts = {
        abbreviation: 0
        for abbreviation in NFL_TEAMS
    }
    game_count = 0

    for week_text, games in weeks.items():
        try:
            week = int(week_text)
        except (TypeError, ValueError) as error:
            raise ScheduleValidationError(
                f"Invalid week key: {week_text!r}."
            ) from error

        if week < FIRST_WEEK or week > LAST_WEEK:
            raise ScheduleValidationError(
                f"Week {week} is outside 1-18."
            )

        if not isinstance(games, list) or not games:
            raise ScheduleValidationError(
                f"Week {week} contains no games."
            )

        for item in games:
            game_id = item.get("gameId")

            if not game_id or game_id in game_ids:
                raise ScheduleValidationError(
                    f"Missing or duplicate gameId: {game_id!r}."
                )

            game_ids.add(game_id)

            for side in ("away", "home"):
                abbreviation = (
                    item.get(side) or {}
                ).get("abbreviation")

                if abbreviation not in NFL_TEAMS:
                    raise ScheduleValidationError(
                        "Unknown NFL team abbreviation: "
                        f"{abbreviation!r}."
                    )

                team_game_counts[abbreviation] += 1

            if not isinstance(
                item.get("kickoffTimestamp"),
                int,
            ):
                raise ScheduleValidationError(
                    f"Game {game_id} has no valid "
                    "kickoffTimestamp."
                )

            game_count += 1

    if not strict:
        return

    if game_count != EXPECTED_GAMES:
        raise ScheduleValidationError(
            f"Expected {EXPECTED_GAMES} regular-season "
            f"games; received {game_count}."
        )

    expected_weeks = {
        str(week)
        for week in range(
            FIRST_WEEK,
            LAST_WEEK + 1,
        )
    }

    if set(weeks) != expected_weeks:
        raise ScheduleValidationError(
            "Strict validation requires Weeks 1-18."
        )

    incorrect_team_counts = {
        abbreviation: count
        for abbreviation, count in team_game_counts.items()
        if count != EXPECTED_GAMES_PER_TEAM
    }

    if incorrect_team_counts:
        raise ScheduleValidationError(
            "Every NFL team must appear in exactly 17 games; "
            "incorrect counts: "
            + json.dumps(
                incorrect_team_counts,
                sort_keys=True,
            )
        )


def schedule_content(schedule: dict) -> dict:
    """Return only fields whose changes require a new publication."""

    return {
        "schemaVersion": schedule.get("schemaVersion"),
        "season": schedule.get("season"),
        "source": schedule.get("source"),
        "weeks": schedule.get("weeks"),
    }


def read_json(path: Path) -> dict:
    try:
        return json.loads(
            path.read_text(encoding="utf-8")
        )
    except (
        OSError,
        json.JSONDecodeError,
    ) as error:
        raise ScheduleValidationError(
            f"Could not read existing {path.name}."
        ) from error


def write_json_atomically(
    value: dict,
    destination: Path,
) -> None:
    destination.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
        delete=False,
    ) as temp_file:
        json.dump(
            value,
            temp_file,
            indent=2,
            ensure_ascii=False,
        )
        temp_file.write("\n")
        temp_path = Path(temp_file.name)

    temp_path.replace(destination)


def publish_candidate(
    candidate: dict,
    current: Path,
    previous: Path,
) -> bool:
    validate_schedule(
        candidate,
        strict=False,
    )

    existing = (
        read_json(current)
        if current.exists()
        else None
    )

    if (
        existing is not None
        and schedule_content(existing)
        == schedule_content(candidate)
    ):
        return False

    if existing is not None:
        previous.parent.mkdir(
            parents=True,
            exist_ok=True,
        )
        shutil.copyfile(
            current,
            previous,
        )

    write_json_atomically(
        candidate,
        current,
    )

    return True


def parse_args(
    argv: list[str],
) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
    )

    parser.add_argument(
        "--input",
        required=True,
        type=Path,
    )

    parser.add_argument(
        "--season",
        required=True,
        type=int,
    )

    parser.add_argument(
        "--current",
        required=True,
        type=Path,
    )

    parser.add_argument(
        "--previous",
        required=True,
        type=Path,
    )

    parser.add_argument(
        "--allow-incomplete",
        action="store_true",
        help=(
            "For local development only; production "
            "publication must remain strict."
        ),
    )

    return parser.parse_args(argv)


def main(
    argv: list[str] | None = None,
) -> int:
    args = parse_args(
        argv or sys.argv[1:]
    )

    try:
        candidate = build_schedule(
            args.input.read_text(
                encoding="utf-8"
            ),
            season=args.season,
        )

        validate_schedule(
            candidate,
            strict=not args.allow_incomplete,
        )

        changed = publish_candidate(
            candidate,
            args.current,
            args.previous,
        )

    except (
        OSError,
        ScheduleValidationError,
    ) as error:
        print(
            f"ERROR: {error}",
            file=sys.stderr,
        )
        return 1

    if changed:
        print(
            f"Published {args.current} and retained "
            "at most one backup file."
        )
    else:
        print(
            "Schedule is unchanged; no files were modified."
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
