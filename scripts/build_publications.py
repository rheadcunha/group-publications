import csv
import html
import json
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
JSON_OUTPUT = OUTPUT_DIR / "publications-by-researcher.json"
HTML_OUTPUT = OUTPUT_DIR / "index.html"

ORCID_API = "https://pub.orcid.org/v3.0"
REQUEST_TIMEOUT = 30
REQUEST_PAUSE_SECONDS = 0.2


def api_get(path):
    response = requests.get(
        f"{ORCID_API}{path}",
        headers={"Accept": "application/json"},
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
            name = (row.get("name") or "").strip()
            orcid = (row.get("orcid") or "").strip().upper()

            if not name or not orcid:
                continue

            if not re.fullmatch(r"\d{4}-\d{4}-\d{4}-\d{3}[\dX]", orcid):
                print(f"Skipping invalid ORCID format: {name} — {orcid}")
                continue

            researchers.append({"name": name, "orcid": orcid})

    return researchers


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

    created = text_value(work, "created-date", "value")

    if created.isdigit():
        return datetime.fromtimestamp(
            int(created) / 1000,
            tz=timezone.utc,
        ).year

    return 0


def normalize_doi(value):
    doi = (value or "").strip().lower()
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi)
    doi = re.sub(r"^doi:\s*", "", doi)
    return doi.rstrip(" .;,")


def get_doi(work):
    external_ids = (work.get("external-ids") or {}).get("external-id") or []

    for external_id in external_ids:
        external_id = external_id or {}
        external_id_type = (external_id.get("external-id-type") or "").lower()

        if external_id_type == "doi":
            doi = normalize_doi(external_id.get("external-id-value"))

            if doi:
                return doi

    return ""


def get_authors(work):
    contributors = (work.get("contributors") or {}).get("contributor") or []
    authors = []

    for contributor in contributors:
        contributor = contributor or {}
        name = text_value(contributor, "credit-name", "value")

        if name:
            authors.append(name)

    return authors


def get_title(work):
    title = text_value(work, "title", "title", "value")
    subtitle = text_value(work, "title", "subtitle", "value")

    if subtitle:
        return f"{title}: {subtitle}"

    return title or "Untitled work"


def format_work_type(work_type):
    return (work_type or "Other").replace("_", " ").title()


def get_put_codes(orcid):
    data = api_get(f"/{orcid}/works")

    if not data:
        return []

    put_codes = []

    for group in data.get("group") or []:
        group = group or {}

        for summary in group.get("work-summary") or []:
            summary = summary or {}
            put_code = summary.get("put-code")

            if put_code is not None:
                put_codes.append(str(put_code))

    return sorted(set(put_codes))


def get_detailed_work(orcid, put_code):
    try:
        return api_get(f"/{orcid}/work/{put_code}")
    except requests.HTTPError as error:
        print(f"  Skipping work {put_code}: {error}")
        return None

def make_publication(work):
    return {
        "put_code": str(work.get("put-code", "")),
        "title": get_title(work),
        "year": publication_year(work),
        "doi": get_doi(work),
        "journal": text_value(work, "journal-title", "value"),
        "volume": text_value(work, "journal-issue", "volume"),
        "issue": text_value(work, "journal-issue", "issue"),
        "pages": text_value(work, "journal-issue", "page-range"),
        "type": work.get("type", ""),
        "authors": get_authors(work),
        "url": text_value(work, "url", "value"),
    }


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


def render_publication(publication):
    citation = html.escape(citation_text(publication))
    type_label = html.escape(format_work_type(publication["type"]))
    links = [f'<span class="type">{type_label}</span>']

    if publication["doi"]:
        doi = html.escape(publication["doi"])
        links.append(
            f'<a href="https://doi.org/{doi}" target="_blank" '
            f'rel="noopener noreferrer">DOI</a>'
        )
    elif publication["url"]:
        url = html.escape(publication["url"], quote=True)
        links.append(
            f'<a href="{url}" target="_blank" rel="noopener noreferrer">'
            f'View record</a>'
        )

    return (
        f"      <li>{citation}"
        f'<div class="publication-meta">{" · ".join(links)}</div>'
        f"      </li>"
    )


