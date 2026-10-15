#!/usr/bin/env python3
"""
corporate_mailer.py — Full Corporate Job Discovery, Evaluation, Tailoring & Auto-Applier
Designed to run headlessly on mailer-vm (remote GCP server) alongside main.py.

Usage:
  python corporate_mailer.py <worker> <workers> [--dry-run] [--scan] [--min-score 4.0] [--max-per-company 3] [--limit N]
  python corporate_mailer.py --fetch-greenhouse-code
  python corporate_mailer.py --auth-gmail
"""

import argparse
import base64
import fcntl
import glob
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from datetime import datetime

# ── Paths & Config ──────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CAREER_OPS_DIR = os.path.abspath(os.path.join(BASE_DIR, "..", "career-ops"))
PROGRESS_FILE = os.path.join(BASE_DIR, "corporate_progress.json")
PROGRESS_LOCK = os.path.join(BASE_DIR, "corporate_progress.lock")

PIPELINE_FILE = os.path.join(CAREER_OPS_DIR, "data", "pipeline.md")
TRACKER_FILE = os.path.join(CAREER_OPS_DIR, "data", "applications.md")
PDF_INDEX_FILE = os.path.join(CAREER_OPS_DIR, "data", "pdf-index.tsv")

DEFAULT_MIN_FIT_SCORE = 4.0
DEFAULT_MAX_PER_COMPANY = 3
GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]
CLAUDE_USAGE_CAP = 90
USAGE_CHECK_INTERVAL = 300
_last_usage_check = 0.0
_use_claude = True

CANDIDATE_INFO = {
    "firstName": "Akhil",
    "lastName": "Raj",
    "fullName": "Akhil Raj",
    "email": "ar2427@cornell.edu",
    "phone": "+1 516-979-6593",
    "location": "New York, NY",
    "linkedin": "https://www.linkedin.com/in/akhil-raj-810261255",
    "github": "https://github.com/Akhil-Raj",
    "portfolio": "https://arxiv.org/abs/2410.17035",
}


# ── Multi-Worker Progress State (same lock + atomic replace as main.py) ────
def load_progress():
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE) as f:
            return json.load(f)
    return {}


