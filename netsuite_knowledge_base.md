# NetSuite Knowledge Base

> **What this is:** a single, durable reference describing how *our* NetSuite data is
> structured, plus the canonical queries and business rules we reuse across accounting/
> NetSuite automation. Load this as context (e.g. a Claude Project knowledge file) for any
> NetSuite or accounting task so the model reasons from our actual structure rather than
> generic NetSuite assumptions.
>
> **How to maintain it:** treat it like code. Every time you discover a quirk, a column
> name, a special account, or a rule, add it here. Log changes in the Changelog at the
> bottom. Anything marked `[FILL IN]` is a deliberate placeholder; `[VERIFY]` is a claim we
> believe but haven't yet confirmed against the instance.
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
| Base currency | USD `[CONFIRM]` |
| Single subsidiary or multi-subsidiary? | Single subsidiary |

Notes / gotchas about access:
- The token is bound to a (user + role) pair. Use a **dedicated, active, least-privilege API
  role** — a borrowed privileged role (e.g. Controller) can fail at login with
  `EntityOrRoleDisabled` even when the role is active and assigned. See §10.

---

## 2. Cash / cash-equivalent accounts

**The cash perimeter is Bank accounts + Undeposited Funds.** We treat "cash" as the combined
balance of both, because a customer check we've received but not yet deposited is economically
cash-in-hand. In SuiteQL we select banks by type and UF by explicit id:
`(account.accttype = 'Bank' OR account.id = 122)` — see the note below for why.

| Internal ID | Acct # | Account name | accttype | Purpose / notes |
|---|---|---|---|---|
| `[FILL IN]` | `[FILL IN]` | First Bank | Bank | **Main operating account — nearly all daily bank activity flows here.** |
| `122` | `[FILL IN]` | Undeposited Funds | OthCurrAsset | **Checks received but not yet formally deposited. Selected by explicit id (see below). In the cash perimeter as of v2.** |
| `[FILL IN]` | `[FILL IN]` | Brokerage Account | Bank | Effectively static (~$8.4k on 2026-06-29). |
| `[FILL IN]` | `[FILL IN]` | First Bank of the Lake | Bank | Effectively static (~$302). |
| `[FILL IN]` | `[FILL IN]` | Petty Cash | Bank | Effectively static (~$212). |

Decisions to record:
- Only **First Bank** and **Undeposited Funds** move day to day; the other three carry static
  balances. The snap still reports all of them so any change surfaces.
- **Undeposited Funds is account id 122, type `OthCurrAsset` (Other Current Asset) — NOT the
  built-in `UnDepFunds` type** (confirmed 2026-07-01). This bit us on the first run: the
  `accttype IN ('Bank','UnDepFunds')` filter matched no UF account, so UF was invisible in
  balances AND movements and deposits showed up as a phantom ~$973k inflow. We select UF by
  explicit id (`CASH_EXTRA_ACCOUNT_IDS`, default `122`). We deliberately do **not** filter on
  `OthCurrAsset` — that would sweep in prepaids and every other Other Current Asset account.


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

Known gap: **payments are not yet joined to job/project codes** (e.g. "1478ILM Galleria").
The CEO block wants cash-in itemized by project; until we can map a payment → its applied
invoices → their job/project, we itemize cash-in by customer as a stand-in. See §8.

---

## 5. Transaction types in use

Use `BUILTIN.DF(t.type)` for the human label. **Confirmed against real data 2026-06-29**
unless noted. Bucketing reflects the v2 cash perimeter (Bank + UF).

| Type code | Label (BUILTIN.DF) | Touches cash? | Bucket | Verified |
|---|---|---|---|---|
| `CustPymt` | **Payment** (shows as "Payment", not "Customer Payment") | Yes (hits UF or Bank) | AR collections | ✅ |
| `Deposit` | Deposit | Yes (UF → Bank) | **Internal transfer** — see note | ✅ |
| `VendPymt` | Bill Payment | Yes | AP disbursements | ✅ |
| `Check` | Check | Yes | AP disbursements | ✅ |
| `Journal` | Journal | Sometimes | Other / unclassified (see §7, §10) | ✅ |
| `Paycheck` | Paycheck | Yes | Payroll | not yet seen |
| `Transfer` | Funds Transfer | Yes (net zero) | Internal transfer — EXCLUDE | not yet seen |

