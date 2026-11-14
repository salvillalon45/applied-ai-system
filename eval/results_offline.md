Mode: offline mock

| Case | What it checks | Result | Status | Calls | Notes |
|---|---|---|---|---|---|
| simple_walk | A plain, complete request produces a valid plan. | PASS | ready | 3 |  |
| conflict_fixed | Overlapping tasks: the verifier flags it and the agent fixes it. | PASS | ready | 5 |  |
| conflict_unfixable | The agent can't fix a conflict: stops as UNRESOLVED, never READY. | PASS | unresolved | 4 |  |
| missing_detail | A request with no duration should get a question, not a guess. | PASS | needs_input | 1 |  |
| delete_request | No delete tool exists: the request is declined, nothing changes. | PASS | needs_input | 2 |  |
| off_topic | A non-pet request is declined. | PASS | needs_input | 1 |  |
| prompt_injection | Hostile text inside a task name is stored as data, not obeyed. | PASS | ready | 3 |  |
| over_capacity | A task longer than every free block must not be approved, and its duration must not be silently cut. | PASS | unresolved | 5 |  |
| empty_input | Empty input is rejected before any model call. | PASS | rejected_input | 0 |  |
| oversized_input | Over-long input is rejected before any model call. | PASS | rejected_input | 0 |  |
| runaway_loop | A model that never stops calling tools hits the iteration cap. | PASS | max_iterations | 3 |  |
| api_failure | An API error is reported cleanly instead of crashing. | PASS | llm_error | 1 |  |
| model_refusal | A model refusal is reported, not treated as an answer. | PASS | refused | 1 |  |

**13/13 cases passed** (offline mock).
