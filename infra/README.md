# Cloud collector (Phase 1.5)

Moves collection off the laptop. No sleeping machine, no changing wifi, no
corporate firewall in the path — and matches age out of the PUBG API after
**14 days**, so uptime is the difference between having data and not.

## What it builds

| Resource | Why |
|---|---|
| S3 bucket | Raw lake. Same `raw/{kind}/shard=/dt=/` layout the local collector writes, so one dbt source can read either |
| DynamoDB table | The ledger. Lambda's filesystem is ephemeral and unshared, so SQLite can't work here |
| Lambda (arm64, python3.13) | The collector. **Zero dependencies** — stdlib only, so no layer, no container, no cross-compiled wheels |
| EventBridge rule | Runs it every 2 hours |
| SSM SecureString | The API key. Standard parameters are free; Secrets Manager is $0.40/month for the same job |
| CloudWatch log group | 14-day retention, set explicitly — logs default to *never* expiring, which is a slow silent cost |
| Budget + alarm | Cost alerts at 25%, 80% and forecast-100%; an alarm on Lambda errors |

## Prerequisites — yours, not mine

I can't create an AWS account or handle credentials. You'll need to:

1. **Create a personal AWS account** with a personal email and payment method.
   Not work SSO.
2. **Create an IAM user or Identity Center user**, then configure a *named*
   profile:

   ```bash
   aws configure --profile pubg-personal
   ```

   The profile name matters. `var.aws_profile` has no default precisely so a
   stray `default` profile — possibly a work account — can never be picked up by
   accident.

## Deploy

```bash
cp terraform.tfvars.example terraform.tfvars   # then edit it
tofu init
tofu plan
tofu apply
```

Then write the real API key. It never lands in git and never lands in state:

```bash
aws ssm put-parameter --profile pubg-personal --region us-east-1 \
  --name /pubg-analytics/pubg_api_key --type SecureString \
  --overwrite --value 'YOUR_KEY'
```

Verify with one manual invocation:

```bash
aws lambda invoke --profile pubg-personal --region us-east-1 \
  --function-name pubg-analytics-collector /dev/stdout
```

`tofu` and `terraform` are interchangeable here — the HCL is plain and uses no
provider-specific extensions. OpenTofu is used locally only because HashiCorp
moved Terraform out of Homebrew core under the BUSL licence.

## Cost

Everything here is either free-tier or fractions of a cent:

- **Lambda** — 12 runs/day × ~10 min at 1024 MB. Comfortably inside the free tier;
  a few cents a month beyond it.
- **DynamoDB** — on-demand, a few thousand writes a day. Pennies.
- **S3** — about $0.023/GB/month in Standard, dropping to ~$0.004 after the
  Glacier IR transition at 90 days. **This is the line item that grows**: at
  ~1.4 MB/match and 800 matches per run, roughly 13 GB/month.
- **Everything else** — SSM standard parameters, EventBridge rules, budgets, and
  one CloudWatch alarm are free or near enough.

Realistically **under $2/month** for the first few months, trending up with S3 as
the lake grows.

### Deliberately not used

- **NAT Gateway** — ~$32/month to do nothing. The function needs no VPC, so it
  has none. This is the classic surprise AWS bill.
- **Secrets Manager** — $0.40/secret/month for what Parameter Store does free.
- **Persistent EMR / MWAA / Redshift** — hundreds per month. Nothing here needs them.

**`tofu destroy` stops all charges.** That is the real reason this is in code
rather than clicked together in the console.

## Known warning

`tofu validate` reports `hash_key is deprecated. Use key_schema instead` on the
DynamoDB table. As of AWS provider 6.x there is **no top-level `key_schema` block**
on `aws_dynamodb_table` — only inside `global_secondary_index` — so the suggested
replacement isn't available at the table level yet. The current syntax is valid
and correct; migrating half of it would just make the file inconsistent.

## The hot-partition caveat

`status_index` is keyed on `status`, which has four values, so writes concentrate
on the `pending` partition. That is a genuine anti-pattern and it is fine at tens
of thousands of items. At millions the key would need a shard suffix
(`pending#07`) with fan-out reads. Noted here rather than discovered later.
