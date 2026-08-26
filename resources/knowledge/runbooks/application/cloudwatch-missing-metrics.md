---
id: "cloudwatch-missing-metrics"
title: "CloudWatch missing metrics/logs: agent, IAM, namespace, retention, or INSUFFICIENT_DATA"
domain: application
service: aws-cloudwatch
symptom_class: [service_unavailable, data_loss]
severity: high
scope: global
version: "1.0.1"
last_updated: "2026-08-26"
verified_by: "kb-researcher"
status: draft
tags: [cwagent, insufficient-data, access-denied, namespace-mismatch, log-retention, putmetricdata]
difficulty: intermediate
---

## Symptom Recognition

- Expected custom metrics absent from the CloudWatch console; `aws cloudwatch list-metrics --namespace CWAgent` returns an empty `Metrics` array.
- A CloudWatch alarm sits in `INSUFFICIENT_DATA` (`StateValue`) instead of `OK`/`ALARM`.
- No new log events arriving: a log stream's `lastIngestionTime` is stale or the log group/stream does not exist.
- Agent log shows `AccessDenied` / `AccessDeniedException` on `cloudwatch:PutMetricData`, `logs:CreateLogStream`, or `logs:PutLogEvents`.
- `amazon-cloudwatch-agent-ctl -a status` reports `"status": "stopped"` or the service is not running.
- Metrics appear under the default `CWAgent` namespace (or a custom one) but a dashboard/alarm is querying a different namespace or dimension set.

## Applicability

- Amazon CloudWatch agent (unified agent) on EC2, on-prem Linux/Windows, or ECS/EKS; AWS CLI v2.
- Required access: IAM principal able to read CloudWatch/Logs (`cloudwatch:ListMetrics`, `cloudwatch:GetMetricData`, `logs:Describe*`); on the host, the instance role/credentials the agent uses for ingestion.
- Tools: `amazon-cloudwatch-agent-ctl`, AWS CLI (`aws cloudwatch`, `aws logs`, `aws sts`), shell access to the host running the agent.
- Linux config dir: `/opt/aws/amazon-cloudwatch-agent/etc/`; agent log: `/opt/aws/amazon-cloudwatch-agent/logs/amazon-cloudwatch-agent.log`.

## Diagnostic Steps

### Step 1: Check the agent status and effective config

```bash
sudo /opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl -a status
sudo cat /opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json
```

Expected output: a JSON status block with `"status": "running"` and a `version`. The config dump shows the `metrics` section (note the `namespace`, default `CWAgent`) and the `logs.logs_collected.files.collect_list` entries (`log_group_name`, `log_stream_name`).

### Step 2: Inspect the agent log for ingestion errors

```bash
sudo tail -n 100 /opt/aws/amazon-cloudwatch-agent/logs/amazon-cloudwatch-agent.log
sudo grep -iE 'AccessDenied|Throttl|error|no credentials' /opt/aws/amazon-cloudwatch-agent/logs/amazon-cloudwatch-agent.log
```

Expected output: routine lines show successful publishes. Failures surface as `AccessDenied`/`AccessDeniedException` on `PutMetricData`/`CreateLogStream`/`PutLogEvents`, `NoCredentialProviders`, or `ThrottlingException`.

### Step 3: Verify the metric is actually present in CloudWatch

```bash
aws cloudwatch list-metrics --namespace CWAgent
aws cloudwatch list-metrics --namespace CWAgent --metric-name mem_used_percent
```

Expected output: a `Metrics` array listing `MetricName`, `Namespace`, and `Dimensions` (e.g. `InstanceId`, `host`). An empty array means nothing has been published under that namespace.

### Step 4: Verify log group, stream, and last ingestion

```bash
aws logs describe-log-groups --log-group-name-prefix /my/app
aws logs describe-log-streams --log-group-name /my/app/messages \
  --order-by LastEventTime --descending --max-items 1
```

Expected output: `describe-log-groups` shows the group with its `retentionInDays`. `describe-log-streams` shows the newest stream with a recent `lastIngestionTime` (epoch ms). A missing group/stream, or a `lastIngestionTime` far in the past, signals ingestion has stopped.

### Step 5: Confirm the credentials/role the agent uses

