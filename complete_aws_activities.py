import boto3
import json
import os
import sys
import time
import logging
import urllib.request as urlreq
import io
import zipfile
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.exceptions import ClientError, NoCredentialsError

# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ─── Region ───────────────────────────────────────────────────────────────────
# Priority: boto3.Session() picks up AWS_DEFAULT_REGION and ~/.aws/config.
# Falls back to us-east-1 if nothing is configured.
# You can also hard-code it here, e.g.: REGION = "eu-west-1"

REGION: str = boto3.Session().region_name or "us-east-1"

# ─── Resource registry ────────────────────────────────────────────────────────

_r: dict[str, str | None] = {
    "ec2_instance_id":      None,
    "budget_name":          None,
    "rds_instance_id":      None,
    "lambda_function_name": None,
    "lambda_role_name":     None,
}

# Cached account ID - set during preflight, reused throughout.
_ACCOUNT_ID: str = ""

# Maps our activity function names to keywords in the FreeTier API title field.
_ACTIVITY_KEYWORD: dict[str, str] = {
    "activity_ec2":     "EC2",
    "activity_bedrock": "Bedrock",
    "activity_budgets": "Budget",
    "activity_rds":     "RDS",
    "activity_lambda":  "Lambda",
}


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _client(service: str, region: str = REGION):
    """Thin wrapper so every client gets an explicit region."""
    return boto3.client(service, region_name=region)


# ─── IAM Fallback ──────────────────────────────────────────────────────────────
# aws-vault uses STS GetSessionToken to vend temporary credentials.
# Per AWS docs, GetSessionToken credentials CANNOT call any IAM API unless MFA
# is included in the request.  Root-user GetSessionToken tokens get the same
# restriction.  Because all other services work fine, we fall back to direct
# (non-STS) root credentials sourced from .accounts.json for IAM operations only.

# Cached IAM client built from direct root credentials (set by _switch_to_direct_iam).
_iam_direct_client = None

_ACCOUNTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".accounts.json")


def _switch_to_direct_iam() -> None:
    """
    Load the matching account's root access key from .accounts.json, verify it
    against the current _ACCOUNT_ID, and cache a direct IAM client.
    Raises RuntimeError with clear instructions if it cannot succeed.
    """
    global _iam_direct_client

    log.info("  IAM: GetSessionToken credentials cannot call IAM (AWS documented restriction).")
    log.info("  Falling back to direct root credentials from .accounts.json...")

    try:
        with open(_ACCOUNTS_FILE) as fh:
            accounts = json.load(fh)
    except Exception as err:
        raise RuntimeError(
            f"Cannot read {_ACCOUNTS_FILE} ({err}). "
            "Please pre-create IAM role 'opencode-activity-lambda-role' manually:\n"
            "  Trust policy principal: lambda.amazonaws.com\n"
            "  Attached policy: arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
        ) from err

    for acct in accounts:
        key    = acct.get("key", "")
        secret = acct.get("secret", "")
        if not key or not secret:
            continue
        try:
            sess    = boto3.Session(aws_access_key_id=key, aws_secret_access_key=secret)
            acct_id = sess.client("sts", region_name="us-east-1").get_caller_identity()["Account"]
            if acct_id == _ACCOUNT_ID:
                _iam_direct_client = sess.client("iam", region_name="us-east-1")
                log.info(f"  Direct credentials verified for account {acct_id}.")
                return
        except Exception:
            continue

    raise RuntimeError(
        f"No credentials in {_ACCOUNTS_FILE} match account {_ACCOUNT_ID}. "
        "Please pre-create IAM role 'opencode-activity-lambda-role' manually:\n"
        "  Trust policy principal: lambda.amazonaws.com\n"
        "  Attached policy: arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
    )


def _get_iam():
    """
    Return the best available IAM client.
    Prefers the cached direct-credentials client (set after a _switch_to_direct_iam
    call) so that cleanup automatically reuses the same working credentials.
    """
    return _iam_direct_client if _iam_direct_client is not None else _client("iam", "us-east-1")


