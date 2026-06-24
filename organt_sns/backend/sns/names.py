"""봇 이름 — 직군(role)과 분리된 '고유 정체성'.

  봇은 한 사람처럼 고유한 이름을 갖고, 직군(백엔드·QA…)은 그가 맡은 일이다. 두뇌(디스코드) 봇은
  이름이 비어 있어 UI가 직군을 정체성처럼 써 왔다(중복 직군이면 구분 불가) — 여기서 안정적인
  고유 이름을 배정한다. bot_id 기준이라 재시작해도 같은 봇은 같은 이름.
"""
NAME_POOL = [
    "지호", "서연", "민준", "하준", "도윤", "시우", "주원", "예준", "수아", "지우",
    "하윤", "서준", "윤서", "은우", "유나", "정우", "다은", "지안", "준서", "하은",
    "채원", "지율", "소율", "건우", "나윤", "현우", "서아", "민서", "예나", "우진",
    "시아", "도현", "라온", "하랑", "유진", "태오", "리아", "단우", "세아", "온유",
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
