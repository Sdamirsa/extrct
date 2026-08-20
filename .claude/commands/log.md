---
description: Append a dated entry to the work log in docs/log/
---

Append an entry to the current month's file in `docs/log/` (create it if the month has no file
yet, following the format in `docs/log/README.md`).

Base the entry on what actually happened in this session — read `git log` and `git status` if
you need to ground it. Cover:

- **What changed**, with file paths.
- **What was verified**, and how. Distinguish "tested and observed X" from "should work".
- **What was learned** that would otherwise cost someone an hour to rediscover — a gotcha, a
  wrong doc, a non-obvious behaviour.
- **What is now open or blocked**, and on what.

Rules:

- **Append only.** Never edit or delete a past entry. If something recorded earlier turned out
  wrong, write a new entry saying so and link the date.
- Keep it factual. No progress-report tone, no adjectives for their own sake.
- If a decision was made that changes the architecture, do **not** write to
  `decision-log.jsonl` yourself — draft the entry, show it, and ask. That log is a human call.
- Then check whether `TODO.md` needs updating to match reality, and update it if so.