def render_html(researchers, generated_at):
    lines = [
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        '  <meta charset="utf-8">',
        '  <meta name="viewport" content="width=device-width, initial-scale=1">',
        "  <title>Publications by Researcher</title>",
        "  <style>",
        "    :root { color-scheme: light; }",
        "    body { font-family: Arial, Helvetica, sans-serif; color: #1f2937; line-height: 1.6; max-width: 1080px; margin: 2rem auto; padding: 0 1rem 4rem; }",
        "    h1 { margin-bottom: 0.2rem; }",
        "    h2 { margin: 2.8rem 0 0.25rem; padding-top: 0.4rem; border-top: 2px solid #0f4c81; }",
        "    h3 { margin: 1.6rem 0 0.4rem; border-bottom: 1px solid #d1d5db; padding-bottom: 0.25rem; }",
        "    ol { padding-left: 1.5rem; }",
        "    li { margin-bottom: 1rem; }",
        "    .meta, .orcid, .publication-meta, .none { color: #4b5563; font-size: 0.92rem; }",
        "    .orcid a { color: #0757c8; }",
        "    .publication-meta { margin-top: 0.2rem; }",
        "    .publication-meta a { color: #0757c8; }",
        "    .type { display: inline-block; background: #e8f0fe; color: #174ea6; border-radius: 999px; padding: 0.05rem 0.5rem; }",
        "    .count { color: #4b5563; font-weight: normal; font-size: 1rem; }",
        "  </style>",
        "</head>",
        "<body>",
        "  <h1>Publications by Researcher</h1>",
        '  <p class="meta">Publicly visible ORCID works, organized by individual researcher. Last updated: '
        f"{html.escape(generated_at)}.</p>",
    ]

    for researcher in researchers:
        name = html.escape(researcher["name"])
        orcid = html.escape(researcher["orcid"])
        works = researcher["publications"]

        lines.append(f'  <section id="orcid-{orcid}">')
        lines.append(
            f"    <h2>{name} "
            f'<span class="count">({len(works)} public work{"s" if len(works) != 1 else ""})</span>'
            f"</h2>"
        )
        lines.append(
            f'    <p class="orcid">ORCID: '
            f'<a href="https://orcid.org/{orcid}" target="_blank" '
            f'rel="noopener noreferrer">{orcid}</a></p>'
        )

        if not works:
            lines.append(
                '    <p class="none">No publicly visible ORCID works were retrieved for this record.</p>'
            )
            lines.append("  </section>")
            continue

        by_year = defaultdict(list)

        for publication in works:
            by_year[publication["year"]].append(publication)

        for year in sorted(by_year, reverse=True):
            year_heading = str(year) if year else "Year unavailable"
            lines.append(f"    <h3>{html.escape(year_heading)}</h3>")
            lines.append("    <ol>")

            for publication in by_year[year]:
                lines.append(render_publication(publication))

            lines.append("    </ol>")

        lines.append("  </section>")

    lines.extend([
        "</body>",
        "</html>",
    ])

    return "\n".join(lines)


def main():
    OUTPUT_DIR.mkdir(exist_ok=True)

    roster = read_researchers()

    if not roster:
        raise RuntimeError("No valid ORCID records found in data/researchers.csv.")

    researchers_output = []
    failures = []

    for number, researcher in enumerate(roster, start=1):
        name = researcher["name"]
        orcid = researcher["orcid"]

        print(f"[{number}/{len(roster)}] Retrieving public works for {name} ({orcid})")

        publications = []

        try:
            put_codes = get_put_codes(orcid)

            for put_code in put_codes:
                work = get_detailed_work(orcid, put_code)

                if work:
                    publications.append(make_publication(work))

                time.sleep(REQUEST_PAUSE_SECONDS)

        except requests.HTTPError as error:
            failures.append({
                "name": name,
                "orcid": orcid,
                "error": str(error),
            })
            print(f"Warning for {name}: {error}")

        publications.sort(
            key=lambda publication: (
                publication["year"],
                publication["title"].lower(),
            ),
            reverse=True,
        )

        researchers_output.append({
            "name": name,
            "orcid": orcid,
            "publication_count": len(publications),
            "publications": publications,
        })

        time.sleep(REQUEST_PAUSE_SECONDS)

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    output = {
        "generated_at": generated_at,
        "researchers_processed": len(researchers_output),
        "failures": failures,
        "researchers": researchers_output,
    }

    JSON_OUTPUT.write_text(
        json.dumps(output, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    HTML_OUTPUT.write_text(
        render_html(researchers_output, generated_at),
        encoding="utf-8",
    )

    total_publications = sum(
        researcher["publication_count"] for researcher in researchers_output
    )

    print(f"Created {JSON_OUTPUT}")
    print(f"Created {HTML_OUTPUT}")
    print(f"Researchers processed: {len(researchers_output)}")
    print(f"Public works retrieved: {total_publications}")

    if failures:
        print(f"Records with errors: {len(failures)}")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Build failed: {error}", file=sys.stderr)
        sys.exit(1)