def update_progress(key, entry):
    with open(PROGRESS_LOCK, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        progress = load_progress()
        progress[key] = entry
        tmp = PROGRESS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(progress, f, indent=2)
        os.replace(tmp, PROGRESS_FILE)


# ── Claude / AGY CLI Runner (same 90% usage cap as main.py) ────────────────
def claude_usage():
    result = subprocess.run(
        ["claude", "-p", "/usage"],
        cwd=CAREER_OPS_DIR,
        capture_output=True,
        text=True,
    )
    m = re.search(r"Current session:\s*(\d+)%\s*used", result.stdout + result.stderr)
    return int(m.group(1)) if m else 0


def career_ops(args, json_schema=None):
    global _last_usage_check, _use_claude

    now = time.time()
    if now - _last_usage_check >= USAGE_CHECK_INTERVAL:
        _last_usage_check = now
        used = claude_usage()
        _use_claude = used < CLAUDE_USAGE_CAP
        print(f"  [usage] Claude session: {used}% -> {'claude' if _use_claude else 'agy'}")

    prompt = f"/career-ops {args}"
    schema_flags = (
        ["--output-format", "json", "--json-schema", json.dumps(json_schema)]
        if json_schema
        else []
    )

    if _use_claude:
        result = subprocess.run(
            [
                "claude",
                "-p",
                prompt,
                "--model",
                "sonnet",
                "--effort",
                "medium",
                "--allowedTools",
                "Read Write Edit Glob Grep Bash(node:*) Bash(npm:*)",
            ]
            + schema_flags,
            cwd=CAREER_OPS_DIR,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            if json_schema:
                return json.loads(result.stdout)["structured_output"]
            return result.stdout
        print(f"  [claude error] Switching to agy: {(result.stdout + result.stderr).strip()[:200]}")
        _use_claude = False

    result = subprocess.run(
        [
            "agy",
            "-p",
            prompt,
            "--model",
            "gemini-3.8-flash-medium",
            "--dangerously-skip-permissions",
        ]
        + schema_flags,
        cwd=CAREER_OPS_DIR,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"career-ops failed:\n{result.stdout}\n{result.stderr}")
    if json_schema:
        return json.loads(result.stdout)["structured_output"]
    return result.stdout


def run_node(script_and_args, check=False, timeout=90):
    cmd = ["node"] + script_and_args
    return subprocess.run(
        cmd,
        cwd=CAREER_OPS_DIR,
        capture_output=True,
        text=True,
        check=check,
        timeout=timeout,
    )


# ── Pipeline & Tracker Readers ─────────────────────────────────────────────
def normalize_company(name):
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def load_applied_counts_by_company():
    """Return a dict mapping normalized company name -> count of active/Applied rows in data/applications.md."""
    counts = defaultdict(int)
    if not os.path.exists(TRACKER_FILE):
        return counts
    with open(TRACKER_FILE) as f:
        for line in f:
            if not line.startswith("|"):
                continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 8 or not parts[1].isdigit():
                continue
            company = parts[3]
            status = parts[6].replace("*", "").strip().lower()
            if status in {"applied", "responded", "interview", "offer", "hired"}:
                counts[normalize_company(company)] += 1
    return counts


def load_pending_pipeline_jobs():
    """Parse unchecked `- [ ] <url> | <Company> | <Role> | ...` lines from data/pipeline.md."""
    jobs = []
    if not os.path.exists(PIPELINE_FILE):
        return jobs
    with open(PIPELINE_FILE) as f:
        for line in f:
            m = re.match(r"^\s*-\s*\[\s*\]\s*(\S+)\s*\|\s*([^|]+)\|\s*([^|\n]+)(?:\|(.*))?$", line)
            if not m:
                continue
            url = m.group(1).strip()
            company = m.group(2).strip()
            role = m.group(3).strip()
            extra = (m.group(4) or "").strip()
            jobs.append({"url": url, "company": company, "role": role, "extra": extra})
    return jobs


def mark_pipeline_url_done(url):
    """Mark `- [ ] <url>` as `- [x] <url>` in data/pipeline.md under the progress lock."""
    if not os.path.exists(PIPELINE_FILE):
        return
    with open(PROGRESS_LOCK, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with open(PIPELINE_FILE) as f:
            content = f.read()
        updated = content.replace(f"- [ ] {url}", f"- [x] {url}")
        if updated != content:
            tmp = PIPELINE_FILE + ".tmp"
            with open(tmp, "w") as f:
                f.write(updated)
            os.replace(tmp, PIPELINE_FILE)


# ── Step 1: Liveness & JD Extraction (Zero LLM Tokens) ─────────────────────
def extract_jd(url):
    """Fetch JD via zero-browser ATS API first (fetch-jd.mjs), fallback to headless Playwright (browser-extract.mjs)."""
    res = run_node(["fetch-jd.mjs", url], timeout=30)
    if res.returncode == 0 and len(res.stdout.strip()) >= 150:
        return res.stdout.strip()

    res2 = run_node(["browser-extract.mjs", url, "--mode", "jd"], timeout=45)
    if res2.returncode == 0 and len(res2.stdout.strip()) >= 150:
        return res2.stdout.strip()

    return None


# ── Step 2: Fit Evaluation (/career-ops oferta) ────────────────────────────
FIT_SCHEMA = {
    "type": "object",
    "properties": {
        "fit_score": {"type": "number", "description": "Overall fit score out of 5"},
        "report_number": {
            "type": "string",
            "description": "3-digit report number assigned to the saved evaluation report, e.g. 051",
        },
        "hard_stop": {
            "type": "boolean",
            "description": "True if the role triggers a hard stop (e.g. active US security clearance required, non-AI generalist role, or posted comp below $180K USD walk-away floor)",
        },
        "reasoning": {"type": "string", "description": "Brief summary of fit and archetype"},
    },
    "required": ["fit_score", "report_number", "hard_stop", "reasoning"],
}


def find_report_path(report_num):
    num_int = int(report_num)
    for pattern in [f"{num_int:03d}-*.md", f"{num_int}-*.md"]:
        matches = [
            p
            for p in glob.glob(os.path.join(CAREER_OPS_DIR, "reports", pattern))
            if not p.endswith("-RESERVED.md")
        ]
        if matches:
            return matches[0]
    return None


def evaluate_corporate_fit(job, jd_text):
    res = run_node(["reserve-report-num.mjs"], timeout=20)
    reserved_num = res.stdout.strip() if res.returncode == 0 else None

    try:
        data = career_ops(
            f"oferta Evaluate this corporate job posting for {job['company']} — {job['role']} "
            f"(URL: {job['url']}). Use report number {reserved_num or 'next available'}. "
            f"Save the full report in reports/ including `## Job Description (archived verbatim)` "
            f"and `## Machine Summary`, and write a tracker addition TSV in batch/tracker-additions/.\n\n"
            f"Job Description:\n{jd_text}",
            json_schema=FIT_SCHEMA,
        )
    finally:
        if reserved_num:
            run_node(["reserve-report-num.mjs", "--release", reserved_num], timeout=20)

    report_num = str(data.get("report_number") or reserved_num or "").zfill(3)
    report_path = find_report_path(report_num)
    score = float(data["fit_score"])
    hard_stop = bool(data.get("hard_stop", False))

    if report_path and os.path.exists(report_path):
        with open(report_path) as f:
            content = f.read()
        m = re.search(r"\*\*Score:\*\*\s*([\d.]+)/5", content)
        if m:
            score = float(m.group(1))
        if re.search(r"final_decision:\s*skip", content, re.I):
            hard_stop = True

    run_node(["merge-tracker.mjs"], timeout=30)
    return score, report_num, report_path, hard_stop, data.get("reasoning", "")


# ── Step 3: Tailored CV & Cover Letter PDF Generation ──────────────────────
def generate_tailored_cv(report_num, job):
    career_ops(
        f"pdf {report_num} — Generate a 1-page ATS-optimized tailored CV PDF for "
        f"{job['company']} ({job['role']}) linked to report #{report_num} using --report={report_num}."
    )
    if os.path.exists(PDF_INDEX_FILE):
        with open(PDF_INDEX_FILE) as f:
            for line in f:
                parts = line.strip().split("\t")
                if len(parts) >= 2 and parts[0].lstrip("0") == str(report_num).lstrip("0"):
                    pdf_path = os.path.join(CAREER_OPS_DIR, parts[1])
                    if os.path.exists(pdf_path):
                        return pdf_path
    raise RuntimeError(f"Tailored CV PDF not found in pdf-index.tsv for report #{report_num}")


COVER_LETTER_SCHEMA = {
    "type": "object",
    "properties": {
        "role_title": {"type": "string"},
        "company": {"type": "string"},
        "city": {"type": "string"},
        "opening": {"type": "string", "description": "2-sentence opening connecting to the role and mission"},
        "profile_intro": {"type": "string", "description": "1-paragraph profile intro grounded strictly in cv.md"},
        "achievements": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "lead": {"type": "string", "description": "Short bold lead phrase WITHOUT trailing comma"},
                    "impact": {"type": "string", "description": "1 sentence with exact metric from cv.md"},
                },
                "required": ["lead", "impact"],
            },
        },
        "problems_section": {"type": "string", "description": "2-3 sentences on problems to solve in this role"},
        "closing": {"type": "string", "description": "1-2 sentence closing"},
    },
    "required": [
        "role_title",
        "company",
        "city",
        "opening",
        "profile_intro",
        "achievements",
        "problems_section",
        "closing",
    ],
}


