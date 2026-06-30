# NetSuite Knowledge Base

> **What this is:** a single, durable reference describing how *our* NetSuite data is
> structured, plus the canonical queries and business rules we reuse across accounting/
> NetSuite automation. Load this as context (e.g. a Claude Project knowledge file) for any
> NetSuite or accounting task so the model reasons from our actual structure rather than
> generic NetSuite assumptions.
>
> **How to maintain it:** treat it like code. Every time you discover a quirk, a column
> name, a special account, or a rule, add it here. Log changes in the Changelog at the
> bottom. Anything marked `[FILL IN]` is a deliberate placeholder.
>
> **Never store secrets here.** Token IDs, secrets, API keys, and passwords live in your
> secrets manager / environment variables. This file references *where* they live, not the
> values.

---

## 1. Environment & access

| Item | Value |
|---|---|
| NetSuite account ID (realm) | stored in `NS_ACCOUNT_ID` GitHub secret (not recorded here) |
| Environment | Production |
| Auth method | Token-Based Auth (TBA) — confirmed working 2026-06-29 |
| SuiteQL endpoint | `https://<accountId>.suitetalk.api.netsuite.com/services/rest/query/v1/suiteql` |
| Where credentials live | GitHub Actions encrypted secrets: `NS_ACCOUNT_ID`, `NS_CONSUMER_KEY`, `NS_CONSUMER_SECRET`, `NS_TOKEN_ID`, `NS_TOKEN_SECRET` |
| API integration record name | `Claude - Scanner 2` (State: Enabled, TBA checked) |
| Role used by the integration | Dedicated least-privilege API role (NOT a borrowed privileged role). See §10. |
| Base currency / consolidation currency | USD `[CONFIRM]` |
| Single subsidiary or multi-subsidiary? | Single subsidiary |

Notes / gotchas about access:
- The token is bound to a (user + role) pair. Use a **dedicated, active, least-privilege API
  role** — a borrowed privileged role (e.g. Controller) can fail at login with
  `EntityOrRoleDisabled` even when the role is active and assigned. See §10.

---

## 2. Cash / bank accounts

Cash accounts are GL accounts of type **Bank** (`account.accttype = 'Bank'`). Confirmed via
the first successful run (2026-06-29). Update whenever an account is opened/closed.

| Internal ID | Acct # | Account name | Currency | Purpose / notes |
|---|---|---|---|---|
| `[FILL IN]` | `[FILL IN]` | First Bank | USD | **Main operating account — effectively all daily cash activity flows through here.** |
| `[FILL IN]` | `[FILL IN]` | Brokerage Account | USD | Effectively static (~$8.4k on 2026-06-29); no daily movement. |
| `[FILL IN]` | `[FILL IN]` | First Bank of the Lake | USD | Effectively static (~$302). |
| `[FILL IN]` | `[FILL IN]` | Petty Cash | USD | Effectively static (~$212). |

Decisions to record:
- Only **First Bank** is transactional day to day; the other three carry static balances.
  The snap still reports all four so a balance change in a "static" account would surface.
- Money-market / sweep / investment accounts in "cash"? Brokerage Account is included today
  as a Bank-type account. `[CONFIRM whether it should be treated as cash]`

---

## 3. Segments (the dimensions we slice by)

| Segment | NetSuite field | What it means for us | Values that matter |
|---|---|---|---|
| Subsidiary | `transactionline.subsidiary` | Single subsidiary — no filtering needed | n/a |
| Department | `transactionline.department` | `[FILL IN]` | `[FILL IN]` |
| Class | `transactionline.class` | `[FILL IN]` | `[FILL IN]` |
| Location | `transactionline.location` | `[FILL IN]` | `[FILL IN]` |

---

## 4. Custom fields & custom segments

| Field/segment | Script ID / column | Where it appears | Meaning | Used in cash snap? |
|---|---|---|---|---|
| `[FILL IN]` | `custbody_…` / `custcol_…` | header / line | | |

---

## 5. Transaction types in use

SuiteQL exposes the internal type code on `transaction.type`. Use `BUILTIN.DF(t.type)` for
the human label. **Confirmed against real data on 2026-06-29** unless noted.

