#!/usr/bin/env python3
"""
Per-user Amazon Bedrock usage report for the CMR/Thrive shared account.

Reads CloudTrail management events (already captured by the org trail) and
aggregates Bedrock model invocations by IAM Identity Center user.

No changes are made to the AWS account - this is read-only.

Usage:
    ./bedrock-usage-report.py > report.md    # since the start of this calendar month
    ./bedrock-usage-report.py --days 30 --csv usage.csv
    ./bedrock-usage-report.py --days 7 --profile cmr-bedrock-reporting

CloudTrail and Cost Explorer results are cached per day under
~/.cache/bedrock-usage-report/<profile>/, so only new data is downloaded.

Per-user cost apportions the real billed spend from Cost Explorer.

Requires: awscli v2, python3. Permissions: cloudtrail:LookupEvents and
          ce:GetCostAndUsage (without the latter, only usage is reported).
"""

import argparse
import collections
import csv
import datetime as dt
import gzip
import json
import os
import re
import shutil
import subprocess
import sys
import time

REGION = "ap-southeast-2"
ONE_DAY = dt.timedelta(days=1)
SETTLE = dt.timedelta(hours=2)  # CloudTrail delivery lag margin
CE_SETTLE = dt.timedelta(days=3)  # Cost Explorer posts and revises charges for days
GEO_PREFIXES = {"au", "apac", "global", "us", "eu", "jp", "ca", "us-gov"}
MARKETPLACE_SUFFIX = "(Amazon Bedrock Edition)"
PIXI_CENTRAL_TOML = os.environ.get("PIXI_CENTRAL_TOML", "/pkg/cmr/sw/pixi_central/pixi.toml")
# Non-human principals that show up in the trail and are not researchers.
NON_HUMAN = {"crowdstrike", "ConfigResourceCompositionSession", "resource-explorer-2"}
# Events that represent an actual model invocation (as opposed to console browsing).
INVOKE_EVENTS = {
    "InvokeModel",
    "InvokeModelWithResponseStream",
    "Converse",
    "ConverseStream",
}


def aws_command():
    """The AWS CLI is not installed system-wide on this cluster: interactive
    shells reach it through an alias onto the central pixi environment, and
    aliases are invisible to subprocess. Resolve it the same way mqbedrock
    does - a real aws on PATH wins, otherwise run it via pixi."""
    if shutil.which("aws"):
        return ["aws"]
    pixi = shutil.which("pixi")
    for candidate in (
        os.path.join(os.path.dirname(os.path.realpath(__file__)), "pixi"),
        "/work/microbiome/sw/hpc_scripts/bin/pixi",
    ):
        if pixi:
            break
        if os.access(candidate, os.X_OK):
            pixi = candidate
    if not pixi or not os.path.isfile(PIXI_CENTRAL_TOML):
        sys.exit("aws not on PATH, and no pixi + %s to run it from" % PIXI_CENTRAL_TOML)
    return [pixi, "run", "--manifest-path", PIXI_CENTRAL_TOML, "aws"]


def aws(args, profile, region=REGION):
    cmd = aws_command()
    if profile:
        cmd += ["--profile", profile]
    cmd += ["--region", region] + args + ["--output", "json"]
    # CloudTrail LookupEvents is limited to 2 requests/second per account, and
    # the CLI paginates as fast as it can. The default "legacy" retry mode gives
    # up after 2 retries; "adaptive" also rate-limits client-side once throttled.
    env = dict(os.environ)
    env.setdefault("AWS_RETRY_MODE", "adaptive")
    env.setdefault("AWS_MAX_ATTEMPTS", "10")
    for wait in (10, 30, 60, None):
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
        if proc.returncode == 0 or "ThrottlingException" not in proc.stderr or wait is None:
            break
        print("  throttled by AWS, retrying in %ds..." % wait, file=sys.stderr)
        time.sleep(wait)
    if proc.returncode != 0:
        err = proc.stderr.strip()
        if "AccessDenied" in err or "not authorized" in err:
            sys.exit(
                "\nAccess denied running: %s\n%s\n\n"
                "You need cloudtrail:LookupEvents in this account (and "
                "ce:GetCostAndUsage for costs).\n" % (" ".join(cmd), err)
            )
        if "token" in err.lower() and "expired" in err.lower():
            sys.exit("\nSSO session expired. Run:  aws sso login --profile %s\n" % profile)
        sys.exit("\nCommand failed: %s\n%s\n" % (" ".join(cmd), err))
    return json.loads(proc.stdout or "{}")