```bash
aws sts get-caller-identity
aws iam list-attached-role-policies --role-name <instance-role-name>
```

Expected output: `get-caller-identity` returns the `Arn` of the identity the host is using. `list-attached-role-policies` should list `CloudWatchAgentServerPolicy` (which grants `cloudwatch:PutMetricData`, `logs:CreateLogGroup`, `logs:CreateLogStream`, `logs:PutLogEvents`, `logs:DescribeLogStreams`, `logs:DescribeLogGroups`).

### Step 6: Inspect the alarm definition for missing-data handling

```bash
aws cloudwatch describe-alarms --alarm-names <alarm-name> \
  --query 'MetricAlarms[0].{Namespace:Namespace,MetricName:MetricName,Dimensions:Dimensions,Period:Period,TreatMissingData:TreatMissingData,State:StateValue}'
```

Expected output: the alarm's `Namespace`/`MetricName`/`Dimensions`/`Period`, its `TreatMissingData` (`missing`|`breaching`|`notBreaching`|`ignore`), and `StateValue`. Compare these exactly against what Step 3 reported as published.

## Causes

### Cause A: CloudWatch agent is not running (stopped, crashed, or never started)

**Statement:** The CloudWatch agent process is stopped, crashed on a bad config, or was never started, so no metrics or log events are published to CloudWatch at all.
**Chain:**
- root: the `amazon-cloudwatch-agent` service is not running (stopped/crashed/never started)
- s1: no `PutMetricData` / `PutLogEvents` calls are made from the host
- D: expected metrics and log streams are absent in CloudWatch
**Indicators:**
- root: [Step 1] `amazon-cloudwatch-agent-ctl -a status` reports `"status": "stopped"`
- s1: [Step 2] agent log is stale or shows a startup/config-parse failure rather than publish lines
- D: [Step 3] `list-metrics` returns an empty `Metrics` array for the namespace
**Interventions:**
- **remediation** (root): fetch (or fix) the config and start the agent with it.

  ```bash
  sudo /opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl \
    -a fetch-config -m ec2 -s \
    -c file:/opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json
  ```

  **Verification:** re-run Step 1 and confirm `"status": "running"`; after one collection interval, Step 3 lists the expected metrics.

### Cause B: IAM role/credentials lack CloudWatch publish permissions

**Statement:** The IAM role or credentials the agent uses do not grant the publish permissions (`cloudwatch:PutMetricData`, `logs:CreateLogStream`/`CreateLogGroup`/`PutLogEvents`), so every ingestion call is rejected with AccessDenied even though the agent is running.
**Chain:**
- root: the instance role/credentials are missing `CloudWatchAgentServerPolicy` (or equivalent publish permissions)
- s1: each `PutMetricData` / `PutLogEvents` call is rejected with `AccessDenied`
- D: metrics and log events never reach CloudWatch
**Indicators:**
- root: [Step 5] `list-attached-role-policies` output does not include `CloudWatchAgentServerPolicy`
- s1: [Step 2] agent log contains `AccessDenied` / `AccessDeniedException` on `PutMetricData` or `CreateLogStream`
- D: [Step 3] published namespace stays empty in `list-metrics`
**Interventions:**
- **remediation** (root): attach the managed publish policy to the role the agent uses.

  ```bash
  aws iam attach-role-policy --role-name <instance-role-name> \
    --policy-arn arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy
  ```

  **Verification:** re-run Step 5 to confirm the policy is attached, then check Step 2 for `AccessDenied` clearing and Step 3 for new metrics within one interval.

### Cause C: Namespace or dimension mismatch between publisher and consumer

