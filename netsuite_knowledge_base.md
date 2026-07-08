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

**The cash perimeter is an explicit allowlist of account internal IDs — First Bank (223) and
Undeposited Funds (122) only.** We treat "cash" as the combined balance of these two, because a
customer check we've received but not yet deposited is economically cash-in-hand. In SuiteQL we
select by id: `account.id IN (223, 122)` (see the note below for why an allowlist, not a type filter).

| Internal ID | Acct # | Account name | accttype | In perimeter? | Purpose / notes |
|---|---|---|---|---|---|
| `223` | `100` | First Bank | Bank | **Yes** | **Main operating account — nearly all daily bank activity flows here.** |
| `122` | — | Undeposited Funds | OthCurrAsset | **Yes** | Checks received but not yet formally deposited. Selected by explicit id. |
| `312` | — | Brokerage Account | Bank | No | Static (~$8.4k on 2026-06-29); excluded as of v3. |
| `311` | — | First Bank of the Lake | Bank | No | Static (~$302); excluded as of v3. |
| `224` | `104` | Petty Cash | Bank | No | Static (~$212); excluded as of v3. |

Decisions to record:
- **v3 (2026-07-01): perimeter narrowed to an id allowlist `{223, 122}`.** Previously it was every
  `Bank`-type account + UF, which pulled Brokerage (312), First Bank of the Lake (311) and Petty
  Cash (224) into total cash — static balances that added noise to the daily net change and aren't
  part of the operating-cash view. Only First Bank and UF are transactional, so the perimeter is now
  those two by internal id. Override via `CASH_ACCOUNT_IDS` (comma-separated internal ids, default
  `"223,122"`). NOTE: narrowing the perimeter re-bases total cash (drops ~$8,943.69 of static
  balances), so `state/cash_state.json` must be deleted before the next run to re-bootstrap the
  baseline (otherwise the roll-forward mismatches; this also clears the pre-existing −$715 gap).
- **Undeposited Funds is account id 122, type `OthCurrAsset` (Other Current Asset) — NOT the
  built-in `UnDepFunds` type** (confirmed 2026-07-01). Selecting UF needs its explicit id; an
  `accttype`-based filter can't reach it, and filtering on `OthCurrAsset` would sweep in prepaids and
  every other Other Current Asset account. The id allowlist sidesteps both problems.


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
| Job class | `custentity_r_it_class` | project (`job`) record | **1 = Production, 2 = Service** (matches the Class master, §3). Distribution: 354 Production, 11,925 Service, 2 null. This is the authoritative Production/Service flag. **GOTCHA: the SuiteQL REST client returns this value as a STRING (`"1"`/`"2"`), not an int, so a raw `== 1` comparison silently fails and every job collapses to Service (blank Class column, no Production breakout). Coerce to int at the source of every by-project query (`_class_int()`), confirmed 2026-07-01.** | Yes — cash-in split |
| GL project | `custcol_r_it_reporting_project` | invoice **line** | The RABB-IT reporting project (a `job` id) on the line. Populated on Production invoices; **null on Service** (fine — those fall through to Service). NOT the header customer. | Yes — cash-in split |
| Procore project # | `custentity_r_it_pc_project_number` | project (`job`) record | RABB-IT's Procore project number (usually equals `entityid`). | Reference |

**Project = the native `job` record** (NetSuite renamed Jobs → Projects in the UI but kept the
SuiteQL name `job`). Job # = `job.entityid` (e.g. "1471CLT"); Job name = `job.companyname`
(e.g. "Stanly County EOC"). The invoice **header** `entity` is the GC **customer** (Oscar Renda,
Barnhill, Easterseals), NOT the project — do not use it for project attribution.

**Cash-in-by-project (resolved 2026-07-01).** Payment → applied invoice(s) → GL project → project
class. Production is itemized by project #/name; Service (Service-class or null GL project) is a
single total. See §6d for the query and §7 for the rule. (Previously we itemized by customer as a
stand-in; that is superseded.)

---

## 5. Transaction types in use

Use `BUILTIN.DF(t.type)` for the human label. **Confirmed against real data 2026-06-29**
unless noted. Bucketing reflects the v3 cash perimeter (id allowlist {223 First Bank, 122 UF}).

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
WHERE  a.id IN (223, 122)                            -- explicit cash perimeter (CASH_ACCOUNT_IDS)
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
WHERE  a.id IN (223, 122)                            -- explicit cash perimeter (CASH_ACCOUNT_IDS)
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

