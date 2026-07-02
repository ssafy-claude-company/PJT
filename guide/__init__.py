"""[Guide] Rule의 구현층 — 브레인(SYS)이 각 서비스와 대화하는 전송기.
DiscordGuide(→Discord) · HttpSnsGuide(→murmur SNS API). SYS의 Guide 계약(post·read_thread…)을 구현.
의존: system(SYS)만 — 봇(Organt)도 매체 서비스도 모름(순수 전송). murmur/Discord는 API로 대화."""