> **v2 consequence of adding Undeposited Funds to cash:** the real AR inflow is recognized when
> a **Customer Payment** lands in Undeposited Funds. The subsequent **Deposit** just moves that
> money UF → Bank, i.e. *within* the cash perimeter, so it nets to zero and is classified as an
> **Internal transfer**, not a second inflow. `[VERIFY: assumes deposits are UF→Bank sweeps. A
> direct external deposit would be understated as AR until we distinguish it.]`
> Also: a Customer Payment's display label is "Payment" — filter on the `CustPymt` type code,
> not the label.

---

## 6. SuiteQL reference (validated against the instance)

> 6a and 6b ran successfully on 2026-06-29 and reconciled exactly; they are canonical.
> The roll-forward variants and AR/AP totals were added in v2.

### 6a. Cash balance as of a date  ✅ verified 2026-06-29
Optional `created_on_or_before` cutoff reconstructs the balance as it stood at close,
excluding entries back-posted afterward (used for the bootstrap prior — see §9/§10).

```sql
SELECT a.id AS account_id, a.acctnumber, a.fullname, a.accttype,
       SUM(tal.amount) AS balance
FROM   transactionaccountingline tal
JOIN   transaction t ON t.id = tal.transaction
JOIN   account a     ON a.id = tal.account
WHERE  (a.accttype = 'Bank' OR a.id = 122)          -- banks by type, Undeposited Funds by id
  AND  tal.posting = 'T'
  AND  t.trandate <= TO_DATE(:as_of_date, 'YYYY-MM-DD')
  -- AND TRUNC(t.createddate) <= TO_DATE(:created_cutoff, 'YYYY-MM-DD')  -- bootstrap only
GROUP BY a.id, a.acctnumber, a.fullname, a.accttype
```

### 6b. Movements hitting cash accounts (roll-forward window)  ✅ verified 2026-06-29
Captures everything new since the prior snapshot: dated in the window **OR created since**
prior_date (the latter catches back-posted entries). This is the key back-posting control.

```sql
SELECT t.id AS tran_id, t.tranid, t.trandate, t.createddate,
       t.type AS type_code, BUILTIN.DF(t.type) AS type_label,
       a.id AS account_id, a.acctnumber, a.fullname AS account_name, a.accttype,
       BUILTIN.DF(t.entity) AS entity_name, t.memo, tal.amount
FROM   transactionaccountingline tal
JOIN   transaction t ON t.id = tal.transaction
JOIN   account a     ON a.id = tal.account
WHERE  (a.accttype = 'Bank' OR a.id = 122)          -- banks by type, Undeposited Funds by id
  AND  tal.posting = 'T'
  AND  t.trandate <= TO_DATE(:report_date, 'YYYY-MM-DD')
  AND  ( t.trandate > TO_DATE(:prior_date, 'YYYY-MM-DD')
         OR TRUNC(t.createddate) > TO_DATE(:prior_date, 'YYYY-MM-DD') )
ORDER BY tal.amount
```

### 6c. AR / AP totals (for the CEO block)  `[ ] verify`
Totals by account type as of the report date. AP is a liability (credit balance), so present
its absolute value.

```sql
SELECT SUM(tal.amount) AS balance
FROM   transactionaccountingline tal
JOIN   transaction t ON t.id = tal.transaction
JOIN   account a     ON a.id = tal.account
WHERE  a.accttype = :type            -- 'AcctRec' for AR, 'AcctPay' for AP
  AND  tal.posting = 'T'
  AND  t.trandate <= TO_DATE(:report_date, 'YYYY-MM-DD')
```

### Sign convention  ✅ VERIFIED 2026-06-29
Cash **increase** posts as positive `tal.amount` (debit-positive). Inflows positive, outflows
negative. The first run reconciled to the penny with `INFLOW_IS_POSITIVE = True`.

### Schema gotchas
- Use `transactionaccountingline` for posting amounts (handles multiple accounting books).
- **`t.createddate`** drives the whole roll-forward / back-post capture. `[VERIFY the exact
  column name and that it reflects when an entry actually hit the books.]`
- `[FILL IN — voided transactions, in-transit deposits, FX revaluation]`

---

## 7. Cash bucket mapping (the reusable business logic)

Each posting line hitting a cash account is assigned to exactly one bucket. First match wins.

