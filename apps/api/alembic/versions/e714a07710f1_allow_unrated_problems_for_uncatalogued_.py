"""Allow unrated problems, so an uncatalogued attempt is not discarded.

The catalogue holds 50 problems. LeetCode has thousands, so practising anything
outside the seeded set produced `Unknown problem: leetcode/<slug>`, the event
was marked `invalid`, and the evidence was thrown away — silently, because a
rejected event is dropped from the extension's queue and surfaced nowhere.

A problem can now exist without a rating or a difficulty. That is the honest
shape: the attempt genuinely happened and is a fact, while the rating is
genuinely unknown and must not be invented (invariant 5). An unrated problem is
inserted `is_active = false`, so the scheduler will never offer it, and the
readiness models skip it because there is no reference rating to score against.

Enriching the catalogue later turns those rows into usable evidence
retroactively: `recompute` replays every attempt, so an attempt recorded today
against an unrated problem starts counting the moment the problem gains a
rating.

Revision ID: e714a07710f1
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e714a07710f1"
down_revision: str | Sequence[str] | None = "7880eee4ef74"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # SQLite cannot alter a column in place, so both dialects go through
    # batch_alter_table — a no-op wrapper on Postgres, a table rebuild on SQLite.
    with op.batch_alter_table("problems") as batch:
        batch.alter_column("rating", existing_type=sa.Integer(), nullable=True)
        batch.alter_column("difficulty", existing_type=sa.String(length=20), nullable=True)


def downgrade() -> None:
    # Rows added for uncatalogued problems have no rating and cannot be given
    # one without inventing it, so they are removed rather than backfilled.
    # Their attempts cascade, which is the honest reversal: this migration is
    # what made them storable in the first place.
    op.execute(sa.text("DELETE FROM problems WHERE rating IS NULL"))
    with op.batch_alter_table("problems") as batch:
        batch.alter_column("rating", existing_type=sa.Integer(), nullable=False)
        batch.alter_column("difficulty", existing_type=sa.String(length=20), nullable=False)
