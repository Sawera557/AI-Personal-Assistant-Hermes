# Meeting Copilot: Comprehensive Guidance Specification

## Overview
Your meetings copilot needs **really good guidance** for a founder. That means pulling from ALL available business data, not just what's in indexed emails.

---

## Two Core Capabilities

### 1. **Smart Question Suggester** 
Surfaces the questions you should be asking:
- Strategic alignment: "How does this serve Q3 goals?"
- Risk questions: "What could break if we do this?"
- Dependency questions: "Which teams does this impact?"
- Execution questions: "Do we have bandwidth for this?"

### 2. **Data Explainer**
Turn raw numbers into insights:
- Anomalies: "This is unusual, you should know"
- Trends: "This is accelerating/declining"
- Gaps: "Your discussion doesn't match what the data shows"
- Constraints: "Budget/timeline/team capacity impacts this"

---

## Data Sources to Pull From

### Business Strategy Layer
**Source: Database + Strategic Docs**
```
q3_goals: [
  "Goal 1: description and KPIs",
  "Goal 2: description and KPIs",
  ...
]

roadmap: {
  "Q3": ["Milestone 1", "Milestone 2", ...],
  "Q4": ["Milestone 1", ...],
  key_milestones: ["date: deliverable", ...]
}

budget: {
  total_allocated: amount,
  remaining: amount,
  committed: {team: amount, ...},
  constraints: ["..."]
}

priorities_ranked: [1, 2, 3, ...]  # Which goals rank where
```

### Team & Organizational Structure
**Source: Floormap + Calendar Data**
```
team_structure: {
  departments: {
    "Engineering": ["Sarah", "James", ...],
    "Product": ["Maria", ...],
    ...
  },
  
  role_owners: {
    "API redesign": "Sarah",
    "Mobile app": "James",
    ...
  },
  
  dependencies: [
    {from: "Engineering", to: "Product", reason: "..."},
    {from: "Product", to: "Growth", reason: "..."},
    ...
  ]
}

capacity: {
  "Engineering": {utilization: 85%, available_capacity: "15%"},
  "Product": {utilization: 70%, available_capacity: "30%"},
  ...
}
```

### Known Risks & Blockers
**Source: Risk Register + Recent Issues**
```
known_risks: [
  {
    id: "R1",
    title: "Vendor integration timing",
    severity: "high",
    status: "active",
    impact: "Could delay launch by 2 weeks",
    owner: "Sarah",
    date_identified: "2026-06-15"
  },
  ...
]

blockers: [
  {
    title: "API redesign blocked on security review",
    severity: "critical",
    date_reported: "2026-06-20",
    owner: "James",
    resolution_blocker: "Waiting for security team"
  },
  ...
]
```

### Historical Decisions & Outcomes
**Source: Semantic Search + Past Meeting Notes**
```
recent_decisions: [
  {
    date: "2026-06-20",
    meeting: "Product Planning Q3",
    decision: "Launch v2 API by Sept 15",
    owner: "Sarah",
    reasoning: "Market timing critical",
    status: "on_track",
    last_update: "2026-07-01"
  },
  ...
]

decision_conflicts: [
  {
    current: "This new decision",
    previous: "Decision from 2026-06-15",
    conflict_type: "timeline|resource|priority",
    severity: "medium"
  }
]
```

### Indexed Historical Records
**Source: Gmail + Drive (Semantic Search)**
```
- Past emails mentioning this topic
- Previous meeting notes on related topics
- Strategic documents and plans
- Risk assessments and post-mortems
- Team capacity/utilization data
```

---

## Guidance Output Format

### During Each Meeting Turn (0-4 bullets max):