def cache_dir(profile):
    """One cache per AWS profile, since different profiles may be different accounts."""
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    name = profile or os.environ.get("AWS_PROFILE") or "default"
    return os.path.join(base, "bedrock-usage-report", name)


def load_json_gz(path):
    try:
        with gzip.open(path, "rt") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def save_json_gz(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with gzip.open(tmp, "wt") as fh:
        json.dump(obj, fh)
    os.replace(tmp, path)


def parse_time(s):
    return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(dt.timezone.utc)


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def lookup_events(start, end, profile, max_pages):
    """All Bedrock CloudTrail events in [start, end). The CLI paginates
    itself, so this is one process per call rather than one per page."""
    args = [
        "cloudtrail",
        "lookup-events",
        "--lookup-attributes",
        "AttributeKey=EventSource,AttributeValue=bedrock.amazonaws.com",
        "--start-time",
        iso(start),
        "--end-time",
        iso(end),
        "--page-size",
        "50",
    ]
    if max_pages:
        args += ["--max-items", str(max_pages * 50)]
    page = aws(args, profile)
    # Keep only what the report reads; the rest of each event duplicates
    # what is inside CloudTrailEvent.
    events = [
        {k: e.get(k) for k in ("EventId", "EventTime", "Username", "CloudTrailEvent")}
        for e in page.get("Events", [])
    ]
    return events, bool(page.get("NextToken"))


def utc_midnight(day):
    return dt.datetime(day.year, day.month, day.day, tzinfo=dt.timezone.utc)


def utc_days(start, now):
    day = start.date()
    while day <= now.date():
        yield day
        day += ONE_DAY


def window_start(days):
    """Whole UTC days, so the window lines up with Cost Explorer's daily buckets."""
    return utc_midnight(dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=days))


def fetch_events(start, now, profile, max_pages, use_cache=True):
    """Bedrock CloudTrail events from midnight UTC `start` until now.

    Events are cached per UTC day. A day is final once SETTLE has passed after
    its end (CloudTrail delivers events up to ~15 minutes late), after which it
    is never fetched again; an unfinished day (normally just today) is topped
    up from shortly before the previous fetch."""
    cdir = cache_dir(profile)
    for day in utc_days(start, now):
        d0 = utc_midnight(day)
        d1 = d0 + ONE_DAY
        path = os.path.join(cdir, "cloudtrail-%s.json.gz" % day)
        cached = load_json_gz(path) if use_cache else None
        if cached and cached.get("complete"):
            events = cached["events"]
        else:
            fetch_from = d0
            if cached:
                fetch_from = max(d0, parse_time(cached["fetched_at"]) - SETTLE)
            print("  fetching %s (from %s)" % (day, fetch_from.strftime("%H:%M UTC")), file=sys.stderr)
            fresh, truncated = lookup_events(fetch_from, min(d1, now), profile, max_pages)
            merged = {e["EventId"]: e for e in (cached or {}).get("events", [])}
            merged.update((e["EventId"], e) for e in fresh)
            events = list(merged.values())
            if truncated:
                print(
                    "\n  WARNING: %s stopped at --max-pages=%d; that day is incomplete "
                    "and was not cached. Raise --max-pages.\n" % (day, max_pages),
                    file=sys.stderr,
                )
            else:
                save_json_gz(
                    path,
                    {"fetched_at": iso(now), "complete": now >= d1 + SETTLE, "events": events},
                )
        yield from events


def model_of(raw):
    """Model id of an invocation record, e.g. anthropic.claude-opus-5-5."""
    for r in raw.get("resources") or []:
        arn = r.get("ARN", "")
        if "foundation-model/" in arn:
            return arn.split("foundation-model/")[-1]
    # No foundation-model resource (e.g. a denied call): fall back to the
    # requested id, minus any cross-region inference profile prefix (au., global.).
    mid = ((raw.get("requestParameters") or {}).get("modelId") or "?").split("/")[-1]
    head, _, rest = mid.partition(".")
    return rest if head in GEO_PREFIXES and rest else mid


