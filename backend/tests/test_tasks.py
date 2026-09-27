"""Enqueueing a task must never hold an HTTP request open.

The bug this file exists for: every call site wrapped ``task.delay()`` in a
``try``, which reads like it makes the publish cheap. It does not. Celery
retries the broker connection twenty times at one-second intervals, so the
exception surfaces about 108 seconds later and the reader who pressed "publish"
watches a spinner for nearly two minutes. The work is already committed by
then; only the response is late.
"""

from __future__ import annotations

from app.tasks import dispatch


class _Task:
    """A stand-in for a Celery task that records how it was called."""

    def __init__(self, name: str = "test.task", raises: Exception | None = None) -> None:
        self.name = name
        self.calls: list[tuple] = []
        self._raises = raises

    def delay(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self._raises is not None:
            raise self._raises
        return "task-id"


class TestDispatch:
    def test_publishes_when_the_broker_is_there(self, app, monkeypatch):
        monkeypatch.setattr(dispatch, "broker_available", lambda: True)
        task = _Task()
        assert dispatch.enqueue(task, 1, 2) is True
        assert task.calls == [((1, 2), {})]

    def test_skips_publish_when_the_broker_is_absent(self, app, monkeypatch):
        """The whole point: no publish attempt, so no connection retries."""
        monkeypatch.setattr(dispatch, "broker_available", lambda: False)
        task = _Task()
        assert dispatch.enqueue(task, 1) is False
        assert task.calls == [], "a publish was attempted against a broker that is not there"

    def test_a_broker_that_appears_to_fail_is_reported_not_raised(self, app, monkeypatch):
        """The check is an optimisation, not a guarantee: the broker can vanish
        between the check and the publish, and that must not reach the caller."""
        monkeypatch.setattr(dispatch, "broker_available", lambda: True)
        task = _Task(raises=ConnectionError("broker gone"))
        assert dispatch.enqueue(task) is False

    def test_keyword_arguments_are_forwarded(self, app, monkeypatch):
        monkeypatch.setattr(dispatch, "broker_available", lambda: True)
        task = _Task()
        dispatch.enqueue(task, post_id=7)
        assert task.calls == [((), {"post_id": 7})]

    def test_broker_available_reflects_the_redis_client(self, app, monkeypatch):
        monkeypatch.setattr("app.tasks.dispatch.get_redis", lambda: None)
        assert dispatch.broker_available() is False
        monkeypatch.setattr("app.tasks.dispatch.get_redis", lambda: object())
        assert dispatch.broker_available() is True


class TestCallSitesUseTheHelper:
    """A new ``.delay()`` outside :mod:`app.tasks.dispatch` reintroduces the stall.

    Checked by reading the source rather than by calling it: the property is
    about where the call is written, and a test that exercised each site would
    need a live broker to be meaningful.
    """

    SOURCES = (
        "app/services/post_service.py",
        "app/services/comment_service.py",
        "app/services/auth_service.py",
        "app/api/posts.py",
    )

    def test_no_bare_delay_outside_the_dispatcher(self):
        import pathlib

        import app.services

        # SOURCES are written as ``app/...`` so a failure names the module the
        # way the rest of the project does, which means the root is the
        # directory *containing* the ``app`` package.
        root = pathlib.Path(app.services.__file__).parent.parent.parent
        offenders: list[str] = []
        for relative in self.SOURCES:
            for number, line in enumerate((root / relative).read_text(encoding="utf-8").splitlines(), 1):
                stripped = line.strip()
                if ".delay(" not in stripped or stripped.startswith(("#", "*", "//")):
                    continue
                offenders.append(f"{relative}:{number}: {stripped}")
        assert not offenders, "call .delay() through app.tasks.dispatch.enqueue instead:\n" + "\n".join(offenders)