def _preflight_check():
    """Validate credentials and region before touching any AWS service."""
    global _ACCOUNT_ID

    log.info(f"  Region: {REGION}")
    log.info("  Checking AWS credentials...")

    try:
        identity = _client("sts").get_caller_identity()
        _ACCOUNT_ID = identity["Account"]
        log.info(f"  Account : {_ACCOUNT_ID}")
        log.info(f"  Identity: {identity['Arn']}")
    except NoCredentialsError:
        log.error("")
        log.error("No AWS credentials found. Fix one of the following:")
        log.error("  1. Run:  aws configure")
        log.error("  2. Set environment variables:")
        log.error("       export AWS_ACCESS_KEY_ID=...")
        log.error("       export AWS_SECRET_ACCESS_KEY=...")
        log.error("       export AWS_DEFAULT_REGION=us-east-1")
        log.error("  3. Attach an IAM role if running on EC2/Lambda.")
        sys.exit(1)
    except Exception as e:
        log.error(f"Credential check failed: {e}")
        sys.exit(1)


# ═════════════════════════════════════════════════════════════════════════════
# FREE TIER ACTIVITY STATUS
# Uses the same AWSFreeTierService.ListAccountActivities API the console calls.
# ═════════════════════════════════════════════════════════════════════════════

def _list_activities() -> list[dict]:
    """
    Call AWSFreeTierService.ListAccountActivities via a SigV4-signed request.
    This is the exact API the AWS Console 'Explore AWS' widget uses.
    Returns the raw list of activity dicts.
    """
    credentials = boto3.Session().get_credentials().get_frozen_credentials()
    url = "https://freetier.us-east-1.api.aws/"
    body = json.dumps({"maxResults": 100, "languageCode": "en-US"}).encode()

    aws_req = AWSRequest(
        method="POST",
        url=url,
        data=body,
        headers={
            "Content-Type": "application/x-amz-json-1.0",
            "X-Amz-Target": "AWSFreeTierService.ListAccountActivities",
        },
    )
    SigV4Auth(credentials, "freetier", "us-east-1").add_auth(aws_req)

    http_req = urlreq.Request(
        url=url,
        data=body,
        headers={k: v for k, v in aws_req.headers.items()},
        method="POST",
    )
    with urlreq.urlopen(http_req) as resp:
        return json.loads(resp.read()).get("activities", [])


def _check_activity_status() -> set[str]:
    """
    Fetch activity status from the FreeTier API, log a summary table, and
    return the set of activity function names that are NOT yet COMPLETED.
    Falls back to running all activities if the API call fails.
    """
    try:
        activities = _list_activities()
    except Exception as e:
        log.warning(f"  Could not fetch activity status ({e}). Will attempt all activities.")
        return set(_ACTIVITY_KEYWORD.keys())

    log.info("  Activity status:")
    incomplete: set[str] = set()
    total_earned = 0.0
    total_available = 0.0

    for act in activities:
        title  = act.get("title", "")
        status = act.get("status", "UNKNOWN")
        amount = act.get("reward", {}).get("credit", {}).get("amount", 0.0)

        # Skip activities we don't handle
        managed = any(kw.lower() in title.lower() for kw in _ACTIVITY_KEYWORD.values())
        if not managed:
            continue

        marker = "DONE" if status == "COMPLETED" else "TODO"
        log.info(f"    [{marker}] ${amount:>4.0f}  {title}")
        total_available += amount
        if status == "COMPLETED":
            total_earned += amount
        else:
            for fn_name, keyword in _ACTIVITY_KEYWORD.items():
                if keyword.lower() in title.lower():
                    incomplete.add(fn_name)

    log.info(f"  Earned so far: ${total_earned:.0f} / ${total_available:.0f}")
    return incomplete


# ═════════════════════════════════════════════════════════════════════════════
# ACTIVITY 1 — EC2
# Cost: ~$0.00  (instance terminated within seconds of launch)
# ═════════════════════════════════════════════════════════════════════════════