| Type code | Label (BUILTIN.DF) | Touches cash? | Bucket | Verified |
|---|---|---|---|---|
| `CustPymt` | **Payment** (note: shows as "Payment", not "Customer Payment") | Yes | AR collections | ✅ |
| `Deposit` | Deposit | Yes | AR collections | ✅ |
| `VendPymt` | Bill Payment | Yes | AP disbursements | ✅ |
| `Check` | Check | Yes | AP disbursements | ✅ |
| `Journal` | Journal | Sometimes | Other / unclassified (see §7, §10) | ✅ |
| `Paycheck` | Paycheck | Yes | Payroll | not yet seen |
| `Transfer` | Funds Transfer | Yes (net zero) | Internal transfer — EXCLUDE | not yet seen |

> Gotcha: a Customer Payment's display label is **"Payment"**, not "Customer Payment" — don't
> filter on the label, filter on the `CustPymt` type code.

---

## 6. SuiteQL reference (validated against the instance)

> 6a and 6b both ran successfully on 2026-06-29 and the results reconciled exactly. These
> are now canonical.

### 6a. Cash balance as of a date  ✅ verified 2026-06-29

```sql
SELECT a.id            AS account_id,
       a.acctnumber,
       a.fullname,
       SUM(tal.amount) AS balance
FROM   transactionaccountingline tal
JOIN   transaction t ON t.id = tal.transaction
JOIN   account a     ON a.id = tal.account
WHERE  a.accttype = 'Bank'
  AND  tal.posting = 'T'
  AND  t.trandate <= TO_DATE(:as_of_date, 'YYYY-MM-DD')
GROUP BY a.id, a.acctnumber, a.fullname
```

### 6b. Day's movements hitting bank accounts (detail for bucketizing)  ✅ verified 2026-06-29

```sql
SELECT t.id            AS tran_id,
       t.tranid,
       t.trandate,
       t.type          AS type_code,
       BUILTIN.DF(t.type)    AS type_label,
       a.acctnumber,
       a.fullname      AS account_name,
       BUILTIN.DF(t.entity)  AS entity_name,
       t.memo,
       tal.amount
FROM   transactionaccountingline tal
JOIN   transaction t ON t.id = tal.transaction
JOIN   account a     ON a.id = tal.account
WHERE  a.accttype = 'Bank'
  AND  tal.posting = 'T'
  AND  t.trandate = TO_DATE(:snap_date, 'YYYY-MM-DD')
ORDER BY tal.amount
```

> **Pipeline note:** the live pipeline widens 6b to the range `(prior_business_day,
> report_date]` so the bucketized movements reconcile to the balance delta across weekends
> and holidays. Confirmed: the 2026-06-29 run spanned Fri 6/26 → Mon 6/29 and tied out.

### Sign convention  ✅ VERIFIED 2026-06-29
For a Bank account, a cash **increase** posts as a positive `tal.amount` (debit-positive).
Inflows are positive, outflows negative. **Confirmed:** the first run reconciled to the
penny with `INFLOW_IS_POSITIVE = True`, so this is settled for our instance.

### Schema gotchas
- Use `transactionaccountingline` for posting amounts (handles multiple accounting books).
  Working as expected with the default book.
- `[FILL IN — voided transactions, in-transit deposits, undeposited funds, FX revaluation]`

---

## 7. Cash bucket mapping (the reusable business logic)

Each posting line that hits a cash account is assigned to exactly one bucket. Rule order:
first match wins.

| Priority | Match condition | Bucket | Notes |
|---|---|---|---|
| 1 | `type_code = 'Transfer'` OR both legs internal | `Internal transfer` | **Excluded** from net flow (nets to zero). Not yet seen in data. |
| 2 | `type_code IN ('CustPymt','Deposit')` | `AR collections` | ✅ confirmed |
| 3 | `type_code = 'Paycheck'` OR account in payroll set | `Payroll` | not yet seen |
| 4 | `type_code IN ('VendPymt','Check')` | `AP disbursements` | ✅ confirmed |
| 5 | `type_code = 'Journal'` AND account = `[debt acct]` | `Debt service` | `[FILL IN debt acct]` |
| 6 | `type_code = 'Journal'` AND account = `[tax acct]` | `Taxes` | `[FILL IN tax acct]` |
| 99 | (fallback) | `Other / unclassified` | Flag for review if material |

