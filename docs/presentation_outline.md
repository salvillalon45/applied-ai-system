# Presentation outline: PawPal+ Planning Agent (target: 6.5 minutes)

One-sentence pitch: **"An AI agent that turns a plain-English request into a pet-care schedule, checks its own work with code instead of trusting itself, and never saves anything until I approve."**

## Before you start (5 minutes, do this the day before and again 10 minutes before)

- [ ] `source .venv/bin/activate`, then `python -m pytest -q` (should say 153 passed)
- [ ] `python -m eval.run_eval` (should say 13/13 pass; no API key needed)
- [ ] One live request to confirm your Gemini key and quota work (command below). If you get a rate-limit message, wait a minute.
- [ ] Open these tabs/windows ahead of time: the terminal (large font), `diagrams/architecture.mmd` rendered on GitHub, `eval/results_live.md`
- [ ] Wait about 60 seconds between live runs. The free tier rate-limits you.
- [ ] Start the demo with no saved pets (the CLI starts empty every time, so just run `python main.py`)

## Timeline

| Time | Segment | Show |
|---|---|---|
| 0:00 – 0:45 | The problem and where this started | Slide or README top |
| 0:45 – 1:45 | How it works | Architecture diagram |
| 1:45 – 4:30 | Live demo (3 short parts) | Terminal |
| 4:30 – 5:30 | How I know it works | Eval table |
| 5:30 – 6:15 | What surprised me and what's still weak | Model card |
| 6:15 – 6:30 | Reflection and close | One slide |

## 1. The problem (0:00 – 0:45)

Say:
- "PawPal+ was my Module 2 project. You typed in pets and tasks by hand, and a scheduler built a daily plan."
- "The problem: typing every field is slow, and real requests are messy. 'Walk Rex at 8, bath at 8:15 tomorrow.'"
- "For the final I kept the scheduler exactly as it was and added an agent around it that reads the request and does the data entry."

## 2. How it works (0:45 – 1:45)

Show the diagram. Walk it left to right in one breath:
- **Plan:** the LLM (Gemini) reads the request and picks tool calls.
- **Act:** my code runs those tools on a *draft copy* of my data. There are only 6 tools, and **there is no delete tool**.
- **Check:** a verifier, plain code with no AI in it, looks for conflicts, overflow, missing tasks. Its errors go back to the model so it can fix them.
- **Approve:** I see the plan. Nothing is saved until I type `y`.

Key line: **"The model only decides what to do next. Everything that makes it safe is ordinary code I can test."**

## 3. Live demo (1:45 – 4:30)

### Demo A: it finds and fixes its own mistake (about 1 minute)

Type at the `You>` prompt (start the CLI with `python3 main.py --morning 90 --evening 60`):

```
Rex the dog needs a 30 minute walk at 08:00 and a 30 minute bath at 08:15 in the morning.
```

Point out:
- The walk and bath overlap by 15 minutes. The verifier caught it and the model moved the bath to 08:30.
- Look at "What approving will save": the durations are still 30 and 30. It did not shorten anything.
- Type `y`.

### Demo B: it refuses to force an impossible request (about 1 minute)

Restart with `python3 main.py --morning 30 --afternoon 30 --evening 30`, then type:

```
Rex needs a 60 minute walk in the morning.
```

Point out:
- Every block has 30 free minutes, so a 60-minute walk fits nowhere.
- The agent will not shorten the walk or change my availability. It stops and says what would have to change.
- Nothing was saved.

### Demo C: seeing where I stand (about 30 seconds)

After an approval, type `/plan`. Say: "`/plan` shows minutes used per time block with no model call at all, so it's instant."

### If the live demo fails (rate limit, wifi, API error)

Do not troubleshoot on stage. Say "The free tier rate-limits me, so here is the same run offline," then run:

```
python demo_trace.py
```

It prints every loop step of the overlapping walk and bath with a scripted model and needs no network. Also have the README's sample interactions open as a second fallback.

## If you pre-record the demo (recommended given the API latency)