**Statement:** The agent publishes metrics correctly but under a different `namespace` or dimension set (e.g. custom namespace, or `append_dimensions` differences) than the dashboard/alarm queries, so the data exists but the query finds nothing.
**Chain:**
- root: the publishing config's `namespace`/dimensions differ from what the alarm or query references (default is `CWAgent`)
- s1: the metric exists under the published namespace/dimensions but not under the queried ones
- D: the dashboard/alarm shows no data and the alarm reads INSUFFICIENT_DATA
**Indicators:**
- root: [Step 1] the config `metrics.namespace` (or `append_dimensions`) differs from the alarm's `Namespace`/`Dimensions` in Step 6
- s1: [Step 3] `list-metrics` returns the metric under the *published* namespace but with different dimensions than queried
- D: [Step 6] alarm `Namespace`/`Dimensions` do not match the published values; `State` is `INSUFFICIENT_DATA`
**Interventions:**
- **remediation** (root): point the alarm/query at the exact namespace and dimensions the agent publishes (re-create the alarm with matching `--namespace`/`--dimensions`).

  ```bash
  aws cloudwatch put-metric-alarm --alarm-name <alarm-name> \
    --namespace CWAgent --metric-name mem_used_percent \
    --dimensions Name=InstanceId,Value=<instance-id> \
    --statistic Average --period 300 --evaluation-periods 1 \
    --threshold 90 --comparison-operator GreaterThanThreshold
  ```

  **Verification:** re-run Step 6 and confirm `Namespace`/`Dimensions` now match Step 3's published values and the alarm leaves `INSUFFICIENT_DATA`.

### Cause D: Log events expired by an aggressive log-group retention setting

**Statement:** The log group's `retentionInDays` is set so low that recently ingested events are already marked for deletion, so log events that were ingested no longer appear when queried.
**Chain:**
- root: the log group's `retentionInDays` is shorter than the window being queried
- s1: events older than the retention window are marked for deletion and removed
- D: expected log events are absent even though the stream exists and ingested them
**Indicators:**
- root: [Step 4] `describe-log-groups` shows a small `retentionInDays` (e.g. 1) on the group
- s1: [Step 4] the stream's `lastIngestionTime` is recent but events beyond the window are gone
- D: [Step 4] queries over the expired window return no events for an existing stream
**Interventions:**
- **remediation** (root): raise retention to cover the needed window (valid values include 1, 3, 5, 7, 14, 30, 60, 90, 365, ... 3653 days).

  ```bash
  aws logs put-retention-policy \
    --log-group-name /my/app/messages --retention-in-days 30
  ```

  **Verification:** re-run Step 4 and confirm `retentionInDays` is the new value; events ingested from now on are retained for the full window.
- **mitigation** (s1): for events still within the (short) window, query and export them before they expire.

  ```bash
  aws logs filter-log-events --log-group-name /my/app/messages \
    --start-time $(( ($(date +%s) - 3600) * 1000 )) > export.json
  ```

  **Risk:** only captures events still present; nothing recovers events already deleted. **Duration:** one-off, run before the retention boundary passes. **Verification:** `export.json` contains the events of interest.

### Cause E: Alarm in INSUFFICIENT_DATA because the metric reports sparsely

**Statement:** The monitored resource emits the metric only when active (e.g. a Lambda or idle EC2 metric), so during inactivity there are no data points and the alarm goes INSUFFICIENT_DATA while waiting for data rather than because of a config error.
**Chain:**
- root: the source publishes the metric only when active, so the evaluation range has fewer real data points than `evaluation-periods`
- s1: CloudWatch has missing data points and applies the `TreatMissingData` rule
- D: the alarm sits in `INSUFFICIENT_DATA` despite a healthy resource
**Indicators:**
- root: [Step 3] `list-metrics` confirms the metric exists but `get-metric-statistics` (same dimensions) returns gaps during idle windows
- s1: [Step 6] alarm `TreatMissingData` is `missing` and `State` is `INSUFFICIENT_DATA`
- D: [Symptom] alarm `StateValue` is `INSUFFICIENT_DATA` while the resource is confirmed healthy
**Interventions:**
- **defensive_fix** (s1): set `treat-missing-data` deliberately so sparse reporting does not page — `notBreaching` for "idle is fine", or `missing` so the alarm only fires on real ALARM data.

  ```bash
  aws cloudwatch put-metric-alarm --alarm-name <alarm-name> \
    --namespace AWS/Lambda --metric-name ProvisionedConcurrencyUtilization \
    --statistic Maximum --period 60 --evaluation-periods 3 \
    --threshold 0.9 --comparison-operator GreaterThanThreshold \
    --treat-missing-data notBreaching
  ```

  **Verification:** re-run Step 6 and confirm `TreatMissingData` is the chosen value and the alarm leaves `INSUFFICIENT_DATA` during idle windows.

### Cause Z: Unidentified

