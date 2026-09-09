import csv
import html
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
ROSTER_PATH = ROOT / "data" / "researchers.csv"
OUTPUT_DIR = ROOT / "docs"
JSON_OUTPUT = OUTPUT_DIR / "publications.json"
HTML_OUTPUT = OUTPUT_DIR / "index.html"

ORCID_API = "https://pub.orcid.org/v3.0"
TOKEN_URL = "https://orcid.org/oauth/token"
REQUEST_TIMEOUT = 30
BATCH_SIZE = 50


def get_env(name):
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def request_token():
    response = requests.post(
        TOKEN_URL,
        data={
            "client_id": get_env("ORCID_CLIENT_ID"),
            "client_secret": get_env("ORCID_CLIENT_SECRET"),
            "grant_type": "client_credentials",
            "scope": "/read-public",
        },
        headers={"Accept": "application/json"},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()

    access_token = response.json().get("access_token")

    if not access_token:
        raise RuntimeError("ORCID did not return an access token.")

    return access_token


def api_get(path, token):
    response = requests.get(
        f"{ORCID_API}{path}",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        },
        timeout=REQUEST_TIMEOUT,
    )

    if response.status_code == 404:
        return None

    response.raise_for_status()
    return response.json()


def read_researchers():
    researchers = []

    with ROSTER_PATH.open(newline="", encoding="utf-8") as file:
        for row in csv.DictReader(file):
            name = row["name"].strip()
            orcid = row["orcid"].strip().upper()

            if not name or not orcid:
                continue

            if not re.fullmatch(r"\d{4}-\d{4}-\d{4}-\d{3}[\dX]", orcid):
                print(f"Skipping invalid ORCID format: {name} — {orcid}")
                continue

            researchers.append({"name": name, "orcid": orcid})

    return researchers


def chunked(items, size):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def get_put_codes(orcid, token):
    record = api_get(f"/{orcid}/works", token)

    if not record:
        return []

    put_codes = []

    for group in record.get("group", []):
        for summary in group.get("work-summary", []):
            put_code = summary.get("put-code")
            if put_code is not None:
                put_codes.append(str(put_code))

    return sorted(set(put_codes))


def get_full_works(orcid, put_codes, token):
    works = []

    for batch in chunked(put_codes, BATCH_SIZE):
        response = api_get(f"/{orcid}/works/{','.join(batch)}", token)

        if not response:
            continue

        works.extend(item for item in response.get("bulk", []) if item)
        time.sleep(0.15)

    return works


def text_value(data, *keys):
    current = data

    for key in keys:
        if not isinstance(current, dict):
            return ""
        current = current.get(key)

    return current.strip() if isinstance(current, str) else ""


def publication_year(work):
    year = text_value(work, "publication-date", "year", "value")

    if year and re.fullmatch(r"\d{4}", year):
        return int(year)

    created_at = text_value(work, "created-date", "value")

    if created_at.isdigit():
        return datetime.fromtimestamp(
            int(created_at) / 1000,
            tz=timezone.utc,
        ).year

    return 0


def normalize_doi(value):
    doi = (value or "").strip().lower()
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi)
    doi = re.sub(r"^doi:\s*", "", doi)
    return doi.rstrip(" .;,")


def get_doi(work):
    for external_id in work.get("external-ids", {}).get("external-id", []):
        if external_id.get("external-id-type", "").lower() == "doi":
            doi = normalize_doi(external_id.get("external-id-value"))
            if doi:
                return doi

    return ""


def get_authors(work):
    authors = []

    for contributor in work.get("contributors", {}).get("contributor", []):
        name = text_value(contributor, "credit-name", "value")
        if name:
            authors.append(name)

    return authors


def normalize_title(title):
    return re.sub(r"[^a-z0-9]", "", title.lower())


def convert_work(work, researcher):
    title = text_value(work, "title", "title", "value")
    subtitle = text_value(work, "title", "subtitle", "value")

    if subtitle:
        title = f"{title}: {subtitle}"

    return {
        "title": title or "Untitled work",
        "year": publication_year(work),
        "doi": get_doi(work),
        "journal": text_value(work, "journal-title", "value"),
        "volume": text_value(work, "journal-issue", "volume"),
        "issue": text_value(work, "journal-issue", "issue"),
        "pages": text_value(work, "journal-issue", "page-range"),
        "type": work.get("type", ""),
        "authors": get_authors(work),
        "group_members": [researcher["name"]],
        "source_orcids": [researcher["orcid"]],
    }


def duplicate_key(publication):
    if publication["doi"]:
        return f"doi:{publication['doi']}"

    return (
        f"title:{normalize_title(publication['title'])}"
        f"|year:{publication['year']}"
    )


