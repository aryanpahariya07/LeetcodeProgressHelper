"""Command line entry points."""

import argparse
import asyncio
import sys
from pathlib import Path

from dsa_coach.catalogue import DEFAULT_CATALOGUE, import_catalogue
from dsa_coach.db import get_session_factory
from dsa_coach.models import RatingSource


async def _import(path: Path, rating_source: RatingSource) -> int:
    factory = get_session_factory()
    async with factory() as session:
        result = await import_catalogue(session, path, rating_source=rating_source)
        await session.commit()
    print(result.summary())
    if rating_source is RatingSource.MANUAL:
        print(
            "\nNOTE: ratings imported as 'manual' - curator estimates carrying a high\n"
            "rating deviation. They are not measured values. See spec section 11."
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dsa-coach")
    sub = parser.add_subparsers(dest="command", required=True)

    imp = sub.add_parser("import-catalogue", help="Import or refresh the problem catalogue.")
    imp.add_argument("--file", type=Path, default=DEFAULT_CATALOGUE)
    imp.add_argument(
        "--rating-source",
        choices=[s.value for s in RatingSource],
        default=RatingSource.MANUAL.value,
        help="Provenance of the ratings in the file. Never claim contest_derived "
        "for curator estimates.",
    )

    sub.add_parser("seed", help="Alias for import-catalogue with defaults.")

    args = parser.parse_args(argv)

    if args.command == "seed":
        return asyncio.run(_import(DEFAULT_CATALOGUE, RatingSource.MANUAL))
    if args.command == "import-catalogue":
        return asyncio.run(_import(args.file, RatingSource(args.rating_source)))

    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