def generate_cover_letter_pdf(report_num, job, report_path):
    letter = career_ops(
        f"Draft a cover letter JSON payload for report #{report_num} ({job['company']} — {job['role']}) "
        f"strictly grounded in cv.md, config/profile.yml, modes/_profile.md, and modes/_custom.md "
        f"(and {report_path}). Every metric must appear verbatim in cv.md so verify-cv-facts.mjs passes. "
        f"Never use em-dashes and never volunteer gaps.",
        json_schema=COVER_LETTER_SCHEMA,
    )
    slug = re.sub(r"[^a-z0-9]+", "-", f"{job['company']}-{job['role']}".lower()).strip("-")[:50]
    out_rel = f"output/cover-{report_num}-{slug}.pdf"
    payload = {
        "candidate": {
            "name": CANDIDATE_INFO["fullName"],
            "email": CANDIDATE_INFO["email"],
            "phone": CANDIDATE_INFO["phone"],
            "location": CANDIDATE_INFO["location"],
            "linkedin": CANDIDATE_INFO["linkedin"],
            "github": CANDIDATE_INFO["github"],
            "credentials": ["M.S. Operations Research & Information Engineering, Cornell University"],
        },
        "letter": {
            **letter,
            "date": datetime.now().strftime("%Y-%m-%d"),
            "signature": {"valediction": "Sincerely,", "name": CANDIDATE_INFO["fullName"]},
        },
        "output_path": out_rel,
    }
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tf:
        json.dump(payload, tf, indent=2)
        payload_path = tf.name

    try:
        res = run_node(
            [
                "generate-cover-letter.mjs",
                "--payload",
                payload_path,
                "--out",
                out_rel,
                "--format",
                "letter",
                "--report",
                str(report_num),
            ],
            timeout=45,
        )
        abs_pdf = os.path.join(CAREER_OPS_DIR, out_rel)
        if res.returncode == 0 and os.path.exists(abs_pdf):
            return abs_pdf, "\n\n".join(
                [
                    letter["opening"],
                    letter["profile_intro"],
                    "\n".join(f"- {a['lead']}, {a['impact']}" for a in letter["achievements"]),
                    letter["problems_section"],
                    letter["closing"],
                ]
            )
    finally:
        if os.path.exists(payload_path):
            os.remove(payload_path)
    return None, None


