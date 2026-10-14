# automatic_mailer
Automatically sends personalised cold emails, with your CV attached, to the Principal Investigators of funded research projects. It uses Gemini through Antigravity CLI, with Claude as the fallback (both through the [career-ops](https://github.com/career-ops-hq/career-ops) skill), to write the content.

Please note that the repo is extremely basic, and will be made professional if there is a sufficient demand.

## How it works

Each PI gets at most one email. For every PI in the CSV (a PI can have several projects), `main.py`:

1. Runs `/career-ops oferta` on each of the PI's projects to score how well you fit it (out of 5), and picks the highest-scoring one. If it scores below `MIN_FIT_SCORE`, the PI is skipped.
2. Runs `/career-ops email` to draft a subject and body personalised to that project.
3. Sends the email through the Gmail API with the CV at `CV_PDF_PATH` attached (the same CV for every PI). `generate_tailored_cv()` can tailor a CV per project with `/career-ops pdf` instead, but isn't used.

Each career-ops step runs headlessly through Antigravity CLI (`agy -p`, with `AGY_MODEL`), using your Google AI Pro login, to save Claude quota. If that fails, for example once its quota is used up, the same step runs through the `claude` CLI (`claude -p`), using your Claude subscription. No API keys are needed. Antigravity has no per-tool allow list, so it runs with all permissions (`--dangerously-skip-permissions`).

The script is built for long, unattended runs:

- **Retries:** network or CLI failures (when both Antigravity and Claude fail) are retried with exponential backoff, waiting 30s at first and doubling up to 10 min. A step gets `MAX_RETRIES` attempts, about 35 min in total, and a `claude` call that hangs longer than `CLAUDE_TIMEOUT` is killed and retried.
- **Resumable:** every row's result is saved to `progress.json` as `sent`, `skipped` or `failed`. Rerunning the script skips rows that were already sent or skipped and retries failed ones, reusing their saved fit score. Delete `progress.json` to start over from the first row.
- **Saved Gmail login:** the first run opens a browser to log in to Gmail and saves the login to `token.json`. Later runs reuse and refresh it. If it has expired or been revoked, at startup or in the middle of a run, the browser login opens again and the run waits until you finish.

## Setup

### 1. Folder layout

Keep `career-ops` in the same parent folder as this repo. `main.py` finds it through a relative path:

```
some-folder/
├── automatic_mailer/
└── career-ops/
```

All other paths in `main.py` are relative to the `automatic_mailer` folder, so the script works from any working directory and on any machine.

### 2. Antigravity CLI, Claude CLI and career-ops

- Install Antigravity CLI (`curl -fsSL https://antigravity.google/cli/install.sh | bash`), run `agy` once and sign in with Google OAuth using the account that has Google AI Pro. Over SSH it prints a link and asks for the code the browser shows.
- Install Claude Code (`npm install -g @anthropic-ai/claude-code`), run `claude` once and log in with `/login`.
- Clone [career-ops](https://github.com/career-ops-hq/career-ops) next to this repo and set it up following its README: `npm install`, `npx playwright install chromium` (needed for the PDF CVs), then add your CV and profile.

### 3. Python dependencies

```bash
python3 -m venv .venv
.venv/bin/pip install pandas google-api-python-client google-auth-oauthlib python-docx
```

### 4. Google CLIENT_SECRET_FILE (Gmail API)

You need a project in the Google Cloud Console with the Gmail API enabled and OAuth credentials:

1. **Create a project:** in the [Google Cloud Console](https://console.cloud.google.com/), click "Select a project" → "New Project", name it and click "Create".
2. **Enable the Gmail API:** go to "APIs & Services" → "+ ENABLE APIS AND SERVICES", search for "Gmail API" and click "Enable".
3. **Configure the OAuth consent screen:** choose "External" (or "Internal" for an organization account), fill in the required fields and add yourself as a test user. You don't need to add scopes there.
4. **Create credentials:** go to "Credentials" → "+ CREATE CREDENTIALS" → "OAuth client ID", choose "Web application" and add `http://localhost:8080/` under "Authorized redirect URIs".
5. **Download the JSON file:** click the download icon next to your client ID and put the file in this folder. Set `CLIENT_SECRET_FILE` in `main.py` to its name.

Keep this file and `token.json` private, because they give access to your Gmail. Both are in `.gitignore`.

> If the consent screen is left in "Testing" status, Google expires the saved login after 7 days. The script then just asks you to log in again.

### 5. Configure `main.py`

Edit the constants at the top of `main.py`:

| Constant | What it is |
| --- | --- |
| `CSV_FILE_PATH` | CSV of research projects |
| `CLIENT_SECRET_FILE` | Google OAuth client file from step 4 |
| `SENDER_EMAIL_ID` | Gmail address the emails are sent from |
| `AGY_MODEL` | Antigravity model tried first, e.g. `gemini-3.8-flash-low` (`agy models` lists them) |
| `CLAUDE_MODEL` | Fallback Claude model: `opus`, `sonnet` or `haiku` |
| `CLAUDE_EFFORT` | `low`, `medium`, `high`, `xhigh` or `max`; lower is faster and uses less of your Claude limit |
| `CV_PDF_PATH` | CV attached to every email (inside the career-ops folder, which isn't in git, so copy it to the server separately) |
| `MIN_FIT_SCORE` | Projects scoring below this (out of 5) are skipped |
| `MAX_RETRIES`, `RETRY_BASE_DELAY`, `RETRY_MAX_DELAY`, `CLAUDE_TIMEOUT` | Retry and timeout behaviour |

Also check these:

- **CSV columns:** the script reads `Title`, `PrincipalInvestigator`, `Abstract`, `Organization` and `PIEmailAddress`. Change the names in `main()` to match your CSV.
- **CSV encoding:** the CSV is read as `ISO-8859-1`. Change it in `main()` if it causes problems.
- **Recipient (test mode):** `pi_email_id` is currently hardcoded to the sender's address, so every email goes to you. Switch it to `row["PIEmailAddress"]` for a real run.

## Running

```bash
.venv/bin/python -u main.py 2>&1 | tee -a run.log
```

For a long run, start it inside `tmux` so it survives the terminal closing. Detach with Ctrl-b then d, and reattach with `tmux attach -t mailer`. On a Mac, prefix the command with `caffeinate -i` so the machine doesn't sleep:

```bash
tmux new -s mailer
caffeinate -i .venv/bin/python -u main.py 2>&1 | tee -a run.log
```

If the run stops for any reason, run the same command again and it continues where it left off.

**Possible duplicate email:** if the connection drops right after Gmail accepts an email but before its reply arrives, the retry can send that email twice.
