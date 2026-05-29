# Enable the inbox router

The inbox router watches a vault named `inbox`, scores each note against your
other vaults, and either moves a note automatically or files a proposal in the
maintenance cockpit for you to confirm. It is off by default.

This guide shows you how to turn it on, tune it, and watch it work.

## Turn it on

Set the enable flag and restart the server:

```bash
export MEMEX_SERVER_MEMORY_INBOX_ROUTER_ENABLED=true
memex server start
```

On the first scheduler tick the router creates the `inbox` vault if it does not
exist. Drop notes into that vault (`memex note add --vault inbox …`, the Firefox
extension, or the API) and the router takes it from there.

## Trigger a triage pass by hand

The scheduler runs a pass every hour by default. To run one now:

```bash
memex inbox triage          # live: may move notes and file proposals
memex inbox triage --dry-run # score and decide only; change nothing
```

`--dry-run` prints what the router *would* do without touching anything — use it
to preview behaviour before you trust auto-routing.

## Check status

```bash
memex inbox status
```

This shows whether the router is enabled, whether it has warmed up enough to
auto-route, and how many routing proposals are waiting in the cockpit.

## Resolve proposals

Open the cockpit and work the routing proposals:

```bash
memex lint review
```

A `inbox_vault_route` proposal offers one option per candidate vault — pick the
right one and the note moves there. A `inbox_vault_no_fit` proposal means no
vault matched; leave the note in the inbox or migrate it yourself with
`memex note migrate <note> <vault>`.

## Tune it

Set any of these (env var form shown) and restart:

| Setting | Env var | Default | Effect |
|---|---|---|---|
| Auto-route on/off | `MEMEX_SERVER_MEMORY_INBOX_ROUTER_AUTO_APPLY_ENABLED` | `true` | When `false` the router only ever proposes. |
| Confidence floor | `…_AUTO_APPLY_MIN_P_MATCH` | `0.5` | Minimum top-vault probability to auto-route. Raise to route less, more safely. |
| Margin gate | `…_T_MARGIN` | `0.4` | How far the top vault must beat the runner-up to auto-route. |
| Warm-up gate | `…_MIN_DECISIONS_BEFORE_AUTO_APPLY` | `50` | Auto-route stays off until this many confirmed routes accumulate. |
| Per-tick cap | `…_MAX_AUTO_APPLIES_PER_TICK` | `10` | Most notes the router will move in one tick. |
| Tick interval | `…_INTERVAL_SECONDS` | `3600` | Seconds between automatic passes. |

## Turn it off

```bash
unset MEMEX_SERVER_MEMORY_INBOX_ROUTER_ENABLED   # or set it to false
memex server start
```

The `inbox` vault and any notes in it stay put; the router simply stops running.

## What to expect early on

Out of the box the router proposes but does not auto-route — it waits until it
has seen `min_decisions_before_auto_apply` confirmed routes (you accepting
proposals in the cockpit). Until then, treat its suggestions as drafts and
confirm them yourself. Its rankings are sensible from the first tick; only the
hands-off auto-move waits for the warm-up.