# ── Step 4: Answer Generation for Custom ATS Form Fields ───────────────────
ANSWERS_SCHEMA = {
    "type": "object",
    "properties": {
        "answers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Exact field id from the scanned form, e.g. field_3"},
                    "label": {"type": "string"},
                    "value": {
                        "type": "string",
                        "description": "Exact option string for select/radio/combobox/button_group, or truthful text answer for text/textarea",
                    },
                },
                "required": ["id", "label", "value"],
            },
        }
    },
    "required": ["answers"],
}


def generate_form_answers(job, report_path, custom_fields):
    if not custom_fields:
        return []

    fields_json = json.dumps(custom_fields, indent=2)
    prompt = (
        f"Generate answers for these ATS application form fields for {job['company']} — {job['role']} "
        f"(report: {report_path}).\n"
        f"CRITICAL STANDING RULES (from config/profile.yml, modes/_profile.md, modes/_custom.md):\n"
        f"- Legal Name: Akhil Raj | Email: ar2427@cornell.edu | Phone: +1 516-979-6593 | Location: New York, NY\n"
        f"- LinkedIn: https://www.linkedin.com/in/akhil-raj-810261255 | GitHub: https://github.com/Akhil-Raj\n"
        f"- Website / Portfolio / Other URL: https://arxiv.org/abs/2410.17035\n"
        f"- Legally authorized to work in the US? Yes (F-1 STEM OPT valid through June 2027)\n"
        f"- Will you now or in the future require visa sponsorship (H-1B)? Yes (March 2027 H-1B cap cycle)\n"
        f"- Do you have indefinite work authorization without sponsorship? No\n"
        f"- Willing to relocate / work hybrid or onsite in US? Yes\n"
        f"- Salary / Compensation expectation: $180,000 - $230,000 USD total compensation (or numeric 195000 if single number required)\n"
        f"- Notice period / earliest start date: 2 weeks (14 days)\n"
        f"- NEVER invent metrics or facts outside cv.md and never volunteer gaps.\n"
        f"- When a field has `options`, `value` MUST match one of the listed options.\n\n"
        f"Scanned Form Fields:\n{fields_json}"
    )
    out = career_ops(prompt, json_schema=ANSWERS_SCHEMA)
    return out.get("answers", [])


