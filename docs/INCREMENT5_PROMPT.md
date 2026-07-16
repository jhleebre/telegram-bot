# Increment 5 starting prompt

Paste the block below into a fresh session. It is written for a cold start: it assumes no memory of
increments 1-4, and points at the docs rather than repeating them.

---

telegram-bot 프로젝트의 Phase 2 증분 5를 개발해줘. **마지막 증분이고, 제일 복잡하다.**

먼저 docs/PHASE2.md를 읽어. 헤더에 "Picking this up in a fresh session?" 블록이 있고 읽는 순서가
적혀 있으니 그대로 따라가면 된다 (마지막이 "Next up: increment 5"). 증분 1-4는 완료돼서 main에
병합돼 있고, 문서에 "resolved / decided / shipped"로 적힌 건 다시 논의하지 말고 그대로 둬라.

## 착수 전에 반드시 읽을 것 (둘 다 설계를 바꾼다)

1. **"Next up: increment 5 → The STT dependency"** — 실측표다. 요약하면 **이 머신에서는 절대
   실패하지 않는 함정**이 있다: Whisper 모델(1.5GB)은 패키지 의존성이 아니라 HF repo id라서
   `mlx_whisper`가 **첫 사용 때 조용히 다운로드**하는데, 소유자가 meeting-transcriber를 쓰기 때문에
   이미 캐시에 있다. 새 머신에서는 회의록 잡 한가운데서 수 분간 멈추고, 오프라인이면 그냥 실패한다.
   증분 1의 Dock PATH 버그와 **같은 모양**(개발 환경에서는 재현 불가)이다.
2. **"Next up: increment 5 → First: delete the `#검토` scaffolding"** — 증분 5는 리뷰 루프의
   **producer를 교체**하는 작업이다. `#검토`는 발판이었고 증분 5가 지운다. **단 마지막 단계로**
   지워라(이유는 문서에 있다 — 먼저 지우면 증분 4가 앱에서 도달 불가능해진다). 삭제/보존 경계표가
   심볼 단위로 이미 적혀 있으니 새로 조사하지 말고 그대로 쓰면 된다.

## 이번 작업: 증분 5 — 오디오 → 회의록

`handlers/audio_handler.py`, STT, 용어집, 그리고 증분 4의 리뷰 루프에 물리기. 파이프라인 개요는
"A. Audio → meeting note" 절에 있다.

`~/Projects/meeting-transcriber/`가 레퍼런스 구현이다. **읽고, 필요한 걸 가져다 써라:**

- `.claude/skills/meeting/scripts/transcribe.py` — 깔끔한 `transcribe(...)` 함수. **함수를 이식해라.
  그 스크립트를 shell out 하지 마라.**
- `.claude/skills/meeting/SKILL.md` — 회의록 템플릿과 절차. 프롬프트가 실어날라야 할 내용이다.
- `.claude/skills/meeting/glossary.md` — 공유 용어집 (약 20KB).

**단 repo의 독립성과 완결성이 우선이다.** 런타임에 `~/Projects/meeting-transcriber/`에 의존하면
안 된다 — 코드는 이식해 넣어라. 그리고 **Whisper 모델이 없으면 이 프로젝트가 직접 받아서 설치하는
것까지 포함해야 한다.** 지금은 아무도 그걸 "설치"하지 않는다 — 첫 전사의 부작용으로 받아질 뿐이다.
그건 잡 한가운데서 일어나면 안 된다. 문서가 제안하는 방향: `claude-engine` 스타일 헬스 프로브가
**DEGRADED — 모델 미다운로드**를 보고하게 하고(로컬·무비용), 명시적인 워밍업/다운로드 경로를 둬라.
패키지(`mlx-whisper`)는 `requirements.txt`에 넣어라. `ffmpeg`는 `/opt/homebrew/bin`에 있으니
**PATH에 있다고 가정하지 말고** `engine/claude_cli.resolve_executable` 방식으로 resolve해라.

착수 순서:
1. **열린 결정들을 먼저 정하고 문서에 기록한다** — "The decisions increment 5 has to make"에 네 개가
   있다. 그중 두 개가 특히 무겁다:
   - **한 번에 하나 제약.** 메모는 재전송이 공짜라 괜찮았지만 **오디오는 재업로드 + Whisper 재실행**
     이라 아프다. "둘을 허용"은 답이 아니다(맨 `확인`을 어느 초안에 귀속시킬지 모른다) — **큐**냐
     **주소지정**이냐를 정해라.
   - **수락 시점의 부작용.** 지금 수락은 노트 쓰기 하나뿐인데, 증분 5의 "성공"은 **용어집 append +
     오디오 삭제**까지다. 그게 **폴러 백그라운드 태스크**에서, 다운로드한 핸들러로부터 몇 턴 떨어진
     곳에서 일어난다. `review.work_dir`이 유력한 후보다(리뷰와 수명이 정확히 같다).
   - 나머지: 용어집 git 커밋(열린 결정 2), 용어집 공유/복사/심링크(열린 결정 5).
