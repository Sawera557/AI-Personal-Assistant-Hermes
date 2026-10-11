# Multi-agents: Stage 1 test results (2026-10-08)

Plan and test definitions: [MULTI_AGENTS_PLAN.md](../hermes/MULTI_AGENTS_PLAN.md) §9.

Raw run files, each with the agent's full replies and every automated check:

- [Nemotron, run 1](MULTI_AGENTS_TEST_RESULTS_nemotron-run1.md)
- [Nemotron, run 2](MULTI_AGENTS_TEST_RESULTS_nvidia-nemotron-3-5-lightning-30b-a3b.md)
- [Claude Haiku 4.5, run 2](MULTI_AGENTS_TEST_RESULTS_claude-haiku-4-5-20251001.md); run 1 results are summarised below

Runner: `scripts/_test_multi_agents_owner.py [--model <id>] [O1 …]`.

## 1. Platform tests: 24/24 pass

`tests/test_multi_agents.py` runs offline with stubbed providers. It covers:

- the tool whitelist as the capability boundary
- forced off-list tools being refused
- agent-only tools
- mode tools, autonomy and apps reaching `build_tools`
- the draft and read-only write gates
- the prompt block
- validation of every built-in preset
- the `reviews_data` business scope
- built-in delete and reset rules

Full suite: **2,390 passed**. The 2 failing tests (`test_command_center_reference_visual`, `test_workflow_schematic_visual`) were already failing before this change and don't touch Multi-agents.

## 2. Owner-value tests: real turns, real data, checked against independent ground truth

Every automated failure below was then checked by hand against the raw review CSVs. Some flags were checker artefacts, such as thin-space thousands separators or a line break inside a quote. Those are marked as such, so each verdict reflects the agent's actual output.

| Test | Claude Haiku 4.5 | Nemotron 3.5 Lightning (current platform model) |
|---|---|---|
| **O1** Rank Minnesota competitors | ✅ All 8 venues, every Google rating matches the Steam & Signal workbook (2 runs) | ✅ Same, exact (3 runs) |
| **O2** Complaint themes + quotes | ✅ Theme counts exact; quotes verbatim from the CSVs | Run 1 ❌ wrote a literal `<tool_call>` as text, so there was no answer · Run 2 ✅ counts exact |
| **O3** Launch promises | ✅ No unsourced number. "77 venues" is correct; "45 minutes" comes from a real review | Run 1 ❌ invented a denominator ("524 of **883**": 883 is a star-histogram count, mislabelled), misquoted a share ("Wi Spa 50%", actually 42.5%), and added operating details that aren't in any source · Run 2 ❌ "**71** venues" (actually 77) |
| **O4** "Reviews from Tokyo?" (trap) | ✅ No data, no invented count | ✅ Same (2 runs) |
| **O9** MN/Minneapolis licences | ⚠️ Run 1 answered from the knowledge base's NH compliance matrix with **no source URLs**. The prompt was tightened afterwards, but re-testing was blocked (see §3) | ❌ No web tools on the NVIDIA path; run 1 also hit a provider timeout |
| **O10** GLOBALG.A.P. research | ⚠️ Run 1 cited only 1 URL. Prompt tightened, re-test blocked (§3) | Not run (no web tools) |
| **O11** 14-day farm weather | ✅ Table matches a direct Open-Meteo call; rain bands used correctly | ✅ Values exact. Before the fix it called 3.9 mm "heavy"; fixed with tool-side rain bands |

Speed and cost per turn:

| | Claude Haiku 4.5 | Nemotron 3.5 Lightning |
|---|---|---|
| Time | 20–110 s | 70–410 s |
| Cost | ≈ $0.006–0.05 | ≈ $0.003–0.007 |

## 3. Blockers found (outside this feature)

1. **The Anthropic API credit is exhausted.** A direct call on 2026-10-08 returned "Your credit balance is too low to access the Anthropic API". The OpenAI and Gemini accounts also report out-of-credit. Hermes' credit fallback then silently runs turns on whatever model still answers; here that was `gemini-3.5-flash-lite`, which has no web tools. The runner now records the model that **actually** answered each test.
2. **Web tools only exist on Claude.** `hermes/agent.py` `_to_openai_tools` drops Anthropic's server-side `web_search` / `web_fetch` for every OpenAI-protocol model (NVIDIA, OpenAI, Gemini). With the platform model on Nemotron, the Research Agent and the property scouts have **no web access**.
3. **Nemotron reliability.** It intermittently emits a literal `<tool_call>` instead of a structured call, makes arithmetic and labelling errors in long answers (O3), and hit one provider read-timeout.

## 4. Fixes made because of these tests

| Fix | What it does |
|---|---|
| `weather_forecast` returns `rain_intensity` bands | Stops misjudging rain amounts |
| Farm Operations prompt | Must use the tool's rain band |
| Shared agent rules | Computed numbers must show their inputs (e.g. `77 (= 20 + 18 + 39)`); no business details that aren't in a source |
| Research Agent prompt | Knowledge-base hits are leads, not sources; verify on the web; full URL for every claim plus a Sources list |
| Test runner | Stricter number-traceability and quote checks; records the model per test; a provider failure becomes a FAIL instead of a crash |

## 5. Verdict

- **Ready on any model:** Nour Hammam Market Analyst for scorecards and the Tokyo-style "do we have data" questions (O1, O4), and Farm Operations weather (O11). Their numbers match ground truth exactly.
- **Needs Claude:** synthesis answers with computed figures (O3; Nemotron made a real error in both runs) and everything that needs the web: Research, House Scout, Commercial Leasing, Markets & Buyers.
- **Owner action:** top up Anthropic credit before using the web-based agents. Then re-run `--model claude-haiku-4-5-20251001 O9 O10` to confirm the tightened Research prompt.
