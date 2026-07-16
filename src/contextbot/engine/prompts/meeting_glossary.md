The author has approved the note. Before this conversation ends, write down what it taught you about
how the speech recogniser mishears this author's world — so the next meeting does not have to ask.

Look back over this whole conversation: the transcript, the note you drafted, the terms you flagged,
and everything the author said in reply. Report **only the terms this review actually settled.**

Output a Markdown table and nothing else — no preamble, no explanation, no code fence:

| 전사 표현 | 정확한 표현 | 유형 | 설명 |
|---|---|---|---|
| 팀웹 | T-map | 제품명 | SKT 내비게이션 서비스 |

One row per term. The columns are:

- **전사 표현** — what the recogniser actually wrote in the transcript, exactly as it appears there.
  This is the lookup key: it must be the *wrong* spelling, because that is what a future transcript
  will contain. If the same word was misheard several ways, list them comma-separated.
- **정확한 표현** — the word that was really said, as the author confirmed it.
- **유형** — exactly one of: 인명, 회사명, 제품명, 기능명, 기술용어, 팀명, 행사명, 단어오인식, 기타.
- **설명** — a short note on who or what it is. May be left empty.

**Include a row only when both of these hold:**

1. It is a **mis-transcription** (the recogniser wrote the wrong word) or a **new domain term** that
   the glossary does not already carry, **and**
2. the author **confirmed** it — they said the corrected form, or they accepted the correction you
   proposed.

**Leave a row out when any of these hold**, and these are where this goes wrong:

- **The author corrected your guess.** Then your guess was wrong, and the row is the author's
  version — never the one you proposed. Getting this backwards writes a wrong correction into every
  future meeting note, silently, and nobody will ever look.
- **You are inferring rather than reporting.** If nothing in this conversation confirmed a term, it
  does not go in. A plausible entry is worse than a missing one: a missing entry gets asked about
  next time, a wrong entry never does.
- **It is a content correction, not a word.** A changed date, a corrected purpose, a revised
  opinion, a reworded sentence — those fixed *this* note and teach the recogniser nothing.
- **The glossary already covers the 전사 표현.** You read the glossary at the start of this
  conversation; a term already in that table is settled and must not be repeated.
- **The recogniser heard it correctly** and you simply did not recognise the term yourself.

If this review settled nothing that meets the bar, reply with exactly `NONE` and nothing else. That
is a perfectly normal outcome — a clean transcript teaches nothing, and an empty table is the honest
report. **Do not pad the table to look useful.**
