# Control-plane migrations

Numbered SQL files, `0001_` upward, applied to the control-plane Postgres.

## How they are applied

There is no migration runner and **no table that records which migrations have run**.
A migration reaches a database exactly two ways:

1. **On a first boot against an empty volume.** Each file is mounted into the Postgres
   container's `/docker-entrypoint-initdb.d/` by `infra/docker-compose.yml`, and the official
   image runs `*.sql` there in alphabetical order of the *mounted* filename. A file with no
   mount never runs, so **adding a migration is not done until its compose mount is added too**.
2. **By hand, against a stack that is already running.** `docker-entrypoint-initdb.d` only fires
   when the data directory is empty, so an existing stack never picks up a new migration on its
   own, no matter how many times it is restarted.

Every file in this directory is idempotent (`CREATE TABLE IF NOT EXISTS`, `ADD COLUMN IF NOT
EXISTS`, `CREATE INDEX IF NOT EXISTS`), so re-running one is a no-op. That is what makes the
by-hand path below safe to run over a populated database.

### Applying migrations to a running stack

From `infra/`:

```sh
make pg-migrate
```

That replays the whole directory in `LC_ALL=C` order, under the same `POSTGRES_USER` /
`POSTGRES_DB` the stack was started with, and **stops on the first failure** with the failing
filename. It is non-destructive: it creates what is missing and leaves what exists alone.

A stop leaves the schema partially applied. Fix the file it named and re-run `make pg-migrate`;
replaying from the top is a no-op for everything that already landed.

To apply a single file instead (`make psql` uses the same user and database):

```sh
docker compose exec -T postgres psql -v ON_ERROR_STOP=1 \
  -U "${POSTGRES_USER:-tally}" -d "${POSTGRES_DB:-tally}" \
  < ../db/postgres/0009_tenant_bq_export_config.sql
```

Then confirm what landed:

```sh
make psql   # then \dt
```

## Known sequence irregularities

Two irregularities are permanent facts about this repo's history, recorded here so nobody
"corrects" them later.

### `0005` is used twice

`0005_cac_periods.sql` (CTO-107) and `0005_tenant_eval_config.sql` (CTO-114) both carry the
number 0005. They are unrelated and neither depends on the other; both only need `tenants` from
`0001`.

These two are **not** renumbered, and the reason is when they were caught, not a rule against
renumbering. This repo does renumber: `e0cc998` moved the idempotency migration to `0032` to clear
a collision, `0023_tenant_revenue_uploads` became `0025_`, and `0013/0014_athena` became
`0020/0021_`. Every one of those was renamed **before it merged**, while the file had never been
applied to any database. Renaming an unapplied file costs nothing.

Both 0005 files are long deployed to real installs under these names. Nothing here tracks applied
migrations, so renaming one now would not be reconciled by any tooling: an operator who ran
`0005_tenant_eval_config.sql` would see a filename in the repo they have never run, no filename
matching what they did run, and nothing to tell them the two are the same migration. That is the
distinguishing fact. A duplicate number caught before merge should still be renumbered.

Ordering is handled by giving each file a distinct `/docker-entrypoint-initdb.d` target in
`infra/docker-compose.yml`, which is what makes initdb deterministic. The eval-config file is
mounted as `0005a_tenant_eval_config.sql`, and that suffix does **not** order it after
`0005_cac_periods.sql`. See below.

#### The `0005a_` suffix does not mean what it looks like

`postgres:16` runs with `LANG=en_US.utf8`, and its entrypoint iterates a bash glob over
`/docker-entrypoint-initdb.d/*`. Under glibc collation the underscore is ignored, so the actual
initdb order is:

```
0005a_tenant_eval_config.sql     <- first
0005_cac_periods.sql             <- second
```

A `LC_ALL=C` sort (what `make pg-migrate` uses) orders them the other way around. The two orders
are exactly opposite, so the by-hand replay does not reproduce initdb's order.

Nothing is broken by this: the two 0005 tables are independent, so either order works. But do
**not** rely on an `a` suffix to sequence a **dependent** pair. Such a pair would run backwards on
a fresh boot while passing an `LC_ALL=C` by-hand replay, which means it would look green in
testing and fail on a customer's first install. If one migration depends on another, give it a
higher number.

### `0017` does not exist

There is no `0017_*.sql` and there never was one on any branch. It is a skipped number, not a
withdrawn migration: no table, column or index was ever assigned to it, so no database anywhere
is missing anything on account of the gap. `docs/specs/initiative-1-orgs-and-access.md` records
the same observation from when 0029 was allocated.

`0017` is **not** to be backfilled with an unrelated migration. Reusing it would make the number
mean two different things depending on when a deployment was provisioned, which is the one
failure mode the numbering exists to prevent.

## Adding a migration

1. Take the next free number after the highest file present. Do not reuse `0017` and do not add a
   second file at an existing number.
2. Make it idempotent, so the by-hand path above stays replayable.
3. Add the `infra/docker-compose.yml` mount in the same commit.
4. If a running stack needs it, `make pg-migrate` from `infra/`. A fresh `make up` on an existing
   volume will not do it.
