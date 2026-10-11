import pandas as pd
import base64
import time
import httplib2
from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import subprocess
import os
import re
import json
from docx import Document
from email.mime.application import MIMEApplication


# Constants
# Paths are relative to this script's folder, so it works from any working directory and on any machine
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_FILE_PATH = os.path.join(SCRIPT_DIR, 'Awards.csv') # csv file that contains the database of research projects : Their project's description, principal investigator's name and their email id, project's title
SCOPES = ['https://www.googleapis.com/auth/gmail.send'] # Don't change
CLIENT_SECRET_FILE = os.path.join(SCRIPT_DIR, 'client_secret_594050885167-jihr4u58rt0b4e08ucl4h5m9re2oif6c.apps.googleusercontent.com.json') # Details mentioned in the README file
API_SERVICE_NAME = 'gmail'
API_VERSION = 'v1'
CLAUDE_MODEL = "opus" # Model alias for the claude CLI ("opus", "sonnet", "haiku"). Uses your Claude subscription login
CLAUDE_EFFORT = "xhigh" # Reasoning effort for the claude CLI ("low", "medium", "high", "xhigh", "max"). Lower is faster and uses less quota
RESUME_DOC_PATH = os.path.join(SCRIPT_DIR, "AkhilRajResumeOriginal.docx") # Path where resume is in the .docx format, so that it can be provided in text format to Claude
RESUME_PDF_FILE_PATH = os.path.join(SCRIPT_DIR, "AkhilRajResumeOriginal.pdf") # Path where resume is in pdf format, so that it can be attached to the mail(s)
SENDER_EMAIL_ID = 'ar2427@cornell.edu'
TOKEN_FILE = os.path.join(SCRIPT_DIR, 'token.json') # Saved Gmail login, so restarts don't need the browser. Delete it to log in again
PROGRESS_FILE = os.path.join(SCRIPT_DIR, 'progress.json') # Which rows are done; rerunning the script resumes from here. Delete it to start over from row 0
MAX_RETRIES = 8 # Attempts per network step before giving up on a row (it will be retried on the next run)
RETRY_BASE_DELAY = 30 # Seconds before the first retry; doubles each time up to RETRY_MAX_DELAY (~35 min of retrying in total)
RETRY_MAX_DELAY = 600
CLAUDE_TIMEOUT = 1800 # Seconds before a hung claude CLI call is killed and retried
TRANSIENT_ERRORS = (subprocess.SubprocessError, HttpError, OSError, httplib2.HttpLib2Error, TransportError, json.JSONDecodeError, KeyError)
CAREER_OPS_DIR = os.path.join(SCRIPT_DIR, "..", "career-ops") # career-ops checkout, kept next to this folder; its /career-ops skill evaluates fit, tailors the CV and drafts the mail
MIN_FIT_SCORE = 3.5 # Skip projects that career-ops 'oferta' scores below this (out of 5)
CAREER_OPS_TOOLS = "Read Write Edit Glob Grep Bash(node:*) Bash(npm:*)" # Tools pre-approved for headless career-ops runs (no one is there to answer permission prompts)
EMAIL_SCHEMA = { # Structure enforced on the 'career-ops email' output
    "type": "object",
    "properties": {
        "subject": {"type": "string", "description": "The email's subject line only, without the 'Subject:' prefix"},
        "body": {"type": "string", "description": "The email body exactly as it should be sent, from greeting to signature/contact block; no attachment checklist, context line, notes or commentary"},
    },
    "required": ["subject", "body"],
    "additionalProperties": False,
}

def with_retries(fn, *args, **kwargs):
    # Retries fn on network/CLI failures with exponential backoff, so a dropped connection just pauses the run
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return fn(*args, **kwargs)
        except TRANSIENT_ERRORS as error:
            if attempt == MAX_RETRIES:
                raise
            delay = min(RETRY_BASE_DELAY * 2 ** (attempt - 1), RETRY_MAX_DELAY)
            details = getattr(error, 'stderr', None) or error
            print(f"Attempt {attempt}/{MAX_RETRIES} of {fn.__name__} failed: {details}\nRetrying in {delay}s")
            time.sleep(delay)

