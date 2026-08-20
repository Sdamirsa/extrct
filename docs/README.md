# docs

Three audiences, three folders:

| Folder | For | Holds |
|---|---|---|
| [`for-user/`](for-user/) | people **using** the library or the stack | [setup guides](for-user/setup/) — install and run the library or the stack |
| [`system-arch/`](system-arch/) | how the system is built, and why | [`extraction-stack/`](system-arch/extraction-stack/) — the design of record (why things are the way they are, and what was measured to find out) · [`log/`](system-arch/log/) — the append-only work log (what happened when) · [`reference/`](system-arch/reference/) — external-tool reference notes |
| `for-amir/` | the maintainer's personal notes | gitignored — local-only, absent from clones |

One authority per fact: guides link to the document that owns a detail rather than
restating it. Open work lives in [TODO.md](../TODO.md), not here.
