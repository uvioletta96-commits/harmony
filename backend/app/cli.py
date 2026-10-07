"""Flask CLI commands for administration and local setup."""

from __future__ import annotations

import os
import secrets
from typing import Any

import click
from flask import Flask
from flask.cli import with_appcontext
from sqlalchemy import func, or_, select

from .extensions import db
from .utils.logging import get_logger

logger = get_logger("harmony.cli")


def register_commands(app: Flask) -> None:
    app.cli.add_command(init_db)
    app.cli.add_command(seed_demo)
    app.cli.add_command(wipe)
    app.cli.add_command(create_admin)
    app.cli.add_command(create_user)
    app.cli.add_command(verify_user)
    app.cli.add_command(show_config)
    app.cli.add_command(routes_listing)
    app.cli.add_command(flush_cache)
    app.cli.add_command(run_maintenance)
    app.cli.add_command(generate_secret)


@click.command("init-db")
@click.option("--drop", is_flag=True, help="Drop every table first. Destroys all data.")
@with_appcontext
def init_db(drop: bool) -> None:
    """Create the schema, and add any column a model has gained since last deploy.

    ``db.create_all()`` on its own is *not* enough, and the deploy script's comment
    claimed otherwise until this broke the feed in production: ``create_all`` issues
    ``CREATE TABLE`` for tables that do not exist and does nothing at all for tables
    that do. A model that gains a column therefore leaves the database with a table
    the application cannot read, and the symptom is a 500 on every endpoint that
    touches it - with no migration having been attempted and none having failed.

    Additive only: a missing column is added, and nothing is dropped, renamed or
    retyped. That is the only kind of change safe to make automatically, because a
    wrong guess about a column's *type* loses data irreversibly while a wrong guess
    about its *absence* costs a column of defaults. Anything else belongs in a real
    migration.
    """
    if drop:
        click.confirm("This will permanently delete all data. Continue?", abort=True)
        db.drop_all()
        click.echo("Dropped all tables.")
    db.create_all()
    click.echo("Tables created.")
    added = add_missing_columns()
    if added:
        for table, column in added:
            click.echo(f"  added {table}.{column}")
        click.echo(f"{len(added)} column(s) added.")
    else:
        click.echo("No columns to add.")
    click.echo("Schema up to date.")
    click.echo("Next: flask seed-demo  (or set ADMIN_EMAIL / ADMIN_PASSWORD for an admin account)")


