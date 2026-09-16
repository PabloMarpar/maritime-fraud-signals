---
description: Close the session cleanly so the next one can resume for free
---

Wrap up the session. Run this **before** context runs low, not after.

1. Update `docs/STATE.md`: move finished items into Done, rewrite In progress, refresh Next up, and
   record anything now blocked. Keep each list short — this file is read at the start of every
   session and its length is a running cost.
2. Update `tasks.json`: set `done: true` on everything completed. Add new tasks if the work revealed
   any.
3. Append to `docs/DECISIONS.md` any decision taken this session, one line each, with the reason.
   Only real decisions — not activity.
4. Commit with a descriptive message covering what changed and why.

Then tell the author, in Spanish and in plain language, what was accomplished and what comes next.

The test of a good close: someone opening a fresh session tomorrow with only `/retomar` should be
able to continue without asking a single question.
