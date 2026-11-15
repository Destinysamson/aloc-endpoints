import os
import re
import sys
import json
import time
from pathlib import Path
from datetime import datetime, timezone

import requests

# ==========================================
# STALWART PREP - JAMB EXTRACTION ENGINE
# ==========================================

BASE_URL = os.getenv(
    "ALOC_BASE_URL",
    "https://dev.aloc.com.ng/api/v1"
).rstrip("/")

API_KEY = os.getenv("ALOC_API_KEY", "").strip()

YEAR_SCOPE = os.getenv("YEAR_SCOPE", "priority")

PRIORITY_YEARS = list(range(2015, 2027))

ARCHIVE_YEARS = list(range(2000, 2027))

DATABASE = Path("database")
REPORTS = Path("reports")

DATABASE.mkdir(exist_ok=True)
REPORTS.mkdir(exist_ok=True)

session = requests.Session()

session.headers.update({
    "X-API-Key": API_KEY,
    "Accept": "application/json",
    "User-Agent": "StalwartPrep/1.0"
})


# ==========================================
# API REQUEST HANDLER
# ==========================================

def api_request(endpoint, params=None):

    url = BASE_URL + endpoint

    for attempt in range(4):

        try:
            response = session.get(
                url,
                params=params,
                timeout=45
            )

            if response.status_code == 429 or response.status_code >= 500:
                print(
                    "Temporary API error:",
                    response.status_code
                )

                time.sleep(5 * (attempt + 1))
                continue

            if response.status_code != 200:
                print("\nALOC REQUEST FAILED")
                print("URL:", url)
                print("HTTP STATUS:", response.status_code)
                print("RESPONSE:", response.text[:2000])

                raise RuntimeError(
                    f"ALOC HTTP {response.status_code}"
                )

            try:
                return response.json()

            except ValueError:
                raise RuntimeError(
                    "ALOC returned invalid JSON."
                )

        except requests.RequestException as error:

            print("Network error:", str(error))

            if attempt == 3:
                raise

            time.sleep(5 * (attempt + 1))

    raise RuntimeError("ALOC API request failed.")


def extract_data(payload):

    if not isinstance(payload, dict):
        raise RuntimeError("Unexpected API response format.")

    data = payload.get("data")

    if not isinstance(data, list):
        raise RuntimeError(
            "API response does not contain a data list."
        )

    return data


# ==========================================
# SUBJECT DISCOVERY
# ==========================================

def discover_subjects():

    print("\nDiscovering available subjects...")

    payload = api_request("/subjects")

    records = extract_data(payload)

    subjects = []

    seen = set()

    for item in records:

        if not isinstance(item, dict):
            continue

        name = item.get("name")

        if not name:
            continue

        name = str(name).strip().lower()

        if name in seen:
            continue

        seen.add(name)

        exam_types = item.get("examTypes") or []

        if "jamb" not in [
            str(x).lower() for x in exam_types
        ]:
            continue

        subjects.append(item)

    print("JAMB subjects discovered:", len(subjects))

    for subject in subjects:
        print("-", subject["name"])

    return subjects


# ==========================================
# YEAR DISCOVERY
# ==========================================

def discover_years(subject):

    endpoint = (
        "/subjects/"
        + requests.utils.quote(subject, safe="")
        + "/years"
    )

    payload = api_request(endpoint)

    records = extract_data(payload)

    available = set()

    for item in records:

        if not isinstance(item, dict):
            continue

        year = item.get("year")

        if year is None:
            continue

        try:
            year = int(year)
        except (ValueError, TypeError):
            continue

        exam_types = item.get("examTypes") or []

        breakdown = item.get("breakdown") or {}

        jamb_count = breakdown.get("jamb", 0)

        if (
            "jamb" in [
                str(x).lower() for x in exam_types
            ]
            or (
                isinstance(jamb_count, (int, float))
                and jamb_count > 0
            )
        ):

            if 2000 <= year <= 2026:
                available.add(year)

    return available


# ==========================================
# QUESTION EXTRACTION
# ==========================================

