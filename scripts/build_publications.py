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
from Bio import Entrez

ROOT = Path(__file__).resolve().parents[1]
ROSTER_PATH = ROOT / "data" / "researchers.csv"
OUTPUT_DIR = ROOT / "docs"
JSON_OUTPUT = OUTPUT_DIR / "publications-by-researcher.json"
HTML_OUTPUT = OUTPUT_DIR / "index.html"

ORCID_API = "https://pub.orcid.org/v3.0"
REQUEST_TIMEOUT = 30
REQUEST_PAUSE_SECONDS = 0.4
PUBMED_MAX_RESULTS_PER_QUERY = 500

Entrez.email = "rheajd@bu.edu"
Entrez.tool = "group_publications_orcid_pubmed"


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
            pubmed_query = (row.get("pubmed_query") or "").strip()

            if not name:
                continue

            if orcid and not re.fullmatch(
                r"\d{4}-\d{4}-\d{4}-\d{3}[\dX]",
                orcid,
            ):
                print(f"Invalid ORCID skipped for {name}: {orcid}")
                orcid = ""

            if not orcid and not pubmed_query:
                print(f"Skipping {name}: no ORCID or PubMed query.")
                continue

            researchers.append({
                "name": name,
                "orcid": orcid,
                "pubmed_query": pubmed_query,
            })

    return researchers


def text_value(data, *keys):
    current = data

    for key in keys:
        if not isinstance(current, dict):
            return ""
        current = current.get(key)

    return current.strip() if isinstance(current, str) else ""


def normalize_doi(value):
    doi = (value or "").strip().lower()
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi)
    doi = re.sub(r"^doi:\s*", "", doi)
    return doi.rstrip(" .;,")


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


def get_orcid_doi(work):
    external_ids = (work.get("external-ids") or {}).get("external-id") or []

    for external_id in external_ids:
        external_id = external_id or {}

        if (external_id.get("external-id-type") or "").lower() == "doi":
            doi = normalize_doi(external_id.get("external-id-value"))

            if doi:
                return doi

    return ""


def get_orcid_authors(work):
    contributors = (work.get("contributors") or {}).get("contributor") or []
    authors = []

    for contributor in contributors:
        contributor = contributor or {}
        author_name = text_value(contributor, "credit-name", "value")

        if author_name:
            authors.append(author_name)

    return authors


def get_orcid_title(work):
    title = text_value(work, "title", "title", "value")
    subtitle = text_value(work, "title", "subtitle", "value")

    if subtitle:
        return f"{title}: {subtitle}"

    return title or "Untitled work"


def format_work_type(work_type):
    return (work_type or "Other").replace("_", " ").title()


def get_orcid_put_codes(orcid):
    if not orcid:
        return []

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


def get_orcid_work(orcid, put_code):
    try:
        return api_get(f"/{orcid}/work/{put_code}")
    except requests.HTTPError as error:
        print(f"  Skipping ORCID work {put_code}: {error}")
        return None


def make_orcid_publication(work):
    return {
        "source": "ORCID",
        "match_type": "Public ORCID work",
        "pmid": "",
        "put_code": str(work.get("put-code") or ""),
        "title": get_orcid_title(work),
        "year": publication_year(work),
        "doi": get_orcid_doi(work),
        "journal": text_value(work, "journal-title", "value"),
        "volume": text_value(work, "journal-issue", "volume"),
        "issue": text_value(work, "journal-issue", "issue"),
        "pages": text_value(work, "journal-issue", "page-range"),
        "type": work.get("type") or "",
        "authors": get_orcid_authors(work),
        "url": text_value(work, "url", "value"),
    }


def pubmed_search(query):
    handle = Entrez.esearch(
        db="pubmed",
        term=query,
        retmax=PUBMED_MAX_RESULTS_PER_QUERY,
        sort="pub date",
    )

    try:
        result = Entrez.read(handle)
    finally:
        handle.close()

    time.sleep(REQUEST_PAUSE_SECONDS)
    return result.get("IdList", [])


