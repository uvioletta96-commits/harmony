"""Read ``.env`` before anything reads the environment.

Every setting in this application comes from ``os.environ`` through
``config.env()``. ``python-dotenv`` was a dependency and ``.env.example``
said "copy to .env and fill in" - but nothing ever loaded the file, so a
reader who followed the instructions exactly would fill it in, restart, and
watch every value be ignored. Nothing reads a missing file, so this cannot
make anything worse.

Real environment variables win. A container platform sets them from its own
secret store, and a shell export is usually deliberate; the file is the
fallback for someone working locally.

**Why this lives in its own module, imported before ``.config``:** every
config profile evaluates ``env("...")`` in its *class body*, so all of them
are resolved once, when ``app/config.py`` is first imported - not when a
profile is instantiated. A loader called from inside ``create_app()`` runs
after ``from .config import get_config`` has already snapshotted an empty
environment, so the file was parsed into ``os.environ`` too late and every
value in it was ignored. That is invisible under Docker, where the values
arrive as real environment variables, and fatal on a host where the file is
the only source. Importing this module first is what makes it work.
"""

from __future__ import annotations

from pathlib import Path


def load_dotenv_files() -> None:
    """Merge the first ``.env`` found into ``os.environ`` without overriding it.

    The repository root, so the same file is found whether the app is started
    from ``backend/`` (``flask --app wsgi``), from the root, or from tests.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - dependency is declared
        return

    for candidate in (Path.cwd(), Path(__file__).resolve().parent.parent.parent):
        path = candidate / ".env"
        if path.is_file():
            load_dotenv(path, override=False)
            return