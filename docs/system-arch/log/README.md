# Work log

Append-only, one file per month (`YYYY-MM.md`), newest entry at the bottom. Written by
whoever (or whatever) did the work, in the same session.

Format per entry:

```markdown
## YYYY-MM-DD — short title

**Changed:** what, with file paths.
**Verified:** how, distinguishing "tested and observed X" from "should work".
**Learned:** anything that would cost the next person an hour to rediscover.
**Open:** what is now open or blocked, and on what.
```

Rules: never edit or delete a past entry — corrections are new entries that link the
date they correct. Measured behaviour that must stay true belongs in a module
docstring or in `docs/`; the log only records when and how it was learned.