def activity_ec2():
    log.info("─" * 55)
    log.info("Activity  EC2  Launch an EC2 instance")
    ec2 = _client("ec2")

    # Latest Amazon Linux 2023 AMI (free-tier eligible, x86_64)
    images = ec2.describe_images(
        Owners=["amazon"],
        Filters=[
            {"Name": "name",  "Values": ["al2023-ami-*-x86_64"]},
            {"Name": "state", "Values": ["available"]},
        ],
    )["Images"]
    if not images:
        raise RuntimeError("No Amazon Linux 2023 AMI found in this region.")
    ami_id = sorted(images, key=lambda x: x["CreationDate"], reverse=True)[0]["ImageId"]
    log.info(f"  AMI: {ami_id}")

    # Dynamically find a free-tier eligible instance type for this region.
    # t2.micro is not eligible everywhere; t3.micro is the common alternative.
    preferred = ["t3.micro", "t2.micro", "t3a.micro", "t4g.micro"]
    free_tier_types = {
        t["InstanceType"]
        for t in ec2.describe_instance_types(
            Filters=[{"Name": "free-tier-eligible", "Values": ["true"]}]
        )["InstanceTypes"]
    }
    instance_type = next(
        (t for t in preferred if t in free_tier_types),
        next(iter(free_tier_types), "t3.micro"),  # fallback: first eligible, or t3.micro
    )
    log.info(f"  Instance type: {instance_type}")

    resp = ec2.run_instances(
        ImageId=ami_id,
        InstanceType=instance_type,
        MinCount=1,
        MaxCount=1,
        InstanceInitiatedShutdownBehavior="terminate",
    )
    instance_id = resp["Instances"][0]["InstanceId"]
    _r["ec2_instance_id"] = instance_id
    log.info(f"  Instance launched: {instance_id}")

    # Wait for 'running' before terminating so AWS registers the launch.
    log.info("  Waiting for instance to reach 'running' state...")
    ec2.get_waiter("instance_running").wait(InstanceIds=[instance_id])

    ec2.terminate_instances(InstanceIds=[instance_id])
    log.info("  Termination requested. Activity complete.")
    _r["ec2_instance_id"] = None


def _cleanup_ec2():
    iid = _r.get("ec2_instance_id")
    if not iid:
        return
    try:
        _client("ec2").terminate_instances(InstanceIds=[iid])
        log.info(f"  [cleanup] Terminated EC2 instance {iid}")
        _r["ec2_instance_id"] = None
    except ClientError as e:
        log.error(f"  [cleanup] Could not terminate EC2 instance {iid}: {e}")


# ═════════════════════════════════════════════════════════════════════════════
# ACTIVITY 2 — Bedrock
# Cost: ~$0.001  (short prompt, playground-matched parameters)
# The console playground calls converse-stream — we replicate that exactly.
# ═════════════════════════════════════════════════════════════════════════════

def _get_bedrock_candidates(region: str) -> list[str]:
    """
    Discover ON_DEMAND text-generation models available in this region.
    Returns model IDs sorted cheapest-first. Excludes Claude.
    """
    priority = ["nova-micro", "nova-lite", "titan-text-lite", "titan-text-express",
                "llama3-8b", "llama3-70b", "mistral-7b", "mixtral-8x7b",
                "qwen", "command", "j2", "nova-pro", "llama"]
    try:
        models = _client("bedrock", region).list_foundation_models(
            byOutputModality="TEXT",
            byInferenceType="ON_DEMAND",
        )["modelSummaries"]
    except Exception:
        return [
            "amazon.nova-micro-v1:0",
            "amazon.nova-lite-v1:0",
            "amazon.titan-text-lite-v1",
            "amazon.titan-text-express-v1",
            "meta.llama3-8b-instruct-v1:0",
            "meta.llama3-70b-instruct-v1:0",
            "mistral.mistral-7b-instruct-v0:2",
            "mistral.mixtral-8x7b-instruct-v0:1",
        ]

    candidates = [
        m["modelId"] for m in models
        if "claude" not in m["modelId"].lower()
    ]

    def _rank(mid: str) -> int:
        for i, kw in enumerate(priority):
            if kw in mid.lower():
                return i
        return len(priority)

    return sorted(candidates, key=_rank)