def model_words(name):
    """Comparable form of a model name, so that the CloudTrail id
    'anthropic.claude-haiku-4-5-20251001-v1:0' and the Cost Explorer service
    'Claude Haiku 4.5 (Amazon Bedrock Edition)' can be matched."""
    name = name.lower().replace("(amazon bedrock edition)", "")
    name = re.sub(r"-\d{8}(-v\d+(:\d+)?)?$", "", name)
    name = re.sub(r"-v\d+(:\d+)?$", "", name)
    return tuple(w for w in re.split(r"[^a-z0-9]+", name) if w)


def ce_item(service, usage_type):
    """(model name, token category) of a Bedrock Cost Explorer line item, or
    None if it is not Bedrock. Third-party models are billed through
    Marketplace as their own service ('Claude Opus 5.5 (Amazon Bedrock
    Edition)'); first-party ones appear under 'Amazon Bedrock' with the model
    in the usage type ('APS2-zai.glm-5-input-tokens')."""
    norm = re.sub(r"[^a-z]", "", usage_type.lower())
    if "cacheread" in norm:
        category = "cache_read"
    elif "cachewrite" in norm:
        category = "cache_write"
    elif "output" in norm:
        category = "output"
    elif "input" in norm:
        category = "input"
    else:
        category = "other"
    if service.endswith(MARKETPLACE_SUFFIX):
        return service[: -len(MARKETPLACE_SUFFIX)].strip(), category
    if service == "Amazon Bedrock":
        m = re.match(r"^[A-Z0-9]+-(.+?)-(cache-read|cache-write|input|output)-", usage_type)
        return (m.group(1) if m else usage_type), category
    return None


def build_report(start, now, profile, max_pages, include_non_human, use_cache):
    users = collections.defaultdict(
        lambda: {
            "calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "token_records": 0,
            "models": collections.Counter(),
            "clients": collections.Counter(),
            "days": set(),
            "first_seen": None,
            "last_seen": None,
        }
    )
    totals = collections.Counter()
    by_day = collections.Counter()
    # (UTC day, model_words) -> user -> Counter(calls, input, output), from the
    # token records, for apportioning Cost Explorer spend.
    activity = collections.defaultdict(lambda: collections.defaultdict(collections.Counter))

    for e in fetch_events(start, now, profile, max_pages, use_cache):
        raw = json.loads(e["CloudTrailEvent"])
        name = raw.get("eventName")
        if name not in INVOKE_EVENTS:
            continue

        user = e.get("Username") or "<unknown>"
        if user in NON_HUMAN and not include_non_human:
            continue

        when = e["EventTime"]
        u = users[user]
        u["days"].add(str(when)[:10])
        if u["first_seen"] is None or str(when) < str(u["first_seen"]):
            u["first_seen"] = when
        if u["last_seen"] is None or str(when) > str(u["last_seen"]):
            u["last_seen"] = when

        # CloudTrail emits two records per invocation: an AwsApiCall (with the
        # request) and a paired AwsServiceEvent (carrying token counts, and
        # only for calls that succeeded). Count calls from the API call record
        # only, so we don't double count.
        extra = (
            (raw.get("serviceEventDetails") or {}).get("AdditionalEventData") or {}
        ).get("additionalEntries") or {}

        model = model_of(raw)
        if "inputTokens" in extra or "outputTokens" in extra:
            u["input_tokens"] += extra.get("inputTokens", 0)
            u["output_tokens"] += extra.get("outputTokens", 0)
            u["token_records"] += 1
            totals["token_records"] += 1
            day = parse_time(raw.get("eventTime") or when).date()
            activity[(day, model_words(model))][user].update(
                calls=1, input=extra.get("inputTokens", 0), output=extra.get("outputTokens", 0)
            )
        else:
            u["calls"] += 1
            totals["calls"] += 1
            by_day[str(when)[:10]] += 1
            u["clients"][raw.get("userAgent", "?").split(" ")[0]] += 1
            u["models"][model] += 1

    return users, totals, by_day, activity


