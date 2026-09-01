# Legacy migration tooling

`scripts/legacy/` is reserved exclusively for **offline, operator-run, manual migration tooling**, including migration from historical SQLite databases into PostgreSQL.

The boundary is strict:

- the TeleRoute application runtime does not use SQLite;
- runtime application modules must not import `scripts/legacy`;
- this tooling is not part of the application backend and must not serve live requests;
- source SQLite database files must be stored outside the repository;
- generated migration reports must remain outside the repository and must not contain sensitive row samples, credentials, or other secrets;
- operators must review inputs, reports, and cleanup requirements before and after each migration.

Existing migration utilities remain in their current locations for now. Their location does not make them runtime components; moving them into this directory, if desired, requires a separate reviewed change.
