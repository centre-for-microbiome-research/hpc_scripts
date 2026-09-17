#!/usr/bin/env python3
"""Regenerate Terminal-Bench score vs. Bedrock cost scatter plots from LIVE data.

Nothing about specific models, scores, or prices is hardcoded in this file.
Every run re-fetches:

  1. The Bedrock model catalog (which models Bedrock offers right now):
     https://docs.aws.amazon.com/bedrock/latest/userguide/model-cards.html

  2. Bedrock on-demand pricing, straight from AWS's official (public, no-auth)
     Price List Bulk API:
     https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonBedrock/...
     https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonBedrockFoundationModels/...

  3. Terminal-Bench 2.1 / 4.0 resolution rates, scraped from the per-model
     comparison dataset that Artificial Analysis embeds in every
     https://artificialanalysis.ai/models/<slug> page (it's the same ~650
     model dataset used to power their "add model to compare" picker).

Because all three sources change over time, the charts this script produces
will drift from any specific numbers quoted in chat -- that's the point: run
`pixi run generate` again and it reflects whatever is live *right now*.

Matching Bedrock's model names to Artificial Analysis's model names is
necessarily a bit fuzzy (the two catalogs don't use identical spellings/
suffixes), so a small NAME_ALIASES table below patches known mismatches.
Anything that still doesn't match is skipped and reported on stderr rather
than guessed.
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from bs4 import BeautifulSoup

HEADERS = {"User-Agent": "Mozilla/5.0 (+bedrock-terminalbench-chart-script)"}
OUT_DIR = Path(__file__).resolve().parent

MODEL_CARDS_URL = "https://docs.aws.amazon.com/bedrock/latest/userguide/model-cards.html"

BEDROCK_PRICE_OFFERS = [
    "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonBedrock/current/us-east-1/index.json",
    "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonBedrockFoundationModels/current/us-east-1/index.json",
]

# Any one of these Artificial Analysis model pages embeds their *entire*
# model comparison dataset (~650 models incl. Terminal-Bench scores) -- we
# only need one successful fetch. Multiple candidates in case one slug gets
# renamed/removed.
AA_MODEL_PAGE_CANDIDATES = [
    "claude-sonnet-5",
    "gpt-6-astra",
    "grok-4-6",
    "claude-opus-5",
    "deepseek-v3-2",
]

# Bedrock model-card entries that are not text-generation/chat models --
# skipped so they don't show up as "no benchmark data" noise.
EXCLUDE_SUBSTRINGS = [
    "Embed", "Rerank", "Canvas", "Reel", "Titan Embeddings", "Titan Image",
    "Titan Multimodal", "Stable Image", "Marengo", "Pegasus", "Sonic",
    "Voxtral", "Palmyra Vision",
]

# Small, manually-curated set of Bedrock-name -> Artificial-Analysis-name
# overrides for cases the automatic normalizer can't reconcile.
NAME_ALIASES = {
    "nvidia nemotron nano 12b v2 vl bf16": "nvidia nemotron nano 12b v2 vl",
    "nvidia nemotron 3 super 120b": "nemotron 3 super 120b a12b",
    "llama 4 maverick 17b instruct": "llama 4 maverick",
    "llama 4 scout 17b instruct": "llama 4 scout",
    "llama 3.3 70b instruct": "llama 3.3 instruct 70b",
    "llama 3.1 405b instruct": "llama 3.1 instruct 405b",
    "llama 3.1 70b instruct": "llama 3.1 instruct 70b",
    "llama 3.1 8b instruct": "llama 3.1 instruct 8b",
    "gpt oss 120b": "gpt-oss-120b",
    "gpt oss 20b": "gpt-oss-20b",
    "deepseek-r1": "DeepSeek R1 0528",
    "nemotron nano 3 30b": "NVIDIA Nemotron 3 Nano 30B A3B",
    "qwen3 next 80b a3b": "Qwen3 Next 80B A3B Instruct",
}

CATEGORY_COLORS = {
    "Anthropic": "#d97757",
    "OpenAI": "#10a37f",
    "xAI": "#000000",
    "Other": "#4c78a8",
}


def category_for(provider: str) -> str:
    if provider == "Anthropic":
        return "Anthropic"
    if provider == "OpenAI":
        return "OpenAI"
    if provider == "xAI":
        return "xAI"
    return "Other"


def fetch(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_json(url: str, timeout: int = 60):
    return json.loads(fetch(url, timeout=timeout))


# ---------------------------------------------------------------------------
# 1. Bedrock model catalog
# ---------------------------------------------------------------------------

def fetch_bedrock_catalog() -> dict[str, list[str]]:
    """provider -> [model display names], live from AWS docs."""
    html = fetch(MODEL_CARDS_URL).decode("utf-8")
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    catalog: dict[str, list[str]] = {}
    for tr in table.find_all("tr")[1:]:
        cells = tr.find_all(["td", "th"])
        if len(cells) < 3:
            continue
        provider = cells[1].get_text(strip=True)
        if not provider:
            continue
        names = [a.get_text(strip=True) for a in cells[2].find_all("a")]
        names = [n for n in names if not any(x.lower() in n.lower() for x in EXCLUDE_SUBSTRINGS)]
        if names:
            catalog.setdefault(provider, [])
            catalog[provider].extend(n for n in names if n not in catalog[provider])
    return catalog


# ---------------------------------------------------------------------------
# 2. Bedrock pricing (official AWS Price List Bulk API)
# ---------------------------------------------------------------------------

TIER_PRIORITY = ["standard", "global-standard", None, "global-priority", "priority", "flex", "batch"]

_FM_SUFFIX_MAP = {
    "inputtokencount": ("input", "standard"),
    "inputtokensstandard": ("input", "standard"),
    "inputtokencountglobal": ("input", "global-standard"),
    "inputtokensglobalstandard": ("input", "global-standard"),
    "outputtokencount": ("output", "standard"),
    "outputtokensstandard": ("output", "standard"),
    "outputtokencountglobal": ("output", "global-standard"),
    "outputtokensglobalstandard": ("output", "global-standard"),
}


def _classify_foundation_models_usagetype(usagetype: str):
    """AmazonBedrockFoundationModels uses a different usagetype scheme than
    the AmazonBedrock offer (e.g. 'USE1-MP:USE1_input_tokens_standard-Units'
    or the older 'USE1-MP:USE1_InputTokenCount-Units')."""
    suffix = re.sub(r"^USE1-MP:USE1_", "", usagetype)
    suffix = re.sub(r"-Units$", "", suffix)
    key = suffix.lower().replace("_", "")
    return _FM_SUFFIX_MAP.get(key, (None, None))


def fetch_bedrock_pricing() -> dict[str, dict]:
    """model display name (as used in the price list) -> {"input": $/1M, "output": $/1M, "tier": str}."""
    by_model: dict[str, dict] = {}  # model -> tier -> {"input":.., "output":..}

    for offer_url in BEDROCK_PRICE_OFFERS:
        data = fetch_json(offer_url)
        products = data["products"]
        ondemand = data.get("terms", {}).get("OnDemand", {})
        is_foundation_models_offer = "AmazonBedrockFoundationModels" in offer_url

        sku_price = {}
        for term_group in ondemand.values():
            for term in term_group.values():
                sku = term["sku"]
                for dim in term["priceDimensions"].values():
                    unit = dim.get("unit", "")
                    try:
                        usd = float(dim["pricePerUnit"].get("USD", 0))
                    except (TypeError, ValueError):
                        continue
                    if usd <= 0:
                        continue
                    if "1K tokens" in unit:
                        sku_price[sku] = usd * 1000
                    elif "1M tokens" in unit:
                        sku_price[sku] = usd

        for sku, prod in products.items():
            attrs = prod["attributes"]
            location = attrs.get("location")
            if location and location != "US East (N. Virginia)":
                continue
            usd_per_m = sku_price.get(sku)
            if usd_per_m is None:
                continue

            if is_foundation_models_offer:
                servicename = attrs.get("servicename", "")
                model_name = re.sub(r"\s*\(Amazon Bedrock Edition\)$", "", servicename).strip()
                if not model_name or model_name == servicename == "":
                    continue
                direction, tier = _classify_foundation_models_usagetype(attrs.get("usagetype", ""))
                if direction is None:
                    continue
                is_input, is_output = direction == "input", direction == "output"
            else:
                model_name = attrs.get("model")
                if not model_name:
                    continue
                inference_type = (attrs.get("inferenceType") or attrs.get("tokenType") or "").lower()
                if "cache" in inference_type or "batch" in inference_type:
                    continue  # only want plain input/output on-demand pricing
                is_input = inference_type.startswith("input") or inference_type == "input tokens"
                is_output = inference_type.startswith("output") or inference_type == "output tokens"
                if not (is_input or is_output):
                    continue
                tier = attrs.get("service_tier")
                if tier is None:
                    for t in ("priority", "flex", "global-standard", "global-priority", "global-flex"):
                        if t.replace("-", " ").split()[-1] in inference_type:
                            tier = t
                            break

            bucket = by_model.setdefault(model_name, {}).setdefault(tier, {})
            bucket[("input" if is_input else "output")] = usd_per_m

    final: dict[str, dict] = {}
    for model_name, tiers in by_model.items():
        chosen = None
        chosen_tier = None
        for t in TIER_PRIORITY:
            vals = tiers.get(t)
            if vals and "input" in vals and "output" in vals:
                chosen, chosen_tier = vals, t
                break
        if chosen is None:
            for t, vals in tiers.items():
                if "input" in vals and "output" in vals:
                    chosen, chosen_tier = vals, t
                    break
        if chosen:
            final[model_name] = {"input": chosen["input"], "output": chosen["output"], "tier": chosen_tier}
    return final


# ---------------------------------------------------------------------------
# 3. Terminal-Bench scores (scraped from Artificial Analysis)
# ---------------------------------------------------------------------------

def _extract_balanced(text: str, start: int) -> str | None:
    """Naive brace-matched extraction of a JSON object starting at `start`."""
    depth = 0
    for i in range(start, len(text)):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


_ANCHOR_RE = re.compile(r'\{\\"id\\":\\"[0-9a-f-]{36}\\",\\"slug\\":\\"([^"\\\\]+)\\",\\"name\\":\\"([^"\\\\]+)\\"')


def fetch_aa_dataset() -> dict[str, dict]:
    """Fetch Artificial Analysis's embedded model dataset (all variants).

    Returns dict[slug] -> parsed model dict (includes 'release', 'creator',
    'terminalBench21', 'terminalBench40', 'price1mInputTokens', ...).
    """
    last_err = None
    for slug in AA_MODEL_PAGE_CANDIDATES:
        url = f"https://artificialanalysis.ai/models/{slug}"
        try:
            html = fetch(url).decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            continue

        results: dict[str, dict] = {}
        for m in _ANCHOR_RE.finditer(html):
            obj_str = _extract_balanced(html, m.start())
            if obj_str is None or "terminalBench21" not in obj_str:
                continue
            cleaned = obj_str.replace('\\"', '"')
            try:
                obj = json.loads(cleaned)
            except json.JSONDecodeError:
                continue
            if obj.get("slug"):
                results[obj["slug"]] = obj

        if len(results) >= 100:  # sanity floor -- a real fetch has ~600+
            return results
        last_err = RuntimeError(f"only parsed {len(results)} entries from {url}")

    raise RuntimeError(f"could not fetch Artificial Analysis dataset: {last_err}")


def normalize(name: str) -> str:
    """Collapse a name to a single comparable string: case/punctuation
    insensitive, and insensitive to trailing '.0' in version numbers."""
    name = name.lower()
    name = re.sub(r"(?<=\d)\.0(?!\d)", "", name)  # "2.0" -> "2", keep "3.2"
    name = re.sub(r"[^a-z0-9]+", "", name)
    return name


def tokenize(name: str) -> tuple[str, ...]:
    """Order-insensitive token key: Anthropic/AA disagree on word order for
    some model names ('Claude Sonnet 4.5' vs 'Claude 4.5 Sonnet'), and this
    makes that not matter."""
    name = name.lower()
    name = re.sub(r"(?<=\d)\.0(?!\d)", "", name)
    name = re.sub(r"\bit$", "instruct", name.strip())
    tokens = re.findall(r"[a-z0-9]+", name)
    return tuple(sorted(tokens))


def build_release_index(aa_dataset: dict[str, dict]) -> dict[tuple, dict]:
    """token-key(release.name) -> canonical variant dict for that release.

    The "canonical" variant is the one whose own slug equals the release
    slug (this is consistently Artificial Analysis's default/flagship
    effort setting for that model family), falling back to
    chartDefaultSelected, then to the highest Terminal-Bench 2.1 score.
    """
    groups: dict[str, list[dict]] = {}
    for obj in aa_dataset.values():
        rel = obj.get("release") or {}
        rel_slug = rel.get("slug")
        if not rel_slug:
            continue
        groups.setdefault(rel_slug, []).append(obj)

    index: dict[tuple, dict] = {}
    for rel_slug, variants in groups.items():
        rel_name = variants[0]["release"]["name"]
        canonical = next((v for v in variants if v.get("slug") == rel_slug), None)
        if canonical is None:
            canonical = next((v for v in variants if v.get("chartDefaultSelected")), None)
        if canonical is None:
            canonical = max(variants, key=lambda v: (v.get("terminalBench21") or -1))
        index[tokenize(rel_name)] = canonical
        # also index the name with any trailing "(...)" annotation (release
        # dates, "(Preview)", etc.) stripped, so short Bedrock names like
        # "DeepSeek-R1" can still match "DeepSeek R1 0528 (May '25)".
        bare_name = re.sub(r"\s*\([^)]*\)\s*$", "", rel_name).strip()
        if bare_name != rel_name:
            index.setdefault(tokenize(bare_name), canonical)
    return index


def match_bedrock_to_aa(bedrock_name: str, release_index: dict[tuple, dict]) -> dict | None:
    candidates = [bedrock_name, NAME_ALIASES.get(bedrock_name.lower(), bedrock_name)]
    for cand in candidates:
        key = tokenize(cand)
        if key in release_index:
            return release_index[key]
    # try stripping a trailing size token ("17B", "70B", "405B", ...) from
    # the bedrock name, since AA sometimes omits it when unambiguous.
    stripped = re.sub(r"\s+\d+b(\s+instruct)?$", "", bedrock_name, flags=re.IGNORECASE)
    if stripped != bedrock_name:
        key = tokenize(stripped)
        if key in release_index:
            return release_index[key]
    # try dropping a trailing single-letter qualifier bedrock uses that AA
    # doesn't track separately (e.g. "Gemma 3 27B PT" base/pretrained model).
    stripped2 = re.sub(r"\s+(IT|PT)$", "", bedrock_name)
    if stripped2 != bedrock_name:
        key = tokenize(stripped2)
        if key in release_index:
            return release_index[key]
    return None


def match_bedrock_to_price(bedrock_name: str, pricing: dict[str, dict]) -> dict | None:
    # AWS's price-list "model" attribute strings don't always match the
    # docs catalog spelling (case, punctuation, or word order), so compare
    # via the same order-insensitive token key used for the AA match.
    target = tokenize(bedrock_name)
    for k, v in pricing.items():
        if tokenize(k) == target:
            return v
    return None


# ---------------------------------------------------------------------------
# Assembly + plotting
# ---------------------------------------------------------------------------

def collect_rows(catalog, pricing, release_index):
    rows = []
    unmatched_score = []
    unmatched_price = []
    for provider, models in catalog.items():
        for name in models:
            aa = match_bedrock_to_aa(name, release_index)
            price = match_bedrock_to_price(name, pricing)
            tb21 = aa.get("terminalBench21") if aa else None
            tb40 = aa.get("terminalBench40") if aa else None
            if aa is None:
                unmatched_score.append(f"{provider} / {name}")
            if price is None:
                unmatched_price.append(f"{provider} / {name}")
            rows.append({
                "provider": provider,
                "name": name,
                "category": category_for(provider),
                "tb21": tb21 * 100 if tb21 is not None else None,
                "tb40": tb40 * 100 if tb40 is not None else None,
                "input_price": price["input"] if price else None,
                "output_price": price["output"] if price else None,
                "price_tier": price["tier"] if price else None,
                "price_source": "AWS Bedrock (live)" if price else None,
            })

    # Fall back to Artificial Analysis's own tracked price for anything
    # Bedrock's public price list doesn't cover (e.g. gated/marketplace-only
    # models), clearly labelled as such.
    for row in rows:
        if row["input_price"] is None:
            aa = match_bedrock_to_aa(row["name"], release_index)
            if aa and aa.get("price1mInputTokens") is not None:
                row["input_price"] = aa["price1mInputTokens"]
                row["output_price"] = aa.get("price1mOutputTokens")
                row["price_source"] = "Artificial Analysis (fallback, not AWS list price)"

    return rows, unmatched_score, unmatched_price


def blended(row, out_weight=0.75):
    if row["input_price"] is None or row["output_price"] is None:
        return None
    return (1 - out_weight) * row["input_price"] + out_weight * row["output_price"]


def plot_scatter(rows, score_key, title, outfile):
    fig, ax = plt.subplots(figsize=(9, 6.5))
    seen_categories = set()
    plotted = 0
    for row in rows:
        score = row[score_key]
        cost = blended(row)
        if score is None or cost is None:
            continue
        cat = row["category"]
        ax.scatter(cost, score, color=CATEGORY_COLORS[cat], s=60,
                   edgecolor="white", linewidth=0.6, zorder=3)
        ax.annotate(row["name"], (cost, score), fontsize=7,
                    xytext=(4, 3), textcoords="offset points")
        seen_categories.add(cat)
        plotted += 1

    ax.set_xscale("log")
    ax.set_xlabel("Blended Bedrock cost per 1M tokens (USD, 25% input / 75% output) -- log scale")
    ax.set_ylabel("Resolution rate (%)")
    ax.set_title(title)
    ax.grid(True, which="both", axis="x", linestyle=":", alpha=0.4)
    ax.grid(True, axis="y", linestyle=":", alpha=0.4)

    if seen_categories:
        handles = [plt.Line2D([0], [0], marker="o", linestyle="", color=CATEGORY_COLORS[c])
                   for c in seen_categories]
        ax.legend(handles, seen_categories, title="Provider", loc="lower right", fontsize=8)

    fig.tight_layout()
    fig.savefig(outfile, dpi=150)
    plt.close(fig)
    return plotted


def main():
    print("Fetching Bedrock model catalog...", file=sys.stderr)
    catalog = fetch_bedrock_catalog()
    n_models = sum(len(v) for v in catalog.values())
    print(f"  {n_models} text-generation models across {len(catalog)} providers", file=sys.stderr)

    print("Fetching Bedrock pricing (AWS Price List Bulk API)...", file=sys.stderr)
    pricing = fetch_bedrock_pricing()
    print(f"  priced {len(pricing)} model SKUs", file=sys.stderr)

    print("Fetching Artificial Analysis Terminal-Bench dataset...", file=sys.stderr)
    aa_dataset = fetch_aa_dataset()
    release_index = build_release_index(aa_dataset)
    print(f"  {len(aa_dataset)} model variants, {len(release_index)} release groups", file=sys.stderr)

    rows, unmatched_score, unmatched_price = collect_rows(catalog, pricing, release_index)

    with_tb21 = sum(1 for r in rows if r["tb21"] is not None and blended(r) is not None)
    with_tb40 = sum(1 for r in rows if r["tb40"] is not None and blended(r) is not None)
    print(f"\n{len(rows)} Bedrock text-generation models total", file=sys.stderr)
    print(f"  {with_tb21} plottable on Terminal-Bench 2.1 (score + price)", file=sys.stderr)
    print(f"  {with_tb40} plottable on Terminal-Bench 4.0 (score + price)", file=sys.stderr)

    if unmatched_score:
        print(f"\nNo Terminal-Bench data found for {len(unmatched_score)} models "
              f"(not benchmarked by Artificial Analysis, or name didn't match):", file=sys.stderr)
        for u in unmatched_score:
            print(f"  - {u}", file=sys.stderr)

    aa_fallback = [r["name"] for r in rows if r["price_source"] and "fallback" in r["price_source"]]
    if aa_fallback:
        print(f"\n{len(aa_fallback)} models priced via Artificial Analysis fallback "
              f"(no public AWS Bedrock list price found):", file=sys.stderr)
        for n in aa_fallback:
            print(f"  - {n}", file=sys.stderr)

    still_no_price = [r["name"] for r in rows if r["input_price"] is None]
    if still_no_price:
        print(f"\nNo price found anywhere for {len(still_no_price)} models:", file=sys.stderr)
        for n in still_no_price:
            print(f"  - {n}", file=sys.stderr)

    n21 = plot_scatter(rows, "tb21", "Terminal-Bench 2.1: Score vs. Cost -- Bedrock-available models",
                        OUT_DIR / "tb21_scatter.png")
    n40 = plot_scatter(rows, "tb40", "Terminal-Bench 4.0: Score vs. Cost -- Bedrock-available models",
                        OUT_DIR / "tb40_scatter.png")

    # dump the raw joined table too, for inspection / reuse
    with open(OUT_DIR / "model_data.json", "w") as f:
        json.dump(rows, f, indent=2)

    print(f"\nWrote tb21_scatter.png ({n21} points), tb40_scatter.png ({n40} points), "
          f"and model_data.json to {OUT_DIR}", file=sys.stderr)


if __name__ == "__main__":
    main()
