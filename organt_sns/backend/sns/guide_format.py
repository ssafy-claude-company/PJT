"""guide_format — SYS/Rule가 만든 디스코드 마크업을 SNS-네이티브 텍스트로 변환.

  SYS·Rule은 매체-중립이지만, 상태블록·멘션·타임스탬프는 디스코드 렌더링을 가정한 마크업
  (<t:UNIX:R>, <@id>, <#id>, <:emoji:id>)을 쓴다. 디스코드에선 예쁘게 렌더되지만 SNS에선
  literal 쓰레기로 보인다. Guide(매체 어댑터)의 책임은 SYS 출력을 *그 매체의 표현*으로 옮기는 것 —
  여기서 디스코드 마크업을 SNS가 그대로 읽을 수 있는 형태로 번역한다.
"""
import datetime
import re

_TS = re.compile(r"<t:(\d+)(?::([tTdDfFR]))?>")
_MENTION = re.compile(r"<@!?(\d+)>")
_CHANNEL = re.compile(r"<#(\d+)>")
_EMOJI = re.compile(r"<a?:(\w+):\d+>")


def _fmt_time(unix, style):
    try:
        dt = datetime.datetime.fromtimestamp(int(unix))
    except (ValueError, OverflowError, OSError):
        return ""
    if style in ("d", "D"):
        return dt.strftime("%Y-%m-%d")
    if style in ("F",):
        return dt.strftime("%Y-%m-%d %H:%M")
    # t/T/R/f/None — 상대시간(R)은 게시 후 stale 되니 절대 시:분으로 안정 표시
    return dt.strftime("%H:%M")


_PROTO = re.compile(r"^\[(?:Response|Request)\][^\n]*?Body:\s*")


def to_native(text):
    """디스코드 마크업·Rule 프로토콜 접두 → SNS-네이티브 텍스트. None/빈값은 그대로."""
    if not text:
        return text
    s = _PROTO.sub("", str(text))                     # [Response] Body: / [Request] … Body: 접두 제거
    s = _TS.sub(lambda m: _fmt_time(m.group(1), m.group(2)), s)
    s = _EMOJI.sub(lambda m: f":{m.group(1)}:", s)   # 커스텀 이모지 → :name:
    s = _MENTION.sub("", s)                            # 멘션 래퍼 제거(SNS는 자체 멘션 렌더)
    s = _CHANNEL.sub("", s)
    return re.sub(r"[ \t]{2,}", " ", s).strip()
