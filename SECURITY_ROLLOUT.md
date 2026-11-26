# Security implementation checkpoint

This checkpoint includes the initial authentication work and the protected-upload increment of the security/reliability plan.
It is not a completed production hardening or staging acceptance report.

## Implemented

- Versioned JWT issuer/audience, immutable MongoDB account IDs, explicit employee/admin
  account types, expiring persisted sessions, active-account checks, and session logout.
- Legacy email-only tokens rejected. No environment-password superadmin fallback,
  employee-to-admin password fallback, reviewer identity bypass, or login-time tenant assignment.
- Employee attendance requires an authenticated employee; arbitrary email/face identity
  lookup is no longer an anonymous attendance mechanism. Kiosk/admin attendance needs
  a separately designed scoped flow if required.
- Explicit feature dependencies on admin routes, with runtime rejection of an admin
  route lacking a declared policy and tests covering registered admin endpoints.
- Single-use, expiring employee invitations with hashed secrets. Organization, employee
  identifier, type, designation, and department come from the invitation.
- Restrictions on owner/admin promotion to system roles, tenant-bound password/binding
  resets, and auth-version invalidation on administrator password reset.
- Exact CORS origins, production secret validation, and no application-level trust of
  arbitrary X-Forwarded-For headers. Configure trusted proxies at the ASGI/proxy boundary.
- DigiLocker callbacks resolve employee/organization identity from opaque, expiring,
  single-use server-side state. PKCE verifiers remain server-side and are passed to
  token exchange; callback cancellation text is HTML-escaped. Existing OAuth flows
  initiated before this release must be restarted.
- Desk mobile registration transports the administrator-provided invitation code.
- An upload ownership registry protects newly written screenshots, face-check images,
  onboarding documents, field media, leave proofs, and expense receipts. Local private
  files require authentication in every environment; only branding logos remain public.
- Media responses replace registered private references with two-minute file-specific
  grants after live account, tenant, feature, and manager-team checks. Grants are
  invalidated by session revocation and subsequent permission changes.
- Screenshot input rejects arbitrary URLs and local paths. OCR and ZIP export read
  registered storage keys through local storage or the configured object-store SDK.
- Image byte/pixel validation and a 20 MB request-body limit include chunked requests.
- Retention deletes only registered files and leaves unresolved legacy records for review.
- A dry-run-first upload backfill command verifies reviewed owner/tenant assignments
  and SHA-256 content hashes before applying metadata.
- HTTP security regression suite and backend CI jobs for tests, dependency audit,
  and Docker image build. CI does not deploy or publish an image.

## Compatibility and deployment gates

1. Back up the database and uploads and verify a restore in an isolated environment.
   No live backup, restore, or production migration has been performed by this change.
2. Review existing administrator records, roles, organization membership, duplicate
   emails, and disabled status. Non-superadmins must not use `system_org`.
3. Configure a random JWT_SECRET of at least 32 characters, explicit ALLOWED_ORIGINS,
   and ACCESS_TOKEN_EXPIRE_MINUTES (default 60, permitted range 1–1440). The old
   example value of 43200 now fails validation. Set production APP_ENV explicitly.
4. If the only administrator previously existed in environment variables, provision a
   real database account from the backend directory:

   ```sh
   python bootstrap_admin.py --email operator@example.com --name "System Operator"
   ```

   This prompts for the password, never accepts it on the command line, never
   overwrites an existing account, and is not run automatically at startup.
5. Release the invitation-aware desk mobile client before enabling self-enrollment.
   Other clients continue to submit employee credentials to `/login`; administrators
   use `/admin/login`. An administrator who needs employee functionality requires a
   separate employee identity. Login response field names remain compatible.
6. Reauthenticate every user. Legacy tokens intentionally have no compatibility path.
   Current tokens expire after the configured lifetime; refresh-token rotation and
   automatic renewal across all clients are **not implemented in this increment**.
   Test this re-login behavior and packaged Electron origins before deployment.
7. Run regression tests, dependency audit, a production image build, and real-MongoDB
   integration checks. Do not declare staging accepted from the mocked tests alone.
8. Canary the release and monitor authentication/permission failures. Rollback must
   retain the identity separation; do not redeploy the vulnerable email-token logic.

## Invitation API

An authorized administrator with the `employees` feature calls
`POST /admin/employee-invitations` with `email`, `employee_id`, `designation`,
`department`, optional `employee_type`, and optional `expires_in_hours` (1–168).
Only a system superadmin may select another `organization_id`.

