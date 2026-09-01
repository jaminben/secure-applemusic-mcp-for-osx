#!/usr/bin/env python3
"""Snapshot public download and traffic numbers into an append-only CSV.

Why this exists: most of these numbers are not retained anywhere. GitHub's
traffic API keeps **14 days** and then the data is gone for good — miss a
fortnight and that window is unrecoverable. Per-asset release counts are
cumulative and never expire, but they are also never broken out by date, so
"how many did the 0.3.1 arm64 build get in its first week" is unanswerable
unless something wrote it down at the time. This writes it down.

Long format (date,source,metric,value) rather than one column per metric,
because the metric set grows every release: each new tag adds asset rows, and a
wide CSV would need a new column each time and a backfill of empty cells for
every prior row.

THE IMPORTANT RULE: a source that cannot be reached records NOTHING. It never
records 0. pypistats rate-limits aggressively (HTTP 429) and stays limited for
hours, and the MCP Registry refuses connections outright for minutes at a time,
so unavailable sources are the normal case, not the exception — and a 0 written
for a failed fetch is indistinguishable, a month later, from a real collapse in
downloads. Absent rows are honest; zero rows are a lie you cannot detect
afterwards.

Because pypistats is unreliable enough to leave whole days blank, pepy.tech is
scraped as a second, independent PyPI source. The two are never merged: pepy
counts mirror traffic and pypistats does not, so they disagree by roughly 5x.

Re-running replaces only the (date, source) pairs it actually collected. It must
not drop a whole date: a later run that hit a rate limit would then delete the
numbers an earlier successful run recorded, turning a transient failure into
permanent loss in the one file whose data cannot be recovered later.

Usage:
    python3 scripts/collect_stats.py            # collect and append
    python3 scripts/collect_stats.py --show     # print the trend, collect nothing
    python3 scripts/collect_stats.py --dry-run  # print what would be written
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import date
from pathlib import Path

REPO = "jaminben/secure-applemusic-mcp-for-osx"
PACKAGE = "secure-applemusic-mcp-for-osx"
# The registry indexes under io.github.jaminben/…; this substring finds it.
REGISTRY_SEARCH = "secure-applemusic"
CSV_PATH = Path(__file__).resolve().parent.parent / "stats" / "downloads.csv"
FIELDS = ["date", "source", "metric", "value"]
UA = "secure-applemusic-mcp stats collector (+https://github.com/%s)" % REPO


class Unavailable(Exception):
    """A source could not be reached. The caller records nothing for it."""


def _gh(path: str):
    """Call the GitHub API through `gh`, which already holds the user's auth."""
    try:
        out = subprocess.run(
            ["gh", "api", path, "--paginate"],
            capture_output=True, text=True, timeout=90, check=True,
        ).stdout
    except FileNotFoundError:
        raise Unavailable("gh is not installed")
    except subprocess.TimeoutExpired:
        raise Unavailable("gh timed out")
    except subprocess.CalledProcessError as exc:
        raise Unavailable((exc.stderr or "").strip().splitlines()[0][:120] or "gh failed")
    # --paginate concatenates JSON arrays as "][", which json cannot parse.
    try:
        return json.loads(out.replace("][", ","))
    except json.JSONDecodeError:
        raise Unavailable("gh returned unparseable JSON")


def _json(url: str, timeout: int = 25):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        # 429 is the common one and is worth naming, since it is transient and
        # a later run on the same day will fill the gap.
        raise Unavailable("HTTP %s" % exc.code)
    except Exception as exc:  # noqa: BLE001 - network errors are all equivalent here
        raise Unavailable(type(exc).__name__)
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        raise Unavailable("not JSON (rate-limit page?)")


