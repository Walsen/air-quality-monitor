# Advisor deployment — Amazon Bedrock AgentCore Runtime

A self-contained AWS CDK (Python) project that deploys the AI Advisor Agent to Amazon Bedrock
AgentCore Runtime. It builds the advisor's `linux/arm64` image, pushes it to the CDK bootstrap ECR
repository, and creates an `AWS::BedrockAgentCore::Runtime` serving `POST /invocations` and
`GET /ping` behind a Cognito JWT authorizer.

## The synth gate is offline; the build and deploy are not

- `just synth-advisor-infra` and the tests under `tests/` run with **no AWS account and no Docker
  daemon**. `DockerImageAsset` builds the image at *deploy*, not synth, so synthesis only computes
  the asset hash and emits CloudFormation — which is why these belong in the offline suite.
- `cdk deploy` builds the arm64 image (needs a Docker/Buildx engine) and touches the account. That
  is a fenced, credentialed step, never part of the offline suite.

## Operator prerequisites — NOT in the CDK, and they cannot be

A `cdk deploy` fails without all three. None can be done by the deploying agent.

1. **Grant the deploy identity and bootstrap the environment**, from an admin identity:
   ```bash
   aws iam attach-role-policy --role-name <deploy-role> \
     --policy-arn arn:aws:iam::aws:policy/AWSCloudFormationFullAccess
   cdk bootstrap aws://<account>/us-east-1     # needs iam:* once; creates the CDKToolkit stack
   ```
   Without the bootstrap stack a deploy fails with `SSM parameter /cdk-bootstrap/.../version not
   found`.

2. **Enable Anthropic model access**, once per account, in the Bedrock console: Model access →
   Claude Sonnet 4.6 → submit the use-case form. There is no API the CDK can call for that form.

3. **Keep the `us.` inference-profile model id** — `us.anthropic.claude-sonnet-4-6`, not the bare
   `anthropic.claude-sonnet-4-6`, which has no in-region on-demand support in any US region and
   errors at invoke. It is the default in `app.py`.

## Deploy

```bash
export CDK_DEPLOY_ACCOUNT=<account>
export CDK_DEPLOY_REGION=us-east-1
export AQM_ADVISOR_MODEL_ID=us.anthropic.claude-sonnet-4-6
export AQM_COGNITO_CLIENT_ID=<the same app client Service 2 validates>
export AQM_COGNITO_DISCOVERY_URL=https://cognito-idp.us-east-1.amazonaws.com/<pool>/.well-known/openid-configuration
npx cdk deploy
```

The Cognito client id is deliberately the same one Service 2 validates against, so a token accepted
inbound by AgentCore is the token Service 2 accepts on receipt — one identity, not two that drift.

## What the stack creates, and does not

- **Creates:** the AgentCore runtime, its execution role (trust principal
  `bedrock-agentcore.amazonaws.com` with `aws:SourceAccount`/`aws:SourceArn` confused-deputy guards,
  and least-privilege grants for ECR pull, CloudWatch logs, X-Ray, and `bedrock:InvokeModel` on the
  configured profile), and the container image asset.
- **Does not create:** the Cognito user pool, the DynamoDB audit table, or Service 2. The advisor
  uses the in-memory audit store until `AdvisorConfig` gains a table-name field — see the advisor's
  `tasks.md`, task 21.1.
