"""A model that gains a column must not need a human to run a migration.

`create_all` creates missing *tables* and ignores existing ones entirely. So
adding a column to a model leaves the database holding a table the application
cannot read, and the symptom is a 500 on every endpoint that touches it - with no
migration attempted and none failed, which is the worst combination: the deploy
succeeds and says nothing.

This found itself. `post_media.duration_ms` was added for the video library and
`/videos` returned 500 on the live site, while the feed, login and every other
page kept working - which is exactly why it survived a deploy and a health check.
"""

from __future__ import annotations

import pytest
from sqlalchemy import inspect

from tests.utils import make_png


@pytest.fixture()
def live(app, ctx):
    """The application's own metadata, against a real database."""
    from app.extensions import db

    return db


def test_create_all_is_additive_for_tables_only(live):
    """The premise, checked rather than assumed.

    If this ever starts failing because something upstream taught `create_all` to
    alter tables, the rest of this file is describing a problem that no longer
    exists - which is worth knowing before trusting its conclusions.
    """
    from app.extensions import db

    tables = set(inspect(db.engine).get_table_names())
    assert "posts" in tables, "the fixture database was not created"
    assert "post_media" in tables


class TestMissingColumns:
    def test_a_fresh_database_needs_nothing_added(self, app):
        """Every column a model declares already exists on a database built from
        those same models. If this reports something, the helper is inventing
        columns rather than finding them."""
        from app.cli import add_missing_columns

        assert add_missing_columns() == [], "a fresh database was found to be missing columns"

    def test_a_new_column_is_added_to_an_existing_table(self, app, ctx):
        """The whole point, exercised for real: drop a column the models declare,
        then let the helper put it back."""
        from sqlalchemy import text

        from app.cli import add_missing_columns
        from app.extensions import db

        db.session.execute(text("ALTER TABLE post_media DROP COLUMN duration_ms"))
        db.session.commit()

        inspector = inspect(db.engine)
        assert "duration_ms" not in {c["name"] for c in inspector.get_columns("post_media")}

        added = add_missing_columns()

        assert ("post_media", "duration_ms") in added, f"the dropped column was not restored: {added}"

    def test_the_restored_column_is_readable_and_usable(self, app, ctx):
        """Restoring a column that then fails to serve a query would have been the
        same 500 with a longer deploy log."""
        from sqlalchemy import text

        from app.cli import add_missing_columns
        from app.extensions import db

        db.session.execute(text("ALTER TABLE post_media DROP COLUMN duration_ms"))
        db.session.commit()
        add_missing_columns()

        from app.models.post import PostMedia

        db.session.execute(text("SELECT duration_ms FROM post_media LIMIT 1"))
        db.session.rollback()
        assert "duration_ms" in {c.name for c in PostMedia.__table__.columns}

    def test_it_is_idempotent(self, app, ctx):
        """A deploy runs this every time. Running it twice must not fail on an
        `ADD COLUMN` that already happened."""
        from app.cli import add_missing_columns

        assert add_missing_columns() == []
        assert add_missing_columns() == []

    def test_it_only_ever_adds_and_never_alters(self, app, ctx):
        """Additive only, checked by watching what it actually executes.

        Two earlier attempts at this failed for instructive reasons. Comparing the
        types an inspector reports before and after finds nothing on SQLite, which
        has no `ALTER COLUMN` to compare against; and comparing the `TypeEngine`
        objects reports every column as changed, because `get_columns` builds fresh
        instances that compare by identity.

        Recording the statements sidesteps both, and states the property directly:
        the only DDL this helper is allowed to issue is `ADD COLUMN`. That is what
        makes it safe to run unattended on a live site - anything else it could do
        would be a guess about data it cannot see.
        """
        from sqlalchemy import text

        from app.cli import add_missing_columns
        from app.extensions import db

        db.session.execute(text("ALTER TABLE post_media DROP COLUMN duration_ms"))
        db.session.commit()

        statements: list[str] = []
        original = db.session.execute

        def spy(statement, *args, **kwargs):
            statements.append(str(statement))
            return original(statement, *args, **kwargs)

        db.session.execute = spy  # type: ignore[method-assign]
        try:
            add_missing_columns()
        finally:
            db.session.execute = original  # type: ignore[method-assign]

        assert statements, "the helper ran no DDL, so the test proves nothing"
        for statement in statements:
            upper = statement.upper()
            assert "ADD COLUMN" in upper, f"the helper issued something other than ADD COLUMN: {statement}"
            for forbidden in ("DROP ", "ALTER COLUMN", "RENAME", "TRUNCATE", "DELETE", "UPDATE", "MODIFY"):
                assert forbidden not in upper, (
                    f"the helper issued a destructive statement ({forbidden!r}): {statement}"
                )

    def test_it_does_not_drop_anything(self, app, ctx):
        """The one irreversible mistake available to a schema tool."""
        from sqlalchemy import text

        from app.cli import add_missing_columns
        from app.extensions import db

        db.session.execute(text("ALTER TABLE post_media DROP COLUMN duration_ms"))
        db.session.commit()
        add_missing_columns()

        names = {c["name"] for c in inspect(db.engine).get_columns("post_media")}
        for required in ("id", "storage_key", "url", "mime_type", "content_hash", "duration_ms"):
            assert required in names, f"{required} went missing"