def collect_github(rows: list, note) -> None:
    """Release asset counts, split by kind, plus repo and traffic signals."""
    try:
        releases = _gh("repos/%s/releases" % REPO)
    except Unavailable as exc:
        note("github releases", exc)
    else:
        zips = wheels = 0
        for rel in releases:
            tag = rel.get("tag_name", "?")
            for asset in rel.get("assets", []):
                name = asset.get("name", "")
                count = asset.get("download_count", 0)
                if name.endswith(".zip"):
                    zips += count
                elif name.endswith(".whl"):
                    wheels += count
                else:
                    continue  # checksums are noise
                # Per-asset rows are what make "first week of 0.3.1" answerable.
                rows.append(("github", "asset:%s/%s" % (tag, name), count))
        rows.append(("github", "zips_total", zips))
        rows.append(("github", "wheels_total", wheels))

    try:
        repo = _gh("repos/%s" % REPO)
    except Unavailable as exc:
        note("github repo", exc)
    else:
        rows.append(("github", "stars", repo.get("stargazers_count", 0)))
        rows.append(("github", "forks", repo.get("forks_count", 0)))
        rows.append(("github", "watchers", repo.get("subscribers_count", 0)))

    # The perishable ones. GitHub keeps 14 days; after that it is gone.
    for kind in ("views", "clones"):
        try:
            data = _gh("repos/%s/traffic/%s" % (REPO, kind))
        except Unavailable as exc:
            note("github %s" % kind, exc)
            continue
        rows.append(("github", "%s_14d" % kind, data.get("count", 0)))
        rows.append(("github", "%s_uniq_14d" % kind, data.get("uniques", 0)))


def collect_pypi(rows: list, note) -> None:
    """PyPI download counts. Frequently unavailable; that is expected."""
    try:
        data = _json("https://pypistats.org/api/packages/%s/recent" % PACKAGE)
    except Unavailable as exc:
        note("pypistats", exc)
    else:
        d = data.get("data", {})
        for key in ("last_day", "last_week", "last_month"):
            if key in d:
                rows.append(("pypi", key, d[key]))

    # Whether the package resolves at all, and at what version. Cheap, and it
    # catches a yanked or renamed release before the download numbers explain it.
    try:
        meta = _json("https://pypi.org/pypi/%s/json" % PACKAGE)
    except Unavailable as exc:
        note("pypi metadata", exc)
    else:
        rows.append(("pypi", "latest_version", meta["info"]["version"]))