```
Guidance bullet structure:

1. RISK/CONFLICT
   "This conflicts with the Sept 15 launch decision — you'd need to slip 
   the timeline by 2 weeks, but product team has H2 roadmap committed."

2. QUESTION NOT YET ASKED
   "Success metric: how will we know this API redesign actually improved 
   adoption? (None mentioned yet in this meeting)"

3. DEPENDENCY/CAPABILITY CONSTRAINT
   "Engineering is at 85% utilization right now — adding this scope means 
   either cutting something or slipping timeline."

4. NEXT STEP / MUST-DO
   "Before deciding: need budget confirmation from CFO (not in this meeting). 
   Schedule that call before you commit to timeline."

5. DATA INSIGHT
   "Last quarter's similar project took 3x longer than estimated — watch 
   estimation bias here."
```

### What Makes Guidance "Really Good"

✅ **Specific** — "This conflicts with the Sept 15 launch" not "timeline issues?"
✅ **Grounded** — Cites actual data, actual decisions, actual constraints
✅ **Actionable** — Points to what needs to happen next
✅ **Owner-aware** — "Sarah owns the API work" not "someone should..."
✅ **Never repetitive** — Won't repeat guidance already given in THIS meeting
✅ **Concise** — 1-3 bullets, tightly written

❌ **Generic advice** — "Make sure everyone agrees" (not grounded)
❌ **Invented constraints** — Makes up risks not in the risk register
❌ **Vague questions** — "What about budget?" instead of specific $ amount
❌ **Repeating itself** — Says the same thing twice in one meeting

---

## Implementation Tasks

### Phase 1: Data Infrastructure
- [ ] Create business strategy queries (Postgres: goals, roadmap, budget)
- [ ] Create risk register schema + queries (Postgres)
- [ ] Create team structure schema + queries (from floormap parsing)
- [ ] Create decision history schema + queries (from indexed meeting notes)
- [ ] Wire up capacity queries from calendar utilization

### Phase 2: Enhanced Guidance System
- [ ] Implement `get_guidance_comprehensive()` (in meeting_copilot.py — DONE)
- [ ] Update system prompt to pull from all data layers
- [ ] Add conflict detection (new decision vs. past decisions)
- [ ] Add capacity checking (team utilization before committing)
- [ ] Add risk surfacing (known risks + new risks this decision introduces)

### Phase 3: API Integration
- [ ] Add optional `?comprehensive=true` mode to /api/meetings/copilot
- [ ] Update frontend to request comprehensive guidance when available
- [ ] Add data refresh triggers (when meetings end, summarize + update risk register)
- [ ] Add "Known conflicts" section to meeting prep brief

### Phase 4: Quality Checks
- [ ] Verify no repetition within a single meeting
- [ ] Verify every claim is grounded in one of the 5 data sources
- [ ] Verify actionable next steps (not just questions)
- [ ] Test with real meetings to refine prompt

---

## Example: Comprehensive Guidance in Action

**Meeting:** "Q3 Planning"
**Attendees:** Gandi, Sarah (eng), Maria (product)
**What's being discussed:** "We should rebuild the authentication system to improve UX"

### Standard Guidance (current):
```
- This will add 3-4 weeks to the roadmap
- Make sure to document the migration strategy
```

### Comprehensive Guidance (what we want):
```
- RISK: This auth redesign wasn't on the Q3 roadmap — product committed to H2 
  on mobile app instead. Need to explicitly reprioritize.
  
- CONFLICT: Sarah's team is 85% utilized already. Adding this scope means either 
  cutting "API redesign" (due Sept 15) or shipping mobile later. Which is it?
  
- UNKNOWN: Success metric for UX improvement isn't defined. Track: new user 
  onboarding time vs. current (baseline?).
```

---

## Next Steps for You

1. **Start with one data source** — e.g., implement Q3 goals + roadmap queries
2. **Test the prompt** with that one source
3. **Add data sources iteratively** — risks, then capacity, then historical decisions
4. **Refine based on real meetings** — what guidance actually helps you decide better?

The system is built and ready; it just needs the data plumbing connected. Each data source makes the guidance sharper and more actionable.
