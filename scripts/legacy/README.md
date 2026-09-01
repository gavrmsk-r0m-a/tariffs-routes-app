# Legacy SQLite migration tooling

The files in `scripts/legacy/` exist only to support explicit, offline/manual
migration from an old SQLite database to PostgreSQL. They are compatibility tools
for operators performing a controlled data transition; they are not an
application backend.

## Boundary rules

- The TeleRoute application runtime uses PostgreSQL and does not use SQLite.
- Runtime application modules must not import `scripts/legacy`.
- Use this tooling only from a deliberate migration command while the source data
  is offline or otherwise protected from concurrent writes.
- Keep source SQLite database files outside the Git repository and outside an
  application release. Treat them as sensitive production data.
- Store generated migration reports outside the repository. Reports may contain
  aggregate counts and validation outcomes, but must not contain sensitive row
  samples, credentials, tokens, or connection URLs.
- Do not expose these modules as a web endpoint, background application backend,
  or fallback runtime.

The canonical application schema and migrations remain under `docs/postgres/`.
Other migration entry-point scripts currently remain in `scripts/`; this boundary
document does not relocate them or change their behavior.