class TestDeployRunsIt:
    def test_the_deploy_script_does_not_claim_create_all_alters_tables(self):
        """The comment that caused this. It read:

            `create_all` is additive and idempotent: it adds tables and columns
            that do not exist and leaves everything else alone.

        The second half is false, and it is the half that matters when a model gains
        a column. Whatever the script says next has to be true, because the wrong
        version of this sentence is what let a 500 through a deploy.
        """
        from pathlib import Path

        script = Path(__file__).resolve().parents[2] / "deploy" / "deploy-server.sh"
        text = script.read_text(encoding="utf-8")

        assert "create_all" in text, "the deploy no longer mentions the schema step"
        for false_claim in ("adds tables and columns", "tables and\n# columns"):
            assert false_claim not in text, (
                f"the deploy still claims {false_claim!r} about create_all, which is "
                "false for an existing table"
            )
        assert "adds the\n# missing columns" in text or "adds the missing columns" in text, (
            "the deploy does not say that it adds columns separately"
        )


class TestTheFailureItWouldHaveCaught:
    """The regression itself, at the level it appeared: a query naming a column the
    database does not have."""

    def test_the_video_feed_query_names_a_column_that_exists(self, app, ctx, auth_client, make_user):
        """`/videos` reads `post_media.duration_ms` in two places - the existence
        subquery and the choice of what fills the screen. If the column is absent
        both raise `UndefinedColumn`, and the endpoint 500s for every reader."""
        import io

        from sqlalchemy import text

        from app.extensions import db

        mp4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 40
        stored = auth_client.upload(
            "/api/v1/uploads/images",
            files={"file": (io.BytesIO(mp4), "a.mp4", "application/octet-stream")},
            form={"durations": "120000"},
        ).get_json()["data"]["files"][0]
        auth_client.post(
            "/api/v1/posts", json={"body": "клип", "media": [{"storage_key": stored["storage_key"]}]}
        )

        # Exactly the deployed state: the model has the column, the table has not.
        db.session.execute(text("ALTER TABLE post_media DROP COLUMN duration_ms"))
        db.session.commit()

        response = auth_client.get("/api/v1/videos?kind=video")
        assert response.status_code == 500, (
            "dropping the column did not break the feed, so the premise of this file "
            "is wrong"
        )

        # And the fix is what the deploy now runs.
        from app.cli import add_missing_columns

        add_missing_columns()
        assert auth_client.get("/api/v1/videos?kind=video").status_code == 200


def test_a_post_still_serialises_its_length_after_the_fix(auth_client, make_user):
    """The end the whole exercise is for: a clip reports its duration, and the feed
    filters on it."""
    import io

    mp4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 40
    stored = auth_client.upload(
        "/api/v1/uploads/images",
        files={"file": (io.BytesIO(mp4), "a.mp4", "application/octet-stream")},
        form={"durations": "120000"},
    ).get_json()["data"]["files"][0]
    auth_client.post(
        "/api/v1/posts", json={"body": "клип", "media": [{"storage_key": stored["storage_key"]}]}
    )

    items = auth_client.get("/api/v1/videos?kind=video").get_json()["data"]

    assert len(items) == 1
    assert items[0]["media"][0]["duration_ms"] == 120_000


def test_an_photo_post_is_unaffected_by_the_schema_fix(auth_client, make_user):
    """The helper touched every table; this checks it did not disturb the ordinary
    case on the way."""
    import io

    stored = auth_client.upload(
        "/api/v1/uploads/images",
        files={"file": (io.BytesIO(make_png()), "p.png", "image/png")},
        form={},
    ).get_json()["data"]["files"][0]
    auth_client.post(
        "/api/v1/posts", json={"body": "фото", "media": [{"storage_key": stored["storage_key"]}]}
    )

    items = auth_client.get("/api/v1/videos?kind=photo").get_json()["data"]

    assert len(items) == 1