Special rules / exclusions:
- Internal transfers between our own bank accounts must net to zero — exclude from the
  net-change explanation but show them if a single account moved materially. (Not yet
  observed in data.)
- **Bank-interest journals** (small `Journal` entries hitting First Bank, e.g. JE14865
  +$69.36 on 2026-06-29) currently fall to `Other / unclassified`. DECISION PENDING: add a
  rule mapping small bank-leg journals to a `Bank interest / adjustments` bucket, or do the
  offset-account lookup so journals classify by their non-bank leg. See §10.
- Note: `Check` is currently bucketed as `AP disbursements`, but some checks are lease/loan
  payments (e.g. Ford Credit, MBFS). `[CONSIDER]` routing these to `Debt service` later.

---

## 8. Cash snap specification

| Item | Value |
|---|---|
| Audience | CEO (Jamie receives it and forwards) |
| Cadence | Daily, 11:00 UTC (GitHub Actions cron) |
| Compares | Closing cash today vs prior business day |
| Delivery channel | Email from `jmschmidtfinance@gmail.com` → Jamie's inbox; Jamie forwards to CEO |
| "Call out" threshold | `SNAP_THRESHOLD` = 10000 (any single item or bucket beyond this is flagged) |

Required contents of the snap:
1. Total cash, today vs prior, and the net change.
2. Net change broken down by bucket (§7), largest movers first.
3. Plain-language explanation of *why* cash moved, citing the drivers.
4. Anomaly flags: anything over threshold, anything in `Other / unclassified`, negative
   balances, large unexpected single transactions.
5. `[FILL IN — runway / forecast tie-in, if the CEO wants it]`

Tone/format: short headline + bulleted drivers + a flags section.
> KNOWN ISSUE (pending fix): the model emits Markdown but the email is sent as plain text,
> so `**bold**` and pipe tables render as raw characters. Fix = send HTML (render the
> Markdown), or instruct the model to emit plain text only.

---

## 9. Pipeline overview

```
[GitHub Actions cron] -> [SuiteQL: §6a balances + §6b range movements]
            -> [code: assign bucket per §7, net by bucket, compute movers + flags]
            -> [structured summary JSON]
            -> [one Anthropic API call: summary + this KB -> CEO narrative]
            -> [email to Jamie; Jamie forwards to CEO]
```

| Component | Choice |
|---|---|
| Scheduler / host | GitHub Actions (scheduled workflow), repo `jmschmidtfinance-svg/cash_snap` |
| Language | Python |
| Secrets store | GitHub Actions encrypted secrets |
| LLM model | claude-sonnet-4-6 |
| Failure handling | Alert email on empty results, SuiteQL error, or reconciliation mismatch |

Design rule: **code computes, the LLM explains.** The model receives finished numbers; it
never categorizes or sums. (Validated end-to-end 2026-06-29.)

---

## 10. Institutional knowledge / running gotchas

- **Integration role (2026-06-29):** the token must use a dedicated, active, least-privilege
  API role. A borrowed privileged role ("Highland - Controller (Diane)") failed at login
  with `EntityOrRoleDisabled` even though it was active and assigned to the user — swapping
  to a dedicated API role fixed it immediately. The dedicated role needs **Log in using
  Access Tokens** + **REST Web Services** (Setup) and **View** on Transactions and Accounts.
- **Sign convention confirmed (2026-06-29):** inflows post positive; the first run
  reconciled exactly.
- **Cash accounts:** only First Bank is transactional; Brokerage, First Bank of the Lake,
  and Petty Cash are effectively static.
- **Bank-interest journals:** small `Journal` entries to First Bank (e.g. +$69.36) land in
  `Other / unclassified` and are almost certainly bank interest/adjustments — see §7
  decision pending.
- `[FILL IN — month-end timing effects, holiday calendars, when deposits clear, etc.]`

---

## 11. Changelog

| Date | Change | By |
|---|---|---|
| 2026-06-29 | Initial skeleton + pipeline wired (single subsidiary, GitHub Actions, email delivery) | |
| 2026-06-29 | First successful end-to-end run. Verified sign convention (inflow positive); marked §6a/6b queries canonical; populated cash accounts (§2) and transaction types (§5); recorded dedicated-API-role fix for `EntityOrRoleDisabled` (§10); set threshold = 10000; noted Markdown-in-plain-text email issue and bank-interest-journal decision. | |