The response returns the invitation token once. Deliver it privately to the intended
employee; never log it. `/register` accepts it as `invitation_token` and binds it to
the normalized email. Expiry is checked on each use, independently of MongoDB TTL cleanup.
The first implementation consumes the invitation before inserting the employee: if
that insert fails, an administrator must issue a replacement invitation. It is never
automatically made reusable after an uncertain write.

`POST /auth/logout` revokes the supplied employee or admin session. Wiring every
client's logout action to this endpoint remains a subsequent client rollout task.

## Verification

Local verification: 60 regression tests pass under Python 3.11 using the pinned
FastAPI 0.109.2/auth test dependencies. The three changed desk registration/API
JavaScript files pass Babel JSX parsing. Full mobile builds have not been run.
Docker's local engine was unavailable; the production image and CI jobs have not
been verified locally. No staging database or backup destination was supplied.

`client-baseline.json` records declared versions and scripts for all five clients.
It is a source inventory, not a compatibility certification or performance benchmark.

```sh
python -m venv .venv-security
# Activate the environment for your platform, then:
python -m pip install -r requirements-security-tests.txt
python -m pytest tests -q
```

Tests exercise real FastAPI routes, JWT signing/validation, password hashing,
invitation issuance/consumption, and permission dependencies. Only the database,
biometric inference, and outbound Sheets integration are replaced. MongoDB uses an
in-memory adapter; production transactions, indexes, concurrent processes, ML models,
external services, and deployment availability require separate integration checks.
The test requirements pin direct dependencies; a complete production/transitive lock
and remediation of dependency audit findings remain pending.

## Remaining plan work

- Phase 0: real staging, backup/restore validation, frontend CI/build gates, supported
  client verification, performance baseline measurements, and complete dependency locks.
- Phase 1: full resource-by-resource tenant audit, shared login limiter, refresh-token
  rotation/client renewal, and complete password-change/session lifecycle integration.
- Phase 2: execute and verify the legacy-upload ownership backfill, object-store integration
  tests, desktop-agent IPC/credential storage, and mobile secure-storage/device-ID migration.
- Phase 3: transactional account-scoped queues, per-item sync acknowledgments,
  idempotency constraints/migration, safe retries, and durable agent uploads.
- Phase 4: complete report totals, honest telemetry, readiness checks, safe deployment
  switching/rollback, and a dedicated durable scheduler.
- Phase 5: bounded inference, live-view query reduction, polling controls, route splitting,
  and measured before/after performance.
- Phase 6: structured/redacted tracing, retention/alerts, client error reporting, and
  incremental domain refactoring.

The desktop-agent security findings remain unresolved. Protected-upload code is tested
locally, but production migration and object-store integration have not been executed.


## Protected-upload rollout gate

Do not deploy the private-file enforcement before reviewing and backfilling legacy
ownership. Unregistered or ambiguous files are denied rather than guessed to belong
to a user. New uploads register ownership when they are created.

Create an operator-reviewed JSON array containing, for each legacy object:
`url`, `owner_id` (MongoDB employee ID), `organization_id`, `purpose`, and `sha256`.
Valid purposes are `screenshot`, `face_check`, `onboarding`, `field_media`,
`leave_proof`, and `expense_receipt`. Obtain hashes from trusted storage; do not
trust ownership inferred solely from filenames or client-submitted references.

```sh
python backfill_upload_registry.py reviewed-manifest.json
python backfill_upload_registry.py reviewed-manifest.json --apply
```

The first command performs validation only. The second creates missing registry
entries after the entire manifest validates. Existing matching entries are skipped;
conflicting ownership or hashes stop validation. Applying entries is not a multi-object
transaction; after an infrastructure failure, rerun the reviewed manifest to resume.
No production backfill has been run at this checkpoint.

Set `PUBLIC_API_URL` to the externally reachable HTTPS API origin behind a proxy so
file links use the correct public hostname. API responses return short-lived display
links; refresh the originating API response to obtain a new link after expiry.
A normal API access token cannot be replaced by a file-download grant.

Request bodies above 20 MB now return 413. Offline batches must be bounded before
production rollout (the durable-sync phase is still pending). Screenshot images have
a 10 MB byte limit and 20-million-pixel limit; other uploaded files have a 15 MB limit.
Confirm existing clients' image/batch sizes and packaged desktop origins in staging.

## Resume here

No production deployment, backup/restore, or object-store migration was performed.
The next security work is the local desktop agent and client credential storage.
Refresh-token/client logout integration, durable sync, performance work, and the
remaining phases are still outstanding. Review this checkpoint before expanding scope.