2. STT를 먼저 독립적으로 세운다 (모델 존재 확인 + 헬스 프로브 포함). 리뷰에 물리기 전에.
3. 전사본 → 초안. **producer 계약**이 문서에 있다: `제목:` 첫 줄 + `## 확인 요청` 헤딩 아래 질문 —
   그래야 `parse_draft`가 그대로 동작하고 `review_revise.md`는 손댈 필요가 없다.
4. 리뷰 루프에 물린다 (`handle_review_request`가 producer 패턴이다 — 세션 id 고정 →
   `store.create()` **호출 전** → `work_dir`을 cwd/add_dir로 → `write_draft` → `store.update`).
5. **마지막에** `#검토` 발판을 지운다 (경계표대로).

## 작업 규칙 (docs/PHASE2.md "Delivery approach")

- 테스트는 계속 초록으로: QT_QPA_PLATFORM=offscreen .venv/bin/pytest (현재 **474개 통과**).
  **실제 모델 호출·모델 다운로드·네트워크 없이** 돌아야 한다 (엔진·봇·STT 전부 목킹).
- 부작용은 전부 마지막 LLM 호출 이후에. 사용량 한도는 그 전에 DeferMessage — **단 리뷰 턴은 절대
  DeferMessage를 던지지 않는다**(증분 4에서 정한 규칙, 이유는 문서에). 턴 1만 해당된다.
- **초안을 절대 유실시키지 마라.** 리뷰는 빈손으로 끝나지 않는다 — 증분 4의 원칙이고 오디오에서
  더 중요하다(초안 하나가 Whisper 몇 분 + LLM 패스다).
- 엔진은 hermetic 플래그로 돌아서 **CLAUDE.md가 `claude -p`에 안 먹힌다**. 회의록 프롬프트가
  의존하는 지침은 전부 템플릿 안에 넣어라.
- 마크다운 범위는 물결표(~)가 아니라 하이픈(-). **회의록 프롬프트도 이 규칙을 실어야 하고,
  `test_prompts.py`가 템플릿별로 검증한다**(파라미터에 새 템플릿을 추가해라).
- 다 되면 실제 앱에서 내가 검증할 수 있게 하고, 내 확인을 받기 전에 마무리하지 마라.
- 코드/주석/문서/커밋 메시지는 영어, 나와의 대화는 한국어.
- 브랜치는 증분 1-4처럼 `phase2-5-audio`로 따고, 확인 후 main에 fast-forward 병합.

## 참고 (증분 1-4에서 확인된 환경 특성)

- 중첩 Claude Code 세션에서도 `claude -p`는 정상 동작한다. 로그인이 만료되면 claude-engine 헬스
  프로브가 DEGRADED로 잡아준다.
- git push는 remote가 HTTPS라 자격증명이 없다. `git push git@github.com:jhleebre/telegram-bot.git main`
  형태로 해라.
- 실기 테스트용 파일은 ~/Desktop, ~/Downloads 대신 프로젝트 폴더나 /tmp 등 접근 가능한 경로로.
- **사용량 한도를 조심해라.** Pro 플랜이고 5시간 창을 내 작업과 공유한다. 증분 4에서 실측 프로빙만으로
  한도를 소진한 적이 있다. 실기 검증은 아껴서 해라.
- 증분 1-4가 반복해서 겪은 교훈:
  - **소비자 쪽 소스를 읽어라** (마크노트의 .metadata.json 원장을 놓쳐서 .assets 방식을 통째로 갈아엎었다).
  - **모델은 못 봤다고 인정하느니 그럴듯하게 답한다고 가정해라** (.docx→이웃 파일, .heic→파일 헤더,
    둘 다 is_error: false). **단 증분 4에서는 이 함정이 안 나왔다** — `--resume` 실패는 시끄럽게
    터진다. 매번 실측해서 확인해라.
  - **증분 4의 버그 네 개는 전부 조용한 실패였다** (본문 훼손, 전송 불가한 질문, 영구히 막힌 리뷰,
    답장으로 오해된 "얼마나 걸려?"). 아무것도 raise하지 않고 테스트는 초록이었다. 리뷰 루프의 실패는
    구조상 조용하다 — 유일한 목격자가 DM을 읽는 사람이기 때문이다. **오디오는 여기에 "몇 분씩 걸리는
    잡"을 더한다.**