### 6d. Cash-in by project (Production itemized / Service lumped)  ✅ verified 2026-07-01
For each customer payment in the roll-forward window, split its applied amount across the invoices
it paid (`PreviousTransactionLineLink.foreignamount` — NOTE the applied amount is `foreignamount`,
not `amount`), resolve each invoice's **GL project** (line field `custcol_r_it_reporting_project`,
taken as one project per invoice via `MIN`), and read the project's class. One row per
(payment, invoice); the day's rows sum to the AR-collections inflow (verified: 6/30 = $64,160.57).

```sql
SELECT pay.tranid         AS payment_no,
       link.foreignamount AS applied_amt,
       proj.entityid      AS project_num,
       proj.companyname   AS project_name,
       proj.custentity_r_it_class AS class_id      -- 1 Production, 2 Service
FROM   transaction pay
JOIN   PreviousTransactionLineLink link
         ON link.nextdoc = pay.id AND link.previoustype = 'CustInvc'
JOIN   transaction inv ON inv.id = link.previousdoc
LEFT JOIN job proj ON proj.id = (
         SELECT MIN(tl.custcol_r_it_reporting_project) FROM transactionline tl
         WHERE  tl.transaction = inv.id AND tl.custcol_r_it_reporting_project IS NOT NULL)
WHERE  pay.type = 'CustPymt'
  AND  pay.trandate <= TO_DATE(:report_date, 'YYYY-MM-DD')
  AND  ( pay.trandate > TO_DATE(:prior_date, 'YYYY-MM-DD')
         OR TRUNC(pay.createddate) > TO_DATE(:prior_date, 'YYYY-MM-DD') )
```
Code then groups class 1 (Production) by project #/name; **Service = the residual** against the
AR inflow total, so Production + Service always ties. `PreviousTransactionLineLink` link fields:
`nextdoc`=payment, `previousdoc`=invoice, `previoustype`='CustInvc', `foreignamount`=applied amount.

### 6e. Overdue unpaid bills  ✅ verified 2026-07-01 (boundary fixed)
Open vendor bills due **on or before** the report date, at remaining (unpaid) balance. Open =
`foreignamountunpaid > 0` (header field). **Boundary is INCLUSIVE (`<=`).** It was originally strict
`<`, which dropped every bill due *on* the report date — for 7/1 that hid ~$82.7k of subcontractor
bills (roofing subs + Watco + AXA) and undercounted overdue by more than half ($71k vs $153,922.83 /
33 bills). A bill due on the report date is past due by the time the snap is read the next morning,
so it belongs. No null-amount or missing-duedate rows exist. `overdue_bills()` now returns the full
bill list (with `days_overdue`), and the summary carries the aggregate `{basis, asof, count, total}`.

```sql
SELECT t.tranid AS bill_no, BUILTIN.DF(t.entity) AS vendor, t.trandate, t.duedate,
       ABS(t.foreigntotal) AS bill_amount, t.foreignamountunpaid AS unpaid, t.status
FROM   transaction t
WHERE  t.type = 'VendBill'
  AND  t.duedate <= TO_DATE(:report_date, 'YYYY-MM-DD')   -- INCLUSIVE
  AND  t.foreignamountunpaid > 0
ORDER BY t.duedate, vendor
```
The full list ships as the `overdue_bills_<date>.xlsx` attachment (`overdue_bills_workbook()`,
openpyxl, most-overdue first) so the AP total can be reconciled line by line.

### 6f. AP paid by job (Production + Service itemized by job × vendor / Other lumped)  ✅ verified 2026-07-01 (Service section added 2026-07-07)
The AP-disbursements outflow attributed to jobs. Two disjoint paths, unioned; both read the GL
**reporting project** (`custcol_r_it_reporting_project`). Same roll-forward window as §6b. Verified
6/30: bill payments $760 + direct checks $59,837.02 = $60,597.02 (ties to the AP bucket; all
non-project that day). Recent Production example: bill payment → `1478ILM` "Galleria West" $11,834.10.

- **(A) Bill payments** (`VendPymt`): payment → applied bill via `PreviousTransactionLineLink`
  (`nextdoc`=payment, `previoustype`='VendBill', `foreignamount`=amount paid on the bill) → the
  **bill's** reporting project. NOTE `foreignamount` is correct on current payments; some legacy
  (2024) links show 0 — ignore, out of window.