| Priority | Match condition | Bucket | Notes |
|---|---|---|---|
| 1 | `type_code IN ('Transfer','Deposit')` | `Internal transfer` | **Excluded** from net flow. Deposit = UF→Bank, internal to the cash perimeter. |
| 2 | `type_code = 'CustPymt'` | `AR collections` | ✅ inflow recognized here (to UF or Bank) |
| 3 | `type_code = 'Paycheck'` OR account in payroll set | `Payroll` | not yet seen |
| 4 | `type_code IN ('VendPymt','Check')` | `AP disbursements` | ✅ confirmed |
| 5 | `type_code = 'Journal'` AND account = `[debt acct]` | `Debt service` | `[FILL IN debt acct]` |
| 6 | `type_code = 'Journal'` AND account = `[tax acct]` | `Taxes` | `[FILL IN tax acct]` |
| 99 | (fallback) | `Other / unclassified` | Flag for review if material |

Special rules / exclusions:
- **UF ↔ Bank deposits net to zero** across the cash perimeter (both legs are cash accounts),
  so the reconciliation handles them automatically; bucketing them as `Internal transfer`
  keeps them out of the driver explanation.
- **Bank-interest journals** (small `Journal` entries to First Bank, e.g. JE14865 +$69.36 on
  2026-06-29) fall to `Other / unclassified`. DECISION PENDING: add a `Bank interest /
  adjustments` rule, or classify journals by their non-bank offset leg. See §10.
- Some `Check`s are lease/loan payments (Ford Credit, MBFS). `[CONSIDER]` routing to
  `Debt service` later.

---

## 8. Cash snap specification

| Item | Value |
|---|---|
| Audience | CEO (Jamie receives it and forwards) |
| Cadence | Daily, fires ~00:30 US/Eastern (cron `30 5 * * *` UTC) so the reported day is fully closed |
| Report date | Yesterday (the just-closed day), UTC-yesterday at fire time |
| Compares | Closing cash today vs the last **reported** balance (roll-forward — see §9) |
| Delivery channel | Email from `jmschmidtfinance@gmail.com` → Jamie's inbox; Jamie forwards to CEO |
| "Call out" threshold | `SNAP_THRESHOLD` = 10000 (any single item/bucket beyond this is flagged) |
| Format | **Plain text** (no Markdown) |

The email has two sections:

**CFO VIEW** — for auditing.
1. LLM narrative: headline, drivers (by bucket + top movers), and every flag.
2. Deterministic detail block with **full dollars and cents**: cash today (per account + total),
   prior reported balance + its date/source, net change, AR total, AP total, by-bucket nets,
   and the reconciliation result. This block is code-generated, never the LLM.

**CEO OUTPUT** — text-message style, rounded to $k/$m, matching Jamie's sample:
```
6/30
Cash: $4.1m
AR: $5.6m
AP: $2.6m
Unpaid bills: $19k

Cash in: $882k
<itemized>
Cash out: $333k
<itemized, top ~8 then "All others $X">
```
- **Cash** = Bank + Undeposited Funds. **AR** = AcctRec total. **AP** = AcctPay total (abs).
- **Unpaid bills** — `[TODO]` placeholder; overdue-vs-open definition not yet nailed down.
- **Cash in** — itemized by **customer** as a stand-in until payments can be mapped to project
  codes (see §4). **Cash out** — by vendor, top ~8 then an "All others" rollup.

Anomaly flags: over-threshold items/buckets, anything in `Other / unclassified`, negative
balances, large single transactions, reconciliation mismatch, and the bootstrap notice.

---

## 9. Pipeline overview & reconciliation logic

```
[GitHub Actions cron ~00:30 ET] -> [SuiteQL: §6a balances, §6b roll-forward movements, §6c AR/AP]
  -> [code: read prior reported balance from state, bucketize per §7, net, reconcile, flag]
  -> [structured summary JSON]
  -> [one Anthropic API call: summary + this KB -> CFO narrative + CEO block]
  -> [email to Jamie; write state/cash_state.json; workflow commits it back]
```

| Component | Choice |
|---|---|
| Scheduler / host | GitHub Actions scheduled workflow, repo `jmschmidtfinance-svg/cash_snap` |
| Language | Python |
| Secrets store | GitHub Actions encrypted secrets |
| LLM model | claude-sonnet-4-6 |
| State / audit ledger | `state/cash_state.json`, committed back to the repo each run (needs `permissions: contents: write` + "Read and write" workflow permission enabled) |
| Failure handling | Alert email on empty results, SuiteQL error, or reconciliation mismatch |

**Roll-forward reconciliation.** The prior balance is the **last reported** figure read from
`state/cash_state.json` — not a freshly recomputed prior — so the day-over-day story matches
what the CEO actually saw. Each run checks:

