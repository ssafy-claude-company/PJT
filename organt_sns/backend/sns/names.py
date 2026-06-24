"""봇 이름 — 직군(role)과 분리된 '고유 정체성'.

  봇은 한 사람처럼 고유한 이름을 갖고, 직군(백엔드·QA…)은 그가 맡은 일이다. 두뇌(디스코드) 봇은
  이름이 비어 있어 UI가 직군을 정체성처럼 써 왔다(중복 직군이면 구분 불가) — 여기서 안정적인
  고유 이름을 배정한다. bot_id 기준이라 재시작해도 같은 봇은 같은 이름.
"""
# 한국 느낌의 3글자 풀네임(성+이름). 직군과 무관한 고유 정체성. 성을 다양하게 섞어
# 중복을 줄였다. 풀이 모자라면 assign_name이 숫자 접미로 유일성을 보장한다.
NAME_POOL = [
    "김도윤", "이서준", "박지호", "최시우", "정하준", "강주원", "조예준", "윤지후",
    "장건우", "임도현", "한서진", "오은우", "신현우", "권민재", "황준영", "안재윤",
    "송지안", "류시현", "홍준호", "배승우", "서도훈", "남세준", "문하람", "양지원",
    "고은호", "백승현", "허재희", "유찬영", "노아진", "심우주", "구다온", "진서우",
    "곽지율", "성하린", "차은채", "주시안", "우민호", "라하율", "표도경", "변예성",
]


def assign_name(bot_id, taken):
    """taken에 없는 고유 이름을 bot_id 기준으로 안정 배정. taken을 갱신한다."""
    if not NAME_POOL:
        return ""
    start = int(bot_id) % len(NAME_POOL)
    for i in range(len(NAME_POOL)):
        n = NAME_POOL[(start + i) % len(NAME_POOL)]
        if n not in taken:
            taken.add(n)
            return n
    base, k = NAME_POOL[start], 2          # 풀 소진 시 숫자 접미
    while f"{base}{k}" in taken:
        k += 1
    n = f"{base}{k}"
    taken.add(n)
    return n