def pubmed_fetch(pmids):
    if not pmids:
        return []

    handle = Entrez.esummary(
        db="pubmed",
        id=",".join(pmids),
        retmode="xml",
    )

    try:
        result = Entrez.read(handle)
    finally:
        handle.close()

    time.sleep(REQUEST_PAUSE_SECONDS)
    return result


def pubmed_authors(author_list):
    authors = []

    for author in author_list or []:
        if not isinstance(author, dict):
            continue

        author_name = (author.get("Name") or "").strip()

        if author_name:
            authors.append(author_name)

    return authors


def pubmed_year(record):
    for date_value in [
        (record.get("PubDate") or "").strip(),
        (record.get("EPubDate") or "").strip(),
    ]:
        match = re.search(r"\b(19|20)\d{2}\b", date_value)

        if match:
            return int(match.group(0))

    return 0


def pubmed_doi(record):
    article_ids = record.get("ArticleIds") or {}

    if isinstance(article_ids, dict):
        return normalize_doi(article_ids.get("doi") or "")

    return ""


def make_pubmed_publication(record, match_type):
    pmid = str(record.get("Id") or "")

    return {
        "source": "PubMed",
        "match_type": match_type,
        "pmid": pmid,
        "put_code": "",
        "title": (record.get("Title") or "Untitled work").strip(),
        "year": pubmed_year(record),
        "doi": pubmed_doi(record),
        "journal": (
            record.get("FullJournalName")
            or record.get("Source")
            or ""
        ).strip(),
        "volume": (record.get("Volume") or "").strip(),
        "issue": (record.get("Issue") or "").strip(),
        "pages": (record.get("Pages") or "").strip(),
        "type": (record.get("PubType") or "").strip(),
        "authors": pubmed_authors(record.get("Authors")),
        "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else "",
    }


def publication_key(publication):
    if publication["doi"]:
        return f"doi:{publication['doi']}"

    normalized_title = re.sub(
        r"[^a-z0-9]",
        "",
        publication["title"].lower(),
    )

    return f"title:{normalized_title}|year:{publication['year']}"


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
    source = html.escape(publication["source"])
    match_type = html.escape(publication["match_type"])
    work_type = html.escape(format_work_type(publication["type"]))

    metadata = [
        f'<span class="source">{source}</span>',
        f'<span class="match">{match_type}</span>',
    ]

    if publication["type"]:
        metadata.append(f'<span class="type">{work_type}</span>')

    if publication["pmid"]:
        pmid = html.escape(publication["pmid"])
        metadata.append(
            f'<a href="https://pubmed.ncbi.nlm.nih.gov/{pmid}/" '
            f'target="_blank" rel="noopener noreferrer">PubMed</a>'
        )

    if publication["doi"]:
        doi = html.escape(publication["doi"])
        metadata.append(
            f'<a href="https://doi.org/{doi}" '
            f'target="_blank" rel="noopener noreferrer">DOI</a>'
        )

    if not publication["pmid"] and not publication["doi"] and publication["url"]:
        url = html.escape(publication["url"], quote=True)
        metadata.append(
            f'<a href="{url}" target="_blank" rel="noopener noreferrer">'
            f'View record</a>'
        )

    return (
        f"      <li>{citation}"
        f'<div class="publication-meta">{" · ".join(metadata)}</div>'
        f"      </li>"
    )


def render_year_sections(publications):
    lines = []
    by_year = defaultdict(list)

    for publication in publications:
        by_year[publication["year"]].append(publication)

    for year in sorted(by_year, reverse=True):
        label = str(year) if year else "Year unavailable"
        lines.append(f"    <h4>{html.escape(label)}</h4>")
        lines.append("    <ol>")

        for publication in by_year[year]:
            lines.append(render_publication(publication))

        lines.append("    </ol>")

    return lines


