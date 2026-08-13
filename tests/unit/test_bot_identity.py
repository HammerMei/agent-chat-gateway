"""Two connectors must never be one bot account (§4.5).

The rule is checked at runtime because config cannot decide it: Mattermost token auth
leaves `username` empty, and two different tokens can authenticate the same account.
These tests cover the rule itself, the normalisation in front of it, and the guarantee
that every connector in the tree answers the identity question deliberately.
"""

import inspect
import pkgutil
import unittest
from pathlib import Path

from gateway.core.bot_identity import (
    BotIdentity,
    ConnectorIdentity,
    ConnectorIdentityError,
    canonical_origin,
    find_identity_conflicts,
)
from gateway.core.connector import Connector


class TestCanonicalOrigin(unittest.TestCase):
    """A comparison is only as good as the normalisation in front of it."""

    def test_the_spellings_of_one_server_converge(self):
        forms = [
            "https://mm.example.com",
            "https://mm.example.com/",
            "https://mm.example.com:443",
            "https://MM.Example.COM",
            "https://mm.example.com/api/v4",
        ]
        self.assertEqual(
            len({canonical_origin(f) for f in forms}),
            1,
            "these are one server; comparing raw strings would call them five accounts",
        )

    def test_a_non_default_port_is_kept(self):
        self.assertNotEqual(
            canonical_origin("https://rc.example.com:3000"),
            canonical_origin("https://rc.example.com"),
            "a different port is a different server, not a spelling",
        )

    def test_the_scheme_distinguishes(self):
        self.assertNotEqual(
            canonical_origin("http://rc.example.com"),
            canonical_origin("https://rc.example.com"),
        )

    def test_a_bare_host_is_accepted(self):
        """`urlsplit` reads a schemeless string as a path, which would make every bare
        host normalise to the same empty origin — every such connector a duplicate."""
        self.assertEqual(canonical_origin("rc.example.com"), "https://rc.example.com")


def _entry(name, user_id="u1", origin="https://s", scope="", owns_dms=False):
    return ConnectorIdentity(name, BotIdentity(origin, user_id, scope), owns_dms)


class TestIdentityConflicts(unittest.TestCase):
    def test_distinct_accounts_are_fine(self):
        self.assertEqual(
            find_identity_conflicts([_entry("a", "u1"), _entry("b", "u2")]), [])

    def test_the_same_account_twice_is_rejected(self):
        conflicts = find_identity_conflicts([_entry("a"), _entry("b")])
        self.assertEqual(len(conflicts), 1)
        self.assertIn("'a'", conflicts[0])
        self.assertIn("'b'", conflicts[0])

    def test_the_same_account_on_different_servers_is_not_a_conflict(self):
        """Platform user ids are not globally unique — two servers can issue the same
        one, and treating that as a collision would reject an unrelated pair."""
        self.assertEqual(
            find_identity_conflicts(
                [_entry("a", origin="https://s1"), _entry("b", origin="https://s2")]),
            [],
        )

    def test_different_teams_on_one_account_are_allowed(self):
        """The Mattermost exception: the socket spans every team, so each connector
        discards other teams' events and channels stay apart."""
        self.assertEqual(
            find_identity_conflicts(
                [_entry("a", scope="team1"), _entry("b", scope="team2")]),
            [],
        )

    def test_the_same_team_twice_is_still_a_conflict(self):
        self.assertEqual(
            len(find_identity_conflicts(
                [_entry("a", scope="team1"), _entry("b", scope="team1")])),
            1,
        )

    def test_one_scoped_and_one_unscoped_is_a_conflict(self):
        """Fail-closed on a mixture: an empty scope means "nothing separates me", so a
        Rocket.Chat connector and a Mattermost one on the same account still collide —
        the exception requires *every* member to be separated, not just one."""
        self.assertEqual(
            len(find_identity_conflicts([_entry("a", scope="team1"), _entry("b")])), 1)

    def test_two_dm_owners_on_one_account_are_rejected_even_across_teams(self):
        """The condition on the exception. A DM has no team, so the team gate cannot
        separate it and the platform delivers it to every open connection."""
        conflicts = find_identity_conflicts([
            _entry("a", scope="team1", owns_dms=True),
            _entry("b", scope="team2", owns_dms=True),
        ])
        self.assertEqual(len(conflicts), 1)
        self.assertIn("direct message", conflicts[0].lower())

    def test_one_dm_owner_across_teams_is_allowed(self):
        self.assertEqual(
            find_identity_conflicts([
                _entry("a", scope="team1", owns_dms=True),
                _entry("b", scope="team2", owns_dms=False),
            ]),
            [],
        )

    def test_every_conflict_is_reported_not_just_the_first(self):
        """Three colliding pairs should not need three restarts to discover."""
        conflicts = find_identity_conflicts([
            _entry("a", "u1"), _entry("b", "u1"),
            _entry("c", "u2"), _entry("d", "u2"),
        ])
        self.assertEqual(len(conflicts), 2)