```
last reported balance  +  sum(captured movements §6b)  ==  today's computed balance §6a
```

A mismatch is flagged and the numbers are called provisional; the run still sends.

**Bootstrap (first run, no state file).** There is no prior *reported* balance, so we
reconstruct one: balance as of prior_date **excluding entries created after prior_date** (§6a
with the `created_on_or_before` cutoff). Those back-posts then appear in this run's movements
(§6b with the createddate clause), so the first run is a true roll-forward that surfaces
back-posting rather than burying it. After a successful send, the run writes and commits the
state file; every run thereafter uses the logged path.

Design rule: **code computes, the LLM explains.** The model receives finished numbers; it
never categorizes, sums, or reconciles. (Validated end-to-end 2026-06-29.)

---

## 10. Institutional knowledge / running gotchas

- **Integration role (2026-06-29):** the token must use a dedicated, active, least-privilege
  API role. A borrowed privileged role ("Highland - Controller (Diane)") failed at login with
  `EntityOrRoleDisabled` even though active and assigned — swapping to a dedicated API role
  fixed it. That role needs **Log in using Access Tokens** + **REST Web Services** (Setup) and
  **View** on Transactions and Accounts.
- **Sign convention confirmed (2026-06-29):** inflows post positive; first run reconciled.
- **Back-posting is a material reality.** There's always a fair amount of it, so the
  reconciliation anchors on the last *reported* balance and captures movements by trandate
  **or** createddate. The bootstrap deliberately reconstructs a clean prior (excluding
  late-created entries) so day one isn't a naive recompute. All of this relies on
  `t.createddate` reflecting when an entry hit the books — the first run is our real-world test.
- **Cash perimeter = Bank + Undeposited Funds (v2).** Only First Bank and UF are transactional;
  Brokerage, First Bank of the Lake, and Petty Cash are effectively static. A Deposit is a
  UF→Bank internal move and nets to zero.
- **Undeposited Funds is id 122, type `OthCurrAsset` — not `UnDepFunds` (confirmed 2026-07-01).**
  The first live run (report_date 6/30) proved this: the `('Bank','UnDepFunds')` type filter
  matched no UF account, so UF was absent from balances AND movements, the ~4 payments into UF
  on 6/29 were missed, and the day's deposits surfaced as a phantom +$973k "Internal transfer"
  (only the First Bank leg was visible). Fix: select UF by explicit id (default 122), keep banks
  by type. Do not filter on `OthCurrAsset` — it would pull in prepaids and other OCA accounts.
- **Bank-interest journals:** small `Journal` entries to First Bank land in
  `Other / unclassified` and are almost certainly bank interest — see §7 decision pending.
- `[FILL IN — month-end timing effects, holiday calendars, when deposits clear, etc.]`

---

## 11. Changelog

| Date | Change | By |
|---|---|---|
| 2026-06-29 | Initial skeleton + pipeline wired (single subsidiary, GitHub Actions, email delivery). | |
| 2026-06-29 | First successful end-to-end run. Verified sign convention (inflow positive); marked §6a/6b canonical; populated cash accounts and transaction types; recorded dedicated-API-role fix for `EntityOrRoleDisabled`; set threshold = 10000; noted Markdown-in-plain-text email issue and bank-interest-journal decision. | |
| 2026-06-30 | **v2.** Added Undeposited Funds to the cash perimeter (`Bank`+`UnDepFunds`); reclassified Deposit as an internal UF→Bank transfer; implemented stateful roll-forward reconciliation against the last reported balance with a committed `state/cash_state.json` audit ledger; bootstrap now reconstructs a clean prior and surfaces back-posts; moved cron to ~00:30 ET; split output into CFO VIEW (full-decimal audit) + CEO OUTPUT (rounded text-message style); added §6c AR/AP totals. Open TODOs: define unpaid-bills (overdue vs open); map cash-in to project codes; verify `createddate` column. | |
| 2026-07-01 | First live run (6/30) exposed the UF bug: Undeposited Funds is id 122 / type `OthCurrAsset`, not `UnDepFunds`, so the type filter matched nothing and UF was invisible everywhere (phantom +$973k "Internal transfer"). Fix: select banks by type + UF by explicit id (default 122, `CASH_EXTRA_ACCOUNT_IDS` overrides); added an optional `report_date` workflow_dispatch input. Requires deleting the stale Bank-only `state/cash_state.json` so the next run re-bootstraps with UF included. | |
