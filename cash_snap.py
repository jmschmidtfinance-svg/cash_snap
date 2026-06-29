#!/usr/bin/env python3
"""
Daily cash snap pipeline (single subsidiary).

Flow:  SuiteQL balances + movements  ->  deterministic bucketizing + reconciliation
       ->  structured summary  ->  one Anthropic call for the CEO narrative  ->  email to you.

Design rule: CODE COMPUTES, THE LLM EXPLAINS. Every number is finished before the model
sees it. The model never categorizes or sums.

Config is entirely via environment variables (see README). No secrets in this file.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import os
import smtplib
import sys
from email.message import EmailMessage

import requests
from requests_oauthlib import OAuth1


# --------------------------------------------------------------------------------------
# Config (env vars only)
# --------------------------------------------------------------------------------------
NS_ACCOUNT_ID   = os.environ["NS_ACCOUNT_ID"]          # e.g. "1234567" or "1234567_SB1"
NS_CONSUMER_KEY = os.environ["NS_CONSUMER_KEY"]
NS_CONSUMER_SEC = os.environ["NS_CONSUMER_SECRET"]
NS_TOKEN_ID     = os.environ["NS_TOKEN_ID"]
NS_TOKEN_SEC    = os.environ["NS_TOKEN_SECRET"]

ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
KB_PATH         = os.environ.get("KB_PATH", "netsuite_knowledge_base.md")

SMTP_HOST = os.environ["SMTP_HOST"]
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ["SMTP_USER"]
SMTP_PASS = os.environ["SMTP_PASSWORD"]
MAIL_FROM = os.environ.get("MAIL_FROM", SMTP_USER)
MAIL_TO   = os.environ["MAIL_TO"]                       # your inbox; you forward to the CEO

# Any single line OR any bucket moving more than this gets called out. See KB §8.
THRESHOLD = float(os.environ.get("SNAP_THRESHOLD", "50000"))

# Optional hard override of the reporting date (ISO yyyy-mm-dd); otherwise auto.
REPORT_DATE_OVERRIDE = os.environ.get("REPORT_DATE")

# --- The one fact you MUST verify against the instance (KB §6 sign convention). ---------
# In transactionaccountingline, a cash INFLOW to a Bank account normally posts as a
# positive (debit) amount. If your instance returns inflows as negative, set this False.
# The reconciliation check below will FAIL LOUDLY if this is wrong, so trust it to catch you.
INFLOW_IS_POSITIVE = True

# --- Bucketizing config. Populate the account-id sets from KB §2 / §7 to light up the ---
# --- payroll/debt/tax rules. Until then, those lines fall through to "Other".         ---
PAYROLL_ACCOUNT_IDS: set[int] = set()      # bank accounts used purely to fund payroll
DEBT_ACCOUNT_ID: int | None = None         # GL account that journals hit for debt service
TAX_ACCOUNT_ID:  int | None = None         # GL account that journals hit for taxes

EPSILON = 0.01  # reconciliation tolerance in currency units


# --------------------------------------------------------------------------------------
# SuiteQL client
# --------------------------------------------------------------------------------------
def _rest_host() -> str:
    # Account 1234567_SB1 -> host 1234567-sb1.suitetalk.api.netsuite.com
    return f"{NS_ACCOUNT_ID.lower().replace('_', '-')}.suitetalk.api.netsuite.com"


def _auth() -> OAuth1:
    # NetSuite TBA: OAuth 1.0a, HMAC-SHA256, realm = account id (original form).
    return OAuth1(
        client_key=NS_CONSUMER_KEY,
        client_secret=NS_CONSUMER_SEC,
        resource_owner_key=NS_TOKEN_ID,
        resource_owner_secret=NS_TOKEN_SEC,
        signature_method="HMAC-SHA256",
        realm=NS_ACCOUNT_ID,
    )


def run_suiteql(sql: str, page_size: int = 1000) -> list[dict]:
    """Run a SuiteQL statement, following pagination, returning all rows as dicts."""
    url = f"https://{_rest_host()}/services/rest/query/v1/suiteql"
    headers = {"Content-Type": "application/json", "Prefer": "transient"}
    auth = _auth()
    rows: list[dict] = []
    offset = 0
    while True:
        resp = requests.post(
            url,
            params={"limit": page_size, "offset": offset},
            headers=headers,
            auth=auth,
            json={"q": sql},
            timeout=60,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"SuiteQL {resp.status_code}: {resp.text[:500]}")
        body = resp.json()
        rows.extend(body.get("items", []))
        if not body.get("hasMore"):
            break
        offset += page_size
    return rows


# --------------------------------------------------------------------------------------
# Dates
# --------------------------------------------------------------------------------------
def previous_business_day(d: dt.date) -> dt.date:
    """Most recent weekday strictly before d. (Holidays are a known gap — see KB §10.)"""
    d -= dt.timedelta(days=1)
    while d.weekday() >= 5:  # 5=Sat, 6=Sun
        d -= dt.timedelta(days=1)
    return d


def resolve_dates() -> tuple[dt.date, dt.date]:
    """report_date = day we report the close of; prior_date = the business day before it."""
    if REPORT_DATE_OVERRIDE:
        report = dt.date.fromisoformat(REPORT_DATE_OVERRIDE)
    else:
        # Default: report the most recent fully-closed business day. Adjust to your
        # convention by setting REPORT_DATE, or change this line.
        report = previous_business_day(dt.date.today())
    return report, previous_business_day(report)


# --------------------------------------------------------------------------------------
# Queries  (single subsidiary -> no subsidiary filter needed; KB §6)
# --------------------------------------------------------------------------------------
def balances_as_of(d: dt.date) -> list[dict]:
    sql = f"""
        SELECT a.id AS account_id, a.acctnumber, a.fullname,
               SUM(tal.amount) AS balance
        FROM   transactionaccountingline tal
        JOIN   transaction t ON t.id = tal.transaction
        JOIN   account a     ON a.id = tal.account
        WHERE  a.accttype = 'Bank'
          AND  tal.posting = 'T'
          AND  t.trandate <= TO_DATE('{d.isoformat()}', 'YYYY-MM-DD')
        GROUP BY a.id, a.acctnumber, a.fullname
    """
    return run_suiteql(sql)


def movements_between(prior: dt.date, report: dt.date) -> list[dict]:
    """All bank-account posting lines in (prior, report]. Spanning the full range (not just
    one day) is what makes the buckets reconcile to the balance delta across weekends."""
    sql = f"""
        SELECT t.id AS tran_id, t.tranid, t.trandate,
               t.type AS type_code, BUILTIN.DF(t.type) AS type_label,
               a.id AS account_id, a.acctnumber, a.fullname AS account_name,
               BUILTIN.DF(t.entity) AS entity_name, t.memo, tal.amount
        FROM   transactionaccountingline tal
        JOIN   transaction t ON t.id = tal.transaction
        JOIN   account a     ON a.id = tal.account
        WHERE  a.accttype = 'Bank'
          AND  tal.posting = 'T'
          AND  t.trandate >  TO_DATE('{prior.isoformat()}', 'YYYY-MM-DD')
          AND  t.trandate <= TO_DATE('{report.isoformat()}', 'YYYY-MM-DD')
        ORDER BY tal.amount
    """
    return run_suiteql(sql)


# --------------------------------------------------------------------------------------
# Bucketizing (KB §7, first match wins)
# --------------------------------------------------------------------------------------
def signed(amount: float) -> float:
    """Normalize to: positive = cash IN, negative = cash OUT."""
    a = float(amount)
    return a if INFLOW_IS_POSITIVE else -a


def classify(row: dict) -> str:
    tc = (row.get("type_code") or "").strip()
    acct = row.get("account_id")
    if tc == "Transfer":
        return "Internal transfer"
    if tc in ("CustPymt", "Deposit"):
        return "AR collections"
    if tc == "Paycheck" or (acct in PAYROLL_ACCOUNT_IDS):
        return "Payroll"
    if tc in ("VendPymt", "Check"):
        return "AP disbursements"
    if tc == "Journal":
        # NOTE: the movement query returns only the BANK leg, so we can't see a journal's
        # offsetting account here. Once DEBT/TAX account ids are known, classify journals
        # with a small secondary lookup of their non-bank lines. Until then -> Other.
        return "Other / unclassified"
    return "Other / unclassified"


# --------------------------------------------------------------------------------------
# Summary assembly (everything the LLM will need, already computed)
# --------------------------------------------------------------------------------------
def build_summary(report: dt.date, prior: dt.date,
                  bal_today: list[dict], bal_prior: list[dict],
                  movements: list[dict]) -> dict:
    by_acct_today = {r["account_id"]: float(r["balance"]) for r in bal_today}
    by_acct_prior = {r["account_id"]: float(r["balance"]) for r in bal_prior}
    names = {r["account_id"]: r["fullname"] for r in (bal_today + bal_prior)}

    total_today = sum(by_acct_today.values())
    total_prior = sum(by_acct_prior.values())
    net_change = total_today - total_prior

    # Net by bucket
    buckets: dict[str, float] = {}
    movement_net = 0.0
    for r in movements:
        amt = signed(r["amount"])
        movement_net += amt
        buckets[classify(r)] = buckets.get(classify(r), 0.0) + amt

    bucket_list = sorted(
        ({"bucket": b, "net": round(v, 2)} for b, v in buckets.items()),
        key=lambda x: abs(x["net"]), reverse=True,
    )

    # Reconciliation: signed movements over the window must equal the balance delta.
    recon_diff = round(movement_net - net_change, 2)
    recon_ok = abs(recon_diff) <= EPSILON

    # Largest single transactions (exclude internal transfer legs from "drivers")
    drivers = [r for r in movements if classify(r) != "Internal transfer"]
    drivers.sort(key=lambda r: abs(signed(r["amount"])), reverse=True)
    top_movers = [{
        "tranid": r.get("tranid"), "date": r.get("trandate"),
        "type": r.get("type_label"), "entity": r.get("entity_name"),
        "account": r.get("account_name"), "memo": r.get("memo"),
        "amount": round(signed(r["amount"]), 2), "bucket": classify(r),
    } for r in drivers[:5]]

    flags = []
    if not recon_ok:
        flags.append(f"RECONCILIATION MISMATCH: movements {movement_net:,.2f} vs balance "
                     f"delta {net_change:,.2f} (diff {recon_diff:,.2f}). Check sign "
                     f"convention / undeposited funds / posting filter before trusting.")
    for b in bucket_list:
        if abs(b["net"]) >= THRESHOLD and b["bucket"] != "Internal transfer":
            flags.append(f"Bucket over threshold: {b['bucket']} net {b['net']:,.2f}")
    for m in top_movers:
        if abs(m["amount"]) >= THRESHOLD:
            flags.append(f"Large single item: {m['type']} {m['amount']:,.2f} "
                         f"({m['entity'] or m['memo'] or m['tranid']})")
    other_net = buckets.get("Other / unclassified", 0.0)
    if abs(other_net) > EPSILON:
        flags.append(f"Unclassified cash movement of {other_net:,.2f} — needs a §7 rule.")
    for aid, bal in by_acct_today.items():
        if bal < 0:
            flags.append(f"Negative balance: {names.get(aid, aid)} at {bal:,.2f}")

    return {
        "report_date": report.isoformat(),
        "prior_date": prior.isoformat(),
        "currency_note": "amounts normalized so positive = cash in, negative = cash out",
        "total_cash_today": round(total_today, 2),
        "total_cash_prior": round(total_prior, 2),
        "net_change": round(net_change, 2),
        "by_bucket": bucket_list,
        "top_movers": top_movers,
        "account_balances": [
            {"account": names.get(aid, aid),
             "today": round(by_acct_today.get(aid, 0.0), 2),
             "prior": round(by_acct_prior.get(aid, 0.0), 2),
             "change": round(by_acct_today.get(aid, 0.0) - by_acct_prior.get(aid, 0.0), 2)}
            for aid in sorted(set(by_acct_today) | set(by_acct_prior),
                              key=lambda a: str(names.get(a, a)))
        ],
        "reconciles": recon_ok,
        "flags": flags,
    }


# --------------------------------------------------------------------------------------
# LLM narrative (the ONLY place the model is involved)
# --------------------------------------------------------------------------------------
def write_narrative(summary: dict) -> str:
    from anthropic import Anthropic  # picks up ANTHROPIC_API_KEY from env

    kb = ""
    if os.path.exists(KB_PATH):
        with open(KB_PATH, encoding="utf-8") as f:
            kb = f.read()

    system = (
        "You write a daily cash snap for a CEO. You receive FINISHED numbers and a knowledge "
        "base describing how this company's NetSuite cash data is structured. Do not "
        "recompute, re-categorize, or sum anything — the figures are authoritative. Explain "
        "in plain language WHY cash moved, naming the drivers from by_bucket and top_movers. "
        "Lead with total cash today vs prior and the net change. Surface every item in "
        "'flags' clearly. Keep it to a tight executive brief: a one-line headline, then a "
        "short paragraph or a few bullets. If 'reconciles' is false, say so prominently and "
        "treat the numbers as provisional."
    )
    user = (
        f"KNOWLEDGE BASE:\n{kb}\n\n"
        f"TODAY'S COMPUTED SUMMARY (JSON):\n{json.dumps(summary, indent=2, default=str)}"
    )

    client = Anthropic()
    resp = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=1500,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()


# --------------------------------------------------------------------------------------
# Delivery (email to you; you forward to the CEO)
# --------------------------------------------------------------------------------------
def movements_csv(movements: list[dict]) -> str:
    buf = io.StringIO()
    cols = ["tranid", "trandate", "type_label", "account_name",
            "entity_name", "memo", "amount"]
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in movements:
        w.writerow(r)
    return buf.getvalue()


def bucket_table(summary: dict) -> str:
    lines = [f"  {b['bucket']:<22} {b['net']:>16,.2f}" for b in summary["by_bucket"]]
    return "\n".join(lines)


def send_email(summary: dict, narrative: str, movements: list[dict]) -> None:
    msg = EmailMessage()
    msg["Subject"] = f"Cash snap — {summary['report_date']}"
    msg["From"] = MAIL_FROM
    msg["To"] = MAIL_TO

    body = (
        f"{narrative}\n\n"
        f"{'-' * 48}\n"
        f"Total cash {summary['report_date']}: {summary['total_cash_today']:,.2f}\n"
        f"Prior      {summary['prior_date']}: {summary['total_cash_prior']:,.2f}\n"
        f"Net change:                {summary['net_change']:,.2f}\n\n"
        f"By bucket:\n{bucket_table(summary)}\n\n"
        f"Reconciles to balance delta: {summary['reconciles']}\n"
        f"(full numbers in the attached JSON; line detail in the CSV)\n"
    )
    msg.set_content(body)
    msg.add_attachment(json.dumps(summary, indent=2, default=str).encode(),
                       maintype="application", subtype="json",
                       filename=f"cash_snap_{summary['report_date']}.json")
    msg.add_attachment(movements_csv(movements).encode(),
                       maintype="text", subtype="csv",
                       filename=f"cash_movements_{summary['report_date']}.csv")
    _smtp_send(msg)


def send_alert(error: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = "Cash snap FAILED"
    msg["From"] = MAIL_FROM
    msg["To"] = MAIL_TO
    msg.set_content(f"The cash snap pipeline failed and produced no report.\n\n{error}")
    try:
        _smtp_send(msg)
    except Exception:
        pass  # alert is best-effort; the non-zero exit still surfaces in the scheduler log


def _smtp_send(msg: EmailMessage) -> None:
    if SMTP_PORT == 465:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as s:
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
    else:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)


# --------------------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------------------
def main() -> int:
    try:
        report, prior = resolve_dates()
        bal_today = balances_as_of(report)
        bal_prior = balances_as_of(prior)
        movements = movements_between(prior, report)

        if not bal_today:
            raise RuntimeError("No bank-account balances returned — check the query, the "
                               "role's permissions, or that accttype='Bank' is correct.")

        summary = build_summary(report, prior, bal_today, bal_prior, movements)
        narrative = write_narrative(summary)
        send_email(summary, narrative, movements)
        print(f"Cash snap sent for {report.isoformat()} "
              f"(reconciles={summary['reconciles']}, flags={len(summary['flags'])}).")
        return 0
    except Exception as exc:  # noqa: BLE001 — top-level guard for a scheduled job
        send_alert(f"{type(exc).__name__}: {exc}")
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
