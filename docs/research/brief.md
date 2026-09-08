# Research Brief

**Question:** I we would have an air quality monitor what parameters should it process and what kind of report or data should it generate, also based on that data an AI agent what of it could take advantage of to perform advices to people with respiratory illness


**Sub-questions (authoritative checklist — answer each; do NOT invent your own initial set). Items tagged _(emergent)_ were discovered mid-research; items tagged _(user guidance)_ are directives the user added — follow them, even if phrased as an instruction rather than a question:**
- How should the AI agent personalize advice by respiratory condition (asthma vs COPD vs allergic rhinitis) given individual triggers and symptom logs? _(emergent)_
- What predictive/forecasting capability (next-day AQI, pollen, symptom-risk) can the agent add on top of live monitor data, and which public forecast APIs feed it? _(emergent)_
- What safety/medical-liability guardrails must an AI health-advice agent respect (non-diagnostic framing, escalation to clinician, emergency thresholds)? _(emergent)_

**Sources allowed:** any
**Max cycles:** 30

**Questions allowed:** if the goal or scope is genuinely ambiguous in a way that would materially change your research direction, you MAY ask ONE high-leverage clarification question. Rules:
- Only ask about DECISIONS the user must make — never ask about facts you can discover by exploring (filesystem, tools, code, web search).
- Ask exactly ONE focused question per pause — multiple questions at once are bewildering and produce shallow answers.
- First-principle: state what you know, the specific decision, and the options. Include your recommended answer.
- Keep the bar high — proceed on a best-reasoned assumption for anything minor or self-resolvable.
Write {"question": ..., "why": ..., "recommended": ...} to questions.json and end the turn — the campaign pauses for the user, who answers via Nudge.

**Recursive exploration (emergent sub-questions):** As you research you will discover NEW high-value questions not in the initial list. Each cycle, in addition to your finding, you MAY propose follow-up sub-questions by writing `emergent_questions.json` in this dir as a JSON array: `[{"text": "...", "priority": 0.0-1.0}, ...]` where priority is how valuable / relevant the lead is to the main question. The system ranks them, admits the top few per round (a budget), de-duplicates against existing questions, and appends the winners to the checklist above (tagged _(emergent)_) for you to investigate in later cycles — so you can follow leads BEYOND the initial questions. Do NOT re-propose questions already on the checklist, and stop proposing once the main question is sufficiently answered (your Definition of Done / verification).

Each cycle, also read `guidance.txt` in this dir if present and follow any directive there (e.g. a FINALIZE MODE instruction to stop exploring and synthesize your final answer).

**Ending the run:** if you decide the research is finished (goal met or no productive work remains), FIRST write `worker_done.json` in this dir as `{"reason": "<one line>"}` — this is the durable signal that you ended the run on purpose if the source stop record is unavailable — and only THEN call `autonudge_stop`.

Adapt direction each cycle from prior findings; pursue the highest-value open lead toward the question.

**Parallel execution:** You have 5 parallel worker slots. Each cycle, use `spawn_run` with a `tasks` array to investigate up to 5 open sub-questions simultaneously (one task per sub-question). Each task should be a self-contained research instruction for that sub-question. Wait for all completion events, then synthesize results into your cycle finding. If fewer than 5 sub-questions remain open, spawn only as many as needed.