# GitHub → Jira push automation

**Ticket:** [KAN-24](https://sawerasworkspace-27791394.atlassian.net/browse/KAN-24) *("HRM-15 — GitHub to Jira push automation")* · Epic KAN-7 · owner: haider

When someone pushes to any branch, opens/merges a PR, or reviews a PR, the update
is written into the referenced Jira issue as a comment — branch, commits, PR
status, reviewer, merge state.

> **Scope rule, from the ticket.** This writes to the vendor team's execution
> tracker only. It is not Gandi's command layer — Asana stays that. Nothing here
> should grow into Gandi's board.

## What already existed

KAN-81 shipped the Jira plumbing: `jira_search_issues`, `jira_get_issue`,
`jira_create_issue`, `jira_assign_issue` in `hermes/tools.py`, over the
Maton-proxied Jira REST transport in `hermes/maton.py`. This package is the
GitHub half only, plus the one Jira call KAN-81 didn't need — `add_comment`.

## Tracked repos

| Repo | Verified? |
|---|---|
| `gandikediye-afk/Jira-Github-update-automation` | 404 unauthenticated |
| `gandikediye-afk/pinecone-mcp-claude` | 404 unauthenticated |
| `gandikediye-afk/Hermes-MVP-DataIngestion` | 404 unauthenticated |

GitHub returns 404 for private repos as well as nonexistent ones, so these could
not be confirmed to exist from here — `gh` is not installed and the GitHub MCP
server needs an interactive OAuth flow. An allowlist entry for a repo that never
sends a webhook is inert, so configuring them is safe either way. Confirm by
installing the webhook: a green tick on the `ping` delivery proves the repo, the
URL and the secret all line up.

## Who pushed

**No roster to maintain.** Every collaborator's real name is already in the
payload — `commits[].author.name` is whatever they set as `git config user.name`
— so Haider, Auroob and anyone else added to a repo are named correctly the
first time they push, with nothing configured.

For one identity, in order:

1. an explicit **correction** (below)
2. the real name on a commit by that same person in this push, matched on GitHub
   login or email — so a push by `haider-dev` reads "Haider Ali", not the login
3. the name GitHub put in the payload
4. the raw login — never a guess dressed up as a name

Two lines appear on a push, because they answer different questions:

| Field | Means |
|---|---|
| **Pushed by** | who ran `git push` |
| **Written by** | the distinct commit authors — only shown when it differs from the pusher |

So a branch Auroob pushes containing Haider's commits reads *"Pushed by: Auroob
Fatima · Written by: Haider Ali, Auroob Fatima"*, and each commit line carries
its own author.

Names are tidied only when entirely lowercase (`haider ali` → `Haider Ali`).
Anything with existing capitals is left exactly as written, so `McDonald`,
`de Vries` and `Auroob F.` survive — mangling someone's own spelling is worse
than leaving it lowercase.

### The one correction

`GITHUB_JIRA_PEOPLE` exists for identities the payload gets **wrong**, not
missing. Only one is configured: the `gandikediye-afk` account's commits are
Sawera's work, so it renders as **Sawera** whether the payload supplies the
login, the display name `Gandi Kediye`, or the email. Additions merge over that
default and can never silently drop it.

## Auto-created tickets

Work that matches nothing on the board opens a Jira task rather than being
skipped.

- **One ticket per branch, not per push.** The first unmatched push to a branch
  opens the ticket and records the link (`github_jira_branch_issues`, migration
  0020); every push after that comments on it. Without this you get a ticket per
  push, all describing the same work.
- **The branch link outranks inference**, so a branch cannot drift to a
  different ticket because a later commit message reads differently.
- **Routine upkeep opens nothing** — dependency bumps, typo fixes, formatting,
  lockfiles, CI config. The model is asked to return null for these, and did so
  in live testing.
- Every auto-created ticket is labelled `github-automation` and `auto-created`,
  and its description says a model drafted it and shows the commits it read.

## Status transitions

| Trigger | Status |
|---|---|
| Commits pushed | **In Progress** |
| Pull request opened / reopened / ready for review | **In Review** |
| Pull request **merged** | **Done** |
| PR closed without merging | *unchanged* — abandoned work is not finished work |
| Branch deleted | *unchanged* — branches are deleted long after merging |

Matched on the status **name**, not a hard-coded transition id: ids are
per-workflow and change when the board is edited, and a stale id moves tickets
silently to the wrong column. Verified live against KAN — `In Progress` and
`In Review` both exist as transitions.

**Done is protected.** The automation refuses to move a ticket *out of* it, so a
stray commit on an old branch cannot drag finished work back onto the board. It
still moves work *into* Done on merge — the protection is one-directional.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/webhooks/github` | the webhook GitHub delivers to |
| `GET` | `/api/webhooks/github/health` | config check — reports whether a secret is set, never what it is |

Mounted into the existing FastAPI app (`app.py`), so it rides the current Vercel
deployment and gets a public HTTPS URL without a second service to run.

## How an event becomes a comment

```
delivery → verify HMAC → parse event → resolve issue key → render ADF → POST comment
             (401)         (skip)         (skip)                          (200 + result)
```

**The branch is the unit of work.** A branch is bound to exactly one ticket the
first time it is seen, and every later event on it resolves through that
binding — no regex, no model call, no chance of drifting onto a different ticket
because someone worded a commit differently.

Resolution stops at the first hit:

1. **an existing binding for this branch** — the normal path after the first event
2. **a binding for a branch this push merged** — a push to `main` belongs to the
   ticket of the branch that landed, not to `main`
3. **an explicit key** in the branch name → merge commit's source branch → PR
   title → PR body → commit messages
4. **AI match** against the live backlog (see [AI issue matching](#ai-issue-matching))
5. **a new ticket**

Whatever resolves at 3–5 is then bound, so step 1 answers next time.

**Trunk branches are never bound** — `main`, `master`, `develop`, `dev`, `trunk`,
`staging`, `production`, `release/*`. Binding a shared branch would nail every
future push to it onto one permanent ticket. Configurable via
`GITHUB_JIRA_TRUNK_BRANCHES`.

### One branch, one ticket, four events

```
push  feature/retry-logic   -> no binding -> no match -> opens KAN-901,
                               binds the branch          -> In Progress
push  feature/retry-logic   -> bound -> KAN-901          (no model call)
PR    feature/retry-logic   -> bound -> KAN-901          -> In Review
merge feature/retry-logic   -> bound -> KAN-901          -> Done
```

Covered end-to-end by `test_the_full_branch_lifecycle`.

**If no key is found and the AI matcher declines, nothing is written.** The response says
`{"action": "skipped", "reason": "no_issue_key"}` and names the sources it
searched. Guessing a ticket would attach real work to the wrong issue, and a
wrong Jira comment is worse than a missing one. `GITHUB_JIRA_DEFAULT_ISSUE_KEY`
opts into a catch-all if you want one; it is reported with `source: "default"`
so it is never mistaken for a real reference found in the event.

**Matching is case-insensitive** while a project allowlist is configured (branch
names are conventionally lowercase, and the allowlist keeps the false-positive
surface tiny). With `GITHUB_JIRA_PROJECT_KEYS=*` there is no allowlist to lean
on, so matching requires the canonical uppercase form — otherwise `utf-8`,
`covid-19` and `sha-1` would all resolve as issue keys.

### Events handled

| Event | Written to Jira |
|---|---|
| `push` | branch, commit list (sha + first line + author), pusher, head commit, force-push / branch created / branch deleted |
| `pull_request` | PR number and title, `head -> base`, status (open / draft / merged by X / closed without merging), author, requested reviewers, merge commit |
| `pull_request_review` | reviewer, decision (approved / changes requested / commented), review note |
| `ping` | acknowledged with a pong |

Tag pushes, `pull_request_review` actions other than `submitted`, and every other
event type are acknowledged and ignored.

### Progress on the ticket, from commits and merges

A merge produces **two** comments, and that is deliberate — they say different
things:

1. the `pull_request` event → *"Pull request #42 merged: … · merged by sawera"* — the state change;
2. the `push` to `main` → the list of commits that actually landed.

The second one is the fragile half: the branch is now `main`, which names no
ticket. Two independent sources keep it attached, so it survives either habit
lapsing:

- **the merge commit's source branch** — GitHub writes
  `Merge pull request #42 from owner/feature/KAN-24-webhook` itself, so this
  works even when nobody prefixed a single commit;
- **the commit messages** — `KAN-24: …`, which covers direct pushes to `main`
  and rebase merges, where there is no merge commit to read.

**The gap that remains: squash merges.** GitHub replaces every message with the
PR title plus `(#42)`, so if the PR title omits the key, nothing in the push
references the ticket and the commit list is skipped. The merge state change
(comment 4) still posts. Keeping the key in PR titles closes this; so does using
merge commits instead of squash.

Covered by `test_merge_commit_recovers_the_source_branch`,
`test_commit_messages_remain_a_source_alongside_the_merge_branch`, and
`test_merge_to_main_reports_the_commits_that_landed`.

## Setup

### 1. Configure the deployment

Set these (see `.env.example` in the repo root for the annotated versions):

```
GITHUB_WEBHOOK_SECRET=<a long random string>
GITHUB_JIRA_REPOS=gandikediye-afk/Hermes-Agent-MVP
GITHUB_JIRA_PROJECT_KEYS=KAN
GITHUB_JIRA_DRY_RUN=true          # flip to false once the mapping looks right
```

`GITHUB_WEBHOOK_SECRET` is required. Without it the endpoint returns 503 and
processes nothing — an unsigned webhook that writes to a live board is an open
comment-injection endpoint, so it fails closed.

Jira access uses `GITHUB_JIRA_MATON_API_KEY` — Sawera's Maton account, which is
the one connected to `sawerasworkspace-27791394`. It is a separate variable from
the default `MATON_API_KEY` (Gandi's Gmail/Drive/Calendar) so that rotating one
never silently breaks the other, and so the ticket's scope rule holds at the
credential level rather than by convention: this key can only reach the vendor
team's board. Unset, it falls back to `MATON_API_KEY`.

## AI issue matching

Rendering a comment is deterministic — a payload-to-fields mapping and an ADF
template, nothing generated, nothing to hallucinate. A model is used for exactly
one decision: **which ticket does this work belong to, when the code says
nothing?**

Those pushes were previously skipped outright, so inference only fills silence.
It runs on `gpt-4.1-nano`, the cheapest OpenAI text model — one short call per
otherwise unattributable push, and no call at all when a key was found the
normal way.

### Guardrails

| Risk | Guard |
|---|---|
| Overriding a human's stated ticket | An explicit key **always** wins; the model is never consulted when one exists |
| Inventing an issue key | Candidates are fetched from Jira and the answer is validated back against that closed set; anything else is discarded |
| Forcing a match on unrelated work | "None" is a first-class answer the prompt asks for by name, plus a 0.7 confidence floor |
| A guess reading as a fact | The comment says *"⚠ Ticket chosen by AI, not stated in the code"* with the confidence, the model's reason, and how to stop the guessing |
| Matcher outage taking the hook down | Every failure path — no key, network error, bad JSON, unknown key, low confidence — returns "no match" and the push simply skips |

### Verified against the real backlog

31 unresolved KAN issues, `gpt-4.1-nano`, 0.7 floor:

| Push | Result |
|---|---|
| `webhook-work` — "add signature verification", "comment the update onto the linked jira issue" | **KAN-24 at 90%**, moved to In Progress |
| `retry-ratelimits` — "retry pinecone upserts when the index rate-limits" | **no match → ticket opened**: *"Implement retry with exponential backoff for Pinecone upsert operations"* |
| `chore/deps` — "bump requests to 2.32.3", "reformat with black" | **nothing** — no match, no ticket |

### Why matching and proposing are two calls

Both decisions were first tried in a single call, and it made the model worse at
each: it proposed a duplicate of the ticket it had previously matched at 90%,
while simultaneously declining to propose anything for genuinely untracked work.
Split into two focused calls — the second only running when the first finds
nothing — all three cases above behave correctly. `test_matching_and_proposing_are_separate_calls`
locks that in.

The two declines matter more than the hit: a matcher that always finds something
is worse than none, because it turns every chore into false progress on a real
ticket.

### The honest limit

This is a guess. It's disclosed as one, bounded to real issues, and held to a
confidence floor — but the fix for a mis-attributed comment is not a better
prompt, it's putting `KAN-24` in the branch name. The comment says exactly that,
every time it guesses.

### 2. Apply the migration

```bash
curl -X POST https://<your-deployment>/api/admin/migrate
```

Creates `github_jira_deliveries` (`hermes/db/migrations/0019_*.sql`), which makes
redelivered webhooks idempotent. If the DB is unreachable the automation still
posts — losing a real push is worse than an occasional duplicate comment on a
replay — so this step is recommended, not blocking.

### 3. Add the webhook in GitHub

`gandikediye-afk/Hermes-Agent-MVP` → Settings → Webhooks → Add webhook:

- **Payload URL:** `https://<your-deployment>/api/webhooks/github`
- **Content type:** `application/json`
- **Secret:** the same `GITHUB_WEBHOOK_SECRET`
- **Events:** *Let me select individual events* → **Pushes**, **Pull requests**,
  **Pull request reviews**

GitHub sends a `ping` on save; a green tick means the signature check passed.

### 4. Verify, then go live

With `GITHUB_JIRA_DRY_RUN=true`, push a branch named `kan-24-test` and read the
delivery response in GitHub's *Recent Deliveries* tab — it contains the resolved
issue key, its provenance, and a plain-text `preview` of the exact comment. When
that looks right, set `GITHUB_JIRA_DRY_RUN=false`.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `GITHUB_WEBHOOK_SECRET` | — | **Required.** Shared secret; empty ⇒ 503, nothing processed |
| `GITHUB_JIRA_MATON_API_KEY` | *(falls back to `MATON_API_KEY`)* | Maton key for the Jira writes — Sawera's account, which holds the connection to `sawerasworkspace-27791394`. Needs `write:jira-work` |
| `GITHUB_JIRA_ENABLED` | `true` | Master switch |
| `GITHUB_JIRA_DRY_RUN` | `false` | Resolve + render, never post |
| `GITHUB_JIRA_REPOS` | *(any)* | `owner/name` allowlist, comma-separated |
| `GITHUB_JIRA_PROJECT_KEYS` | `KAN` | Allowed project keys; `*` for any |
| `GITHUB_JIRA_DEFAULT_ISSUE_KEY` | *(unset)* | Catch-all for unlabelled pushes |
| `GITHUB_JIRA_MAX_COMMITS` | `10` | Commits listed in full; the rest are counted and declared in the comment |

## Layout

```
gh_jira/
  config.py       env-driven settings, read per call so changes take effect
  signature.py    X-Hub-Signature-256 HMAC verification
  issue_keys.py   branch/PR/commit -> Jira key, with provenance
  events.py       GitHub payloads -> one normalised Update shape
  adf.py          Update -> Atlassian Document Format comment body
  jira_client.py  cloud-id resolution (cached) + add_comment, over hermes.maton
  dedupe.py       X-GitHub-Delivery idempotency, degrades open
  service.py      the decision chain; never raises into the request path
  router.py       FastAPI routes
tests/
  test_gh_jira.py 74 offline checks — no network, no DB, no secrets
```

## Status codes, and why

GitHub marks non-2xx deliveries as failures and eventually auto-disables a hook
that keeps failing. So `401` is reserved for a genuinely unauthenticated caller,
`503` for a deployment with no secret configured, and everything downstream —
Jira down, stale issue key, DB unreachable — comes back as `200` with `ok: false`
and the detail in the body. A Jira outage must not take the webhook down with it.

## Tests

```bash
python github-jira-automation/tests/test_gh_jira.py
```

Runs in CI on every push and PR alongside the repo's other offline suites.
