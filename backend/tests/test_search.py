"""Search tests.

Search is the one place where a mistake is invisible until somebody uses the
box: a query that 500s still looks like "nobody matched" from the page, and a
query that silently returns nothing is indistinguishable from an empty network.

`search_users` was broken exactly that way - ``query.limit(...).scalars()`` on a
``Select``, which has no ``scalars()`` - so every people search on the deployed
server answered 500. It had no test at all.
"""

from __future__ import annotations

import pytest

from app.extensions import db
from app.models.post import Post, PostStatus
from app.models.user import PrivacySetting, ProfileVisibility, UserStatus
from app.services import search_service, user_service
from tests.utils import assert_error, assert_ok, create_post


def _opt_out_of_discovery(user) -> None:
    """Put a user in the state "hidden from search", the way settings does."""
    row = db.session.get(PrivacySetting, user.id)
    if row is None:
        row = PrivacySetting(user_id=user.id)
        db.session.add(row)
    row.discoverable_by_search = False
    db.session.commit()


class TestUserSearch:
    def test_finds_a_user_by_username(self, guest_client, other_user):
        rows = assert_ok(guest_client.get(f"/api/v1/users/search?q={other_user.username}"))
        assert [row["username"] for row in rows] == [other_user.username]

    def test_finds_a_user_by_display_name(self, guest_client, other_user):
        other_user.display_name = "Абрикосовый Соня"
        db.session.commit()
        rows = assert_ok(guest_client.get("/api/v1/users/search?q=Абрикосовый"))
        assert [row["username"] for row in rows] == [other_user.username]

    def test_matches_case_insensitively(self, guest_client, other_user):
        rows = assert_ok(guest_client.get(f"/api/v1/users/search?q={other_user.username.upper()}"))
        assert [row["username"] for row in rows] == [other_user.username]

    def test_does_not_return_the_viewer(self, auth_client, user):
        rows = assert_ok(auth_client.get(f"/api/v1/users/search?q={user.username}"))
        assert rows == []

    def test_a_query_with_no_match_is_empty_not_an_error(self, guest_client, other_user):
        rows = assert_ok(guest_client.get("/api/v1/users/search?q=zzzzznotathing"))
        assert rows == []

    def test_a_blank_query_is_rejected(self, guest_client, ctx):
        """An empty box must not become `LIKE '%%'`, which matches every row."""
        assert_error(guest_client.get("/api/v1/users/search?q="), status=422)

    def test_a_user_opted_out_of_discovery_is_not_returned(self, guest_client, other_user):
        _opt_out_of_discovery(other_user)
        rows = assert_ok(guest_client.get(f"/api/v1/users/search?q={other_user.username}"))
        assert rows == []

    def test_a_new_account_is_findable_immediately(self, guest_client, make_user):
        """Registering must not take a second action to become discoverable.

        A brand new account has no `privacy_settings` row at all. Treating that
        as "opted out" leaves every newcomer unfindable until they open settings
        and save, which from the search page is indistinguishable from the people
        list being broken.
        """
        fresh = make_user(username="freshface")
        assert fresh.status == UserStatus.ACTIVE.value
        rows = assert_ok(guest_client.get("/api/v1/users/search?q=freshface"))
        assert [row["username"] for row in rows] == ["freshface"]

    def test_pagination_does_not_lose_or_duplicate_a_row(self, guest_client, make_user):
        made = [make_user(username=f"pageuser{index}") for index in range(5)]
        first = assert_ok(guest_client.get("/api/v1/users/search?q=pageuser&limit=2&offset=0"))
        second = assert_ok(guest_client.get("/api/v1/users/search?q=pageuser&limit=2&offset=2"))
        seen = [row["username"] for row in first + second]
        assert len(seen) == len(set(seen)), "a row came back on two pages"
        assert set(seen) <= {user.username for user in made}


class TestPostSearch:
    def test_finds_a_post_by_its_text(self, auth_client, guest_client, user):
        create_post(auth_client, "Белёвская пастила с вишней")
        posts = assert_ok(guest_client.get("/api/v1/posts/search?q=пастила"))
        assert posts, "the post was not found by a word from its body"
        assert "пастила" in posts[0]["body"]

    def test_a_query_with_no_match_is_empty_not_an_error(self, guest_client, user):
        posts = assert_ok(guest_client.get("/api/v1/posts/search?q=zzzzznotathing"))
        assert posts == []

    def test_a_blank_query_is_rejected(self, guest_client, ctx):
        assert_error(guest_client.get("/api/v1/posts/search?q="), status=422)


class TestSearchVisibility:
    """Search must not be a way around a private profile or an unpublished post."""

    def test_a_private_profile_is_not_returned(self, guest_client, other_user):
        other_user.profile_visibility = ProfileVisibility.PRIVATE.value
        db.session.commit()
        rows = assert_ok(guest_client.get(f"/api/v1/users/search?q={other_user.username}"))
        assert rows == []

    def test_a_draft_post_is_not_returned(self, guest_client, user):
        db.session.add(
            Post(
                author_id=user.id,
                body="черновик не для поиска",
                status=PostStatus.ARCHIVED.value,
            )
        )
        db.session.commit()
        posts = assert_ok(guest_client.get("/api/v1/posts/search?q=черновик"))
        assert posts == []

    def test_a_hidden_post_is_not_returned(self, guest_client, user):
        db.session.add(
            Post(
                author_id=user.id,
                body="скрытый пост",
                status=PostStatus.HIDDEN.value,
            )
        )
        db.session.commit()
        posts = assert_ok(guest_client.get("/api/v1/posts/search?q=скрытый"))
        assert posts == []

    @pytest.mark.parametrize(
        "status",
        [UserStatus.SUSPENDED, UserStatus.BANNED, UserStatus.DEACTIVATED, UserStatus.PENDING],
    )
    def test_an_inactive_account_is_not_returned(self, guest_client, other_user, status):
        other_user.status = status.value
        db.session.commit()
        rows = assert_ok(guest_client.get(f"/api/v1/users/search?q={other_user.username}"))
        assert rows == []


class TestPeopleDirectory:
    """The "who is here" list, which is how a new account finds anyone at all."""

    def test_a_signed_in_visitor_is_offered_people(self, user, other_user):
        """The "who to follow" rail, for somebody who has just registered.

        This is what "make people easy to find" has to mean in practice. It used
        to be reachable only as a side effect of following somebody, so a brand
        new account saw an empty list and had no way to discover anybody.
        """
        people = search_service.discover(user, limit=10)["people"]
        assert other_user.username in [row["username"] for row in people]

    def test_suggestions_include_somebody_to_follow(self, client, user, other_user):
        names = [row["username"] for row in user_service.suggestions_for(user, limit=8)]
        assert other_user.username in names

    def test_a_newcomer_is_offered_to_everybody_else(self, auth_client, user, make_user):
        """The other half of discoverability.

        Being findable is only half the job. A brand new account must also show
        up in other people's suggestions, or "add people to be friends" produces
        an empty list for everyone who just registered.
        """
        newcomer = make_user(username="justarrived")
        names = [row["username"] for row in user_service.suggestions_for(user, limit=20)]
        assert newcomer.username in names