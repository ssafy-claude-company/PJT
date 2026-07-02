"""[Organt 런타임] Organt(외부 주체)의 구현 — LLM 실행(organt.py, claude CLI·세션 resume) + 빌더(builder.py).
Core(system)의 Rule(도구계약 guide_tools + 강제 permissions)을 *소비*할 뿐, Core는 이 층을 모른다(단방향).
매체(organt_discord·organt_sns)와 대칭인 또 하나의 외부 구현층. 인격은 organt/CLAUDE.md(ROOT 기준)."""
