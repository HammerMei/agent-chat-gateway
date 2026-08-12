# scripts/

Standalone lab/diagnostic scripts. Not part of the shipped package
(`pyproject.toml` builds only `gateway`), not imported by anything, and not
covered by the test suite.

## Platform probes for the dynamic-watcher design

`docs/design/dynamic-watcher-design.md` §6 records platform behaviour that
cannot be established from this repository — it depends on what a live
Rocket.Chat or Mattermost server actually sends. These scripts are how that
section was produced, kept so the findings can be re-verified against a
different server version rather than trusted indefinitely.

Each is self-driving: it opens the subscription under test, then generates
the traffic itself from a second account, so nothing depends on hand-timed
posting.

| Script | Question |
|---|---|
| `probe_a1_rc.py` | Does Rocket.Chat's `__my_messages__` subscribe-all work, what does `fields.eventName` contain, and what does the per-message access object carry? |
| `probe_a1_rc_followup.py` | Are system messages and DMs delivered over that subscription, and does the second `sub` parameter change anything? |
| `probe_a2_mm.py` | Does Mattermost's websocket deliver posts for channels the account can merely *read*, or only ones it belongs to? Are channel name/type/team id present on the event? |

They deliberately do **not** reuse the connectors in `gateway/connectors/`:
the point is to observe the wire frame before any of our own parsing can
transform or drop fields.

### Running them

Needs two accounts on the target server — a probe account whose socket is
observed, and a higher-privileged one to generate traffic and provision
fixtures. Fixtures can be created with the admin CLI:

```bash
uv run python -m gateway.admin <profile> create-user probe-bot probe-bot@example.com '<pw>'
uv run python -m gateway.admin <profile> add-to-channel probe-bot sandbox
uv run python -m gateway.admin <profile> create-channel probe-outside   # probe-bot NOT added
```

The `probe-outside` channel must be **public and not joined** by the probe
account — that is the case which distinguishes membership from readability,
and it is the entire point of `probe_a2_mm.py`.

```bash
uv run python scripts/probe_a1_rc.py --url https://rc.example.com \
    --probe-user probe-bot --probe-password '<pw>' \
    --admin-user <admin> --admin-password '<pw>'

uv run python scripts/probe_a2_mm.py --url https://mm.example.com --team <team> \
    --probe-user probe-bot --probe-password '<pw>' \
    --admin-user <admin> --admin-password '<pw>'
```

Pass `--help` for the remaining options. Credentials are arguments only —
nothing is read from `admin-profiles.yaml` and nothing is written to disk.

Run these against a lab server, not production: they post messages, and
`probe_a1_rc_followup.py` removes and re-adds a user to a channel in order to
generate a system message.
