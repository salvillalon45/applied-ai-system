| Rubric asks for | Where it is in the diagram |
| ----------- | ----------- |
| Main components      | Agent (orchestrator loop, Claude API, tool executor), PawPal+ core, Verifier, Reliability harness       |
| Data flow   | User → CLI → input guardrails → agent loop → tools → draft data → Scheduler → Verifier → review → output        |
| Humans / testing checking AI | The Human approval node (y / n / revise) and the Eval harness |

*The colors mark the roles. Yellow is the human, blue is the LLM, red is the guardrails and verifier, and green is testing.*