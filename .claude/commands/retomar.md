---
description: Restore project state at the start of a session, cheaply
---

Restore context for this project. Keep it tight — this should cost around 1.5k tokens, not 30k.

Do exactly this, and nothing more:

1. Read `docs/STATE.md`.
2. Read `tasks.json` and identify the first task with `done: false`.
3. Run `git log --oneline -15`.
4. Read `docs/DECISIONS.md` **only if** the upcoming task touches a settled question.

Then report, in Spanish and in plain language:
- Where the project stands, in two or three sentences.
- The next task, by id and description.
- Anything listed as blocked or as an open question.

Do **not** read source files, data files or the full plan unless the next task requires it. Do not
explore the directory tree. State lives in the four files above; trust them.
