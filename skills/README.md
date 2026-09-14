# Business Skills

Reusable Agent Skills, scoped per business, that show up under **Context → Skills**
in the app. They are sourced from public, openly-licensed skill repos rather
than written here — [`MANIFEST.json`](MANIFEST.json) is the roster and records
where each one came from.

## How they work

A Skill is stored as an ordinary `documents` row with
`source_system = "business_skill"`. That one tag is the only thing separating it
from a Context file:

| Sub-tab  | `source_system`    |
|----------|--------------------|
| Files    | `business_context` |
| Skills   | `business_skill`   |

Because they go through the normal ingest path, every skill is also chunked and
embedded, so the agent can pull one up mid-conversation through
`search_business_knowledge` — a citation will show `business_skill` as its
source.

`scope` is a `businesses.slug` (`afk`, `gak`, `gif`, `gakc`, `nh`, `per`), or
`all` for a global skill every business can see.

## The roster

| Scope | Business | Skills |
|---|---|---|
| `afk` | Afro Farm Holding Ltd | Farm Enterprise Finance · Farm Labour Planning · Crop Calendar & Rotation |
| `gak` | GAK Properties LLC | Rent Roll & T-12 Normalizer · Rental Property Bookkeeping · Receivables Aging |
| `gif` | GIF Properties LLC | Zoning & Entitlement Decoder · Entitlement & Permit Monitor · Cash Flow Forecast |
| `gakc` | GAK Commodities Ltd | Off-Market Deal Sourcing · Developer Feasibility · Multi-Entity Separation |
| `nh` | Nour Hammam LLC | Competitor Analysis · Pricing Strategy · Launch Playbook |
| `per` | Personal & Family | Net Worth Statement · Insurance Audit · Subscription Audit |
| `all` | All Businesses | **Deep Research** · Month-End Close · Monthly Digest |

## Upstream sources

All MIT-licensed:

- [human-avatar/skills-for-agriculture](https://github.com/human-avatar/skills-for-agriculture) — farm operations
- [sasha-deneux/claude-skills-cre](https://github.com/sasha-deneux/claude-skills-cre) — commercial real estate
- [openaccountant/skills](https://github.com/openaccountant/skills) — bookkeeping and personal finance
- [coreyhaines31/marketingskills](https://github.com/coreyhaines31/marketingskills) — marketing
- [199-biotechnologies/claude-deep-research-skill](https://github.com/199-biotechnologies/claude-deep-research-skill) — deep research

The `.md` files under each scope directory are **generated** — they are the
upstream `SKILL.md` verbatim with a provenance header prepended. Don't hand-edit
them; edit `MANIFEST.json` and re-fetch.

## Refreshing and loading

```bash
# Re-download every skill from upstream into skills/<scope>/
python scripts/fetch_business_skills.py

# See what would be uploaded, without touching the database
python scripts/seed_business_skills.py --dry-run

# Upload them (needs Hermes running; embeds each one, so it costs API calls)
python scripts/seed_business_skills.py --base-url http://127.0.0.1:8000
```

Uploading twice creates duplicate rows — `/api/documents` has no upsert. Use
`--replace` to delete a same-titled skill in the same scope first, or `--scope`
to re-seed just one business.

## Adding your own

The Skills sub-tab writes as well as reads: pick a business, open **Skills**,
and either paste the markdown into the quick-add box or use **Upload SKILL.md**.
It saves under the same `business_skill` tag and is immediately searchable. To
make it reproducible instead, add an entry to `MANIFEST.json` and re-run the
two scripts.
