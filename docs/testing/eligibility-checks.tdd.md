# Eligibility Checks: TDD Evidence

## Source

The user journeys and acceptance criteria were derived during the gated
`orch-add-feature` workflow. The implementation is clean-room: protocol facts
were researched, but source code, tests, fixtures, comments, and opaque custom
payment method constants were not copied from the unlicensed reference
repository.

## User journeys

1. As an account operator, I want to see whether a saved Free account is
   eligible for the exact `plus-1-month-free` campaign so that I do not confuse
   another offer with the requested Plus trial.
2. As an account operator, I want to explicitly probe GCash availability so
   that I can distinguish `eligible`, `ineligible`, and inconclusive accounts.
3. As a security-conscious operator, I want the GCash probe to stop before
   checkout confirmation or payment start so that an eligibility check cannot
   execute a payment.
4. As a WebUI user, I want persisted English status labels and the last
   conclusive result so that transient network failures do not erase useful
   evidence.

## RED evidence

The initial backend suite was run before production modules or endpoints
existed:

```text
python -m unittest discover -s tests -p "test_*.py" -v
FAILED (errors=9)
ModuleNotFoundError: No module named 'eligibility'
ModuleNotFoundError: No module named 'gcash_probe'
ImportError: cannot import name 'CheckGCashReq' from 'webui.app'
AttributeError: module 'webui.db' has no attribute 'update_eligibility_check'
```

The initial frontend logic test also failed before its implementation existed:

```text
npm test
ERR_MODULE_NOT_FOUND: webui/frontend/src/eligibility.js
```

Additional boundary tests were introduced and observed RED for dynamic custom
method discovery, best-effort taxes, missing Plus plan evidence, log redaction,
and checkout-session path injection before the corresponding fixes were made.

No RED checkpoint commit was created because the governing Gate 2 requires
explicit user confirmation before any commit.

## Test specification

| # | What is guaranteed | Test target | Type | Result |
| --- | --- | --- | --- | --- |
| 1 | Only the exact `plus-1-month-free` campaign is trial-eligible | `tests/test_eligibility.py` | Unit | PASS |
| 2 | Active, deactivated, Free/no-offer, malformed, and missing-plan states are classified explicitly | `tests/test_eligibility.py` | Unit | PASS |
| 3 | Authorization, cookie, and proxy credentials are redacted from diagnostic text | `tests/test_eligibility.py` | Security unit | PASS |
| 4 | GCash requires explicit method, PHP currency, and zero-due evidence | `tests/test_gcash_probe.py` | Unit | PASS |
| 5 | Missing, conflicting, nonzero, and wrong-currency evidence cannot become eligible | `tests/test_gcash_probe.py` | Unit | PASS |
| 6 | A live custom method ID is discovered and then verified; no copied ID is embedded | `tests/test_gcash_probe.py` | Transport contract | PASS |
| 7 | The probe calls checkout/update/taxes/resolve/Stripe only and never confirm/start | `tests/test_gcash_probe.py` | Security integration | PASS |
| 8 | Upstream session IDs cannot inject a different path | `tests/test_gcash_probe.py` | Security unit | PASS |
| 9 | API input is deduplicated and bounded, missing tokens are safe, and one failure does not abort the batch | `tests/test_eligibility_api.py` | API integration | PASS |
| 10 | Safe results persist without a schema migration and preserve the last conclusive verdict | `tests/test_eligibility_db.py` | Database integration | PASS |
| 11 | English GCash summaries, details, and status colors remain stable | `webui/frontend/tests/eligibility.test.mjs` | Frontend unit | PASS |
| 12 | Plus requests cannot follow redirects while carrying authorization and cannot silently select another account | `tests/test_eligibility_api.py`, `tests/test_eligibility.py` | Security regression | PASS |
| 13 | GCash requires backend side-effect acknowledgement and either loopback access or a matching reverse-proxy admin token | `tests/test_eligibility_api.py` | API security integration | PASS |
| 14 | Concurrent Plus and GCash persistence cannot overwrite the other result | `tests/test_eligibility_db.py` | Concurrency integration | PASS |
| 15 | A failed promotion update is inconclusive and stops before later checkout stages | `tests/test_gcash_probe.py` | Transport contract | PASS |
| 16 | Element Plus, project-owned UI text, and shipped static assets are English-only | `webui/frontend/tests/english-ui.test.mjs` | Frontend/build regression | PASS |

## GREEN evidence

Backend suite:

```text
python -m unittest discover -s tests -p "test_*.py" -v
Ran 37 tests
OK
```

Frontend suite:

```text
npm --prefix webui/frontend test
tests 6, pass 6, fail 0
```

Production frontend:

```text
npm --prefix webui/frontend run build
1702 modules transformed
built successfully
```

Additional validation:

```text
python -m compileall -q eligibility.py gcash_probe.py webui tests
compileall: OK

python -m pip check
No broken requirements found.

npm --prefix webui/frontend audit --omit=dev --package-lock-only --ignore-scripts
found 0 vulnerabilities
```

## Coverage

The environment did not include the third-party `coverage.py` package. Python's
standard-library `trace` module was used instead, with no new project
dependency:

```text
eligibility.py  93.7%
gcash_probe.py  88.3%
```

Both new backend modules exceed the required 80% line coverage.

## Known gaps

- Tests use dependency-injected fake transports and deliberately do not contact
  live ChatGPT or Stripe endpoints. A live result requires authorized account
  credentials and a suitable regional proxy.
- The upstream endpoints are not stable public APIs; unexpected response shapes
  intentionally produce `unknown` rather than optimistic eligibility.
- The existing application has no built-in WebUI authentication or global rate
  limiter. It should remain bound to localhost or be placed behind an
  authenticated TLS reverse proxy.
- The original lockfile references a third-party npm mirror that is blocked by
  the local runtime policy. Validation installed the same manifest versions from
  the official npm registry without replacing the project lockfile wholesale.

## Merge evidence

RED and GREEN were both captured in this report. The change remains uncommitted
until the user approves Gate 2.