- **(B) Direct checks** (`Check`): the check's own non-mainline (expense) lines carry the reporting
  project directly; amount = the line's debit (`tl.amount`, mainline='F'). (A `Check` that pays a
  bill instead posts to AP with no project → falls to Other; rare here.)

```sql
-- (A) bill payments
SELECT pay.tranid AS payment_no, link.foreignamount AS amt,
       proj.entityid AS project_num, proj.custentity_r_it_class AS class_id
FROM   transaction pay
JOIN   PreviousTransactionLineLink link ON link.nextdoc = pay.id AND link.previoustype = 'VendBill'
JOIN   transaction bill ON bill.id = link.previousdoc
LEFT JOIN job proj ON proj.id = (SELECT MIN(tl.custcol_r_it_reporting_project) FROM transactionline tl
                                 WHERE tl.transaction = bill.id AND tl.custcol_r_it_reporting_project IS NOT NULL)
WHERE  pay.type = 'VendPymt' AND <roll-forward window on pay>
UNION ALL
-- (B) direct checks
SELECT t.tranid AS payment_no, tl.amount AS amt,
       proj.entityid AS project_num, proj.custentity_r_it_class AS class_id
FROM   transactionline tl
JOIN   transaction t ON t.id = tl.transaction
LEFT JOIN job proj ON proj.id = tl.custcol_r_it_reporting_project
WHERE  t.type = 'Check' AND tl.mainline = 'F' AND <roll-forward window on t>
```
Code itemizes **both** Production-class and Service-class jobs by job — and, within each job, by
**vendor** — into two sections (`ap_paid_project_split()` → `production[]` and `service[]`, each job
entry carrying a `vendors[]` list; the per-vendor amounts sum to the job total). Every job-tied bill
therefore appears under its job regardless of class. **Other = residual** vs the AP-disbursements
total — genuinely non-project overhead/SG&A only — so Production + Service + Other ties. Job-cost
subs/suppliers land on a job once bills/checks carry the reporting project; e.g. the 2026-07-07
window showed ~$25.5k of **Service** job-tied AP across 26 job×vendor rows (Sunbelt Rentals, ABC
Supply, Construction Metal Products, etc.) that were previously buried in Other.

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
| 5 | `type_code = 'Journal'` whose offsets net to one liability acct + Interest Expense | `Debt service` (labeled) | Debt P&I payment — see the journal-purpose rule below. Liability accts seen: **SBA Loan**, **Loan - Other** (both `LongTermLiab`). |
| 6 | `type_code = 'Journal'` AND account = `[tax acct]` | `Taxes` | `[FILL IN tax acct]` |
| 99 | (fallback) | `Other / unclassified` | Flag for review if material |

Special rules / exclusions:
- **AR collections sub-split by project (§6d).** Within the `AR collections` bucket, each
  customer payment is attributed to the project(s) it collected against: **Production** (project
  class 1) itemized by project #/name; everything else (Service class, or no GL project) summed as
  **Service**, computed as the residual so Production + Service ties to the AR inflow.
- **AP disbursements sub-split by project (§6f).** Within the `AP disbursements` bucket, each
  vendor payment/check is attributed via the GL reporting project (bill payments → bill's project;
  direct checks → the check's own line project): **Production** itemized, everything else (overhead/
  SG&A, Service, no project) summed as **Other**, the residual so Production + Other ties.
- **UF ↔ Bank deposits net to zero** across the cash perimeter (both legs are cash accounts),
  so the reconciliation handles them automatically; bucketing them as `Internal transfer`
  keeps them out of the driver explanation.
- **Journal purpose via offsets (resolved 2026-07-01).** Journals that hit cash used to fall to
  `Other / unclassified`. `journal_offsets()` now pulls each cash-hitting journal's offsetting GL
  accounts, **netted per account** — which cancels the paired intercompany-clearing legs to zero
  automatically — leaving only the meaningful lines. When those are a single liability account plus
  an Interest Expense line it's a debt principal-and-interest payment, and the code hands the model a
  deterministic `suggested_purpose` (e.g. **"Debt Payment - SBA Loan (P&I)"**). The narrative model
  labels each journal from this detail instead of leaving it unclassified. Verified 7/1: JE14866
  −$59,607.52 = SBA Loan $36,402.08 + Interest $23,205.44; JE14906 −$32,804.25 = Loan - Other
  $23,314.65 + Interest $9,489.60. (Bucketing still tags them `Other / unclassified`; the *label* is
  what changed. Formalizing a deterministic `Debt service` bucket is a possible next step.)
