import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import boto3

ACCOUNTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".accounts.json")
DATE_START = datetime(2020, 1, 1, tzinfo=timezone.utc)
DATE_END   = datetime(2030, 1, 1, tzinfo=timezone.utc)


def fetch_account_credits(acct: dict) -> dict:
    """Fetch credit totals for one account. Returns a result dict."""
    key    = acct.get("key", "")
    secret = acct.get("secret", "")
    email  = acct.get("email", "(unknown)")

    sess = boto3.Session(aws_access_key_id=key, aws_secret_access_key=secret)

    try:
        acct_id = sess.client("sts", region_name="us-east-1").get_caller_identity()["Account"]
    except Exception as exc:
        return {"email": email, "acct_id": "ERROR", "initial": 0.0, "remaining": 0.0, "error": str(exc)}

    try:
        billing = sess.client("billing", region_name="us-east-1")
        resp    = billing.get_credits(accountId=acct_id, startDate=DATE_START, endDate=DATE_END)
        credits = resp.get("credits", [])
        initial   = sum(float(c["initialAmount"]["currencyAmount"])   for c in credits)
        remaining = sum(float(c["remainingAmount"]["currencyAmount"]) for c in credits)
        return {"email": email, "acct_id": acct_id, "initial": initial, "remaining": remaining, "error": None}
    except Exception as exc:
        return {"email": email, "acct_id": acct_id, "initial": 0.0, "remaining": 0.0, "error": str(exc)}


def main() -> None:
    if not os.path.exists(ACCOUNTS_FILE):
        print(f"ERROR: accounts file not found: {ACCOUNTS_FILE}", file=sys.stderr)
        sys.exit(1)

    with open(ACCOUNTS_FILE) as fh:
        accounts = json.load(fh)

    # Query all accounts in parallel
    results_by_email: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=len(accounts)) as pool:
        futures = {pool.submit(fetch_account_credits, a): a["email"] for a in accounts}
        for fut in as_completed(futures):
            r = fut.result()
            results_by_email[r["email"]] = r

    # Print in original account order
    col_email = max(len(a["email"]) for a in accounts)
    col_email = max(col_email, 5)
    header = f"{'EMAIL':<{col_email}}  {'ACCOUNT ID':>12}  {'GRANTED':>9}  {'REMAINING':>9}"
    print(header)
    print("-" * len(header))

    grand_initial   = 0.0
    grand_remaining = 0.0

    for acct in accounts:
        r = results_by_email[acct["email"]]
        if r["error"]:
            print(f"{r['email']:<{col_email}}  {r['acct_id']:>12}  ERROR: {r['error']}")
            continue
        grand_initial   += r["initial"]
        grand_remaining += r["remaining"]
        print(
            f"{r['email']:<{col_email}}  {r['acct_id']:>12}"
            f"  ${r['initial']:>8.2f}  ${r['remaining']:>8.2f}"
        )

    print("-" * len(header))
    print(
        f"{'TOTAL':<{col_email}}  {'':>12}"
        f"  ${grand_initial:>8.2f}  ${grand_remaining:>8.2f}"
        f"  ({len(accounts)} accounts)"
    )


if __name__ == "__main__":
    main()
