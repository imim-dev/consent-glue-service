# AI usage notes (raw log, used later for the README)

| Step | What I asked the AI | What it got wrong or I changed | How I verified |
|---|---|---|---|
| Design | Compared scenarios, drafted architecture and rules | Corrected scope to exactly two systems | Checked against the assignment text |
| Simulators | Drafted both FastAPI simulators and the fault-injection module | Checked behavior by hand with curl (429 with Retry-After, 422 on naive timestamps, lost response after a successful write) | Ran the failure modes and read the server logs |
| Policy | Asked for a pure decide() with canonical state and rule ids | Chose the tie-breaker (withdrawn) and the re-consent rule myself, after discussing the GDPR reasoning | 11 unit tests, including a convergence test (no echo loop) |
| Clients | Asked for HTTP clients with error classification and a pure backoff function | Decided myself that retries are scheduled in the database, not slept, and that error messages must not contain the email (it is in the URL) | 13 tests with a mocked HTTP transport, including a no-PII check |
