# ATS fixtures

These are **synthetic** fixtures written from each API's documented response shape (the build
sandbox could not reach the job-board APIs). They pin the parser contract.

To replace them with real recordings, run the `verify` workflow with `record: true` (or locally
`python -m jobpipe verify --company Stripe --record tests/fixtures/live`) and compare the field
names. If a live response differs, update the fetcher and the fixture together.
