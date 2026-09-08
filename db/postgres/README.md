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

From `infra/`, replay the whole directory. Order matters, and `LC_ALL=C` keeps the shell's sort
identical to the one initdb uses:

```sh
for f in $(LC_ALL=C ls ../db/postgres/*.sql); do
  echo "-- $f"
  docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U tally -d tally < "$f"
done
```

This is non-destructive: it creates what is missing and leaves what exists alone. To apply a
single file instead:

```sh
docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U tally -d tally \
  < ../db/postgres/0009_tenant_bq_export_config.sql
```

Then confirm what landed:

```sh
docker compose exec -T postgres psql -U tally -d tally \
  -c "\dt"
```

## Known sequence irregularities

Two irregularities are permanent facts about this repo's history. Neither is a bug to be fixed by
renumbering, and both are recorded here so nobody "corrects" them later.

### `0005` is used twice

`0005_cac_periods.sql` (CTO-107) and `0005_tenant_eval_config.sql` (CTO-114) both carry the
number 0005. They are unrelated and neither depends on the other; both only need `tenants` from
`0001`.

They are **not** renumbered. Nothing tracks applied migrations by filename, so a rename would not
be reconciled by any tooling. It would only mean that an operator who applied
`0005_tenant_eval_config.sql` now sees, in the repo, a filename they have never run and no
filename matching what they did run, with nothing to tell them the two are the same migration.
The filename is the only identity a migration has here, so it is the one thing worth keeping
stable. The ambiguity that actually mattered, initdb's ordering, is resolved in
`infra/docker-compose.yml`: the eval-config file is mounted as `0005a_tenant_eval_config.sql`, so
the two always run in a fixed order.

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
4. If a running stack needs it, apply it by hand as above. A fresh `make up` on an existing volume
   will not do it.