def load_saved_creds():
    # Returns usable saved credentials, or None if there are none or they can't be refreshed (expired/revoked/corrupt)
    try:
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    except (OSError, ValueError):
        return None
    if creds.valid:
        return creds
    if creds.refresh_token:
        try:
            with_retries(creds.refresh, Request())
            return creds
        except RefreshError as error:
            print(f"Saved Gmail login no longer works ({error}); logging in again")
    return None

def get_gmail_service(force_login=False):
    creds = None if force_login else load_saved_creds()
    if not creds:
        print("Opening the browser for Gmail login (the run waits here until you finish)")
        flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_FILE, SCOPES, redirect_uri='http://localhost:8080/') # make sure the port is not occupied!
        creds = flow.run_local_server(port=8080)
    with open(TOKEN_FILE, 'w') as f:
        f.write(creds.to_json())
    service = build(API_SERVICE_NAME, API_VERSION, credentials=creds)
    return service

def load_progress():
    if not os.path.exists(PROGRESS_FILE):
        return {}
    with open(PROGRESS_FILE) as f:
        return json.load(f)

def save_progress(progress):
    # Write to a temp file then rename, so a crash mid-write can't corrupt the progress file
    tmp_path = PROGRESS_FILE + '.tmp'
    with open(tmp_path, 'w') as f:
        json.dump(progress, f, indent=2)
    os.replace(tmp_path, PROGRESS_FILE)

def create_message(sender, to, subject, message_text, resume_attachment):
    message = MIMEMultipart()
    message['to'] = to
    message['from'] = sender
    message['subject'] = subject
    msg = MIMEText(message_text, 'plain', 'utf-8')
    message.attach(msg)
    message.attach(resume_attachment)

    raw = base64.urlsafe_b64encode(message.as_bytes())
    return {'raw': raw.decode()}

def send_message(service, user_id, message):
    message = service.users().messages().send(userId=user_id, body=message).execute()
    print(f'Message Id: {message["id"]}')
    return message["id"]

