# GPT Auto Register

Protocol-based ChatGPT account registration with a FastAPI and Vue 3 WebUI.
The project uses `curl_cffi` browser impersonation, OpenAI Sentinel proof-of-work,
and mailbox providers to complete registration without browser automation.

> Use this software only with accounts, email addresses, phone numbers, and
> payment profiles you are authorized to operate. You are responsible for
> complying with OpenAI's terms and all applicable laws.

## Highlights

- Browserless ChatGPT registration through the authorization protocol
- Outlook IMAP XOAUTH2 and Cloudflare catch-all mailbox support
- Optional SMS verification through supported activation providers
- Concurrent registration workers and proxy-pool rotation
- FastAPI backend with a Vue 3 and Element Plus WebUI
- Account credential storage and configurable export formats
- Exact `plus-1-month-free` Plus trial eligibility checks
- Side-effect-limited GCash payment eligibility checks

## Requirements

- Python 3.10 or newer
- Node.js 18 or newer for the WebUI build and Sentinel QuickJS path
- A supported mailbox configuration
- Optional proxy, SMS provider, and export-panel credentials

## Installation

```bash
git clone https://github.com/Regert888/gpt-auto-register.git
cd gpt-auto-register
python -m pip install -r requirements.txt
```

Build the frontend after changing files under `webui/frontend`:

```bash
npm --prefix webui/frontend ci
npm --prefix webui/frontend run build
```

## Start the WebUI

```bash
python start_webui.py
```

The default address is <http://127.0.0.1:8765/>.

To listen on another interface or port:

```bash
python start_webui.py --host 0.0.0.0 --port 8765
```

Do not expose the WebUI to an untrusted network without an authentication
layer. It can display and export account credentials.

## Command-line Registration

For an Outlook mailbox record:

```bash
python register_outlook.py "email----password----client_id----refresh_token"
```

The Microsoft refresh token must include the IMAP and offline-access scopes
required by the mailbox provider.

## Eligibility Checks

Eligibility checks are available from the **Registered accounts** page. Select
one or more rows and use the dedicated action for the check you want to run.

### Plus trial promotion

The Plus check calls:

```text
GET https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27
```

An account is reported as trial-eligible only when all of the following are
true:

- the account is on the Free plan;
- it does not have an active subscription; and
- `eligible_promo_campaigns.plus.id` is exactly `plus-1-month-free`.

The structured result includes the campaign ID, title, discount percentage,
duration, current plan, decision code, timestamp, and one of these
classifications:

- `eligible` — the exact promotion is available;
- `ineligible` — a conclusive response shows it is unavailable; or
- `unknown` — the request or response was inconclusive.

The existing `POST /api/registered/check_plus` request format and legacy UI
status fields remain supported.

### GCash payment eligibility

GCash uses a separate, explicit action and endpoint:

```text
POST /api/registered/check_gcash
```

Request body:

```json
{
  "emails": ["person@example.com"],
  "proxy": "http://proxy.example:8080"
}
```

The backend accepts this payment-adjacent endpoint from loopback clients and
requires an explicit acknowledgement header:

```text
X-GCash-Probe-Confirmation: checkout-side-effects-acknowledged
```

The WebUI adds that header only after the operator accepts the confirmation
dialog. Direct API clients must make the same acknowledgement. If the WebUI is
published through a reverse proxy, keep FastAPI private on loopback and require
authentication and TLS at the proxy. If the proxy must reach FastAPI from a
non-loopback address, set `GPT_AUTO_REGISTER_ADMIN_TOKEN` on the backend and
have the trusted proxy inject the matching `X-GPT-Admin-Token` header. Never
embed that admin token in frontend JavaScript.

The probe performs only this bounded sequence:

1. Create a PHP checkout for the ChatGPT Plus monthly plan.
2. Update the same checkout with `plus-1-month-free`.
3. Calculate checkout taxes on a best-effort basis.
4. Resolve the checkout and collect amount, currency, and custom-method evidence.
5. If a custom payment method ID is discovered from the live response, ask
   Stripe Elements for its capability metadata.

The probe contains no checkout-confirm, custom-payment start, or payment
execution operation. It never submits a GCash account or authorizes a charge.
Creating and updating a checkout is still a remote side effect, so the WebUI
always requires an explicit confirmation before it runs.

By default, `eligible` requires explicit evidence that:

- the custom method is GCash;
- the checkout currency is PHP; and
- the amount due is zero.

Missing or contradictory evidence is `unknown`, not guessed as eligible. The
custom payment method ID is discovered from live responses; the application
does not embed an ID copied from another repository.

### Proxy behavior

The selected proxy is used consistently for every stage of a check. A failed
proxy request does not silently fall back to a direct connection. Proxy
credentials, access tokens, cookies, checkout session IDs, customer secrets,
and raw upstream bodies are excluded from stored eligibility results.

## Stored Results

Eligibility data is stored inside the existing `registered.extra_json` field:

```text
plus_check
gcash_check
```

No database migration is required. If a later attempt is inconclusive, its
record retains the last conclusive verdict for reference.

The local database is `webui/webui.db`. It is ignored by Git and may contain
sensitive credentials. Back it up before upgrading, and never commit it.

## Tests

Run the backend tests:

```bash
python -m unittest discover -s tests -p "test_*.py" -v
```

Run the frontend logic tests:

```bash
npm --prefix webui/frontend test
```

Build the production frontend:

```bash
npm --prefix webui/frontend run build
```

The eligibility tests use fake transports. They do not contact ChatGPT or
Stripe and explicitly verify that no payment confirmation or start endpoint is
called.

## Project Layout

| Path | Purpose |
| --- | --- |
| `auth_flow.py` | ChatGPT authorization and registration protocol |
| `eligibility.py` | Plus promotion parsing and safe result contracts |
| `gcash_probe.py` | Side-effect-limited GCash capability probe |
| `http_client.py` | TLS impersonation and proxy-aware HTTP sessions |
| `mail_providers/` | Mailbox provider implementations |
| `sms_provider.py` | Optional SMS activation providers |
| `webui/app.py` | FastAPI routes |
| `webui/db.py` | SQLite persistence |
| `webui/frontend/` | Vue 3 frontend source |
| `tests/` | Offline unit and integration tests |

## Security Notes

- Keep access tokens, refresh tokens, cookies, mailbox credentials, proxy
  credentials, SMS API keys, and export-panel keys out of Git.
- Keep the WebUI bound to localhost unless a trusted reverse proxy provides
  authentication and TLS.
- Treat `unknown` as inconclusive. Retrying repeatedly can trigger upstream
  rate limits.
- ChatGPT and Stripe endpoints used by the eligibility checks are not stable
  public APIs and may change without notice.
- Review `git diff` before publishing changes and run dependency audits as part
  of release preparation.

## License

This repository is licensed under the GNU Affero General Public License v3.0.
See [LICENSE](LICENSE).