def extract_questions(subject, year):

    all_questions = []

    cursor = None

    seen_cursors = set()

    while True:

        params = {
            "subject": subject,
            "examType": "jamb",
            "year": year,
            "contentSource": "historical",
            "limit": 15
        }

        if cursor:
            params["cursor"] = cursor

        payload = api_request(
            "/questions",
            params
        )

        records = extract_data(payload)

        for record in records:

            if not isinstance(record, dict):
                continue

            question = dict(record)

            question["_stalwartPrep"] = {
                "source": "ALOC",
                "contentType": "historical",
                "exam": "JAMB",
                "subject": subject,
                "year": year,
                "retrievedAt": datetime.now(
                    timezone.utc
                ).isoformat()
            }

            all_questions.append(question)

        pagination = payload.get("pagination") or {}

        if not pagination.get("hasMore", False):
            break

        next_cursor = pagination.get("nextCursor")

        if not next_cursor:
            raise RuntimeError(
                "Pagination says more questions exist "
                "but no nextCursor was supplied."
            )

        if next_cursor in seen_cursors:
            raise RuntimeError("Repeated pagination cursor.")

        seen_cursors.add(next_cursor)

        cursor = next_cursor

        time.sleep(2.2)

    # Remove duplicates by original question ID.

    unique = {}
    without_id = []

    for question in all_questions:

        question_id = question.get("id")

        if question_id:
            unique[str(question_id)] = question
        else:
            without_id.append(question)

    return list(unique.values()) + without_id


# ==========================================
# FILE SAVING
# ==========================================

def save_subject(subject, questions):

    filename = re.sub(
        r"[^a-zA-Z0-9]+",
        "-",
        subject
    ).strip("-").lower()

    path = DATABASE / f"{filename}.json"

    path.write_text(
        json.dumps(
            questions,
            indent=2,
            ensure_ascii=False
        ),
        encoding="utf-8"
    )

    print(
        "SAVED:",
        path,
        "| Questions:",
        len(questions)
    )


# ==========================================
# MAIN ENGINE
# ==========================================

def main():

    if not API_KEY:
        raise RuntimeError(
            "ALOC_SECRET_TOKEN was not passed to Python."
        )

    if YEAR_SCOPE not in ["priority", "archive"]:
        raise RuntimeError("Invalid year selection.")

    years_to_extract = (
        PRIORITY_YEARS
        if YEAR_SCOPE == "priority"
        else ARCHIVE_YEARS
    )

    print("====================================")
    print("STALWART PREP JAMB EXTRACTION")
    print("====================================")

    print("Year scope:", YEAR_SCOPE)

    print("Years:", years_to_extract)

    subjects = discover_subjects()

    if not subjects:
        raise RuntimeError(
            "No JAMB subjects were returned by ALOC."
        )

    successful = []
    empty = []
    failed = []

    total_questions = 0

    totals_by_subject = {}

    for subject_info in subjects:

        subject = subject_info["name"]

        print("\n====================================")
        print("SUBJECT:", subject)
        print("====================================")

        subject_questions = []

        try:

            available_years = discover_years(subject)

        except Exception as error:

            failed.append({
                "subject": subject,
                "stage": "year discovery",
                "error": str(error)
            })

            continue

        selected_years = [
            year for year in years_to_extract
            if year in available_years
        ]

        print("Available selected years:", selected_years)

        for year in selected_years:

            print(
                "\nExtracting:",
                subject,
                year
            )

            try:

                questions = extract_questions(
                    subject,
                    year
                )

                if questions:

                    subject_questions.extend(questions)

                    successful.append({
                        "subject": subject,
                        "year": year,
                        "question_count": len(questions)
                    })

                    total_questions += len(questions)

                else:

                    empty.append({
                        "subject": subject,
                        "year": year
                    })

            except Exception as error:

                failed.append({
                    "subject": subject,
                    "year": year,
                    "error": str(error)
                })

                print("EXTRACTION ERROR:", str(error))

        if subject_questions:

            save_subject(
                subject,
                subject_questions
            )

            totals_by_subject[subject] = len(
                subject_questions
            )

    # ======================================
    # SAVE REPORT
    # ======================================

    report = {

        "project": "Stalwart Prep",

        "exam": "JAMB",

        "stage": YEAR_SCOPE,

        "requested_years": years_to_extract,

        "subjects_discovered": [
            subject["name"]
            for subject in subjects
        ],

        "successful": successful,

        "empty": empty,

        "failed": failed,

        "total_questions": total_questions,

        "totals_by_subject": totals_by_subject,

        "created_at": datetime.now(
            timezone.utc
        ).isoformat()
    }

    report_path = REPORTS / "extraction-report.json"

    report_path.write_text(
        json.dumps(
            report,
            indent=2,
            ensure_ascii=False
        ),
        encoding="utf-8"
    )

    print("\n====================================")
    print("EXTRACTION SUMMARY")
    print("====================================")

    print("Total questions:", total_questions)

    print("Successful batches:", len(successful))

    print("Empty batches:", len(empty))

    print("Failed batches:", len(failed))

    print("Database folder:", DATABASE)

    print("Report:", report_path)

    if total_questions == 0:
        raise RuntimeError(
            "No question records were saved."
        )


if __name__ == "__main__":

    try:
        main()

    except Exception as error:

        print("\nFATAL ERROR:", str(error))

        sys.exit(1)
