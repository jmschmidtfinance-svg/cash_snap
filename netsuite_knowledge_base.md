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
| NetSuite account ID (realm) | `[FILL IN]` |
| Environment | Production / Sandbox `[FILL IN]` |
| Auth method | Token-Based Auth (TBA) / OAuth 2.0 `[FILL IN — pick one]` |
| SuiteQL endpoint | `https://<accountId>.suitetalk.api.netsuite.com/services/rest/query/v1/suiteql` |
| Where credentials live | `[FILL IN — e.g. AWS Secrets Manager path / env var names]` |
| API integration record name | `[FILL IN]` |
| Role used by the integration | `[FILL IN — note its permission scope]` |
| Base currency / consolidation currency | `[FILL IN]` |
| Single subsidiary or multi-subsidiary? | Single subsidiary |

Notes / gotchas about access:
- `[FILL IN — e.g. role lacks access to certain saved searches; rate limits; concurrency]`

---

## 2. Cash / bank accounts

These are the accounts the daily cash snap tracks. In NetSuite, cash accounts are GL
accounts of type **Bank** (`account.accttype = 'Bank'`). Confirm this list matches reality
and update whenever an account is opened/closed.

| Internal ID | Acct # | Account name | Subsidiary | Currency | Purpose / notes |
|---|---|---|---|---|---|
| `[FILL IN]` | | | | | e.g. main operating |
| `[FILL IN]` | | | | | e.g. payroll funding |
| `[FILL IN]` | | | | | e.g. money market / sweep |

Decisions to record:
- Do we include money-market / sweep / investment accounts in "cash"? `[FILL IN]`
- Any restricted-cash accounts to exclude or report separately? `[FILL IN]`

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

SuiteQL exposes the internal type code on `transaction.type` (e.g. `CustPymt`). Use
`BUILTIN.DF(t.type)` to get the human-readable label. Confirm the codes below against the
instance — they vary by NetSuite version/config.

| Type code (verify) | Label | Touches cash? | Typical bucket |
|---|---|---|---|
| `CustPymt` | Customer Payment | Yes | AR collections |
| `Deposit` | Deposit | Yes | AR collections / other inflow |
| `VendPymt` | Bill Payment | Yes | AP disbursements |
| `Check` | Check | Yes | AP / misc disbursement |
| `Paycheck` | Paycheck | Yes | Payroll |
| `Transfer` | Funds Transfer | Yes (net zero) | Internal transfer — EXCLUDE |
| `Journal` | Journal Entry | Sometimes | depends on accounts — see §7 |
| `[FILL IN]` | | | |

---

## 6. SuiteQL reference (validate against the instance)

> These are starting templates. **Validating column names, the `amount` sign convention,
> and `posting`/`type` codes against our account is itself part of building this KB** — once
> verified, mark them ✅ and they become canonical.

### 6a. Cash balance as of a date  `[ ] verified`

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

### 6b. Day's movements hitting bank accounts (detail for bucketizing)  `[ ] verified`

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
> and holidays. The single-day form above is kept for ad-hoc lookups.

### Sign convention `[FILL IN — VERIFY]`
For a Bank account, a cash **increase** posts as a debit. Confirm whether `tal.amount`
returns inflows as positive or negative in this instance, and record it here. The whole
snap's directionality depends on this one fact. (The pipeline assumes inflow = positive and
will flag a reconciliation mismatch on the first run if that is wrong.)

### Schema gotchas
- Prefer `transactionaccountingline` over `transactionline` for posting amounts (handles
  multiple accounting books). `[VERIFY which book]`
- `[FILL IN — voided transactions, in-transit deposits, undeposited funds, FX revaluation]`

---

## 7. Cash bucket mapping (the reusable business logic)

Each posting line that hits a cash account is assigned to exactly one bucket. Rule order:
first match wins. Adjust the buckets and rules to how the CEO thinks about cash.

| Priority | Match condition | Bucket | Notes |
|---|---|---|---|
| 1 | `type_code = 'Transfer'` OR both legs internal | `Internal transfer` | **Excluded** from net flow (nets to zero) |
| 2 | `type_code IN ('CustPymt','Deposit')` | `AR collections` | |
| 3 | `type_code = 'Paycheck'` OR account in payroll set | `Payroll` | |
| 4 | `type_code IN ('VendPymt','Check')` | `AP disbursements` | |
| 5 | `type_code = 'Journal'` AND account = `[debt acct]` | `Debt service` | |
| 6 | `type_code = 'Journal'` AND account = `[tax acct]` | `Taxes` | |
| 99 | (fallback) | `Other / unclassified` | Flag for review if material |

Special rules / exclusions:
- Internal transfers between our own bank accounts must net to zero — exclude from the
  net-change explanation but show them if a single account moved materially. `[CONFIRM]`
- `[FILL IN — sweeps, FX revaluation, reclasses, in-transit timing]`

---

## 8. Cash snap specification

| Item | Value |
|---|---|
| Audience | CEO |
| Cadence | Daily, by `[FILL IN time / timezone]` |
| Compares | Closing cash `[today]` vs `[prior business day]` |
| Delivery channel | Email to me; I forward to the CEO |
| "Call out" threshold | Any single item or bucket moving more than `[FILL IN $]` (SNAP_THRESHOLD) |

Required contents of the snap:
1. Total cash, today vs prior, and the net change.
2. Net change broken down by bucket (§7), largest movers first.
3. Plain-language explanation of *why* cash moved, citing the drivers.
4. Anomaly flags: anything over threshold, anything in `Other / unclassified`, negative
   balances, large unexpected single transactions.
5. `[FILL IN — runway / forecast tie-in, if the CEO wants it]`

Tone/format the CEO prefers: `[FILL IN — bullet brief vs. short paragraph; level of detail]`

---

## 9. Pipeline overview

```
[scheduler] -> [SuiteQL: §6a balances + §6b day movements]
            -> [code: assign bucket per §7, net by bucket, compute movers + flags]
            -> [structured summary JSON]
            -> [one Anthropic API call: summary + this KB -> CEO narrative]
            -> [deliver via §8 channel]
```

| Component | Choice |
|---|---|
| Scheduler / host | GitHub Actions (scheduled workflow) |
| Language | Python |
| Secrets store | GitHub Actions encrypted secrets |
| LLM model | claude-sonnet-4-6 |
| Failure handling | Alert email on empty results, SuiteQL error, or reconciliation mismatch |

Design rule: **code computes, the LLM explains.** The model receives finished numbers; it
never categorizes or sums.

---

## 10. Institutional knowledge / running gotchas

- `[FILL IN — month-end timing effects, holiday calendars, when deposits clear, etc.]`

---

## 11. Changelog

| Date | Change | By |
|---|---|---|
| 2026-06-29 | Initial skeleton + pipeline wired (single subsidiary, GitHub Actions, email delivery) | |