def render_html(researchers, generated_at):
    lines = [
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        '  <meta charset="utf-8">',
        '  <meta name="viewport" content="width=device-width, initial-scale=1">',
        "  <title>Publications by Researcher</title>",
        "  <style>",
        "    body { font-family: Arial, Helvetica, sans-serif; color: #1f2937; line-height: 1.6; max-width: 1080px; margin: 2rem auto; padding: 0 1rem 4rem; }",
        "    h1 { margin-bottom: 0.2rem; }",
        "    h2 { margin: 3rem 0 0.3rem; padding-top: 0.7rem; border-top: 2px solid #0f4c81; }",
        "    h3 { margin: 1.8rem 0 0.35rem; color: #123b62; }",
        "    h4 { margin: 1.25rem 0 0.35rem; border-bottom: 1px solid #d1d5db; padding-bottom: 0.25rem; }",
        "    ol { padding-left: 1.5rem; }",
        "    li { margin-bottom: 1rem; }",
        "    .meta, .orcid, .publication-meta, .none { color: #4b5563; font-size: 0.92rem; }",
        "    .orcid a, .publication-meta a { color: #0757c8; }",
        "    .publication-meta { margin-top: 0.2rem; }",
        "    .source, .match, .type { display: inline-block; border-radius: 999px; padding: 0.05rem 0.5rem; }",
        "    .source { background: #e8f0fe; color: #174ea6; }",
        "    .match { background: #fff3cd; color: #6e4d00; }",
        "    .type { background: #edf7ed; color: #256029; }",
        "    .count { color: #4b5563; font-weight: normal; font-size: 1rem; }",
        "  </style>",
        "</head>",
        "<body>",
        "  <h1>Publications by Researcher</h1>",
        '  <p class="meta">PubMed records appear first. Results found by an ORCID iD are high-confidence; '
        "author-and-affiliation results should be reviewed for common names. Other public ORCID works appear below. "
        f"Last updated: {html.escape(generated_at)}.</p>",
    ]

    for researcher in researchers:
        name = html.escape(researcher["name"])
        orcid = html.escape(researcher["orcid"])
        pubmed_works = researcher["pubmed_publications"]
        other_orcid_works = researcher["other_orcid_works"]
        total = len(pubmed_works) + len(other_orcid_works)

        lines.append(f'  <section id="researcher-{html.escape(researcher["slug"])}">')
        lines.append(
            f"    <h2>{name} "
            f'<span class="count">({total} work{"s" if total != 1 else ""})</span>'
            f"</h2>"
        )

        if orcid:
            lines.append(
                f'    <p class="orcid">ORCID: '
                f'<a href="https://orcid.org/{orcid}" target="_blank" '
                f'rel="noopener noreferrer">{orcid}</a></p>'
            )
        else:
            lines.append(
                '    <p class="orcid">No ORCID iD currently listed; PubMed author-and-affiliation search only.</p>'
            )

        lines.append(
            f"    <h3>PubMed publications "
            f'<span class="count">({len(pubmed_works)})</span></h3>'
        )

        if pubmed_works:
            lines.extend(render_year_sections(pubmed_works))
        else:
            lines.append(
                '    <p class="none">No PubMed records were retrieved from the configured searches.</p>'
            )

        lines.append(
            f"    <h3>Other public ORCID works "
            f'<span class="count">({len(other_orcid_works)})</span></h3>'
        )

        if other_orcid_works:
            lines.extend(render_year_sections(other_orcid_works))
        else:
            lines.append(
                '    <p class="none">No additional public ORCID works were retrieved.</p>'
            )

        lines.append("  </section>")

    lines.extend([
        "</body>",
        "</html>",
    ])

    return "\n".join(lines)