def merge_work(existing, incoming):
    existing["group_members"] = sorted(
        set(existing["group_members"]) | set(incoming["group_members"])
    )
    existing["source_orcids"] = sorted(
        set(existing["source_orcids"]) | set(incoming["source_orcids"])
    )

    for field in ["title", "journal", "volume", "issue", "pages", "type"]:
        if not existing.get(field) and incoming.get(field):
            existing[field] = incoming[field]

    if not existing["authors"] and incoming["authors"]:
        existing["authors"] = incoming["authors"]

    if not existing["doi"] and incoming["doi"]:
        existing["doi"] = incoming["doi"]

    existing["year"] = max(existing["year"], incoming["year"])


def citation_text(publication):
    parts = []

    if publication["authors"]:
        parts.append(", ".join(publication["authors"]) + ".")

    parts.append(publication["title"] + ".")

    journal_details = publication["journal"]

    if publication["volume"]:
        journal_details += f", {publication['volume']}"

    if publication["issue"]:
        journal_details += f"({publication['issue']})"

    if publication["pages"]:
        journal_details += f", {publication['pages']}"

    if publication["year"]:
        journal_details += f" ({publication['year']})"

    if journal_details:
        parts.append(journal_details + ".")

    return " ".join(parts)


def render_html(publications, generated_at):
    by_year = defaultdict(list)

    for publication in publications:
        by_year[publication["year"]].append(publication)

    lines = [
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        '  <meta charset="utf-8">',
        '  <meta name="viewport" content="width=device-width, initial-scale=1">',
        "  <title>Group Publications</title>",
        "  <style>",
        "    body { font-family: Arial, sans-serif; color: #1f2937; line-height: 1.6; max-width: 980px; margin: 2rem auto; padding: 0 1rem; }",
        "    h1 { margin-bottom: 0.25rem; }",
        "    h2 { margin-top: 2.25rem; border-bottom: 1px solid #d1d5db; padding-bottom: 0.35rem; }",
        "    li { margin-bottom: 1rem; }",
        "    .meta, .members { color: #4b5563; font-size: 0.9rem; }",
        "    a { color: #0757c8; }",
        "  </style>",
        "</head>",
        "<body>",
        "  <h1>Group Publications</h1>",
        f'  <p class="meta">Compiled from public ORCID records. Last updated: {html.escape(generated_at)}.</p>',
    ]

    for year in sorted(by_year, reverse=True):
        heading = str(year) if year else "Year unavailable"
        lines.append(f"  <h2>{html.escape(heading)}</h2>")
        lines.append("  <ol>")

        for publication in by_year[year]:
            citation = html.escape(citation_text(publication))
            members = html.escape(", ".join(publication["group_members"]))
            doi = publication["doi"]

            if doi:
                doi_link = (
                    f' <a href="https://doi.org/{html.escape(doi)}" '
                    f'target="_blank" rel="noopener noreferrer">View publication</a>'
                )
            else:
                doi_link = ""

            lines.append(
                f'    <li>{citation}{doi_link}'
                f'<div class="members">Group member(s): {members}</div></li>'
            )

        lines.append("  </ol>")

    lines.extend([
        "</body>",
        "</html>",
    ])

    return "\n".join(lines)


def main():
    OUTPUT_DIR.mkdir(exist_ok=True)

    researchers = read_researchers()

    if not researchers:
        raise RuntimeError("No valid ORCID records were found in data/researchers.csv.")

    token = request_token()
    publications = {}
    failures = []

    for number, researcher in enumerate(researchers, start=1):
        print(f"[{number}/{len(researchers)}] {researcher['name']}")

        try:
            put_codes = get_put_codes(researcher["orcid"], token)
            works = get_full_works(researcher["orcid"], put_codes, token)

            for work in works:
                publication = convert_work(work, researcher)
                key = duplicate_key(publication)

                if key in publications:
                    merge_work(publications[key], publication)
                else:
                    publications[key] = publication

        except requests.HTTPError as error:
            failures.append({
                "name": researcher["name"],
                "orcid": researcher["orcid"],
                "error": str(error),
            })
            print(f"Warning: {error}")

        time.sleep(0.15)

    ordered_publications = sorted(
        publications.values(),
        key=lambda publication: (
            publication["year"],
            publication["title"].lower(),
        ),
        reverse=True,
    )

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    json_output = {
        "generated_at": generated_at,
        "researchers_processed": len(researchers),
        "publication_count": len(ordered_publications),
        "failures": failures,
        "publications": ordered_publications,
    }

    JSON_OUTPUT.write_text(
        json.dumps(json_output, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    HTML_OUTPUT.write_text(
        render_html(ordered_publications, generated_at),
        encoding="utf-8",
    )

    print(f"Created {JSON_OUTPUT}")
    print(f"Created {HTML_OUTPUT}")
    print(f"Unique publications: {len(ordered_publications)}")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Build failed: {error}", file=sys.stderr)
        sys.exit(1)
