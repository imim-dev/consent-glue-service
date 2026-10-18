# AI usage notes (raw log, used later for the README)

| Step | What I asked the AI | What it got wrong or I changed | How I verified |
|---|---|---|---|
| Design | Compared scenarios, drafted architecture and rules | Corrected scope to exactly two systems | Checked against the assignment text |
| Simulators | Drafted both FastAPI simulators and the fault-injection module | Checked behavior by hand with curl (429 with Retry-After, 422 on naive timestamps, lost response after a successful write) | Ran the failure modes and read the server logs |
| Policy | Asked for a pure decide() with canonical state and rule ids | Chose the tie-breaker (withdrawn) and the re-consent rule myself, after discussing the GDPR reasoning | 11 unit tests, including a convergence test (no echo loop) |
| Clients | Asked for HTTP clients with error classification and a pure backoff function | Decided myself that retries are scheduled in the database, not slept, and that error messages must not contain the email (it is in the URL) | 13 tests with a mocked HTTP transport, including a no-PII check |
| Executor | Asked for an outbox executor with retry, dead-letter, replay and read-back verification | Decided myself that each operation is isolated and that an unexpected bug dead-letters one operation instead of stopping the batch | 10 tests with scripted fake clients, including the lost-response case |
| Engine | Asked for a sync engine with one transaction for snapshot, outbox and cursors | Found two design flaws while reviewing (duplicate key hiding repeated drift, stale pending operations) and had them fixed with tests. Chose the first-sync rule myself: no planning until both systems synced once, because "create" is an upsert and could re-subscribe someone | 10 tests with in-memory fake systems, including an end-to-end convergence test |
| CLI | Asked for an argparse CLI (run-once, status, dead-letters, replay) | Noticed that httpx logs full URLs, which contain the email address, so I lowered its log level. Chose the exit code for a halted run | Smoke tests on an empty database, then a manual run against both simulators |