class TestEveryConnectorAnswersDeliberately(unittest.TestCase):
    """The base returns None, so a new connector inherits "no account" by omission.

    That is the silent failure this whole check exists to prevent, one level up: a
    platform connector that forgot to override would simply never be compared against
    anything. Enumerating the package makes the omission fail here instead — the same
    treatment used for the state schema's fields, and for the same reason: a rule that
    depends on someone remembering is not a rule.
    """

    # The test: does this connector authenticate as an account on a server that
    # another connector could also authenticate as? Both of these listen locally and
    # log in to nothing, so two of them cannot become one account — they would collide
    # on a port or a pipe, loudly, which is a different failure with its own message.
    # A platform connector belongs here only if that question is genuinely "no".
    ACCOUNTLESS = {
        "ScriptConnector",  # stdin/stdout
        "VoiceConnector",   # local HTTP endpoint, no login
    }

    def _connector_classes(self):
        import gateway.connectors as pkg

        found = {}
        for mod in pkgutil.walk_packages(pkg.__path__, f"{pkg.__name__}."):
            try:
                module = __import__(mod.name, fromlist=["_"])
            except Exception:
                continue  # an optional dependency missing is not this test's business
            for name, obj in vars(module).items():
                if (
                    inspect.isclass(obj)
                    and issubclass(obj, Connector)
                    and obj is not Connector
                    and obj.__module__ == mod.name
                ):
                    found[name] = obj
        return found

    def test_no_connector_lives_outside_the_package_this_walks(self):
        """The walk above is scoped to `gateway.connectors`, and that scope is an
        assumption — the same shape as anchoring a sweep on a hand-picked directory and
        calling it exhaustive. This checks the assumption instead of trusting it: an
        `ast` pass over the whole `gateway/` tree, needing no imports, so a connector
        defined somewhere else fails here rather than silently inheriting "no account".
        """
        import ast

        import gateway

        # Anchored to the package's own location, not the working directory: a relative
        # "gateway" path would find nothing when pytest runs from elsewhere, and the
        # sweep would report a clean tree because it had read no files.
        root = Path(gateway.__file__).parent
        found = []
        for path in root.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if not isinstance(node, ast.ClassDef) or node.name == "Connector":
                    continue
                for base in node.bases:
                    name = getattr(base, "id", None) or getattr(base, "attr", None)
                    if name and "Connector" in name:
                        found.append((str(path), node.name))

        connectors_dir = str(root / "connectors")
        outside = sorted(
            f"{p}:{n}" for p, n in found if not p.startswith(connectors_dir))
        self.assertEqual(
            outside, [],
            "a Connector subclass outside gateway/connectors/ is invisible to the walk "
            "below, so it would never be asked for a bot identity — move it into the "
            "package, or widen the walk",
        )
        self.assertTrue(found, "the ast sweep found no connectors at all; it is broken")

    def test_the_sweep_finds_the_known_connectors(self):
        """Guards the enumeration itself: a walk that silently found nothing would make
        every assertion below vacuously true."""
        names = set(self._connector_classes())
        self.assertIn("RocketChatConnector", names)
        self.assertIn("MattermostConnector", names)

    def test_each_connector_overrides_identity_or_is_declared_accountless(self):
        for name, cls in sorted(self._connector_classes().items()):
            with self.subTest(connector=name):
                overrides = cls.bot_identity is not Connector.bot_identity
                self.assertTrue(
                    overrides or name in self.ACCOUNTLESS,
                    f"{name} neither reports a bot identity nor is listed as "
                    f"accountless, so two of them on one account would go unnoticed",
                )


if __name__ == "__main__":
    unittest.main()


