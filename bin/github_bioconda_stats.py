#!/usr/bin/env python3
"""Count GitHub repositories with more than N stars across a set of accounts,
plus any repository pinned to those accounts' profile pages, and report the
total number of bioconda downloads for each.

Repositories are excluded if they are archived, or if their last commit is
older than a cutoff year (default: nothing committed since 2022 or earlier).

Bioconda packages are matched to repositories in two ways:

1. GitHub code search over ``bioconda/bioconda-recipes`` for recipes that
   reference the repository URL.  This is the reliable route -- it finds
   packages whose name differs from the repository name (e.g.
   ``rhysnewell/Lorikeet`` -> ``lorikeet-genome``).
2. For repositories no recipe references by URL (recipes that build from
   PyPI rather than GitHub), candidate package names derived from the
   repository name are looked up on anaconda.org and accepted only if the
   package's ``dev_url``/``home`` points back at the repository.

Download counts come from anaconda.org's ``ndownloads`` for the bioconda
channel, i.e. all-time downloads of every file of that package.

Requires a GitHub token in GITHUB_TOKEN or GH_TOKEN.  No third-party
dependencies.

Examples
--------
    ./github_bioconda_stats.py
    ./github_bioconda_stats.py --min-stars 100 --tsv results.tsv
    ./github_bioconda_stats.py --cache gh.json      # reuse GitHub half
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_ACCOUNTS = [
    "wwood",
    "geronimp",
    "AroneyS",
    "BigDataBiology",
    "luispedro",
    "chklovski",
]

GITHUB_API = "https://api.github.com"
ANACONDA_API = "https://api.anaconda.org"
USER_AGENT = "cmr-github-bioconda-stats/1.0"

# Seconds between GitHub code-search calls: that endpoint allows 10 requests
# per minute for authenticated users.
CODE_SEARCH_INTERVAL = 6.5


class GitHubError(Exception):
    pass


def _token():
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(var):
            return os.environ[var]
    raise GitHubError(
        "no GitHub token found -- set GITHUB_TOKEN or GH_TOKEN "
        "(a classic or fine-grained token with public-repo read access)"
    )


def _request(url, headers=None, data=None, retries=4):
    """GET (or POST, if data is given) returning parsed JSON."""
    headers = dict(headers or {})
    headers.setdefault("User-Agent", USER_AGENT)
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        headers.setdefault("Content-Type", "application/json")

    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            # 403/429 from the search endpoints means secondary rate limiting.
            if exc.code in (403, 429) and attempt < retries:
                wait = float(exc.headers.get("Retry-After") or 2 ** (attempt + 3))
                sys.stderr.write("  rate limited, sleeping %.0fs\n" % wait)
                time.sleep(wait)
                continue
            if exc.code == 404:
                return None
            detail = exc.read().decode(errors="replace")[:400]
            raise GitHubError("%s -> HTTP %d: %s" % (url, exc.code, detail))
        except urllib.error.URLError as exc:
            if attempt < retries:
                time.sleep(2 ** (attempt + 1))
                continue
            raise GitHubError("%s -> %s" % (url, exc))
    raise GitHubError("%s -> exhausted retries" % url)


def gh_get(path, params=None):
    url = GITHUB_API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return _request(
        url,
        headers={
            "Authorization": "Bearer " + _token(),
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )


def gh_graphql(query, variables=None):
    result = _request(
        GITHUB_API + "/graphql",
        headers={"Authorization": "Bearer " + _token()},
        data={"query": query, "variables": variables or {}},
    )
    if result and result.get("errors"):
        raise GitHubError("GraphQL: %s" % json.dumps(result["errors"])[:400])
    return (result or {}).get("data")


def search_repositories(query, per_page=100):
    """All repositories matching a GitHub repository-search query."""
    items = []
    page = 1
    while True:
        result = gh_get(
            "/search/repositories",
            {"q": query, "per_page": per_page, "page": page},
        )
        batch = (result or {}).get("items", [])
        items.extend(batch)
        if len(batch) < per_page or len(items) >= 1000:
            break
        page += 1
    return items


PINNED_QUERY = """
query($login: String!) {
  repositoryOwner(login: $login) {
    ... on ProfileOwner {
      itemShowcase {
        items(first: 6) {
          nodes { ... on Repository { nameWithOwner } }
        }
      }
    }
  }
}
"""


def pinned_repositories(login):
    """Repositories pinned to an account's profile page.

    Uses GraphQL, which is the only API that exposes pins, and falls back to
    scraping the profile HTML if GraphQL is unavailable.
    """
    try:
        data = gh_graphql(PINNED_QUERY, {"login": login})
        owner = (data or {}).get("repositoryOwner") or {}
        showcase = owner.get("itemShowcase") or {}
        nodes = (showcase.get("items") or {}).get("nodes") or []
        return [n["nameWithOwner"] for n in nodes if n and n.get("nameWithOwner")]
    except GitHubError as exc:
        sys.stderr.write("  GraphQL pinned lookup failed (%s); scraping HTML\n" % exc)

    req = urllib.request.Request(
        "https://github.com/" + login, headers={"User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(req, timeout=60) as response:
        html = response.read().decode(errors="replace")
    # Each pin is an <a> whose href is the repo path, inside a pinned-item card.
    cards = re.findall(
        r'class="[^"]*pinned-item-list-item[^"]*".*?</li>', html, re.S
    )
    pinned = []
    for card in cards:
        match = re.search(r'href="/([^/"]+)/([^/"?#]+)"', card)
        if match:
            full = "%s/%s" % (match.group(1), match.group(2))
            if full not in pinned:
                pinned.append(full)
    return pinned


def get_repo(full_name):
    return gh_get("/repos/" + full_name)


def last_commit_date(full_name, default_branch=None):
    """ISO date of the most recent commit on the default branch."""
    params = {"per_page": 1}
    if default_branch:
        params["sha"] = default_branch
    commits = gh_get("/repos/%s/commits" % full_name, params)
    if not commits:
        return None
    commit = commits[0].get("commit", {})
    date = (commit.get("committer") or {}).get("date") or (
        commit.get("author") or {}
    ).get("date")
    return date


def bioconda_recipes_for_repo(full_name):
    """Recipe directory names in bioconda-recipes that reference this repo."""
    query = '"%s" repo:bioconda/bioconda-recipes path:recipes' % full_name
    result = gh_get("/search/code", {"q": query, "per_page": 100})
    recipes = []
    for item in (result or {}).get("items", []):
        parts = item.get("path", "").split("/")
        # recipes/<package>/meta.yaml or recipes/<package>/<version>/meta.yaml
        if len(parts) >= 2 and parts[0] == "recipes" and parts[1] not in recipes:
            recipes.append(parts[1])
    return recipes


def anaconda_package(name, channel="bioconda"):
    return _request("%s/package/%s/%s" % (ANACONDA_API, channel, name))


def candidate_package_names(full_name):
    """Plausible bioconda package names for a repository."""
    name = full_name.split("/")[-1]
    lowered = name.lower()
    candidates = [lowered, lowered.replace("_", "-"), lowered.replace("-", "_")]
    for suffix in ("-download", "-genome", "-tool", "-toolkit", "-py"):
        if lowered.endswith(suffix):
            candidates.append(lowered[: -len(suffix)])
    seen = []
    for candidate in candidates:
        if candidate and candidate not in seen:
            seen.append(candidate)
    return seen


def package_points_at_repo(package, full_name):
    """Whether an anaconda package's metadata references this repository.

    Recipes point at a repository either directly (github.com/owner/repo) or
    via its documentation site (owner.github.io/repo, readthedocs, ...), so a
    URL naming both the owner and the repository counts as a match.
    """
    owner, name = full_name.lower().split("/")
    for field in ("dev_url", "home", "source_git_url", "doc_url"):
        value = (package.get(field) or "").lower()
        if not value:
            continue
        if full_name.lower() in value:
            return True
        if owner in value and name in value:
            return True
    return False


def bioconda_packages_for_repo(full_name, use_code_search=True):
    """(package names, how they were matched) for one repository."""
    packages = []
    if use_code_search:
        for recipe in bioconda_recipes_for_repo(full_name):
            if anaconda_package(recipe):
                packages.append(recipe)
        if packages:
            return packages, "recipe-url"

    for candidate in candidate_package_names(full_name):
        package = anaconda_package(candidate)
        if package and package_points_at_repo(package, full_name):
            return [candidate], "name+dev_url"
    return [], "none"


def package_downloads(name, channel="bioconda"):
    package = anaconda_package(name, channel)
    if not package:
        return 0
    total = package.get("ndownloads")
    if total is None:
        total = sum(f.get("ndownloads", 0) for f in package.get("files", []))
    return total


def collect_github(accounts, min_stars, cutoff_year, verbose=True):
    """Candidate repositories and the filters each one passed or failed."""
    candidates = {}  # full_name -> {"sources": set()}

    for account in accounts:
        if verbose:
            sys.stderr.write("Searching repositories owned by %s\n" % account)
        # GitHub repository search excludes forks by default; fork:true adds
        # them back so a pinned, heavily starred fork is not missed.
        for query in (
            "user:%s stars:>%d" % (account, min_stars),
            "user:%s stars:>%d fork:true" % (account, min_stars),
        ):
            for repo in search_repositories(query):
                entry = candidates.setdefault(
                    repo["full_name"], {"repo": repo, "sources": set()}
                )
                entry["repo"] = repo
                entry["sources"].add("owned:%s" % account)

    for account in accounts:
        if verbose:
            sys.stderr.write("Reading pinned repositories of %s\n" % account)
        for full_name in pinned_repositories(account):
            entry = candidates.setdefault(full_name, {"repo": None, "sources": set()})
            entry["sources"].add("pinned:%s" % account)
            if entry["repo"] is None:
                repo = get_repo(full_name)
                if repo is None:
                    sys.stderr.write("  %s not found, skipping\n" % full_name)
                    del candidates[full_name]
                    continue
                entry["repo"] = repo

    rows = []
    for full_name in sorted(candidates):
        entry = candidates[full_name]
        repo = entry["repo"]
        stars = repo.get("stargazers_count", 0)
        archived = bool(repo.get("archived"))

        committed = None
        if stars > min_stars and not archived:
            committed = last_commit_date(full_name, repo.get("default_branch"))
        elif stars > min_stars:
            committed = repo.get("pushed_at")

        reasons = []
        if stars <= min_stars:
            reasons.append("<=%d stars" % min_stars)
        if archived:
            reasons.append("archived")
        if committed and int(committed[:4]) <= cutoff_year:
            reasons.append("last commit %s" % committed[:10])

        rows.append(
            {
                "full_name": full_name,
                "stars": stars,
                "archived": archived,
                "last_commit": committed,
                "sources": sorted(entry["sources"]),
                "included": not reasons,
                "excluded_because": reasons,
            }
        )
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--accounts",
        nargs="+",
        default=DEFAULT_ACCOUNTS,
        help="GitHub users/organisations to include (default: %(default)s)",
    )
    parser.add_argument(
        "--min-stars",
        type=int,
        default=50,
        help="keep repositories with strictly more stars than this "
        "(default: %(default)s)",
    )
    parser.add_argument(
        "--cutoff-year",
        type=int,
        default=2022,
        help="exclude repositories whose last commit is in this year or "
        "earlier (default: %(default)s)",
    )
    parser.add_argument(
        "--cache",
        help="JSON file holding the GitHub half of the results; read from it "
        "if it exists, otherwise query GitHub and write it. Rows that already "
        "carry a bioconda_packages key skip the recipe code search",
    )
    parser.add_argument(
        "--no-code-search",
        action="store_true",
        help="skip the bioconda-recipes code search and match packages by "
        "name only (much faster, but misses renamed packages)",
    )
    parser.add_argument("--tsv", help="also write the per-repository table here")
    parser.add_argument(
        "--json", dest="json_out", help="write the full results as JSON here"
    )
    args = parser.parse_args(argv)

    if args.cache and os.path.exists(args.cache):
        sys.stderr.write("Reading GitHub results from %s\n" % args.cache)
        with open(args.cache) as handle:
            rows = json.load(handle)
    else:
        rows = collect_github(args.accounts, args.min_stars, args.cutoff_year)
        if args.cache:
            with open(args.cache, "w") as handle:
                json.dump(rows, handle, indent=2)

    included = [r for r in rows if r["included"]]
    excluded = [r for r in rows if not r["included"]]

    sys.stderr.write("\nLooking up bioconda packages for %d repositories\n" % len(included))
    searched = 0
    for row in included:
        if row.get("bioconda_packages") is not None:
            # The cache already knows which recipes reference this repository.
            packages, how = row["bioconda_packages"], row.get("bioconda_match", "cached")
        else:
            if searched and not args.no_code_search:
                time.sleep(CODE_SEARCH_INTERVAL)
            searched += 1
            packages, how = bioconda_packages_for_repo(
                row["full_name"], use_code_search=not args.no_code_search
            )
        row["bioconda_packages"] = packages
        row["bioconda_match"] = how
        row["bioconda_downloads"] = sum(package_downloads(p) for p in packages)
        sys.stderr.write(
            "  %-45s %-22s %s\n"
            % (
                row["full_name"],
                ",".join(packages) or "-",
                "{:,}".format(row["bioconda_downloads"]) if packages else "-",
            )
        )

    included.sort(key=lambda r: -r["stars"])

    header = ("repository", "stars", "last_commit", "source", "bioconda", "downloads")
    widths = [42, 6, 12, 26, 18, 11]
    print()
    print("  ".join(h.ljust(w) for h, w in zip(header, widths)).rstrip())
    print("  ".join("-" * w for w in widths))
    for row in included:
        print(
            "  ".join(
                value.ljust(width)
                for value, width in zip(
                    (
                        row["full_name"],
                        str(row["stars"]),
                        (row["last_commit"] or "?")[:10],
                        ",".join(row["sources"])[:26],
                        ",".join(row["bioconda_packages"]) or "-",
                        "{:,}".format(row["bioconda_downloads"])
                        if row["bioconda_packages"]
                        else "-",
                    ),
                    widths,
                )
            ).rstrip()
        )

    with_conda = [r for r in included if r["bioconda_packages"]]
    print()
    print("Repositories matching all criteria: %d" % len(included))
    print("  of which on bioconda:             %d" % len(with_conda))
    print("Total stars:                        {:,}".format(sum(r["stars"] for r in included)))
    print(
        "Total bioconda downloads:           {:,}".format(
            sum(r["bioconda_downloads"] for r in included)
        )
    )

    if excluded:
        print("\nExcluded (%d):" % len(excluded))
        for row in sorted(excluded, key=lambda r: -r["stars"]):
            print(
                "  %-45s %5d stars  %s"
                % (row["full_name"], row["stars"], "; ".join(row["excluded_because"]))
            )

    if args.tsv:
        with open(args.tsv, "w") as handle:
            handle.write("repository\tstars\tlast_commit\tsources\tbioconda_packages\tbioconda_downloads\n")
            for row in included:
                handle.write(
                    "%s\t%d\t%s\t%s\t%s\t%d\n"
                    % (
                        row["full_name"],
                        row["stars"],
                        row["last_commit"] or "",
                        ",".join(row["sources"]),
                        ",".join(row["bioconda_packages"]),
                        row["bioconda_downloads"],
                    )
                )
        sys.stderr.write("\nWrote %s\n" % args.tsv)

    if args.json_out:
        with open(args.json_out, "w") as handle:
            json.dump(rows, handle, indent=2)
        sys.stderr.write("Wrote %s\n" % args.json_out)

    return 0


if __name__ == "__main__":
    sys.exit(main())
