# iCloud Relay OTP Regression Evidence

## User journey

As a registration worker, I want to read an OpenAI verification code from a
tokenized iCloud relay even when its inbox API is referenced with a query
string and the message arrives before the first polling request completes.

## RED/GREEN evidence

| Guarantee | Test | RED evidence | GREEN evidence |
|---|---|---|---|
| API paths with a bounded query string are discovered without retaining the query credentials. | `tests/test_icloud_relay.py::test_discovers_inline_api_path_with_query_string` | Failed before the query-aware path pattern. | Passed in the focused relay suite. |
| A recent OTP already present in the initial snapshot remains eligible when `issued_after` is supplied. | `tests/test_icloud_relay.py::test_wait_accepts_recent_otp_already_present_in_initial_snapshot` | Timed out because the initial snapshot marked the message as seen. | Returned the mocked OTP immediately after the cutoff-aware snapshot. |
| The discovered API response is parsed as a relay inbox without exposing credentials. | `tests/test_icloud_relay.py::test_provider_loads_query_endpoint_without_revealing_credentials` | Regression test added with credential-free fixtures. | Passed in the focused relay suite. |

## Validation commands

- `python -m unittest tests.test_icloud_relay -v` — 4/4 passed.
- `python -m unittest discover -s tests -v` — 41/41 passed.
- `python -m compileall -q .` — passed.
- `npm test` in `webui/frontend` — 6/6 passed.
- `npm run build` in `webui/frontend` — passed; generated artifacts were restored because this backend fix does not change the frontend bundle.
- `npm audit --omit=dev --audit-level=high` — 0 vulnerabilities.

## Read-only relay validation

One supplied relay was checked without printing its address, key, mailbox, or
codes. The provider discovered `/api/pickup/messages`, parsed 6 messages,
recognized 6 OTP records, and the full `wait_for_otp()` path returned a
recognized result.

## Known gaps

Python coverage tooling is not installed in this environment, so a percentage
coverage report was not generated. The live check validates retrieval and
parsing only; it does not send a registration request or submit a payment.