def fetch_costs(start, now, profile, use_cache=True):
    """{UTC day: [[service, usage_type, usd], ...]} for Bedrock line items.

    Cached per day like the CloudTrail events, but Cost Explorer posts and
    revises charges for longer, so a day is only final after CE_SETTLE. Each
    request costs US$0.01, so all unfinished days are fetched in one request."""
    cdir = cache_dir(profile)
    costs = {}
    missing = []
    for day in utc_days(start, now):
        cached = load_json_gz(os.path.join(cdir, "ce-%s.json.gz" % day)) if use_cache else None
        if cached and cached.get("complete"):
            costs[day] = cached["items"]
        else:
            missing.append(day)
    if not missing:
        return costs

    first, last = missing[0], missing[-1]
    print("  fetching Cost Explorer %s to %s" % (first, last), file=sys.stderr)
    fetched = collections.defaultdict(list)
    token = None
    while True:
        args = [
            "ce",
            "get-cost-and-usage",
            "--time-period",
            "Start=%s,End=%s" % (first, last + ONE_DAY),
            "--granularity",
            "DAILY",
            "--metrics",
            "UnblendedCost",
            "--group-by",
            "Type=DIMENSION,Key=SERVICE",
            "Type=DIMENSION,Key=USAGE_TYPE",
            # Usage only: no tax, credits or refunds.
            "--filter",
            json.dumps({"Dimensions": {"Key": "RECORD_TYPE", "Values": ["Usage"]}}),
        ]
        if token:
            args += ["--next-page-token", token]
        res = aws(args, profile, region="us-east-1")
        for r in res.get("ResultsByTime", []):
            day = dt.date.fromisoformat(r["TimePeriod"]["Start"])
            for g in r.get("Groups", []):
                service, usage_type = g["Keys"]
                if ce_item(service, usage_type):
                    usd = float(g["Metrics"]["UnblendedCost"]["Amount"])
                    fetched[day].append([service, usage_type, usd])
        token = res.get("NextPageToken")
        if not token:
            break

    for day in missing:
        costs[day] = fetched.get(day, [])
        save_json_gz(
            os.path.join(cdir, "ce-%s.json.gz" % day),
            {
                "fetched_at": iso(now),
                "complete": now >= utc_midnight(day) + ONE_DAY + CE_SETTLE,
                "items": costs[day],
            },
        )
    return costs


def apportion_costs(costs, activity):
    """Split each day's billed spend for each model among that day's users.

    Input and output spend are split by input and output tokens. CloudTrail
    does not record cache read/write tokens, which are most of the spend for
    agentic tools, so those (and anything else) are split by number of calls.
    A model billed on a day with no CloudTrail activity (Cost Explorer days
    can straddle activity) falls back to the window's users of that model;
    spend on a model never seen in CloudTrail is left unattributed."""
    # (model key, user) -> Counter over the whole window, the fallback when a
    # model was billed on a day with no CloudTrail activity.
    window = collections.defaultdict(collections.Counter)
    for (day, words), per_user in activity.items():
        for user, c in per_user.items():
            window[(words, user)].update(c)

    user_model_cost = collections.Counter()  # (model key, user) -> USD
    model_cost = collections.Counter()  # Cost Explorer model name -> USD
    unattributed = collections.Counter()
    names = {}  # model key -> Cost Explorer model name
    for day, items in costs.items():
        for service, usage_type, usd in items:
            name, category = ce_item(service, usage_type)
            model_cost[name] += usd
            words = model_words(name)
            # Cost Explorer names drop the vendor ('Claude ...' for 'anthropic.claude-...').
            keys = {k for k, _ in window if k == words or k[1:] == words}
            names.update((k, name) for k in keys)
            rows = [
                ((k, user), c)
                for k in keys
                for user, c in activity.get((day, k), {}).items()
            ]
            if not rows:
                rows = [(key, c) for key, c in window.items() if key[0] in keys]
            metric = category if category in ("input", "output") else "calls"
            total = sum(c[metric] for _, c in rows)
            if not total:
                metric = "calls"
                total = sum(c[metric] for _, c in rows)
            if not total:
                unattributed[name] += usd
                continue
            for key, c in rows:
                user_model_cost[key] += usd * c[metric] / total
    return user_model_cost, model_cost, unattributed, names


def fmt(n):
    return "{:,}".format(n)


def short_model(model):
    return model.split(".", 1)[-1] if model.startswith("anthropic.") else model


