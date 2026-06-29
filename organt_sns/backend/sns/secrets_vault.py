"""개인 자격증명 금고 — 암호화 유틸(BYO 키 저장용).

  사용자가 자기 RENDER_KEY·GH_PAT 등을 SNS에 맡기면 *암호화해서* 저장한다(평문 DB 금지).
  배포 시 프로젝트 owner의 값을 복호화해 쓴다. 값은 클라이언트로 절대 안 돌려준다(이름+힌트만).

  키 운영: 프로덕션은 ORGANT_VAULT_KEY(레포 밖 강한 시크릿)를 쓴다. 미설정이면 Django SECRET_KEY에서
  파생(데모 편의 — 단 SECRET_KEY가 레포 기본값이면 사실상 공개키니, 실서비스는 ORGANT_VAULT_KEY 필수).
  ⚠️ 이 키가 바뀌면 기존 저장값은 복호화 불가(고아) — 키는 안정적으로 유지할 것.
"""
import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


def _fernet():
    base = (os.environ.get("ORGANT_VAULT_KEY")
            or getattr(settings, "SECRET_KEY", "") or "organt-dev-fallback")
    key = base64.urlsafe_b64encode(hashlib.sha256(base.encode()).digest())
    return Fernet(key)


def encrypt(value: str) -> str:
    """평문 → 암호문(at-rest 저장용)."""
    return _fernet().encrypt((value or "").encode()).decode()


def decrypt(token: str) -> str:
    """암호문 → 평문. 복호화 실패(키 변경·손상)면 빈 문자열."""
    try:
        return _fernet().decrypt((token or "").encode()).decode()
    except (InvalidToken, ValueError, TypeError):
        return ""


def hint(value: str) -> str:
    """확인용 힌트 — 값 자체가 아니라 '••••마지막4자'. 빈 값/짧은 값은 길이만."""
    v = value or ""
    if len(v) <= 4:
        return "••••"
    return "••••" + v[-4:]