# ── Greenhouse 8-char Verification Code via ar2427@cornell.edu Gmail API ───
def authorize_gmail_token():
    """Authorize token.json for ar2427@cornell.edu with both gmail.send and gmail.readonly scopes."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    token_path = os.path.join(BASE_DIR, "token.json")
    creds_path = os.path.join(BASE_DIR, "credentials.json")
    creds = None
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, GMAIL_SCOPES)
    if not creds or not creds.valid or not creds.has_scopes(GMAIL_SCOPES):
        flow = InstalledAppFlow.from_client_secrets_file(creds_path, GMAIL_SCOPES)
        creds = flow.run_local_server(port=0)
        with open(token_path, "w") as f:
            f.write(creds.to_json())
    print(f"Successfully authorized {token_path} with scopes: {creds.scopes}")


def _extract_body_text(payload):
    texts = []
    if not payload:
        return ""
    body_data = payload.get("body", {}).get("data")
    if body_data:
        try:
            texts.append(base64.urlsafe_b64decode(body_data).decode("utf-8", errors="ignore"))
        except Exception:
            pass
    for part in payload.get("parts", []) or []:
        texts.append(_extract_body_text(part))
    return "\n".join(texts)


def fetch_greenhouse_code_from_gmail():
    """Polls ar2427@cornell.edu via token.json for the latest 8-character Greenhouse security code."""
    token_path = os.path.join(BASE_DIR, "token.json")
    if not os.path.exists(token_path):
        return ""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        creds = Credentials.from_authorized_user_file(token_path)
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(token_path, "w") as f:
                f.write(creds.to_json())

        service = build("gmail", "v1", credentials=creds)
        for _ in range(9):  # Poll up to ~45s (9 x 5s) for the verification email to arrive
            res = (
                service.users()
                .messages()
                .list(userId="me", q="from:greenhouse.io security code newer_than:1d", maxResults=5)
                .execute()
            )
            for msg_meta in res.get("messages", []):
                msg = service.users().messages().get(userId="me", id=msg_meta["id"], format="full").execute()
                internal_ms = int(msg.get("internalDate", "0"))
                # Only accept codes received within the last 15 minutes
                if internal_ms and (time.time() * 1000 - internal_ms) > 15 * 60 * 1000:
                    continue
                combined = (msg.get("snippet", "") or "") + "\n" + _extract_body_text(msg.get("payload", {}))
                m = re.search(
                    r"(?:verification\s+field|security\s+code)[^A-Za-z0-9]{1,40}([A-Za-z0-9]{8})\b",
                    combined,
                    re.I,
                )
                if not m:
                    m = re.search(r"\b([A-Za-z0-9]{8})\b", msg.get("snippet", ""))
                if m:
                    return m.group(1)
            time.sleep(5)
    except Exception:
        return ""
    return ""


# ── Main Company Processing Loop ───────────────────────────────────────────
def submit_single_role(company_name, candidate_entry, role_idx, total_selected, dry_run=False):
    job = candidate_entry["job"]
    url = job["url"]
    report_num = candidate_entry["report_num"]
    report_path = candidate_entry["report_path"]
    print(
        f"  [3/5] Role ({role_idx}/{total_selected}) at {company_name}: "
        f"{job['role']} ({candidate_entry['score']}/5, Report #{report_num})"
    )

    # Step C: Scan the live ATS form before generating PDFs
    scan_res = run_node(["ats-submit.mjs", url, "--mode", "scan"], timeout=60)
    if scan_res.returncode != 0:
        raise RuntimeError(f"ats-submit.mjs scan failed: {scan_res.stderr}")
    scan_data = json.loads(scan_res.stdout)

    # Step D: Generate tailored CV PDF (and Cover Letter PDF if form accepts one)
    print(f"  [4/5] Generating tailored CV PDF for Report #{report_num}...")
    cv_pdf_path = generate_tailored_cv(report_num, job)
    cover_pdf_path, cover_text = (None, None)
    if scan_data.get("hasCoverLetter"):
        print("        Form has cover letter field -> generating tailored Cover Letter PDF...")
        cover_pdf_path, cover_text = generate_cover_letter_pdf(report_num, job, report_path)

    if scan_data.get("loginWall"):
        prev = load_progress().get(url, {})
        prev.update(
            {
                "status": "manual_needed_login_wall",
                "cv_pdf": cv_pdf_path,
                "cover_pdf": cover_pdf_path,
                "timestamp": datetime.now().isoformat(),
            }
        )
        update_progress(url, prev)
        print(f"  [manual] Login wall detected ({scan_data.get('atsType')}). Tailored CV saved at {cv_pdf_path}")
        return

    # Step E: Generate answers for custom questions & record in report
    custom_fields = [
        f
        for f in scan_data.get("fields", [])
        if f.get("category") == "custom" and f.get("type") != "file"
    ]
    answers = generate_form_answers(job, report_path, custom_fields)

    snapshot = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "state": "filled",
        "freeText": [
            {"question": a["label"], "answer": a["value"]}
            for a in answers
            if len(str(a.get("value", ""))) > 40
        ],
        "selections": [
            {"question": a["label"], "selection": a["value"]}
            for a in answers
            if len(str(a.get("value", ""))) <= 40
        ],
        "files": [{"field": "Resume", "path": cv_pdf_path}]
        + ([{"field": "Cover Letter", "path": cover_pdf_path}] if cover_pdf_path else []),
    }
    if report_path and os.path.exists(report_path):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as af:
            json.dump(snapshot, af, indent=2)
            ans_path = af.name
        try:
            run_node(["application-answers.mjs", "--report", report_path, "--input", ans_path, "--state", "filled"])
        finally:
            if os.path.exists(ans_path):
                os.remove(ans_path)

    # Step F: Fill & Submit the ATS form headlessly
    plan = {
        "slug": f"{report_num}-{normalize_company(company_name)}",
        "candidate": CANDIDATE_INFO,
        "resumePdfPath": cv_pdf_path,
        "coverLetterPdfPath": cover_pdf_path,
        "coverLetterText": cover_text,
        "answers": answers,
    }
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as pf:
        json.dump(plan, pf, indent=2)
        plan_path = pf.name

    try:
        submit_args = [
            "ats-submit.mjs",
            url,
            "--mode",
            "fill",
            "--plan",
            plan_path,
            "--fetch-code-cmd",
            f"{sys.executable} {os.path.abspath(__file__)} --fetch-greenhouse-code",
        ]
        if not dry_run:
            submit_args.append("--submit")

        print(f"  [5/5] {'Filling (dry-run)' if dry_run else 'Submitting'} ATS application on {url}...")
        fill_res = run_node(submit_args, timeout=150)
        if fill_res.returncode != 0:
            raise RuntimeError(f"ats-submit.mjs fill failed: {fill_res.stderr}")
        fill_data = json.loads(fill_res.stdout)
    finally:
        if os.path.exists(plan_path):
            os.remove(plan_path)

    sub_status = fill_data.get("status", "manual_needed")
    prev = load_progress().get(url, {})
    prev.update(
        {
            "status": sub_status,
            "reason": fill_data.get("reason", ""),
            "cv_pdf": cv_pdf_path,
            "cover_pdf": cover_pdf_path,
            "pre_screenshot": fill_data.get("preScreenshot"),
            "post_screenshot": fill_data.get("postScreenshot"),
            "timestamp": datetime.now().isoformat(),
        }
    )
    update_progress(url, prev)

    if sub_status == "submitted":
        today = datetime.now().strftime("%Y-%m-%d")
        run_node(
            [
                "set-status.mjs",
                "--report",
                str(report_num),
                "Applied",
                "--note",
                f"Applied {today} via corporate_mailer.py",
            ],
            timeout=20,
        )
        if report_path and os.path.exists(report_path):
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as af:
                json.dump(snapshot, af, indent=2)
                ans_path = af.name
            try:
                run_node(
                    ["application-answers.mjs", "--report", report_path, "--input", ans_path, "--state", "submitted"]
                )
            finally:
                if os.path.exists(ans_path):
                    os.remove(ans_path)
        run_node(["followup-seed.mjs", "--backfill"], timeout=20)
        mark_pipeline_url_done(url)
        print(f"  [SUCCESS] Submitted application to {company_name} ({job['role']})!")
    else:
        print(
            f"  [STATUS: {sub_status}] {fill_data.get('reason', 'Ready for inspection')} "
            f"| Screenshot: {fill_data.get('preScreenshot')}"
        )


def process_company(
    company_name,
    jobs,
    min_score,
    max_per_company=DEFAULT_MAX_PER_COMPANY,
    existing_applied_count=0,
    dry_run=False,
):
    progress = load_progress()

    submitted_in_progress = sum(
        1 for j in jobs if progress.get(j["url"], {}).get("status") in {"submitted", "applied"}
    )
    already_used = max(existing_applied_count, submitted_in_progress)
    remaining_slots = max(0, max_per_company - already_used)
    if remaining_slots == 0:
        print(f"  [skip] {company_name} already has {already_used}/{max_per_company} applications submitted.")
        return

    # Step A: Evaluate all pending roles for this company that aren't evaluated yet
    evaluated_candidates = []
    for job in jobs:
        url = job["url"]
        prev = progress.get(url, {})
        if prev.get("status") in {"submitted", "applied"}:
            continue

        if "fit_score" in prev and prev.get("report_num"):
            if not prev.get("hard_stop") and float(prev["fit_score"]) >= min_score:
                evaluated_candidates.append(
                    {
                        "job": job,
                        "score": float(prev["fit_score"]),
                        "report_num": prev["report_num"],
                        "report_path": prev.get("report_path") or find_report_path(prev["report_num"]),
                    }
                )
            continue

        print(f"  [1/5] Extracting JD for {job['role']} ({url})...")
        jd_text = extract_jd(url)
        if not jd_text:
            print(f"  [skip] Could not extract live JD (expired or unreachable): {url}")
            update_progress(
                url,
                {
                    "company": company_name,
                    "role": job["role"],
                    "status": "skipped_dead_or_empty_jd",
                    "timestamp": datetime.now().isoformat(),
                },
            )
            continue

        print(f"  [2/5] Evaluating fit for {company_name} — {job['role']}...")
        score, report_num, report_path, hard_stop, reasoning = evaluate_corporate_fit(job, jd_text)
        print(f"        Score: {score}/5 | Hard Stop: {hard_stop} | Report #{report_num}")

        status = (
            "evaluated_qualified"
            if (score >= min_score and not hard_stop)
            else "skipped_low_fit_or_hard_stop"
        )
        update_progress(
            url,
            {
                "company": company_name,
                "role": job["role"],
                "fit_score": score,
                "report_num": report_num,
                "report_path": report_path,
                "hard_stop": hard_stop,
                "reasoning": reasoning,
                "status": status,
                "timestamp": datetime.now().isoformat(),
            },
        )
        if status == "skipped_low_fit_or_hard_stop":
            mark_pipeline_url_done(url)
        else:
            evaluated_candidates.append(
                {
                    "job": job,
                    "score": score,
                    "report_num": report_num,
                    "report_path": report_path,
                }
            )

    if not evaluated_candidates:
        print(f"  [done] No role at {company_name} met threshold >= {min_score} without hard stops.")
        return

    # Step B: Select at most `remaining_slots` (up to 3) highest-scoring roles per company
    evaluated_candidates.sort(key=lambda c: c["score"], reverse=True)
    selected_roles = evaluated_candidates[:remaining_slots]
    top_reports = ",".join(str(r["report_num"]) for r in selected_roles)
    for other in evaluated_candidates[remaining_slots:]:
        other_url = other["job"]["url"]
        prev = load_progress().get(other_url, {})
        prev["status"] = f"skipped_company_cap_{max_per_company}_in_favor_of_{top_reports}"
        update_progress(other_url, prev)
        mark_pipeline_url_done(other_url)

    for role_idx, chosen in enumerate(selected_roles, 1):
        try:
            submit_single_role(
                company_name,
                chosen,
                role_idx=role_idx,
                total_selected=len(selected_roles),
                dry_run=dry_run,
            )
        except Exception as role_exc:
            print(f"  [ERROR] {company_name} ({chosen['job']['role']}): {role_exc}")


def main():
    if "--auth-gmail" in sys.argv:
        authorize_gmail_token()
        return

    if "--fetch-greenhouse-code" in sys.argv:
        print(fetch_greenhouse_code_from_gmail())
        return

    parser = argparse.ArgumentParser(description="Corporate Job Auto-Applier for mailer-vm")
    parser.add_argument("worker", type=int, nargs="?", default=0, help="0-indexed worker ID")
    parser.add_argument("workers", type=int, nargs="?", default=1, help="Total number of workers")
    parser.add_argument("--dry-run", action="store_true", help="Fill forms and take screenshots without clicking Submit")
    parser.add_argument("--scan", action="store_true", help="Run node scan.mjs before loading data/pipeline.md")
    parser.add_argument("--min-score", type=float, default=DEFAULT_MIN_FIT_SCORE, help="Minimum fit score out of 5")
    parser.add_argument(
        "--max-per-company",
        type=int,
        default=DEFAULT_MAX_PER_COMPANY,
        help="Max highest-scoring roles to apply to per company (default: 3)",
    )
    parser.add_argument("--limit", type=int, default=0, help="Max companies to process in this run (0 = all)")
    args = parser.parse_args()

    if args.scan and args.worker == 0:
        print("[scan] Running career-ops portal scanner (node scan.mjs)...")
        run_node(["scan.mjs"], timeout=300)

    applied_counts = load_applied_counts_by_company()
    pending_jobs = load_pending_pipeline_jobs()

    by_company = defaultdict(list)
    for job in pending_jobs:
        comp_key = normalize_company(job["company"])
        if applied_counts.get(comp_key, 0) >= args.max_per_company:
            continue
        by_company[job["company"]].append(job)

    all_companies = list(by_company.items())
    my_companies = [c for i, c in enumerate(all_companies) if i % args.workers == args.worker]
    if args.limit > 0:
        my_companies = my_companies[: args.limit]

    print(
        f"Worker {args.worker}/{args.workers}: {len(my_companies)} companies assigned "
        f"(out of {len(all_companies)} pending companies with < {args.max_per_company} applications)."
    )

    for idx, (company_name, jobs) in enumerate(my_companies, 1):
        comp_key = normalize_company(company_name)
        existing_cnt = applied_counts.get(comp_key, 0)
        print(
            f"\n[{idx}/{len(my_companies)}] Company: {company_name} "
            f"({len(jobs)} pending posting(s), {existing_cnt}/{args.max_per_company} already applied)"
        )
        try:
            process_company(
                company_name,
                jobs,
                min_score=args.min_score,
                max_per_company=args.max_per_company,
                existing_applied_count=existing_cnt,
                dry_run=args.dry_run,
            )
        except Exception as exc:
            print(f"  [ERROR] {company_name}: {exc}")


if __name__ == "__main__":
    main()