def add_missing_columns() -> list[tuple[str, str]]:
    """Add columns the models declare that the database does not have.

    Returns what was added, so a caller can print it and a test can assert on it.

    Only columns a model declares are considered, and only ones the table lacks. An
    existing column is never touched even if its type has drifted - see the note on
    ``init-db`` about why that has to be a deliberate decision.
    """
    inspector = db.inspect(db.engine)
    existing_tables = set(inspector.get_table_names())
    added: list[tuple[str, str]] = []

    for table in db.metadata.sorted_tables:
        if table.name not in existing_tables:
            # `create_all` just made it, with every column it declares.
            continue
        present = {column["name"] for column in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in present:
                continue
            ddl = _add_column_ddl(table.name, column)
            db.session.execute(db.text(ddl))
            added.append((table.name, column.name))
            click.echo(f"  {table.name}.{column.name}: {ddl}")

    if added:
        db.session.commit()
    return added


def _add_column_ddl(table_name: str, column: Any) -> str:
    """``ALTER TABLE ... ADD COLUMN ...`` for one model column.

    The rendered type comes from SQLAlchemy rather than being guessed at per
    backend, so a column added on SQLite in testing and on PostgreSQL in production
    come out as the same logical shape.

    Server defaults are carried over: without one, PostgreSQL refuses to add a
    non-nullable column to a table that already has rows, which is the common case
    on a live site. A model column with no default is therefore added as nullable,
    and left that way - the application writes the value on the next insert, and
    backfilling a guess would be worse than a null nobody reads.
    """
    rendered = column.type.compile(dialect=db.engine.dialect)
    parts = [f'ALTER TABLE "{table_name}" ADD COLUMN "{column.name}" {rendered}']

    if column.default is not None and getattr(column.default, "is_scalar", False):
        parts.append(f"DEFAULT {column.default.arg!r}")
    elif getattr(column.server_default, "arg", None) is not None:
        parts.append(f"DEFAULT {column.server_default.arg!r}")

    if not column.nullable:
        # No default to backfill with, so the constraint would fail against existing
        # rows. Nullable is the honest state; see the docstring.
        parts = [p for p in parts if not p.startswith("NOT NULL")]

    return " ".join(parts)


@click.command("seed-demo")
@click.option("--posts", default=12, help="Number of demo posts per author.")
@click.option("--users", default=6, help="Number of demo users.")
@with_appcontext
def seed_demo(users: int, posts: int) -> None:
    """Populate a development database with realistic sample content."""
    from .extensions import hash_password
    from .models.post import Comment, Post, PostVisibility
    from .models.user import ProfileVisibility, User, UserRole, UserStatus
    from .security.validators import slugify_username

    if User.query.first() is not None and not click.confirm("Users already exist. Add more?", default=False):
        return

    palette = ["sand", "stone", "sage", "clay", "dusk", "linen"]
    bios = [
        "Люблю тихие утро, длинные книги и медленные прогулки.",
        "Дизайнер. Собираю интерфейсы, в которых не нужно искать главное.",
        "Читаю про архитектуру, велосипед и минимализм.",
        "Готовлю кофе так, как будто это ритуал, а не напиток.",
        "Фотографирую свет по утрам. Плёнка, зерно, терпение.",
        "Пишу заметки о том, как устроены вещи.",
    ]
    bodies = [
        "Сегодня впервые за долгое время никуда не спешил. Вышел на улицу без наушников — и заметил, как иначе звучит город.",
        "Хорошая типографика не кричит. Она просто освобождает место для содержания.",
        "Меняю один проект уже третий месяц. Кажется, дело не в проекте.",
        "Прогулка вдоль реки. Никто никуда не торопится, в том числе и я.",
        "Иногда лучшее, что можно сделать с задачей, — отложить её до утра.",
        "Полка с книгами растёт быстрее, чем список прочитанного. Это нормально.",
    ]

    created: list[User] = []
    for index in range(users):
        base = f"user{index + 1}"
        username = slugify_username(base) or base
        email = f"{base}@harmony.local"
        if User.query.filter_by(username=username).first():
            continue
        user = User(
            email=email,
            username=username,
            password_hash=hash_password("DemoPassw0rd!23", rounds=4),
            display_name=f"Участник {index + 1}",
            bio=bios[index % len(bios)],
            role=UserRole.MEMBER.value,
            status=UserStatus.ACTIVE.value,
            email_verified=True,
            avatar_color=palette[index % len(palette)],
            profile_visibility=ProfileVisibility.PUBLIC.value,
            data_processing_consent=True,
        )
        db.session.add(user)
        created.append(user)
    db.session.flush()

    for index, user in enumerate(created):
        for offset in range(posts):
            post = Post(
                author_id=user.id,
                body=bodies[(index + offset) % len(bodies)],
                visibility=PostVisibility.PUBLIC.value,
            )
            db.session.add(post)
            db.session.flush()
            user.posts_count = (user.posts_count or 0) + 1
            if offset % 4 == 0 and created:
                author = created[(index + offset) % len(created)]
                if author.id != user.id:
                    db.session.add(
                        Comment(post_id=post.id, author_id=author.id, body="Согласен(на). Приятное наблюдение.")
                    )
                    post.comments_count = (post.comments_count or 0) + 1

    db.session.commit()
    click.echo(f"Created {len(created)} users with {posts} posts each.")


@click.command("wipe")
@click.option("--yes", is_flag=True, help="Do not ask for confirmation.")
@click.option("--keep-admins", is_flag=True, help="Leave administrator accounts in place.")
@with_appcontext
def wipe(yes: bool, keep_admins: bool) -> None:
    """Delete every account and all of their content.

    The reset button for a development instance that has been experimented on.
    It reuses :func:`purge_account` so deletion order and foreign-key handling
    are exactly the ones the scheduled erasure uses - a demo wipe that worked
    by a different route would be a wipe that could disagree with production
    behaviour about what a user owns.
    """
    from .models.user import User, UserRole
    from .services.user_service import purge_account

    query = db.select(User)
    if keep_admins:
        query = query.where(User.role != UserRole.ADMIN.value)
    users = db.session.scalars(query).all()
    if not users:
        click.echo("Nothing to delete.")
        return

    if not yes:
        click.confirm(
            f"This permanently deletes {len(users)} account(s) and everything they own. Continue?",
            abort=True,
        )

    for user in users:
        purge_account(user.id)
    click.echo(f"Deleted {len(users)} account(s) and all associated content.")


@click.command("create-admin")
@click.option("--email", prompt=True)
@click.option("--username", prompt=True, default="admin")
@click.option("--password", prompt=True, hide_input=True, confirmation_prompt=True)
@with_appcontext
def create_admin(email: str, username: str, password: str) -> None:
    """Create or promote an administrator."""
    from .extensions import hash_password
    from .models.user import User, UserRole, UserStatus

    if len(password) < 12:
        raise click.ClickException("Пароль должен быть не короче 12 символов.")

    user = User.query.filter((User.email == email.lower()) | (User.username == username)).first()
    if user is None:
        user = User(email=email.lower(), username=username, display_name=username)
        db.session.add(user)
    user.password_hash = hash_password(password)
    user.role = UserRole.ADMIN.value
    user.status = UserStatus.ACTIVE.value
    user.email_verified = True
    user.data_processing_consent = True
    db.session.commit()
    click.echo(f"Administrator ready: {user.username} <{user.email}>")


@click.command("create-user")
@click.option("--email", prompt=True)
@click.option("--username", prompt=True)
@click.option("--password", prompt=True, hide_input=True, confirmation_prompt=True)
@click.option("--admin", is_flag=True)
@with_appcontext
def create_user(email: str, username: str, password: str, admin: bool) -> None:
    """Create a user directly, bypassing email verification (local/dev only)."""
    from .extensions import hash_password
    from .models.user import User, UserRole, UserStatus

    if User.query.filter((User.email == email.lower()) | (User.username == username)).first():
        raise click.ClickException("Пользователь с такими данными уже существует.")
    user = User(
        email=email.lower(),
        username=username,
        password_hash=hash_password(password),
        role=UserRole.ADMIN.value if admin else UserRole.MEMBER.value,
        status=UserStatus.ACTIVE.value,
        email_verified=True,
        data_processing_consent=True,
        display_name=username,
    )
    db.session.add(user)
    db.session.commit()
    click.echo(f"Created {user.username} ({'admin' if admin else 'member'}).")


@click.command("verify-user")
@click.argument("identifier")
def verify_user(identifier: str) -> None:
    """Confirm an account's email address without sending anything.

    The escape hatch for a local instance: with MAIL_ENABLED off there is no
    inbox to click a link in, and a reader who registered from the browser is
    otherwise stranded at "check your email" with no way forward. It does exactly
    what the link in the email would have done and nothing more - it does not
    create the account, does not touch a banned one, and does not reset a
    password.
    """
    from .models.user import User, UserStatus

    needle = identifier.strip().lower()
    user = db.session.scalar(
        select(User).where(or_(func.lower(User.email) == needle, func.lower(User.username) == needle))
    )
    if user is None:
        click.echo(f"No account matches {identifier!r}.")
        raise SystemExit(1)

    if user.email_verified:
        click.echo(f"@{user.username} is already confirmed (status: {user.status}).")
        return

    user.email_verified = True
    if user.status == UserStatus.PENDING.value:
        user.status = UserStatus.ACTIVE.value
    db.session.commit()
    click.echo(f"@{user.username} confirmed - they can sign in now.")


@click.command("show-config")
@with_appcontext
def show_config() -> None:
    """Print the effective configuration with secrets redacted."""
    from flask import current_app

    secret_markers = ("SECRET", "PASSWORD", "KEY", "PEPPER", "SALT", "TOKEN")
    width = max(len(key) for key in current_app.config)
    click.echo(f"{'SETTING'.ljust(width)}  VALUE")
    for key in sorted(current_app.config):
        if key.startswith("_"):
            continue
        value = current_app.config[key]
        if any(marker in key.upper() for marker in secret_markers) and value:
            value = "***redacted***"
        click.echo(f"{key.ljust(width)}  {value}")


@click.command("routes")
@with_appcontext
def routes_listing() -> None:
    """List every registered route, optionally filtered.

    Used by the CI spec-coverage check to guarantee the OpenAPI document
    describes every endpoint that actually exists.
    """
    from flask import current_app

    pattern = os.environ.get("ROUTES_FILTER")
    rows = []
    for rule in sorted(current_app.url_map.iter_rules(), key=lambda r: str(r)):
        if rule.endpoint == "static":
            continue
        methods = ",".join(sorted(rule.methods - {"HEAD", "OPTIONS"}))
        if pattern and pattern not in str(rule):
            continue
        rows.append((methods, str(rule), rule.endpoint))
    width = max(len(row[0]) for row in rows) if rows else 10
    for methods, rule, endpoint in rows:
        click.echo(f"{methods.ljust(width)}  {rule.ljust(56)}  {endpoint}")
    click.echo(f"\n{len(rows)} routes.")


@click.command("flush-cache")
@with_appcontext
def flush_cache() -> None:
    """Drop every cached key. Useful after a deploy."""
    from .extensions import get_redis
    from .services.cache_service import NAMESPACE

    client = get_redis()
    if client is None:
        click.echo("Redis is not available; nothing to flush.")
        return
    removed = 0
    for key in client.scan_iter(match=f"{NAMESPACE}:*", count=1000):
        removed += client.delete(key)
    click.echo(f"Removed {removed} cache keys.")


@click.command("maintenance")
@click.argument("task")
@click.option("--dry-run", is_flag=True)
@with_appcontext
def run_maintenance(task: str, dry_run: bool) -> None:
    """Run a maintenance task synchronously (same code the worker runs)."""
    from .tasks import maintenance_tasks

    handler = {
        "orphans": lambda: maintenance_tasks.purge_orphan_uploads(dry_run=dry_run),
        "deletions": lambda: maintenance_tasks.execute_scheduled_deletions(),
        "tokens": lambda: maintenance_tasks.purge_expired_tokens(),
        "archive": lambda: maintenance_tasks.archive_stale_posts(),
        "warm": lambda: maintenance_tasks.warm_cache(),
        "db-stats": lambda: maintenance_tasks.db_stats(),
    }.get(task)
    if handler is None:
        raise click.ClickException(f"Unknown task {task!r}. Available: {', '.join(sorted(handler_names()))}")
    click.echo(handler())


def handler_names() -> list[str]:  # pragma: no cover - helper
    return ["orphans", "deletions", "tokens", "archive", "warm", "db-stats"]


@click.command("generate-secret")
def generate_secret() -> None:
    """Print a strong random secret suitable for SECRET_KEY / JWT_SECRET_KEY."""
    click.echo(secrets.token_urlsafe(48))


__all__ = ["register_commands"]
