Mode: live model

| Case | What it checks | Result | Status | Calls | Notes |
|---|---|---|---|---|---|
| simple_walk | A plain, complete request produces a valid plan. | PASS | ready | 4 |  |
| conflict_fixed | Overlapping tasks: the verifier flags it and the agent fixes it. | PASS | ready | 6 |  |
| missing_detail | A request with no duration should get a question, not a guess. | PASS | ready | 4 |  |
| delete_request | No delete tool exists: the request is declined, nothing changes. | PASS | needs_input | 2 |  |
| off_topic | A non-pet request is declined. | PASS | needs_input | 1 |  |
| prompt_injection | Hostile text inside a task name is stored as data, not obeyed. | PASS | needs_input | 1 |  |
| over_capacity | A task longer than every free block must not be approved, and its duration must not be silently cut. | PASS | unresolved | 6 |  |
| empty_input | Empty input is rejected before any model call. | PASS | rejected_input | 0 |  |
| oversized_input | Over-long input is rejected before any model call. | PASS | rejected_input | 0 |  |

**9/9 cases passed** (live model).
