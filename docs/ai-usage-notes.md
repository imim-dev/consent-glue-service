# AI usage notes (raw log, used later for the README)

| Step | What I asked the AI | What it got wrong or I changed | How I verified |
|---|---|---|---|
| Design | Compared scenarios, drafted architecture and rules | Corrected scope to exactly two systems | Checked against the assignment text |
| Simulators | Drafted both FastAPI simulators and the fault-injection module | Checked behavior by hand with curl (429 with Retry-After, 422 on naive timestamps, lost response after a successful write) | Ran the failure modes and read the server logs |