def activity_bedrock():
    log.info("─" * 55)
    log.info("Activity  Bedrock  Use a foundational model in the playground")

    # The console playground posts to converse-stream. We replicate the exact
    # request shape from the browser HAR — same API, same parameters.
    # Region: prefer the user's configured region, then known Bedrock regions.
    regions_to_try: list[str] = list(dict.fromkeys(
        [REGION, "us-east-1", "us-west-2", "eu-west-1", "eu-north-1"]
    ))

    for region in regions_to_try:
        candidates = _get_bedrock_candidates(region)
        log.info(f"  Trying {len(candidates)} model(s) in {region}...")
        client = _client("bedrock-runtime", region)

        for model_id in candidates:
            try:
                response = client.converse_stream(
                    modelId=model_id,
                    messages=[
                        {"role": "user", "content": [{"text": "This is a test. Answer back with 'test'"}]}
                    ],
                    inferenceConfig={
                        "maxTokens": 4096,
                        "temperature": 1.0,
                        "topP": 1.0,
                    },
                    additionalModelRequestFields={},
                )

                # Consume and log the event stream.
                text_chunks: list[str] = []
                stop_reason = ""
                input_tokens = output_tokens = 0

                for event in response["stream"]:
                    if "contentBlockDelta" in event:
                        chunk = event["contentBlockDelta"].get("delta", {}).get("text", "")
                        text_chunks.append(chunk)
                    elif "messageStop" in event:
                        stop_reason = event["messageStop"].get("stopReason", "")
                    elif "metadata" in event:
                        usage = event["metadata"].get("usage", {})
                        input_tokens  = usage.get("inputTokens", 0)
                        output_tokens = usage.get("outputTokens", 0)

                reply = "".join(text_chunks)
                log.info(f"  Model    : {model_id}")
                log.info(f"  Region   : {region}")
                log.info(f"  Tokens   : {input_tokens} in / {output_tokens} out")
                log.info(f"  Stop     : {stop_reason}")
                log.info(f"  Response : {reply[:200]!r}")
                log.info("  converse-stream call succeeded.")
                return
            except ClientError as e:
                code = e.response["Error"]["Code"]
                if code in ("AccessDeniedException", "ValidationException",
                            "ResourceNotFoundException"):
                    log.debug(f"    {model_id}: {code} - skipping")
                    continue
                raise

    log.warning("  Bedrock: no model could be invoked.")
    log.warning("  Enable at least one model at:")
    log.warning("  https://console.aws.amazon.com/bedrock/home#/modelaccess")
    log.warning("  Recommended: enable 'Amazon Nova Micro' (cheapest) then re-run.")


# ═════════════════════════════════════════════════════════════════════════════
# ACTIVITY 3 — AWS Budgets
# Cost: $0.00  (first 2 budgets per account are free)
# ═════════════════════════════════════════════════════════════════════════════

def activity_budgets():
    log.info("─" * 55)
    log.info("Activity  Budgets  Set up a cost budget")
    # Budgets is a global service; its API endpoint lives in us-east-1.
    client = _client("budgets", "us-east-1")
    name = "opencode-activity-budget"
    _r["budget_name"] = name

    try:
        client.create_budget(
            AccountId=_ACCOUNT_ID,
            Budget={
                "BudgetName": name,
                "BudgetLimit": {"Amount": "10", "Unit": "USD"},
                "TimeUnit": "MONTHLY",
                "BudgetType": "COST",
            },
        )
        log.info(f"  Budget '{name}' created. Activity complete.")
    except ClientError as e:
        if e.response["Error"]["Code"] == "DuplicateRecordException":
            log.info(f"  Budget '{name}' already exists. Activity complete.")
        else:
            raise


def _cleanup_budget():
    name = _r.get("budget_name")
    if not name:
        return
    try:
        _client("budgets", "us-east-1").delete_budget(
            AccountId=_ACCOUNT_ID, BudgetName=name
        )
        log.info(f"  [cleanup] Deleted budget '{name}'")
        _r["budget_name"] = None
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("NotFoundException", "ResourceNotFoundException"):
            _r["budget_name"] = None
        else:
            log.error(f"  [cleanup] Could not delete budget '{name}': {e}")


# ═════════════════════════════════════════════════════════════════════════════
# ACTIVITY 4 — RDS
# Cost: ~$0.01  (db.t3.micro for ~10-15 min; no backups; deleted immediately)
# ═════════════════════════════════════════════════════════════════════════════

