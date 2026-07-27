# PostgreSQL Security Gate

## Status after Stage 67D

- Security gate is implemented and verified.
- Production enablement is still blocked.
- Deployment rollback and final enablement remain blocked.

## Production security mode

Set `MVP_PRODUCTION_SECURITY=1` to enable the fail-fast production security policy. A secret of at least 32 characters must be supplied through `MVP_AUTH_SECRET` (preferred) or `SECRET_KEY`. The local `dev-mvp-auth-secret-change-me` fallback and obvious secrets are forbidden in this mode.

Authentication cookies always use `Path=/`, `HttpOnly`, and `SameSite=Lax`. Production security mode additionally requires `Secure`. Local development keeps the existing non-Secure cookie compatibility.

## Default credentials policy

The default development users are local/demo-only; their retained password-change behavior is documented as development compatibility. Production security mode does not seed them. An empty production users table requires `MVP_BOOTSTRAP_ADMIN_USERNAME` and `MVP_BOOTSTRAP_ADMIN_PASSWORD`, with optional `MVP_BOOTSTRAP_ADMIN_DISPLAY_NAME`, supplied from the deployment secret store. The bootstrap password must contain at least 12 characters, differ from the username, and not be a known default password. Startup rejects existing known default credentials.

## Login throttling

Failed password login attempts are tracked by normalized username and a SHA-256 client identity derived from IP address and user agent. Plaintext passwords and session secrets are never stored or logged. The defaults are five failures in a 15-minute sliding window and a 15-minute lockout. Configure them with `MVP_LOGIN_MAX_FAILED_ATTEMPTS`, `MVP_LOGIN_FAILURE_WINDOW_SECONDS`, and `MVP_LOGIN_LOCKOUT_SECONDS`.

Locked, unknown-user, and wrong-password requests receive the same generic error. A locked request is rejected before password verification, and a successful login clears failures for that username/client pair.

## Passwordless user switching

Passwordless user switching and its UI are allowed only in development/demo mode. Production security mode forbids and hides them; authentication must use the password login flow.

## Not covered yet

- Deployment rollback procedure.
- Final enablement approval.

This security gate does not enable PostgreSQL runtime. SQLite remains the default, and PostgreSQL remains guarded by `POSTGRES_RUNTIME_ENABLED=1`.