**Statement:** Missing metrics or logs are confirmed but none of the known roots above is established by the diagnostics gathered.
**Indicators:**
- [Default]
**Interventions:**
- **mitigation** (D): capture a full diagnostic snapshot and escalate to the CloudWatch/observability SME on call.

  ```bash
  sudo /opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl -a status > cw-snap-status.json
  sudo tail -n 500 /opt/aws/amazon-cloudwatch-agent/logs/amazon-cloudwatch-agent.log > cw-snap-agent.log
  aws sts get-caller-identity > cw-snap-identity.json
  aws cloudwatch list-metrics --namespace CWAgent > cw-snap-metrics.json
  aws logs describe-log-groups > cw-snap-loggroups.json
  ```

  **Risk:** read-only; no impact to the running agent. **Duration:** one-off during the incident window. **Verification:** confirm all five snapshot files are written, then hand off for analysis.

## Prevention

- Alarm on the agent's own heartbeat: publish/scrape a liveness metric and alert when `amazon-cloudwatch-agent` stops, so a silent agent is caught before data gaps accrue.
- Attach `CloudWatchAgentServerPolicy` via the instance role in your launch template / ASG / IaC so new hosts can never start without publish permissions.
- Standardize the metric `namespace` and `append_dimensions` in a shared agent config so dashboards and alarms always reference the same namespace/dimensions.
- Set log-group `retentionInDays` explicitly (and consistently) in IaC; never leave a production log group at 1-day retention by accident.
- Choose `TreatMissingData` deliberately for every alarm: `missing` for stop/terminate/reboot/recover actions, `notBreaching` for sparsely reported metrics, and trigger only on the `ALARM` state.
- Periodically reconcile `aws cloudwatch list-metrics` output against expected metrics in a synthetic check to detect publish gaps early.

## Sources

- [Troubleshooting the CloudWatch agent](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/troubleshooting-CloudWatch-Agent.html) — `amazon-cloudwatch-agent-ctl -a status`, the default `CWAgent` namespace, config dir `/opt/aws/amazon-cloudwatch-agent/etc/...`, and the agent log file location/rotation.
- [Troubleshoot CloudWatch logs and metrics access errors](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch-IM-troubleshooting.html) — AccessDenied behavior for missing publish permissions.
- [CloudWatchAgentServerPolicy (AWS managed policy)](https://docs.aws.amazon.com/aws-managed-policy/latest/reference/CloudWatchAgentServerPolicy.html) — exact actions granted (`PutMetricData`, `CreateLogGroup`/`CreateLogStream`/`PutLogEvents`, `DescribeLogStreams`/`DescribeLogGroups`).
- [CloudWatch agent prerequisites — IAM roles and users](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/prerequisites.html) — creating the agent's EC2 role with `CloudWatchAgentServerPolicy` and attaching that role to the instance.
- [Configuring how CloudWatch alarms treat missing data](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/alarms-and-missing-data.html) — `TreatMissingData` values (`breaching`/`notBreaching`/`ignore`/`missing`), INSUFFICIENT_DATA evaluation behavior, metrics that report only intermittently by design, and the recommendation to treat missing data as `missing` for alarms taking stop/terminate/reboot/recover actions.
- [Using Amazon CloudWatch alarms](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch_Alarms.html) — alarm states (`OK`/`ALARM`/`INSUFFICIENT_DATA`), `describe-alarms` for reading an alarm's configuration, and INSUFFICIENT_DATA on a resource that stops sending metric data.
- [Working with log groups and log streams](https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/Working-with-log-groups-and-streams.html) — retention applies to groups, expired events deleted, ~72h delete delay.
- [describe-log-groups (AWS CLI)](https://docs.aws.amazon.com/cli/latest/reference/logs/describe-log-groups.html) — `retentionInDays` valid values.
- [describe-log-streams (AWS CLI)](https://docs.aws.amazon.com/cli/latest/reference/logs/describe-log-streams.html) — `--order-by LastEventTime --descending` and `lastIngestionTime`.
- [put-metric-alarm (AWS CLI)](https://docs.aws.amazon.com/cli/latest/reference/cloudwatch/put-metric-alarm.html) — `--namespace`/`--dimensions`/`--treat-missing-data` alarm syntax.