def human(n):
    """Compact token count: 950, 12.3k, 4.56M."""
    for div, suffix in ((1e9, "G"), (1e6, "M"), (1e3, "k")):
        if n >= div:
            v = n / div
            return ("%.2f" if v < 10 else "%.1f" if v < 100 else "%.0f") % v + suffix
    return "%d" % n


OTHER = ("other",)  # model key for the models without a column of their own
TOP_USERS = 10  # per-model columns: models used by this many top users...
MIN_MODEL_COST = 1.0  # ...that cost more than this (USD) in total
LOGIN_DOMAIN = "@qut.edu.au"  # Identity Center usernames are <cluster login> + this


def md_table(headers, rows, right=()):
    """A Markdown table, padded so it also reads as plain text. Columns whose
    index is in `right` are right-aligned."""
    cols = list(zip(headers, *rows))
    widths = [max(3, *(len(str(v)) for v in col)) for col in cols]

    def line(values):
        cells = [
            str(v).rjust(w) if i in right else str(v).ljust(w)
            for i, (v, w) in enumerate(zip(values, widths))
        ]
        return "| " + " | ".join(cells) + " |"

    rule = ["-" * (w + 1) + ":" if i in right else "-" * (w + 2) for i, w in enumerate(widths)]
    print(line(headers))
    print("|" + "|".join(rule) + "|")
    for r in rows:
        print(line(r))


def short_name(name):
    return name[len("Claude ") :] if name.startswith("Claude ") else name


def real_names(users):
    """{user: real name}, looked up the way the microbiome_users alias does: the
    GECOS field of `getent passwd`. A user with no entry keeps their username,
    e.g. everyone but the caller inside an mqyolo sandbox, which cannot see LDAP."""
    names = {user: user for user in users}
    logins = {u[: -len(LOGIN_DOMAIN)]: u for u in users if u.endswith(LOGIN_DOMAIN)}
    if not logins:
        return names
    try:
        # Exits non-zero if any login is missing, but still prints the rest.
        out = subprocess.run(["getent", "passwd", *logins], capture_output=True, text=True).stdout
    except OSError:
        out = ""
    for line in out.splitlines():
        fields = line.split(":")
        if len(fields) >= 5 and fields[0] in logins and fields[4]:
            names[logins[fields[0]]] = fields[4].split(",")[0]
    missing = sum(1 for user in users if names[user] == user)
    if missing:
        print("  no real name found for %d of %d users" % (missing, len(users)), file=sys.stderr)
    return names