def researcher_slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def main():
    OUTPUT_DIR.mkdir(exist_ok=True)

    roster = read_researchers()

    if not roster:
        raise RuntimeError("No valid researcher entries found.")

    output_researchers = []
    failures = []

    for number, researcher in enumerate(roster, start=1):
        name = researcher["name"]
        orcid = researcher["orcid"]
        author_query = researcher["pubmed_query"]

        print(f"[{number}/{len(roster)}] Retrieving records for {name}")

        pmid_match_types = {}

        if orcid:
            try:
                orcid_pmids = pubmed_search(f'"{orcid}"[AUID]')

                for pmid in orcid_pmids:
                    pmid_match_types[pmid] = "ORCID match"

            except Exception as error:
                failures.append({
                    "name": name,
                    "orcid": orcid,
                    "source": "PubMed ORCID search",
                    "error": str(error),
                })
                print(f"  PubMed ORCID warning: {error}")

        if author_query:
            try:
                author_pmids = pubmed_search(author_query)

                for pmid in author_pmids:
                    pmid_match_types.setdefault(
                        pmid,
                        "Author + Boston University match — review recommended",
                    )

            except Exception as error:
                failures.append({
                    "name": name,
                    "orcid": orcid,
                    "source": "PubMed author/affiliation search",
                    "error": str(error),
                })
                print(f"  PubMed author-search warning: {error}")

        pubmed_works = []

        try:
            pubmed_records = pubmed_fetch(sorted(pmid_match_types))

            for record in pubmed_records:
                pmid = str(record.get("Id") or "")
                match_type = pmid_match_types.get(
                    pmid,
                    "PubMed match",
                )
                pubmed_works.append(
                    make_pubmed_publication(record, match_type)
                )

        except Exception as error:
            failures.append({
                "name": name,
                "orcid": orcid,
                "source": "PubMed fetch",
                "error": str(error),
            })
            print(f"  PubMed fetch warning: {error}")

        pubmed_keys = {
            publication_key(publication)
            for publication in pubmed_works
        }

        other_orcid_works = []

        if orcid:
            try:
                put_codes = get_orcid_put_codes(orcid)

                for put_code in put_codes:
                    work = get_orcid_work(orcid, put_code)

                    if not work:
                        continue

                    publication = make_orcid_publication(work)

                    if publication_key(publication) not in pubmed_keys:
                        other_orcid_works.append(publication)

                    time.sleep(REQUEST_PAUSE_SECONDS)

            except Exception as error:
                failures.append({
                    "name": name,
                    "orcid": orcid,
                    "source": "ORCID works",
                    "error": str(error),
                })
                print(f"  ORCID warning: {error}")

        pubmed_works.sort(
            key=lambda publication: (
                publication["year"],
                publication["title"].lower(),
            ),
            reverse=True,
        )

        other_orcid_works.sort(
            key=lambda publication: (
                publication["year"],
                publication["title"].lower(),
            ),
            reverse=True,
        )

        output_researchers.append({
            "name": name,
            "slug": researcher_slug(name),
            "orcid": orcid,
            "pubmed_query": author_query,
            "pubmed_publication_count": len(pubmed_works),
            "other_orcid_work_count": len(other_orcid_works),
            "pubmed_publications": pubmed_works,
            "other_orcid_works": other_orcid_works,
        })

        time.sleep(REQUEST_PAUSE_SECONDS)

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    output = {
        "generated_at": generated_at,
        "researchers_processed": len(output_researchers),
        "failures": failures,
        "researchers": output_researchers,
    }

    JSON_OUTPUT.write_text(
        json.dumps(output, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    HTML_OUTPUT.write_text(
        render_html(output_researchers, generated_at),
        encoding="utf-8",
    )

    total_pubmed = sum(
        researcher["pubmed_publication_count"]
        for researcher in output_researchers
    )
    total_orcid_other = sum(
        researcher["other_orcid_work_count"]
        for researcher in output_researchers
    )

    print(f"Created {JSON_OUTPUT}")
    print(f"Created {HTML_OUTPUT}")
    print(f"Researchers processed: {len(output_researchers)}")
    print(f"PubMed publications: {total_pubmed}")
    print(f"Other ORCID works: {total_orcid_other}")

    if failures:
        print(f"Warnings recorded: {len(failures)}")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Build failed: {error}", file=sys.stderr)
        sys.exit(1)