- Each request makes 4 to 8 model calls one after another, so total time is roughly `calls x API latency`. At 1 second per call that is a few seconds. At 10 seconds per call it is over a minute.
- Check the API speed before you hit record: run `tail -f logs/agent.log` in a second terminal and look at the `took=` value on each `llm call` line. Record when most calls take under about 3 seconds.
- Record each demo as its own take, so a slow one can be retried without redoing the others.
- In the video editor, you can cut or speed up the waiting time. Say "sped up" on screen if you do, so it is honest.
- Keep `python demo_trace.py` as the offline backup. It has no network wait at all.

## 4. How I know it works (4:30 – 5:30)

Show the eval table.

- **153 automated tests**, about 1 second, no network.
- **A reliability harness with 13 cases**: 13/13 pass offline, **9/9 pass live** against Gemini.
- Each case is judged on the same rules: real data untouched until approved, no pet disappears, a READY plan passed the verifier, stated durations survive, the loop stays under its cap.
- "I tested the harness too, by feeding it deliberately bad runs, to make sure it can fail."

Best story to tell (pick one):
- **Live runs found bugs the mock never could.** A valid plan was reported as failed because the model fixed the conflict on exactly the last allowed call. Another "valid" plan was empty because the tasks were due tomorrow and the plan was for today. Both only showed up against the real model.

## 5. What surprised me and what's still weak (5:30 – 6:15)

Pick two or three. These are real and are in the model card:

- **It guesses instead of asking.** I typed a message that said "in the morning" and also "in the evening", and the model silently picked one. I told it in the prompt to ask, and it still didn't. For a pet's species, it filled in "dog" when I never said it.
- **When a request is impossible, it flails.** It kept editing my other tasks to try to make room, even though the prompt forbids it.
- **My fix is the approval screen, not the prompt.** "What approving will save" lists every task with priority, frequency and preferred time, so a wrong guess is visible before it's saved. Failed attempts throw away their draft.
- **I misread my own test once.** I thought the verifier had a gap. The scheduler had actually moved the task to another block. Fixing the test then exposed a real gap, which I closed.

Key line: **"You can't prompt your way to a guarantee. A visible approval step and checks in code are what protect the owner."**

## 6. Reflection and close (6:15 – 6:30)

- Use your own words from the README Reflection. Aim for one or two sentences.
- Suggested ending: "Next I'd connect this to the Streamlit UI and add smarter checks, like warning when a task has no stated duration."

## Likely questions, with honest answers

| Question | Answer |
|---|---|
| Why Gemini and not Claude? | It's the key I already had from class, so the project is free to run. A Claude client exists and switching is one environment variable, but my results are all from Gemini. |
| Why not just prompt the model to be careful? | Prompts are requests, not guarantees. I saw the model break prompt rules (it shortened a duration, it edited stated details). Code checks give the same answer every time. |
| How is the verifier different from the model checking itself? | It's plain code with no LLM, so it can't make the same mistake twice, and it's easy to test. |
| What stops it from deleting my data? | There is no delete tool, so it can't. And all edits happen on a copy that's discarded unless I approve. |
| What about prompt injection? | The tool list is fixed and every argument is schema-checked, so injected text can't add a tool. Text in names is treated as data. The prompt rule is a request; the real protection is the fixed tools plus the approval step. I tested a hostile task name and nothing was obeyed. |
| What does the mock model test then? | The loop machinery: guardrails, tool whitelist, verifier feedback, caps. It can't test the model's judgment, which is why I added a live mode. |
| What would you do with more time? | Streamlit front end, smarter verifier checks, more eval cases, and run each live case several times since the model varies run to run. |
| What did the AI get wrong while you built this? | See the model card: a test that made a real API call, a wrong diagnosis of a verifier gap, and model names that didn't exist. |

## Slide list (if you need slides, 6 is enough)

1. Title + one-sentence pitch
2. Base project (PawPal+) and what's new
3. Architecture diagram
4. Eval results table
5. Surprises and limits (3 bullets)
6. Reflection / next steps

## Don't claim

- That it's reliable in general. 13 cases and one model is a small test.
- That the prompt prevents bad behavior. It reduces it, and you have the examples above.
- Anything about Google's data policy. You didn't check it, and the model card says so.