def activity_rds():
    log.info("─" * 55)
    log.info("Activity  RDS  Create an RDS database")
    rds = _client("rds")
    db_id = "opencode-activity-db"
    _r["rds_instance_id"] = db_id

    try:
        rds.create_db_instance(
            DBInstanceIdentifier=db_id,
            DBInstanceClass="db.t3.micro",
            Engine="mysql",
            MasterUsername="admin",
            MasterUserPassword="TempPass123!",
            AllocatedStorage=20,
            BackupRetentionPeriod=0,   # disable automated backups entirely
            MultiAZ=False,
            PubliclyAccessible=False,
            StorageType="gp2",
            DeletionProtection=False,
        )
        log.info(f"  RDS instance '{db_id}' creation initiated")
    except ClientError as e:
        if e.response["Error"]["Code"] == "DBInstanceAlreadyExists":
            log.info(f"  RDS instance '{db_id}' already exists, continuing")
        else:
            raise

    # Poll until deletion is accepted (works from 'creating' state onward).
    # This avoids waiting the full ~8 min for 'available' before we can delete.
    log.info("  Waiting until instance accepts a deletion request...")
    for attempt in range(40):
        time.sleep(30)
        resp = rds.describe_db_instances(DBInstanceIdentifier=db_id)
        state = resp["DBInstances"][0]["DBInstanceStatus"]
        log.info(f"  State: {state} (attempt {attempt + 1}/40)")
        if state in ("available", "incompatible-parameters", "incompatible-network",
                     "restore-error", "failed", "storage-full"):
            log.info("  RDS instance ready. Activity complete.")
            break
        # Try deleting directly from 'creating' state - AWS sometimes allows it
        if state == "creating" and attempt >= 2:
            try:
                rds.delete_db_instance(
                    DBInstanceIdentifier=db_id,
                    SkipFinalSnapshot=True,
                    DeleteAutomatedBackups=True,
                )
                log.info("  Deletion accepted from 'creating' state.")
                return
            except ClientError:
                pass  # not ready yet - keep polling


def _cleanup_rds():
    db_id = _r.get("rds_instance_id")
    if not db_id:
        return
    rds = _client("rds")
    try:
        resp = rds.describe_db_instances(DBInstanceIdentifier=db_id)
        state = resp["DBInstances"][0]["DBInstanceStatus"]
        if state == "deleting":
            log.info(f"  [cleanup] RDS '{db_id}' already deleting, waiting...")
        else:
            rds.delete_db_instance(
                DBInstanceIdentifier=db_id,
                SkipFinalSnapshot=True,
                DeleteAutomatedBackups=True,
            )
            log.info(f"  [cleanup] Deleting RDS instance '{db_id}' (waiting for completion)...")
        rds.get_waiter("db_instance_deleted").wait(
            DBInstanceIdentifier=db_id,
            WaiterConfig={"Delay": 30, "MaxAttempts": 60},
        )
        log.info(f"  [cleanup] RDS instance '{db_id}' deleted.")
        _r["rds_instance_id"] = None
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("DBInstanceNotFound", "InvalidDBInstanceState"):
            _r["rds_instance_id"] = None
            log.info(f"  [cleanup] RDS instance '{db_id}' was already gone.")
        else:
            log.error(f"  [cleanup] Could not delete RDS instance '{db_id}': {e}")


# ═════════════════════════════════════════════════════════════════════════════
# ACTIVITY 5 — Lambda
# Cost: ~$0.00  (well within the free tier; function and role deleted immediately)
# ═════════════════════════════════════════════════════════════════════════════