- **Bank-interest journals** (small `Journal` entries to First Bank, e.g. JE14865 +$69.36 on
  2026-06-29) still fall to `Other / unclassified` — their only offset is an income account, so no
  debt label applies.
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
| Attachments | `cash_snap_<date>.json` (full summary), `cash_movements_<date>.csv` (cash line detail), `payments_by_project_<date>.xlsx` (cash in AND out by job — sheets: AR detail [payment×invoice + customer + job], AR by job, AR by customer, AP detail [payment/check + vendor + job], AP by job [Class column; Production then Service jobs, then Other/non-project], AP by job × vendor [Class/Job/Vendor; Production + Service sections, then Other]), `overdue_bills_<date>.xlsx` (every open bill due on/before the report date, most-overdue first — Bill#/Vendor/Bill date/Due date/Days overdue/Bill amount/Unpaid/Status); all openpyxl, values only |
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
AR: $5.7m
AP: $2.6m
Overdue bills: $70k (23)

Cash in: $64k
Production
  Stanly County EOC (1471CLT) - $58k
Service - $6k
Cash out: $61k
<itemized, top ~8 then "All others $X">
```
- **Cash** = Bank + Undeposited Funds. **AR** = AcctRec total. **AP** = AcctPay total (abs).
- **Overdue bills** — open vendor bills past due date (§6e), shown as `$total (count)`. The summary
  carries `unpaid_bills = {basis:"due on or before report date", asof, count, total}`; if null, render `[tbd]`. Full per-bill list ships in `overdue_bills_<date>.xlsx`.
- **Cash in** — split by project (§6d/§7): **Production** itemized as `Name (project#) - $Xk`;
  **Service** a single total. **Cash out** — by vendor, top ~8 then an "All others" rollup.

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
- **Cash perimeter = id allowlist {223 First Bank, 122 UF} (v3, 2026-07-01).** Only these two are
  transactional; Brokerage (312), First Bank of the Lake (311), and Petty Cash (224) are static and
  now **excluded** (previously the `Bank`-type filter pulled them in). A Deposit is a UF→Bank
  internal move and nets to zero. Override via `CASH_ACCOUNT_IDS`. Narrowing re-bases total cash
  (−~$8,943.69) → delete `state/cash_state.json` before the next run to re-bootstrap.
- **Undeposited Funds is id 122, type `OthCurrAsset` — not `UnDepFunds` (confirmed 2026-07-01).**
  The first live run (report_date 6/30) proved this: the `('Bank','UnDepFunds')` type filter
  matched no UF account, so UF was absent from balances AND movements, the ~4 payments into UF
  on 6/29 were missed, and the day's deposits surfaced as a phantom +$973k "Internal transfer"
  (only the First Bank leg was visible). Fix: select UF by explicit id (default 122), keep banks
  by type. Do not filter on `OthCurrAsset` — it would pull in prepaids and other OCA accounts.
- **Journals hitting cash** are now labeled by purpose via `journal_offsets()` (netted offset
  accounts; one liability + Interest Expense ⇒ debt P&I, e.g. "Debt Payment - SBA Loan (P&I)").
  Small bank-interest journals (only an income offset) remain `Other / unclassified`. See §7.
- `[FILL IN — month-end timing effects, holiday calendars, when deposits clear, etc.]`

---

## 11. Changelog

| Date | Change | By |
|---|---|---|
| 2026-06-29 | Initial skeleton + pipeline wired (single subsidiary, GitHub Actions, email delivery). | |
| 2026-06-29 | First successful end-to-end run. Verified sign convention (inflow positive); marked §6a/6b canonical; populated cash accounts and transaction types; recorded dedicated-API-role fix for `EntityOrRoleDisabled`; set threshold = 10000; noted Markdown-in-plain-text email issue and bank-interest-journal decision. | |
| 2026-06-30 | **v2.** Added Undeposited Funds to the cash perimeter (`Bank`+`UnDepFunds`); reclassified Deposit as an internal UF→Bank transfer; implemented stateful roll-forward reconciliation against the last reported balance with a committed `state/cash_state.json` audit ledger; bootstrap now reconstructs a clean prior and surfaces back-posts; moved cron to ~00:30 ET; split output into CFO VIEW (full-decimal audit) + CEO OUTPUT (rounded text-message style); added §6c AR/AP totals. Open TODOs: define unpaid-bills (overdue vs open); map cash-in to project codes; verify `createddate` column. | |
| 2026-07-01 | First live run (6/30) exposed the UF bug: Undeposited Funds is id 122 / type `OthCurrAsset`, not `UnDepFunds`, so the type filter matched nothing and UF was invisible everywhere (phantom +$973k "Internal transfer"). Fix: select banks by type + UF by explicit id (default 122, `CASH_EXTRA_ACCOUNT_IDS` overrides); added an optional `report_date` workflow_dispatch input. Requires deleting the stale Bank-only `state/cash_state.json` so the next run re-bootstraps with UF included. | |
| 2026-07-01 | **Cash-in by project + overdue bills.** Verified against live NetSuite: project = `job` record (# = entityid, name = companyname); GL project on invoice line = `custcol_r_it_reporting_project`; class = `custentity_r_it_class` (1 Production / 2 Service); payment→invoice link = `PreviousTransactionLineLink` (`foreignamount` = applied amount). Cash-in now splits Production (itemized by project) vs Service (residual); ties to AR inflow ($64,160.57 on 6/30). Overdue unpaid bills (§6e): `VendBill`, `duedate < report`, `foreignamountunpaid > 0` ($70,140.53 / 23 bills on 6/30). New code: `cash_in_by_project()`, `overdue_bills()`, `cash_in_project_split()`; CFO/CEO blocks and prompt updated. | |
| 2026-07-01 | Added `payments_received_<date>.xlsx` email attachment (`payments_workbook()`, openpyxl): Detail sheet (one row per payment×invoice with customer + job # / name + class), plus By-job (Production itemized, Service lumped) and By-customer summary sheets. `cash_in_by_project()` extended to also return payment date, customer, and invoice #. Added `openpyxl` to requirements. Values only (no formulas) — the CI runner has no spreadsheet engine to recalc. | |
| 2026-07-01 | **AP paid by project** (§6f). `ap_paid_by_project()` unions bill payments (VendPymt → bill via `PreviousTransactionLineLink.foreignamount` → bill reporting project) and direct checks (Check expense lines' reporting project); `ap_paid_project_split()` itemizes Production, Other = residual vs the AP-disbursements bucket. Verified 6/30 ties ($60,597.02, all overhead); Production path confirmed on recent 2026 bill payments (e.g. 1478ILM Galleria West $11,834.10). Added to summary (`ap_out`), CFO detail block, and the Excel (renamed `payments_by_project_<date>.xlsx`, now with AP detail + AP by job sheets). CEO block left vendor-itemized. Added `_window_clause()` helper. | |
| 2026-07-02 | **v3 — five fixes against the 7/1 run** (cash_snap.py commit `113ef71`). (1) **Perimeter → id allowlist** `{223,122}` via `CASH_ACCOUNT_IDS`; dropped Brokerage/FBotL/Petty (§2, §6a/6b). (2)+(3) **class_id string coercion** (`_class_int()`): REST returns `custentity_r_it_class` as `"1"`/`"2"`, so `== 1` collapsed all jobs to Service (blank Class column, no Production breakout, e.g. 1493ILM). Coerced at the source of both by-project queries (§4). (4) **Overdue boundary → inclusive `<=`** (was strict `<`, dropped bills due on the report date; 7/1 undercount $71k vs $153,922.83 / 33); `overdue_bills()` returns the full list; new `overdue_bills_<date>.xlsx` attachment (§6e, §8). (5) **Journal purpose** via `journal_offsets()` (net offsets; liability + Interest ⇒ `suggested_purpose` "Debt Payment - &lt;acct&gt; (P&I)") + prompt update (§7). Verified live via SuiteQL. Requires deleting `state/cash_state.json` before the next run (perimeter re-base). | |
| 2026-07-07 | **AP by job × vendor — Service section** (cash_snap.py commit `302bda48`). §6f previously itemized only Production-class job-tied AP by job×vendor; Service-class job-tied bills fell into the "Other" residual and were never broken out. `ap_paid_project_split()` now returns a parallel `service[]` section (job→vendor, mirroring `production[]`); **Other is the residual after Production AND Service**, i.e. genuinely non-project overhead only, and the AP total still ties. Surfaced in `ap_out` (summary JSON), the CFO narrative prompt, the deterministic `cfo_detail_block` ("Service (by job, then vendor)"), and the Excel workbook (the "AP by job" and "AP by job x vendor" sheets gain a **Class** column and a grouped Service section). Production breakout unchanged. Verified live: ~$25.5k across 26 Service job×vendor rows over the recent window (§6f, §8). | |