class TestDeclaredAccountsAtConfigLoad(unittest.TestCase):
    """The early half: catch the obvious case before the operator restarts the daemon.

    Deliberately weaker than the runtime barrier and in the safe direction — every
    blind spot is a missed duplicate the barrier still catches, never a rejection of a
    configuration that would have worked.
    """

    def _validate(self, yaml_text):
        import tempfile
        from pathlib import Path as P

        from gateway.config_validate import validate_config

        with tempfile.TemporaryDirectory() as d:
            path = P(d) / "config.yaml"
            path.write_text(yaml_text)
            return validate_config(str(path))

    def _yaml(self, second_username="acg-bot"):
        # The second URL differs by an explicit default port, NOT a trailing slash:
        # the RC config parser already `rstrip("/")`s, so a trailing-slash pair would
        # compare equal with no normalisation of ours involved — the test would pass
        # while proving nothing. Found by disabling `canonical_origin` and watching
        # this test stay green.
        return f"""
connectors:
  - name: rc-one
    type: rocketchat
    server:
      url: https://chat.example.com
      username: acg-bot
      password: secret
  - name: rc-two
    type: rocketchat
    server:
      url: https://chat.example.com:443
      username: {second_username}
      password: secret
agents:
  default:
    type: claude
    working_directory: /tmp
watchers: []
"""

    def test_two_connectors_declaring_one_account_are_rejected(self):
        result = self._validate(self._yaml())
        self.assertTrue(
            any("same bot account" in e for e in result.errors),
            f"expected a duplicate-account error, got {result.errors}",
        )

    def test_a_different_spelling_of_one_server_does_not_hide_it(self):
        """The two URLs above are one server written two ways. Without normalisation
        this check reports nothing and looks exactly like a clean config."""
        self.assertTrue(any("same bot account" in e for e in self._validate(self._yaml()).errors))

    def test_distinct_usernames_pass(self):
        result = self._validate(self._yaml(second_username="other-bot"))
        self.assertFalse(
            [e for e in result.errors if "same bot account" in e],
            "two different accounts on one server are the supported setup",
        )


class TestTheRealConnectorsReportTheirIdentity(unittest.TestCase):
    """The enumeration test proves RC and MM *override* the method; this proves the
    overrides work. Renaming `_rest.user_id` or `_rest.team_id` would otherwise pass the
    entire suite and fail at boot, where the only symptom is a refusal to start.

    `RocketChatConnector.connect()` awaits `self._rest.login()` before anything else
    (connector.py), and Mattermost's awaits `get_me()` then `resolve_team()`, so both
    ids are populated by the time the barrier reads them.
    """

    def _rc(self, user_id, url="https://chat.example.com/"):
        from unittest.mock import MagicMock as M

        from gateway.connectors.rocketchat.connector import RocketChatConnector

        c = RocketChatConnector.__new__(RocketChatConnector)
        c._rest = M(user_id=user_id)
        c._config = M(server_url=url)
        return c

    def _mm(self, user_id, team_id, url="https://mm.example.com"):
        from unittest.mock import MagicMock as M

        from gateway.connectors.mattermost.connector import MattermostConnector

        c = MattermostConnector.__new__(MattermostConnector)
        c._rest = M(bot_user_id=user_id, team_id=team_id)
        c._config = M(server_url=url, team="eng")
        return c

    def test_rocketchat_reports_the_login_id_under_a_canonical_origin(self):
        identity = self._rc("rc-user-1").bot_identity()
        self.assertEqual(identity.user_id, "rc-user-1")
        self.assertEqual(identity.origin, "https://chat.example.com")
        self.assertEqual(identity.scope, "", "Rocket.Chat has no team to scope by")

    def test_rocketchat_refuses_when_it_has_no_id(self):
        with self.assertRaises(ConnectorIdentityError):
            self._rc("").bot_identity()

    def test_mattermost_scopes_by_the_resolved_team_id(self):
        """Not the configured `team:` string — that field takes a name *or* an id, so
        two connectors on one team can spell it two ways and compare as different,
        breaking the one case the exception is meant to make safe."""
        identity = self._mm("mm-user-1", "team-id-abc").bot_identity()
        self.assertEqual(identity.user_id, "mm-user-1")
        self.assertEqual(identity.scope, "team-id-abc")
        self.assertNotEqual(identity.scope, "eng", "must not be the configured name")

    def test_mattermost_refuses_without_a_user_id(self):
        with self.assertRaises(ConnectorIdentityError):
            self._mm("", "team-id-abc").bot_identity()

    def test_mattermost_refuses_without_a_resolved_team(self):
        """An empty scope would claim "nothing separates me from another connector on
        this account", which is not an answer Mattermost is allowed to give."""
        with self.assertRaises(ConnectorIdentityError):
            self._mm("mm-user-1", "").bot_identity()
