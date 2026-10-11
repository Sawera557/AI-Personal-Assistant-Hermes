# Meeting Copilot: Implementation Summary

## What You Now Have

### 1. **Enhanced Guidance System** (READY)
✅ `get_guidance_comprehensive()` function in `hermes/meeting_copilot.py`
- Pulls from 5 data sources simultaneously
- Richer system prompt designed for founder-level guidance
- Concurrent fetching (fast as the slowest source)
- Degrades gracefully if data sources unavailable

### 2. **API Support** (READY)
✅ Updated `/api/meetings/copilot` endpoint in `app.py`
- New optional parameter: `comprehensive: bool`
- New context fields: `meeting_title`, `attendees`
- Automatically routes to comprehensive guidance when requested
- Backward compatible (defaults to standard mode)

### 3. **Specification Documents** (READY)
✅ `MEETINGS_COPILOT_SPECIFICATION.md` — complete reference for:
- Two-capability framework (Question Suggester + Data Explainer)
- All 5 data sources and what they should contain
- Output format for "really good guidance"
- Implementation roadmap with 4 phases

### 4. **Memory System** (SAVED)
✅ Project memories documenting:
- Floormap processing workflow
- Comprehensive guidance framework
- All data sources and their roles

---

## What the System Expects (Data Sources)

### For Comprehensive Guidance to Work, You Need:

**1. Business Strategy** (Postgres queries)
```python
# Goals, roadmap, budget, priorities
q3_goals: ["Goal 1: ...", "Goal 2: ...", ...]
roadmap: {Q3: [...], Q4: [...]}
budget: {total, remaining, constraints}
```

**2. Team Structure** (From floormap parsing + org data)
```python
# Departments, owners, capacity, dependencies
team_structure: {departments, role_owners, dependencies}
capacity: {team: utilization%, available%}
```

**3. Risk Register** (Postgres + semantic search)
```python
# Known risks, blockers, decision impacts
known_risks: [{id, title, severity, status, owner, ...}, ...]
blockers: [{title, severity, owner, resolution_blocker}, ...]
```

**4. Decision History** (Semantic search + meeting notes)
```python
# Past decisions, their outcomes, conflicts
recent_decisions: [{date, meeting, decision, owner, status}, ...]
decision_conflicts: [{current, previous, conflict_type}, ...]
```

**5. Historical Context** (Already in place)
```python
# Indexed emails, files, meeting notes — via semantic search
# No additional setup needed
```

---

## Current Status: What Works

### ✅ Ready to Use Today:
- Standard guidance (existing)
- API endpoint with optional comprehensive mode
- Fallback system (graceful degradation if data missing)

### 🚀 Ready to Build (In Order):

**Phase 1: Data Infrastructure** (~2-3 hours)
1. Create Postgres queries for business strategy
2. Create risk register schema + queries
3. Parse floormap → team structure data
4. Create decision history schema

**Phase 2: Enhanced System** (~1-2 hours)
1. Wire up data fetchers to `_fetch_business_data()`
2. Test prompt with real meeting data
3. Refine guidance quality

**Phase 3: Frontend** (~1 hour)
1. Add "comprehensive mode" toggle to UI
2. Send `comprehensive: true` when enabled
3. Show "comprehensive guidance" badge when active

**Phase 4: Iteration** (ongoing)
1. Test with real meetings
2. Refine prompts based on what helps you decide better
3. Add more data sources as they become available

---

## How to Use It

### Standard Mode (Today):
```bash
POST /api/meetings/copilot
{
  "business": "your-slug",
  "latest_text": "We should rebuild auth",
  "notes_so_far": "[previous discussion...]",
  "session_id": "optional-session-id"
}

# Returns:
{
  "bullets": ["Risk: conflicts with X", "Question: why?", ...],
  "sources": [...]
}
```

### Comprehensive Mode (Once Data Sources Ready):
```bash
POST /api/meetings/copilot
{
  "business": "your-slug",
  "latest_text": "We should rebuild auth",
  "notes_so_far": "[previous discussion...]",
  "meeting_title": "Q3 Planning",
  "attendees": [{name, email}, ...],
  "comprehensive": true,
  "session_id": "optional-session-id"
}

# Returns (much richer):
{
  "bullets": [
    "RISK: This conflicts with the Sept 15 launch decision — would slip mobile by 2 weeks",
    "CAPACITY: Engineering at 85% utilization already; this requires cut elsewhere",
    "UNKNOWN: Success metric for UX improvement not defined; track onboarding time",
    "NEXT: CFO needs to approve budget before committing timeline"
  ],
  "sources": [...]
}
```

---

## The Real Power: Data-Driven Decisions

Your meetings copilot will become truly valuable when it can say:

❌ *Generic:* "Make sure to plan resources"
✅ *Specific:* "Sarah's team is 85% utilized. Adding this scope means slipping the Sept 15 API launch or cutting the mobile app work. What's the priority?"

❌ *Vague:* "Watch out for risks"
✅ *Specific:* "Last quarter's similar project took 3x longer than estimated. Watch for estimation bias — build 2x buffer into your timeline estimate."

❌ *Guessing:* "Budget might be a constraint"
✅ *Specific:* "Engineering headcount budget remaining: $180k. This hire costs $90k + benefits. You have room for 1.5 more FTEs, not 2."

---

## Next Action: Choose Your Starting Point

### Option A: Quick Win (1 hour)
Test comprehensive mode with dummy data to prove the concept:
1. Hardcode sample business data in `_fetch_business_data()`
2. Call endpoint with `comprehensive: true`
3. See if the guidance is actually better
4. Then build the real data pipeline

### Option B: Build Incrementally (Start with Phase 1)
1. Create business strategy queries first (Q3 goals, roadmap)
2. Wire them into `_fetch_business_data()`
3. Test comprehensive guidance with just strategy layer
4. Add risk register next, then capacity, then decision history

### Option C: Hook Into Existing Systems
If you already have goals/roadmap/risks somewhere:
1. Create queries to fetch from those systems
2. Implement the fetchers in `_fetch_business_data()`
3. Enable comprehensive mode on next meeting

---

## Files Changed

- ✅ `hermes/meeting_copilot.py` — Added `get_guidance_comprehensive()` + `_fetch_business_data()`
- ✅ `app.py` — Updated imports + added comprehensive support to endpoint
- ✅ Memory system — Saved implementation details for future reference

## Files to Create (Phase 1)

- `hermes/db/business_strategy.py` — Queries for goals, roadmap, budget
- `hermes/db/risk_register.py` — Risk and blocker queries
- `hermes/db/team_structure.py` — Org data from floormap
- `hermes/db/decision_history.py` — Past decisions and outcomes

---

## Success Criteria

Your meetings copilot is truly excellent when:

1. **Every bullet is grounded** — Can point to the source (goal, risk, person, decision)
2. **No fluff** — 0-4 bullets per turn, never generic advice
3. **Actionable** — "Do this before next step" or "Ask X before deciding"
4. **Owner-aware** — "Sarah owns this" not "someone should handle it"
5. **Helpful in real meetings** — You actually use the guidance to make better decisions

---

## Questions?

See the specification for details on data schema, prompt design, and quality checks.