def print_cost_table(users, activity, cost):
    """A summary per user (cost and token share per notable model), then the
    same users with tokens and cost in full."""
    user_model_cost, model_cost, unattributed, names = cost
    user_cost = collections.Counter()
    model_total = collections.Counter()
    for (k, user), usd in user_model_cost.items():
        user_cost[user] += usd
        model_total[k] += usd
    tokens = collections.defaultdict(collections.Counter)  # (model key, user)
    for (day, k), per_user in activity.items():
        for user, c in per_user.items():
            tokens[(k, user)].update(c)

    ranked = sorted(users, key=lambda user: -user_cost[user])
    top = set(ranked[:TOP_USERS])
    models = sorted(
        {k for (k, user) in tokens if user in top and model_total[k] > MIN_MODEL_COST},
        key=lambda k: -model_total[k],
    )
    # Fold the remaining models into one "Other" group, so each row's
    # percentages add up to 100%.
    folded_tokens = collections.defaultdict(collections.Counter)
    for (k, user), c in tokens.items():
        folded_tokens[(k if k in models else OTHER, user)].update(c)
    folded_cost = collections.Counter()
    for (k, user), usd in user_model_cost.items():
        folded_cost[(k if k in models else OTHER, user)] += usd
    tokens, user_model_cost = folded_tokens, folded_cost
    model_total = collections.Counter()
    for (k, user), usd in user_model_cost.items():
        model_total[k] += usd
    if any(k == OTHER for k, _ in tokens):
        models.append(OTHER)
        names[OTHER] = "Other"

    def pct(part, whole):
        return "%.0f%%" % (100.0 * part / whole) if whole else "-"

    # Percentages are of the row's total over all models, shown or not.
    def row(columns, label, usd, total_tokens, per_model):
        cells = [label, "%.2f" % usd]
        for k in models:
            c, mcost = per_model(k)
            if not c["calls"] and mcost < 0.005:
                cells += [""] * len(columns)
                continue
            n = c["input"] + c["output"]
            values = {
                "tok": human(n),
                "tok%": pct(n, total_tokens),
                "in": human(c["input"]),
                "out": human(c["output"]),
                "USD": "%.2f" % mcost,
                "USD%": pct(mcost, usd),
            }
            cells += [values[column] for column in columns]
        return cells

    user_tokens = collections.Counter()
    for (k, user), c in tokens.items():
        user_tokens[user] += c["input"] + c["output"]

    def model_sum(k):
        c = collections.Counter()
        for user in users:
            c.update(tokens[(k, user)])
        return c, model_total[k]

    def table(first_column, label_of, columns):
        # Markdown has no grouped headers, so every column names its model.
        headers = [first_column, "Cost USD"]
        for k in models:
            name = short_name(names.get(k, " ".join(k)))
            headers += [name + " " + column for column in columns]
        rows = [
            row(
                columns,
                label_of(user),
                user_cost[user],
                user_tokens[user],
                lambda k: (tokens[(k, user)], user_model_cost[(k, user)]),
            )
            for user in ranked
        ]
        total = row(
            columns,
            "TOTAL (%d users)" % len(users),
            sum(user_cost.values()),
            sum(user_tokens.values()),
            model_sum,
        )
        rows.append(["**%s**" % v if v else "" for v in total])
        md_table(headers, rows, right=range(1, len(headers)))

    real_name = real_names(users)
    table("Name", real_name.get, ("USD", "tok%"))
    print(
        "\nModel columns: models used by the top %d users costing over $%.0f in total,"
        " then *Other* for the rest. *tok%%* is that model's share of the row's input +"
        " output tokens across all models (cache tokens are not in CloudTrail)."
        % (TOP_USERS, MIN_MODEL_COST)
    )

    print("\n## Tokens and cost by user and model\n")
    table("User", str, ("tok", "tok%", "in", "out", "USD", "USD%"))
    print(
        "\nSame model columns. *tok* is input + output; *tok%* and *USD%* are that model's"
        " share of the row's tokens and cost across all models."
    )


def print_report(users, totals, by_day, start, activity, cost, month=None, month_usd=None):
    print("# Bedrock usage by user since %s 00:00 UTC\n" % start.date())
    if month_usd is not None:
        print("**Total billed in %s so far: US$%s**\n" % (month.strftime("%B %Y"), "{:,.2f}".format(month_usd)))
    if not users:
        print("No Bedrock invocations found.")
        return

    if cost:
        print_cost_table(users, activity, cost)
    else:
        ranked = sorted(users.items(), key=lambda kv: -kv[1]["calls"])
        rows = []
        for user, u in ranked:
            top = u["models"].most_common(1)
            rows.append(
                [
                    user,
                    fmt(u["calls"]),
                    fmt(u["input_tokens"]),
                    fmt(u["output_tokens"]),
                    len(u["days"]),
                    short_model(top[0][0]) if top else "-",
                ]
            )
        rows.append(
            [
                "**TOTAL (%d users)**" % len(users),
                "**%s**" % fmt(sum(u["calls"] for u in users.values())),
                "**%s**" % fmt(sum(u["input_tokens"] for u in users.values())),
                "**%s**" % fmt(sum(u["output_tokens"] for u in users.values())),
                "",
                "",
            ]
        )
        md_table(
            ["User", "Calls", "Input tok", "Output tok", "Days", "Top model"], rows, right=(1, 2, 3, 4)
        )

    print("\n## Activity by day\n")
    md_table(["Date", "Calls"], [[d, fmt(by_day[d])] for d in sorted(by_day)], right=(1,))

    print("\n## Clients in use\n")
    clients = collections.Counter()
    for u in users.values():
        clients.update(u["clients"])
    md_table(["Client", "Calls"], [[c, fmt(n)] for c, n in clients.most_common(8)], right=(1,))

    if cost:
        user_model_cost, model_cost, unattributed, names = cost
        print("\n## Billed Bedrock spend by model\n")
        print("From Cost Explorer: USD, usage only, excluding tax.\n")
        rows = [
            [name, "%.2f" % usd, "%.2f" % unattributed[name] if unattributed[name] >= 0.005 else ""]
            for name, usd in model_cost.most_common()
            if usd >= 0.005
        ]
        rows.append(["**TOTAL**", "**%.2f**" % sum(model_cost.values()), "**%.2f**" % sum(unattributed.values())])
        md_table(["Model", "USD", "Not attributed to users"], rows, right=(1, 2))

    covered = totals["token_records"]
    calls = totals["calls"]
    pct = (100.0 * covered / calls) if calls else 0
    print("\n## Caveats\n")
    print(
        "- Token counts come from CloudTrail: %.0f%% of calls (%s of %s) carried a token"
        " record (failed calls do not)." % (pct, fmt(covered), fmt(calls))
    )
    print("- CloudTrail does not record cache read/write tokens, so input tokens are only the uncached input.")
    if cost:
        print(
            "- Per-user cost apportions each day's real billed spend per model: input and output"
            " by tokens, cache read/write (usually most of the bill) by number of calls, which is"
            " an approximation."
        )
        print(
            "- Unattributed spend is on models with no CloudTrail activity (e.g. calls made via"
            " the OpenAI-compatible endpoint)."
        )
        print("- Cost Explorer lags by up to a day, so the most recent day's cost is incomplete.")


