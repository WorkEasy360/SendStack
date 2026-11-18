# Sign-in with WorkEasy360 Accounts

SendStack can sign people in with **WorkEasy360 Accounts**, the identity provider of the WorkEasy360 product family. SendStack never sees a WorkEasy360 password. It receives an ID token naming:

- the person (`sub`);
- the WorkEasy360 organization they chose (`organization_id`);
- this product (`product_code`, `workeasy_sendstack` by default).

SendStack then keeps **its own server-side session**, puts the person in the one workspace their organization is linked to, asks WorkEasy360 whether the organization is still entitled, and ends the session the moment WorkEasy360 says so (back-channel logout).

It is **off** until `WORKEASY360_ISSUER` (and the rest of the registration below) is set. Password sign-in keeps working unless `WORKEASY360_SSO_ONLY=true`.

## How it is built

The OpenID Connect protocol is the **`workeasy360-sso` Django connector** (WorkEasy360's private package), pinned by commit. It does:

- the authorization code flow with PKCE S256;
- state and nonce, stored server-side and used once;
- `private_key_jwt` client authentication. The assertion has `iss = sub = client id`, `aud` set to **the token endpoint** (not the issuer), a 60 s lifetime and a fresh `jti`;
- offline ID-token verification:
  - signature by `kid`, with the algorithm taken from the key set (RS256/ES256 only);
  - `iss` exactly the configured issuer;
  - `aud`/`azp` this client;
  - `exp`/`nbf`/`iat` within ±30 s;
  - `typ=id_token`;
  - `nonce`;
  - `product_code`;
  - `organization_id` present;
- `(issuer, sub)` identity links;
- entitlement re-checks;
- the back-channel logout receiver.

SendStack adds its own rules through the connector's adapters (`backend/apps/workeasy360/`):

| What | Where |
|---|---|
| `email_verified` must be true. The connector checks it only when linking by address, which SendStack never does. | `adapters.py` `SendStackUserAdapter` |
| Never merge on an email address: a new WorkEasy360 person whose address already has an account is refused (`account_conflict`) | `adapters.py` |
| Organization → workspace (`Tenant`). The first sign-in creates the workspace; an operator can link an existing one instead. | `adapters.py` `SendStackOrganizationAdapter`, `models.py` `OrganizationLink` |
| Entitlement checked when the person arrives, before anything is created | `adapters.py` `_require_entitlement` |
| The API accepts a WorkEasy360 session, re-checked on every request | `authentication.py` `WorkEasy360SessionAuthentication` |
| Password sessions: SSO-only, WorkEasy360-owned accounts, and linked workspaces | `apps/accounts/authentication.py` |
| Resellers and impersonation never cross into a WorkEasy360 organization | `apps/accounts/models.py` `can_manage`, `apps/accounts/views.py`, `apps/credits/services.py`, `apps/tenants/services.py` |
| The sign-in page, sign-out, the CSRF token | `views.py` (`session/`), `frontend/src/services/workeasy360.js`, `frontend/src/pages/auth/Login.jsx` |

### No tokens in localStorage

A WorkEasy360 sign-in does **not** mint SimpleJWT tokens. The callback opens a Django **database session** in the `sessionid` cookie:

- httpOnly;
- host-only (no `Domain`, never `.workeasy360.com`);
- `SameSite=Lax`;
- `Secure` in production.

The single-page app keeps nothing about it in `localStorage`. It asks `GET /api/v1/auth/workeasy360/session/` whether it has a session, and receives the CSRF token there. That token is held in memory only, because the CSRF cookie is httpOnly in production. It is sent as `X-CSRFToken` on writes.

Password sign-in is unchanged: SimpleJWT tokens, as before.

### Every request

`WorkEasy360SessionAuthentication` (in `REST_FRAMEWORK["DEFAULT_AUTHENTICATION_CLASSES"]`, after the JWT class) accepts a request only when all of these hold:

1. The Django session exists. Sessions live in PostgreSQL, so every instance sees the same rows.
2. Its WorkEasy360 sign-in record (`ProductSession`) is open, belongs to this session key and this person, and is under the configured issuer.
3. The person is an ordinary user (`USER`) of the workspace linked to the session's organization, and that workspace is active.
4. CSRF is valid, for unsafe methods.
5. WorkEasy360's entitlement answer allows it. The answer is cached (below); `write_allowed` is required for writes.

A Django session that WorkEasy360 did not open (the Django admin's) is not an API credential, as before.

## Registration in Super Admin

The platform catalog has **no `workeasy_sendstack` product yet**. Create it first (Products → Add product), or set `WORKEASY360_PRODUCT_CODE` to whatever code it gets. Then, under Product → Integration (host `sendstack.workeasy360.com`):

| Field | Value |
|---|---|
| Launch URL | `https://sendstack.workeasy360.com/api/v1/auth/workeasy360/start/` (the launcher appends `?organization=<uuid>`) |
| Redirect URI | `https://sendstack.workeasy360.com/api/v1/auth/workeasy360/callback/`, exactly, **with** the trailing slash |
| Back-channel logout URI | `https://sendstack.workeasy360.com/api/v1/auth/workeasy360/backchannel-logout/` |
| Post-logout redirect URI | `https://sendstack.workeasy360.com/login` |
| Scopes | `openid email profile organization`. `email` is required: `email_verified` comes with it. `organization` names the workspace a first sign-in creates. |
| Token endpoint auth method | `private_key_jwt` |
| Sign-in mode | `sso_optional`, with login URL `https://sendstack.workeasy360.com/login`, while password sign-in is on. Change it to `sso_only` once `WORKEASY360_SSO_ONLY=true`. |
| Credential | The **public** key below; its credential id is `WORKEASY360_KEY_ID` |

**Domain proof (ADR-070).** Publish the `_w360-product-verify.sendstack.workeasy360.com` TXT record that Super Admin shows. The integration cannot be activated until it is proven.

**Keys.** Generate the pair on the product's machine. The private key goes into the secret manager and is mounted into the backend container as a file:

```bash
python manage.py w360sso_keygen --out /secure/workeasy360-sendstack.pem   # prints the public key to paste into Super Admin
# or: openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out workeasy360-sendstack.pem
#     openssl pkey -in workeasy360-sendstack.pem -pubout
```

Then:

- `python manage.py w360sso_manifest --display-name SendStack` prints the exact URIs this deployment serves.
- `python manage.py w360sso_check` proves discovery, the key set and **this product's credential** against the live provider.

## Installing the connector

`backend/requirements/workeasy360.txt` pins `workeasy360-sso` to commit `a98552f8f37e1e4f1fab1de99cc5b915cb9ed5c1` (v0.3.1) of the **private** repository `WorkEasy360/workeasy360-sso`. This repository is public, so nothing of the connector is vendored here.

```bash
pip install -r requirements/production.txt
pip install --no-deps -r requirements/workeasy360.txt     # needs read access to the private repository
```

**Why `--no-deps`.** The connector's 0.3.1 metadata declares `Django>=4.2,<6.1`, and SendStack runs Django 6.1. The connector's whole suite (183 tests) passes on Django 6.1 and 6.1.3, with deprecation warnings as errors and its migrations complete. So it is compatible, but pip refuses the combination on metadata alone. Its dependencies are pinned in `requirements/base.txt` instead:

- `PyJWT[crypto]==2.15.1`;
- `httpx==0.28.1`.

Drop `--no-deps` once a connector release declares 6.1.

**Access.**

- **Docker:** `backend/Dockerfile` installs it in a BuildKit step that forwards a read-only **deploy key** from your ssh-agent (`--mount=type=ssh`). The key is never stored in a layer, and GitHub's host key is pinned. `docker-compose.yml` forwards the agent (`build.ssh: [default]`). The legacy builder (`DOCKER_BUILDKIT=0`, the README's OneDrive workaround) cannot do this: build from outside OneDrive instead.
- **Development:** your own GitHub SSH access works. With https and the `gh` credential helper, rewrite the URL: `git config --global url."https://github.com/".insteadOf ssh://git@github.com/`.

Without access, the backend does not install: the connector is a hard dependency once this change is merged.

## Environment

The connector reads every `WORKEASY360_*` variable itself. `WORKEASY360_SSO` in `config/settings/base.py` fixes only what SendStack decides: the adapters, the single-page app's routes, no automatic start, and never linking by email.

| Variable | Default | Meaning |
|---|---|---|
| `WORKEASY360_ISSUER` | (empty: off) | Exactly the issuer WorkEasy360's integration kit names, no trailing slash. A product trusts one issuer: today the live provider's current name, or `https://accounts.workeasy360.com` once that is an issuer (platform ADR-067, stage b). https, or http only for a loopback issuer with `WORKEASY360_ALLOW_INSECURE_HTTP=true` outside production. |
| `WORKEASY360_CLIENT_ID` | | From the registration. |
| `WORKEASY360_PRODUCT_CODE` | `workeasy_sendstack` | Must equal the ID token's `product_code`. |
| `WORKEASY360_REDIRECT_URI` | | `https://<host>/api/v1/auth/workeasy360/callback/`, exactly as registered. System check `workeasy360_sso.E005` compares its path with the route. |
| `WORKEASY360_POST_LOGOUT_REDIRECT_URI` | | `https://<host>/login`. Enables "Sign out of WorkEasy360 too". |
| `WORKEASY360_KEY_ID` | | The credential id (`kid` of the client assertion). |
| `WORKEASY360_PRIVATE_KEY_FILE` | | PEM, EC P-256 or RSA. Never in the repository or an image. `WORKEASY360_PRIVATE_KEY` (literal `\n` allowed) works too. |
| `WORKEASY360_SCOPES` | `openid email profile organization` | Each must be registered. |
| `WORKEASY360_SSO_ONLY` | `false` | `true`: WorkEasy360 is the only way in ([below](#password-sign-in-and-sso-only)). An SSO-only production process refuses to start while misconfigured. |
| `WORKEASY360_PROVISION_TENANTS` | `true` | `false`: only workspaces an operator linked can be signed in to (`no_workspace` otherwise). |
| `WORKEASY360_PURCHASE_URL` | | Where an organization that is not entitled can get SendStack. |
| `WORKEASY360_ALLOW_INSECURE_HTTP` | `false` | Development only: http on loopback hosts. |

## What happens, step by step

| Step | What SendStack does |
|---|---|
| Start | The sign-in page's **Continue with WorkEasy360** opens `GET /api/v1/auth/workeasy360/start/?next=/`. The connector stores state, nonce and the PKCE verifier server-side, single use, for 10 minutes. It sets a random binding cookie and redirects to WorkEasy360, forwarding the launcher's `organization` when there is one. The sign-in page never starts this by itself. |
| Callback | `GET /api/v1/auth/workeasy360/callback/` takes the flow first and compares the state. It exchanges the code with PKCE and `private_key_jwt`, then verifies the ID token. |
| Person | `SendStackUserAdapter` requires `email_verified`. The person is linked on `(issuer, sub)`. A new person becomes a new `USER` account with an unusable password; an address that already has an account is `account_conflict`. |
| Entitlement | `SendStackOrganizationAdapter` asks WorkEasy360's entitlement endpoint for the organization and the person. A denial is `not_entitled`, and nothing is created. A refusal of this product's client is `access_unverified`. An outage is not a denial. |
| Workspace | The organization's linked workspace. Without one, the first sign-in creates a **customer workspace directly under the platform tenant**, never under a reseller. It is named after `organization_name`, starts the trial plan if trials are on, and is audited. A person whose account already belongs to another workspace is refused (`other_organization`). Joining an existing workspace respects the plan's `users` limit (`seat_limit`). |
| Session | Any previous session in the browser ends. A new database session is opened and recorded for back-channel logout, then the browser is sent to `next`. |

Every failure lands on `/login?sso_error=<code>`. The sign-in page shows one sentence per code: the connector's vocabulary, plus SendStack's codes in `apps/workeasy360/messages.py`. Tokens, codes, state and nonce are never logged.

## Organizations, resellers and impersonation

- **One organization, one workspace.** `OrganizationLink` is unique both ways, and a link is never re-pointed. The connector links a person once per issuer, and a SendStack account belongs to one workspace. So a person who is in two WorkEasy360 organizations can use SendStack in the first one only; the second is refused (`other_organization`).
- **A WorkEasy360 session reaches only its workspace.** Every tenant-scoped endpoint already filters by `request.user.tenant`. On top of that, the session is refused if the account's workspace is no longer the organization's.
- **No reseller above a WorkEasy360 organization.**
  - Workspaces created by sign-in hang under the platform tenant.
  - `w360_link_organization` refuses a reseller's customer workspace unless `--detach-from-reseller` moves it under the platform tenant first.
  - Defence in depth, even if such data existed: a reseller does not list, manage, delete, credit or impersonate anyone in a linked workspace (`can_manage`, `ManagedUserViewSet`, `visible_accounts`, `customer_summaries`).
- **Reseller and superadmin accounts cannot be used through WorkEasy360** (`role_not_allowed`): their powers span workspaces.
- **Impersonation never mints a WorkEasy360 identity.**
  - Nobody, superadmin included, can impersonate a WorkEasy360 person or anyone in a linked workspace: 403 `workeasy360_account`.
  - A WorkEasy360 session cannot start an impersonation.
  - A SimpleJWT token for an account that belongs to a WorkEasy360 person is refused on every request, however it was minted (`workeasy360_account`). That includes one minted before the account was linked.
  - An impersonation that began before the workspace was linked ends at the link.
  - Support for such a workspace goes through WorkEasy360.

## Entitlement, on WorkEasy360's schedule

The connector's `entitlements.check`, called by the authentication classes:

- **Cache.** An `allow` is trusted for `min(revalidate_after, 900)` s, and at least 30 s. It is kept in the Django cache, which is Redis in every deployment (shared by all instances), and in each process's own copy. WorkEasy360 is never asked per request; one request asks while the others use the last answer.
- **Denial.** Re-asked after 60 s. It never lapses into access, through an outage or a restart. Requests get 403 `workeasy360_access_ended` with `reason`. `GET /api/v1/auth/me/` stays reachable and reports `workeasy360.access`, so the app shows "access has ended" with a sign-out button.
- **Read-only.** While `write_allowed` is false, writes get 403 `workeasy360_read_only`.
- **Outage.** A 5xx, 429, timeout or network error is never a denial: the last answer stands, and with none yet, access continues.
- **Refusal of this product's client.** A 400/401/403 (revoked credential, suspended integration) fails **closed** for the whole client for 60 s: 403 `workeasy360_access_unverified`.
- **Password sessions.** In a workspace linked to an organization, they answer to that organization's entitlement (asked without `subject`). Unlinked workspaces are untouched.

## Back-channel logout

`POST /api/v1/auth/workeasy360/backchannel-logout/`, form field `logout_token`. This is the connector's receiver, per OpenID Connect Back-Channel Logout 1.0 §2.6. It checks:

- JOSE `typ` is `logout+jwt`;
- the signature by `kid`;
- `iss` exactly, and `aud`;
- `iat` and `exp`, with the token at most 10 minutes old;
- the back-channel-logout event is present;
- there is **no `nonce`**;
- `sub` and/or `sid` are present, and so is a `jti`.

Then, in one transaction:

1. The `jti` is recorded once.
2. With `sid`, the sessions that sign-in opened end. With `sub` alone, every SendStack session of that person ends.
3. The Django session rows are **deleted** and the `ProductSession` rows closed.

The receiver answers:

- **200** when the token is verified;
- **400** for a token that does not verify, **or one whose `jti` was already accepted**. The platform mints a fresh `jti` for every delivery attempt, so a repeated `jti` is a replay. It ends nothing.
- **503** when the key set or the database could not be reached, so that WorkEasy360 retries.

Because sessions live in PostgreSQL and are checked on every request, the next request with that cookie is refused (401) by **every** instance at once. `apps/workeasy360/tests_instances.py` proves it with a second process.

A sign-in still in flight for a WorkEasy360 session that has just ended is refused (`session_ended`).

## Signing out

- **Sign out** (sidebar) is a form POST to `/api/v1/auth/workeasy360/logout/`. It ends the SendStack session only. WorkEasy360 and the other products stay signed in.
- **Sign out of WorkEasy360 too** (shown for a WorkEasy360 session) is a form POST to `/api/v1/auth/workeasy360/logout/workeasy360/`. It ends the SendStack session, then sends the browser to WorkEasy360's `end_session_endpoint` with `client_id` and the registered post-logout URI. WorkEasy360 asks the person to confirm.

## Password sign-in and SSO-only

**`WORKEASY360_SSO_ONLY=false` (default):** password sign-in, signup, password reset and SimpleJWT work as before. Accounts that belong to a WorkEasy360 person have no password and get no reset email.

**`WORKEASY360_SSO_ONLY=true`:**

- `/api/v1/auth/token/` (and the legacy `/api/auth/token/`) answers 403 `password_sign_in_disabled`;
- refresh answers 401;
- self-service signup reports itself disabled;
- password reset sends nothing;
- every existing SimpleJWT token is refused at once (401 `workeasy360_sign_in_required`), including reseller and superadmin tokens;
- the sign-in page shows only **Continue with WorkEasy360**.

The Django admin keeps its own sign-in.

Switch it on only after every customer's people can sign in with WorkEasy360: link their workspaces and accounts first.

**Existing accounts.** People are keyed on `(issuer, sub)` and never merged by email. For an existing customer, before their organization's first WorkEasy360 sign-in:

1. `w360_link_organization <workspace id> <organization uuid>`: the organization's sign-ins land in that workspace.
2. `w360_link_user <user id> <sub>`, per person who should keep their account. The account keeps its data and credits; its password is retired and its SimpleJWT sessions end.

**Issuer move (accounts.workeasy360.com).** Run the connector's `w360sso_move_issuer --from <old> --apply` in the same release that changes `WORKEASY360_ISSUER`. It moves the `(issuer, sub)` links and open sessions. The workspace links are by organization id, which does not change.

## Operator commands

Run them in the backend container (or `python manage.py ...` in `backend/`).

| Command | What it does |
|---|---|
| `w360_link_organization <tenant id> <organization uuid> --actor <who> [--detach-from-reseller]` | The organization signs in to this existing workspace. It refuses: the platform's or a reseller's tenant; a suspended workspace; one holding reseller or platform accounts; a reseller's customer (unless detached); and anything already linked. Audited. |
| `w360_link_user <user id> <sub> --actor <who> [--issuer <iss>]` | You vouch that an existing account is this WorkEasy360 person. The account must be in a linked workspace. Refused for reseller and platform accounts, and when either side is already linked. The password is retired and SimpleJWT sessions end. Audited. |
| `w360_organizations` | Linked organizations, their workspaces, people and live sessions; people per issuer. |
| `w360sso_keygen`, `w360sso_manifest`, `w360sso_check`, `w360sso_cleanup`, `w360sso_move_issuer` | The connector's own (see its README). `w360sso_cleanup` purges expired flows, spent logout `jti`s and old ended sessions; run it from cron (hourly is safe). |

## Run it locally against WorkEasy360 on `http://127.0.0.1:8031`

1. Run a WorkEasy360 platform locally with issuer `http://127.0.0.1:8031`.
2. Create the product `workeasy_sendstack` and register the integration:
   - redirect URI `http://localhost:5173/api/v1/auth/workeasy360/callback/`;
   - back-channel URI `http://localhost:5173/api/v1/auth/workeasy360/backchannel-logout/`;
   - post-logout URI `http://localhost:5173/login`;
   - scopes `openid email profile organization`, `private_key_jwt`.
3. Add the public key, activate, and grant the product to a test organization. Loopback hosts need no DNS proof, and http redirect URIs are accepted outside production.
4. In `backend/.env` (or the environment of the backend container):

   ```dotenv
   WORKEASY360_ISSUER=http://127.0.0.1:8031
   WORKEASY360_ALLOW_INSECURE_HTTP=true
   WORKEASY360_CLIENT_ID=<client id>
   WORKEASY360_KEY_ID=<credential id>
   WORKEASY360_PRIVATE_KEY_FILE=/path/to/workeasy360-sendstack.pem
   WORKEASY360_REDIRECT_URI=http://localhost:5173/api/v1/auth/workeasy360/callback/
   WORKEASY360_POST_LOGOUT_REDIRECT_URI=http://localhost:5173/login
   ```

   With the containerised backend, `127.0.0.1` is the container itself. Use `http://host.docker.internal:8031` only if the platform answers under that issuer name; the issuer must match exactly.
5. Start SendStack: `docker compose up -d --build` (backend behind nginx on :80), then `npm run dev` in `frontend/`. Vite on :5173 proxies `/api` to nginx.
6. Open `http://localhost:5173/login` and choose **Continue with WorkEasy360**. Or open `http://localhost:5173/api/v1/auth/workeasy360/start/?organization=<organization uuid>`, as the launcher would.
7. `python manage.py w360sso_check` proves the credential against the platform.

The platform must reach `http://localhost:5173/api/v1/auth/workeasy360/backchannel-logout/` from where it runs.

### Proving 200 vs 401 from a script

The protected endpoint is **`GET /api/v1/auth/me/`**. The credential is the **`sessionid`** cookie set by the callback; it is httpOnly, so copy it from the browser's developer tools.

```bash
curl -s -o /dev/null -w '%{http_code}\n' -H 'Cookie: sessionid=<value>' http://localhost:5173/api/v1/auth/me/   # 200
# sign out everywhere in WorkEasy360 (it delivers a logout token), then the same call:
curl -s -o /dev/null -w '%{http_code}\n' -H 'Cookie: sessionid=<value>' http://localhost:5173/api/v1/auth/me/   # 401
```

In production: `https://sendstack.workeasy360.com/api/v1/auth/me/`, same cookie name.

## Tests

`backend/apps/workeasy360/tests.py` and `tests_instances.py` run against `fake_provider.py`. The fake provider is a WorkEasy360 stand-in serving real HTTP on 127.0.0.1, with keys generated at start. It checks `private_key_jwt` (`aud` = token endpoint, 60 s, `jti` once) and PKCE S256 as the platform does. They cover:

- the happy path;
- a wrong issuer, audience, signature, nonce or `typ`;
- a spent or foreign flow;
- a wrong product;
- a missing organization;
- an unverified email;
- a launcher organization mismatch;
- an organization that is not entitled, a WorkEasy360 refusal, and a refusal of the client;
- `account_conflict`;
- provisioning off;
- `role_not_allowed`;
- cross-organization sign-in and access;
- operator links;
- resellers and impersonation;
- the entitlement cadence, denial, read-only, outage and fail-closed;
- password sessions in linked workspaces;
- logout tokens:
  - valid (by `sid`; by `sub`);
  - eleven forged or malformed kinds;
  - a replayed `jti`;
  - an in-flight sign-in after logout;
- both sign-outs and CSRF;
- the admin session;
- SSO-only;
- two instances (a second process sharing the database).

```bash
python manage.py test apps.workeasy360
```

## Not covered yet, and before go-live

- **No catalog product, integration or key yet**, and `sendstack.workeasy360.com` has no DNS record or TXT proof.
- **The connector's metadata says Django<6.1.** SendStack installs it with `--no-deps`. The connector's CI should add Django 6.1 and widen the pin.
- **Private dependency in a public repository.** Building needs the deploy key, and SendStack has no CI to run these tests.
- **Commercial overlap is not reconciled.** SendStack's own plans, trial, credits, reseller tiers and billing still apply inside a workspace. The WorkEasy360 entitlement is the gate on top of them. Mapping a WorkEasy360 subscription to a SendStack plan (and reporting WhatsApp/SMS usage to the platform meters) is a product decision not made here. A workspace created by WorkEasy360 sign-in starts on the trial plan if trials are on.
- **SendStack's own Terms acceptance** is not recorded for workspaces created by WorkEasy360 sign-in.
- **Not built:** automatic start for SSO-only direct visits (integration guide §6b), and "confirm it's you" (`prompt=login`).
- **A person in several WorkEasy360 organizations** can use SendStack in one of them only.
- **An already-open live-update stream** (the inbox's SSE stream, `apps/realtime`) is not cut by a back-channel logout. It closes within its 10-minute lifetime and cannot be renewed, because a new ticket needs a live session. Password sessions behave the same.
- **SendStack's own audit still says it is not sellable** (`SELL_READINESS_REPORT.md`). This change does not alter that.