def ask_claude(prompt):
    # Runs Claude Code in headless mode, which uses the logged-in Claude subscription (run `claude` then `/login` once)
    result = subprocess.run(
        ["claude", "-p", "--model", CLAUDE_MODEL, "--effort", CLAUDE_EFFORT, "--output-format", "text"],
        input=prompt, capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()

def career_ops(args, json_schema=None):
    # Runs the /career-ops skill from the career-ops repo headlessly (e.g. "oferta <JD>", "pdf 051", "email 051")
    # With json_schema, the claude CLI validates the final answer against it and returns it as a dict
    cmd = ["claude", "-p", f"/career-ops {args}", "--model", CLAUDE_MODEL, "--effort", CLAUDE_EFFORT, "--allowedTools", CAREER_OPS_TOOLS]
    if json_schema:
        cmd += ["--output-format", "json", "--json-schema", json.dumps(json_schema)]
    def run_career_ops():
        result = subprocess.run(cmd, cwd=CAREER_OPS_DIR, capture_output=True, text=True, check=True, timeout=CLAUDE_TIMEOUT)
        return json.loads(result.stdout)["structured_output"] if json_schema else result.stdout
    return with_retries(run_career_ops)

def report_mtimes(reports_dir):
    return {name: os.path.getmtime(os.path.join(reports_dir, name)) for name in os.listdir(reports_dir)}

def evaluate_fit(jd):
    # career-ops 'oferta' saves reports/{NNN}-{slug}-{date}.md, whose header has '**Score:** X/5'
    # A project evaluated before gets its existing report revised instead of a new one, so look for new or changed files
    reports_dir = os.path.join(CAREER_OPS_DIR, "reports")
    before = report_mtimes(reports_dir)
    career_ops(f"oferta {jd}")
    after = report_mtimes(reports_dir)
    changed = [name for name, mtime in after.items() if before.get(name) != mtime]
    if not changed:
        return None, 0.0
    report = max(changed, key=after.get)
    with open(os.path.join(reports_dir, report)) as f:
        score = re.search(r"\*\*Score:\*\*\s*([\d.]+)", f.read())
    return report.split("-")[0], (float(score.group(1)) if score else 0.0)

def generate_tailored_cv(report_num):
    # career-ops 'pdf' records the generated PDF against the report in data/pdf-index.tsv (report <tab> pdf <tab> ...)
    career_ops(f"pdf {report_num}")
    pdf_path = RESUME_PDF_FILE_PATH # fallback if no tailored PDF was generated
    with open(os.path.join(CAREER_OPS_DIR, "data", "pdf-index.tsv")) as f:
        for line in f:
            cols = line.rstrip("\n").split("\t")
            if len(cols) > 1 and cols[0] == report_num:
                pdf_path = os.path.join(CAREER_OPS_DIR, cols[1])
    return pdf_path

def make_attachment(path):
    with open(path, 'rb') as f:
        attachment = MIMEApplication(f.read(), Name=os.path.basename(path))
    attachment['Content-Disposition'] = f'attachment; filename="{os.path.basename(path)}"'
    return attachment

def generate_email_content(report_num):
    # career-ops 'email' drafts the mail; the schema makes the claude CLI return just its subject and body
    draft = career_ops(f"email {report_num}", json_schema=EMAIL_SCHEMA)

    return draft["subject"].strip(), draft["body"].strip()

def main():
    service = get_gmail_service()
    df = pd.read_csv(CSV_FILE_PATH, encoding="ISO-8859-1") # change encoding if required

    progress = load_progress()

    count = 0

    for index, row in df.iterrows():
        entry = progress.get(str(index), {})
        if entry.get("status") in ("sent", "skipped"):
            continue # already handled in an earlier run; "failed" rows are tried again
        count += 1
        project_title = row['Title'] # Change as per your needs
        pi_name = row['PrincipalInvestigator'] # change as per your needs
        project_description = row['Abstract'] # change as per your needs
        pi_email_id = "ar2427@cornell.edu" # row["PIEmailAddress"] # change as per your neeeds

        organization = row['Organization'] # change as per your needs

        jd = f"Funded research project (this is a cold job application to the Principal Investigator, asking for a paid role on the project; no specific position is assumed)\n\nPrincipal Investigator : {pi_name}\nOrganization : {organization}\nProject title : {project_title}\n\nProject description : {project_description}"
        try:
            if entry.get("report"): # scored in an earlier run that failed later on; reuse it
                report_num, score = entry["report"], entry["score"]
            else:
                report_num, score = evaluate_fit(jd)
            print(f"Fit score {score}/5 for '{project_title}' (career-ops report {report_num})")
            if report_num is None: # career-ops wrote no report, so the score is unknown; retry on the next run
                print(f"No career-ops report for row {index}; marking it failed")
                progress[str(index)] = {"status": "failed", "error": "career-ops produced no report"}
                save_progress(progress)
                continue
            if score < MIN_FIT_SCORE:
                progress[str(index)] = {"status": "skipped", "report": report_num, "score": score}
                save_progress(progress)
                continue
            progress[str(index)] = {"status": "failed", "report": report_num, "score": score} # until the mail goes out
            save_progress(progress)

            resume_attachment = make_attachment(generate_tailored_cv(report_num))
            subject, body = generate_email_content(report_num)

            to = pi_email_id #"ar2427@cornell.edu"
            # to = 'akhil.raj1997@gmail.com'
            message = create_message(SENDER_EMAIL_ID, to, subject, body, resume_attachment)
            print(f"Sending mail number {count} to {pi_name} at {pi_email_id}")
            try:
                message_id = with_retries(send_message, service, 'me', message)
            except RefreshError as error: # login expired/revoked mid-run (e.g. 7-day limit on Testing apps)
                print(f"Gmail login expired during the run ({error})")
                service = get_gmail_service(force_login=True)
                message_id = with_retries(send_message, service, 'me', message)
            progress[str(index)] = {"status": "sent", "report": report_num, "score": score, "message_id": message_id}
            save_progress(progress)
        except TRANSIENT_ERRORS as error:
            # Out of retries (e.g. a long outage): note it and move on; the next run picks this row up again
            print(f"Giving up on row {index} for now: {error}")
            progress[str(index)] = {**progress.get(str(index), {}), "status": "failed", "error": str(error)}
            save_progress(progress)

if __name__ == '__main__':
    main()
