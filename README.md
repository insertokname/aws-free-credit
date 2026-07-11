# Vibecoded bullshit ahead!

This is a small personal helper tool mostly written by ai. Read source before use.

# AWS free credit

Completes the 5 AWS "Explore AWS" activities needed to earn **$100 in credits**, then
cleans up every resource it created. Estimated cost is about **$0.02**.

## Prerequisites

- Python 3.8+
- I recommend using [aws-vault](https://github.com/99designs/aws-vault)
- One or more new AWS accounts (the $100 credit offer is for new customers only)

> **Why direct root credentials?**
> aws-vault injects STS `GetSessionToken` temporary credentials at runtime.
> Per [AWS documentation](https://docs.aws.amazon.com/STS/latest/APIReference/API_GetSessionToken.html),
> these credentials cannot call IAM APIs unless MFA is included in the request.
> The Lambda activity needs to create an IAM execution role, so the script reads
> the raw root credentials from `.accounts.json` for IAM operations only,
> bypassing the STS restriction transparently.

## New account setup

Follow these steps each time you add a fresh AWS account.

### 1. Create the AWS account

Sign up at [https://aws.amazon.com/](https://aws.amazon.com/).
Use a unique email address (e.g. a `+tag` alias or a new inbox).

### 2. Generate a root access key

Log into the Console as root, then go to:

```
https://us-east-1.console.aws.amazon.com/iam/home#/security_credentials/access-key-wizard
```

Create a new access key and save the **Access Key ID** and **Secret Access Key**.

### 3. Add the account to `.accounts.json`

Append an entry to `.accounts.json` (create the file if it doesn't exist):

The format of `.accounts.json` looks like this:

```json
[
    {
        "email":    "example@email.com",
        "password": "xxxxxxxxxxxxxxxxxxxxxxxx",
        "key":      "xxxxxxxxxxxxxxxxxxxx",
        "secret":   "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    },
    {
        "email":    "example@email.com",
        "password": "xxxxxxxxxxxxxxxxxxxxxxxx",
        "key":      "xxxxxxxxxxxxxxxxxxxx",
        "secret":   "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    }
]
```

### 4. Add the account to aws-vault

```bash
aws-vault add your-new-account@example.com
```

### 5. Run the script

```bash
aws-vault exec your-new-account@example.com -- python3 complete_aws_activities.py
```

## Installation

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

Run the script through aws-vault, using the account's email as the profile name:

```bash
aws-vault exec your-new-account@example.com -- python3 complete_aws_activities.py
```

The script will:

1. Check which of the 5 activities are already completed via the FreeTier API
2. Run only the incomplete ones
3. Clean up every AWS resource it created
4. Print a final status table showing credits earned

The script takes **15–25 minutes** to complete, most of which is waiting for the RDS
instance to reach a state where it can be deleted.