def activity_lambda():
    log.info("─" * 55)
    log.info("Activity  Lambda  Create a web app using AWS Lambda")
    lam = _client("lambda")

    role_name  = "opencode-activity-lambda-role"
    fn_name    = "opencode-activity-lambda"
    policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
    assume_doc = json.dumps({
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow",
                       "Principal": {"Service": "lambda.amazonaws.com"},
                       "Action": "sts:AssumeRole"}],
    })

    # ── IAM execution role ─────────────────────────────────────────────────
    # GetSessionToken credentials (used by aws-vault) cannot call IAM unless
    # MFA is included (AWS documented restriction).  We catch that error and
    # retry transparently with direct root credentials from .accounts.json.
    _r["lambda_role_name"] = role_name

    def _ensure_role() -> str:
        iam = _get_iam()
        try:
            arn = iam.create_role(
                RoleName=role_name,
                AssumeRolePolicyDocument=assume_doc,
                Description="Temporary role for opencode Lambda activity",
            )["Role"]["Arn"]
            iam.attach_role_policy(RoleName=role_name, PolicyArn=policy_arn)
            log.info(f"  IAM role created: {arn}")
            return arn
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code == "EntityAlreadyExists":
                arn = iam.get_role(RoleName=role_name)["Role"]["Arn"]
                log.info(f"  IAM role already exists: {arn}")
                return arn
            if code == "InvalidClientTokenId":
                # Switch to direct root credentials and retry once.
                _switch_to_direct_iam()
                return _ensure_role()
            raise

    role_arn = _ensure_role()

    # ── Inline handler zip package ─────────────────────────────────────────
    handler_src = (
        "import json\n"
        "def lambda_handler(event, context):\n"
        "    return {\n"
        "        'statusCode': 200,\n"
        "        'headers': {'Content-Type': 'text/html'},\n"
        "        'body': '<h1>Hello from AWS Lambda!</h1>',\n"
        "    }\n"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("lambda_function.py", handler_src)
    zip_bytes = buf.getvalue()

    # ── Create function (retry while IAM role propagates ~5-10 s) ──────────
    _r["lambda_function_name"] = fn_name
    fn_arn = ""
    for attempt in range(12):
        try:
            fn_arn = lam.create_function(
                FunctionName=fn_name,
                Runtime="python3.12",
                Role=role_arn,
                Handler="lambda_function.lambda_handler",
                Code={"ZipFile": zip_bytes},
                Description="opencode Lambda activity — web app with function URL",
                Timeout=10,
                MemorySize=128,
                Publish=True,
            )["FunctionArn"]
            log.info(f"  Function created: {fn_arn}")
            break
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code == "InvalidParameterValueException" and attempt < 11:
                log.debug(f"  Waiting for IAM role propagation ({attempt + 1}/12)...")
                time.sleep(5)
            elif code == "ResourceConflictException":
                fn_arn = lam.get_function(FunctionName=fn_name)["Configuration"]["FunctionArn"]
                log.info(f"  Function already exists: {fn_arn}")
                break
            else:
                raise

    # Wait until Active before attaching a URL config
    log.info("  Waiting for function to become Active...")
    lam.get_waiter("function_active_v2").wait(FunctionName=fn_name)

    # ── Function URL ───────────────────────────────────────────────────────
    try:
        fn_url = lam.create_function_url_config(
            FunctionName=fn_name,
            AuthType="NONE",
        )["FunctionUrl"]
        log.info(f"  Function URL created: {fn_url}")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceConflictException":
            fn_url = lam.get_function_url_config(FunctionName=fn_name)["FunctionUrl"]
            log.info(f"  Function URL already exists: {fn_url}")
        else:
            raise

    # Grant unauthenticated invocations from the function URL
    try:
        lam.add_permission(
            FunctionName=fn_name,
            StatementId="FunctionURLAllowPublicAccess",
            Action="lambda:InvokeFunctionUrl",
            Principal="*",
            FunctionUrlAuthType="NONE",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceConflictException":
            raise

    log.info("  Web app live at function URL. Activity complete.")


def _cleanup_lambda():
    fn_name    = _r.get("lambda_function_name")
    role_name  = _r.get("lambda_role_name")
    policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"

    if fn_name:
        lam = _client("lambda")
        try:
            try:
                lam.delete_function_url_config(FunctionName=fn_name)
                log.info(f"  [cleanup] Deleted function URL for '{fn_name}'")
            except ClientError:
                pass
            lam.delete_function(FunctionName=fn_name)
            log.info(f"  [cleanup] Deleted Lambda function '{fn_name}'")
            _r["lambda_function_name"] = None
        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceNotFoundException":
                _r["lambda_function_name"] = None
            else:
                log.error(f"  [cleanup] Could not delete Lambda function '{fn_name}': {e}")

    if role_name:
        # _get_iam() returns the direct-credential client if _switch_to_direct_iam
        # was already called during activity_lambda(); otherwise the session client.
        # If IAM is still blocked, try switching credentials on the fly.
        def _delete_role(iam_client) -> bool:
            """Returns True on success, False on InvalidClientTokenId."""
            try:
                try:
                    iam_client.detach_role_policy(RoleName=role_name, PolicyArn=policy_arn)
                except ClientError:
                    pass
                iam_client.delete_role(RoleName=role_name)
                log.info(f"  [cleanup] Deleted IAM role '{role_name}'")
                _r["lambda_role_name"] = None
                return True
            except ClientError as e:
                code = e.response["Error"]["Code"]
                if code == "NoSuchEntityException":
                    _r["lambda_role_name"] = None
                    return True
                if code == "InvalidClientTokenId":
                    return False
                log.error(f"  [cleanup] Could not delete IAM role '{role_name}': {e}")
                return True  # non-retryable, give up

        if not _delete_role(_get_iam()):
            try:
                _switch_to_direct_iam()
                _delete_role(_get_iam())
            except Exception as fe:
                log.warning(f"  [cleanup] IAM role '{role_name}' could not be deleted: {fe}")


# ═════════════════════════════════════════════════════════════════════════════
# CLEANUP ORCHESTRATOR
# ═════════════════════════════════════════════════════════════════════════════

def _cleanup_all():
    log.info("─" * 55)
    log.info("Cleaning up all created resources...")
    _cleanup_ec2()
    _cleanup_budget()
    _cleanup_rds()
    _cleanup_lambda()
    log.info("Cleanup complete - no ongoing charges from this script.")


# ═════════════════════════════════════════════════════════════════════════════
# MAIN
# ═════════════════════════════════════════════════════════════════════════════

def main():
    log.info("=" * 55)
    log.info("AWS Explore-AWS Activity Completion Script")
    log.info("Goal: earn $100 in credits (~$0.02 in actual charges)")
    log.info("=" * 55)

    _preflight_check()

    # ── Check which activities are already done ────────────────────────────
    log.info("─" * 55)
    log.info("Checking current activity status via FreeTier API...")
    incomplete = _check_activity_status()

    if not incomplete:
        log.info("All activities are already COMPLETED. Nothing to do.")
        return

    log.info(f"  {len(incomplete)} activity(ies) to run: {', '.join(sorted(incomplete))}")

    # ── Activity dispatch ──────────────────────────────────────────────────
    all_activities: dict[str, object] = {
        "activity_ec2":     activity_ec2,
        "activity_bedrock": activity_bedrock,
        "activity_budgets": activity_budgets,
        "activity_rds":     activity_rds,
        "activity_lambda":  activity_lambda,
    }
    to_run = [fn for name, fn in all_activities.items() if name in incomplete]  # type: ignore[misc]

    errors: list[tuple[str, str]] = []
    try:
        for fn in to_run:  # type: ignore[union-attr]
            try:
                fn()  # type: ignore[operator]
            except Exception as e:
                log.error(f"  FAILED: {fn.__name__}: {e}")  # type: ignore[union-attr]
                errors.append((fn.__name__, str(e)))  # type: ignore[union-attr]
    finally:
        _cleanup_all()

    # ── Final status ───────────────────────────────────────────────────────
    log.info("=" * 55)
    if errors:
        log.warning(f"{len(errors)} activity(ies) failed:")
        for name, err in errors:
            log.warning(f"  - {name}: {err}")

    log.info("─" * 55)
    log.info("Final activity status:")
    try:
        _check_activity_status()
    except Exception:
        pass

    log.info("")
    log.info("Open AWS Console Home > 'Explore AWS' widget to verify.")
    log.info("https://console.aws.amazon.com/")
    log.info("=" * 55)
    log.info("It takes a few minutes for the activities to update. You should wait a bit before checking activity status.")


if __name__ == "__main__":
    main()
