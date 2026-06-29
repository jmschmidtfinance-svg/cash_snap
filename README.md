# Daily cash snap — setup

A hands-off pipeline that pulls cash data from NetSuite via SuiteQL, buckets the day's
movements, has Claude write a CEO-ready narrative, and emails the snap + data to you.

**Principle:** code computes, the LLM explains. Every number is final before the model
sees it; the model only writes prose.

## Repo layout

```
.
├── cash_snap.py                 # the pipeline
├── requirements.txt
├── netsuite_knowledge_base.md   # your KB — loaded as context for the narrative
└── .github/workflows/
    └── cash_snap.yml            # the scheduled workflow
```

## One-time setup

1. **Create a NetSuite integration + access token** (Token-Based Auth) for a role that can
   read transactions and accounts. You'll get a consumer key/secret and token id/secret.
2. **Add repository secrets** (Settings → Secrets and variables → Actions → Secrets):
   `NS_ACCOUNT_ID`, `NS_CONSUMER_KEY`, `NS_CONSUMER_SECRET`, `NS_TOKEN_ID`,
   `NS_TOKEN_SECRET`, `ANTHROPIC_API_KEY`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`,
   `SMTP_PASSWORD`, `MAIL_FROM`, `MAIL_TO`.
   Add `SNAP_THRESHOLD` as a repository **variable** (not a secret).
3. **Run it manually first** from the Actions tab (`workflow_dispatch`) and read the email.

These tokens are yours to generate and store — they go in GitHub's encrypted secrets, never
in the code. The script only reads them from the environment.

## Verify this BEFORE you trust the output

The sign convention is the one fact the whole snap hinges on (KB §6). The script assumes a
cash inflow posts as a positive amount (`INFLOW_IS_POSITIVE = True`). If that's backwards
for your instance, the reconciliation check fails loudly and the email says the numbers are
provisional — so the first run tells you immediately. If it reports a mismatch, flip the
flag and re-run.

## Known limitations (tracked in the KB)

- **Journal entries** are currently routed to "Other / unclassified," because the movement
  query returns only the bank leg of each transaction. Fill in `DEBT_ACCOUNT_ID`,
  `TAX_ACCOUNT_ID`, and `PAYROLL_ACCOUNT_IDS` in `cash_snap.py` (from KB §2/§7) and add the
  small offset-lookup to light up the Debt service / Taxes buckets.
- **Holidays** aren't modeled — `previous_business_day` only skips weekends. The
  movement window spans the full gap between the two dates, so totals still reconcile; only
  the date labels may look off around holidays.
- **Cron timing** is UTC-only and best-effort on GitHub Actions. See the note in the
  workflow file.

## Docs

NetSuite SuiteQL is reached through the SuiteTalk REST endpoint; Anthropic Messages API
reference is at https://docs.claude.com/en/api/overview if you want to adjust the model or
call shape.