def write_csv(path, users, user_cost):
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(
            [
                "user",
                "calls",
                "input_tokens",
                "output_tokens",
                "cost_usd",
                "active_days",
                "first_seen",
                "last_seen",
                "top_model",
                "top_client",
            ]
        )
        for user, u in sorted(users.items(), key=lambda kv: -kv[1]["calls"]):
            w.writerow(
                [
                    user,
                    u["calls"],
                    u["input_tokens"],
                    u["output_tokens"],
                    "%.2f" % user_cost[user] if user_cost is not None else "",
                    len(u["days"]),
                    u["first_seen"],
                    u["last_seen"],
                    u["models"].most_common(1)[0][0] if u["models"] else "",
                    u["clients"].most_common(1)[0][0] if u["clients"] else "",
                ]
            )
    print("Wrote %s" % path, file=sys.stderr)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument(
        "--days",
        type=int,
        help="whole UTC days before today to include, plus today so far; max 89 "
        "(default: since the start of this calendar month, UTC)",
    )
    p.add_argument("--profile", default=None, help="AWS CLI profile to use")
    p.add_argument("--csv", help="also write per-user totals to this CSV file")
    p.add_argument(
        "--max-pages",
        type=int,
        default=600,
        help="safety cap on CloudTrail pages per day, 50 events each (default 600)",
    )
    p.add_argument(
        "--refresh",
        action="store_true",
        help="ignore cached CloudTrail and Cost Explorer data and download again (cache: %s)"
        % os.path.dirname(cache_dir(None)),
    )
    p.add_argument("--include-non-human", action="store_true", help="include service principals")
    a = p.parse_args()

    if a.days is not None and a.days > 89:
        sys.exit("CloudTrail only retains 90 days of events. Use --days 89 or less.")

    now = dt.datetime.now(dt.timezone.utc)
    month = utc_midnight(now.date().replace(day=1))
    start = month if a.days is None else window_start(a.days)
    print("Querying CloudTrail for Bedrock activity since %s..." % start.date(), file=sys.stderr)
    users, totals, by_day, activity = build_report(
        start, now, a.profile, a.max_pages, a.include_non_human, not a.refresh
    )

    month_usd = None
    try:
        # One fetch covering both the report window and the whole month, which
        # differ when --days is given.
        costs = fetch_costs(min(start, month), now, a.profile, not a.refresh)
        cost = apportion_costs({d: v for d, v in costs.items() if d >= start.date()}, activity)
        month_usd = sum(
            usd for d, items in costs.items() if d >= month.date() for _, _, usd in items
        )
    except SystemExit as e:
        print("%s\n  (costs not shown - Cost Explorer lookup failed)" % e, file=sys.stderr)
        cost = None

    print_report(users, totals, by_day, start, activity, cost, month, month_usd)
    if a.csv:
        user_cost = None
        if cost:
            user_cost = collections.Counter()
            for (k, user), usd in cost[0].items():
                user_cost[user] += usd
        write_csv(a.csv, users, user_cost)


if __name__ == "__main__":
    main()