def collect_pepy(rows: list, note) -> None:
    """All-time PyPI downloads, scraped from pepy.tech's page title.

    pypistats is the better source but rate-limits hard and stays limited, which
    left whole days with no PyPI figure at all. pepy's API needs a key; its page
    does not, and is server-rendered, so the total is sitting in the <title>:

        <title>secure-applemusic-mcp-for-osx · 378 downloads</title>

    That is a scrape and will break the day they change the title. It records
    nothing when the pattern misses, same as any other unreachable source.

    Kept as its own source because pepy counts mirror traffic and pypistats does
    not -- 378 against 79 for overlapping windows. Never average the two.
    """
    req = urllib.request.Request(
        "https://pepy.tech/projects/%s" % PACKAGE,
        headers={"User-Agent": "Mozilla/5.0 (Macintosh)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            html = resp.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        note("pepy", Unavailable(type(exc).__name__))
        return
    match = re.search(r"<title>[^<]*?([\d,]+)\s+downloads", html)
    if not match:
        note("pepy", Unavailable("page title no longer carries the total"))
        return
    rows.append(("pepy", "total", int(match.group(1).replace(",", ""))))


def collect_registry(rows: list, note) -> None:
    """What the MCP Registry currently serves. Flaky — it refuses connections
    for minutes at a time — so this is best-effort like everything else."""
    url = ("https://registry.modelcontextprotocol.io/v0/servers?search=%s"
           % REGISTRY_SEARCH)
    try:
        data = _json(url)
    except Unavailable as exc:
        note("mcp registry", exc)
        return
    for entry in data.get("servers", []):
        server = entry.get("server", entry)
        meta = (entry.get("_meta") or server.get("_meta") or {}).get(
            "io.modelcontextprotocol.registry/official", {})
        if meta.get("isLatest"):
            rows.append(("registry", "latest_version", server.get("version", "?")))
            rows.append(("registry", "packages", len(server.get("packages") or [])))
            return
    note("mcp registry", Unavailable("no isLatest entry found"))


def read_existing() -> list[dict]:
    if not CSV_PATH.exists():
        return []
    with CSV_PATH.open(newline="") as fh:
        return list(csv.DictReader(fh))


def write_all(records: list[dict]) -> None:
    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CSV_PATH.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()
        # Sorted so a diff between two runs is readable rather than a reshuffle.
        for rec in sorted(records, key=lambda r: (r["date"], r["source"], r["metric"])):
            writer.writerow(rec)


def show_trend() -> int:
    records = read_existing()
    if not records:
        print("No stats collected yet. Run: python3 scripts/collect_stats.py")
        return 1
    by_date: dict[str, dict[str, str]] = defaultdict(dict)
    for rec in records:
        by_date[rec["date"]]["%s.%s" % (rec["source"], rec["metric"])] = rec["value"]
    headline = ["github.zips_total", "github.wheels_total", "pepy.total",
                "pypi.last_day", "pypi.last_month", "github.stars",
                "github.clones_14d"]
    width = max(len(h) for h in headline) + 2
    dates = sorted(by_date)[-10:]
    print("%-*s %s" % (width, "metric", "  ".join("%8s" % d[5:] for d in dates)))
    for metric in headline:
        cells = ["%8s" % by_date[d].get(metric, "-") for d in dates]
        print("%-*s %s" % (width, metric, "  ".join(cells)))
    print("\n  '-' means the source was unreachable that day, not zero.")
    print("  %s" % CSV_PATH)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--show", action="store_true", help="print the trend and exit")
    ap.add_argument("--dry-run", action="store_true", help="collect but do not write")
    args = ap.parse_args()

    if args.show:
        return show_trend()

    today = date.today().isoformat()
    collected: list = []
    problems: list[str] = []

    def note(source: str, exc: Exception) -> None:
        problems.append("%s: %s" % (source, exc))

    collect_github(collected, note)
    collect_pypi(collected, note)
    collect_pepy(collected, note)
    collect_registry(collected, note)

    if not collected:
        print("Every source was unreachable; nothing written.", file=sys.stderr)
        for problem in problems:
            print("  - %s" % problem, file=sys.stderr)
        return 1

    new_rows = [
        {"date": today, "source": src, "metric": met, "value": str(val)}
        for src, met, val in collected
    ]

    if args.dry_run:
        for row in new_rows:
            print("  %(date)s  %(source)-9s %(metric)-46s %(value)s" % row)
    else:
        # Replace only the (date, source) pairs this run actually collected.
        # Dropping every row for today would mean a later run that hit a 429
        # DELETES the numbers an earlier successful run recorded -- turning a
        # transient rate-limit into permanent data loss. It also preserves rows
        # added by hand, such as the reconstructed 2026-08-30 snapshot.
        fresh = {src for src, _, _ in collected}
        kept = [r for r in read_existing()
                if not (r["date"] == today and r["source"] in fresh)]
        write_all(kept + new_rows)

    headline = {(s, m): v for s, m, v in collected}
    print("%s — %d metrics%s" % (today, len(new_rows), " (dry run)" if args.dry_run else ""))
    for label, key in (("GitHub app zips", ("github", "zips_total")),
                       ("GitHub wheels  ", ("github", "wheels_total")),
                       ("PyPI all-time ", ("pepy", "total")),
                       ("PyPI last day  ", ("pypi", "last_day")),
                       ("PyPI last month", ("pypi", "last_month")),
                       ("Stars          ", ("github", "stars"))):
        if key in headline:
            print("  %s : %s" % (label, headline[key]))
    if problems:
        print("\nUnavailable (recorded as absent, NOT as zero):")
        for problem in problems:
            print("  - %s" % problem)
    if not args.dry_run:
        print("\n  %s" % CSV_PATH)
    return 0


if __name__ == "__main__":
    sys.exit(main())
