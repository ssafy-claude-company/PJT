from django.apps import AppConfig
from django.db.backends.signals import connection_created


def _tune_sqlite(sender, connection, **kwargs):
    """다중 접속 안정화 — SQLite 연결마다 PRAGMA 적용. Postgres(자체 서버)에선 no-op.

    기본 SQLite(저널=DELETE)는 쓰기 한 번이 DB 전체를 EXCLUSIVE 잠금 → 모든 읽기를 막는다.
    여러 명이 폴(읽기)하면서 요청 전송·러너 기록(쓰기)이 겹치면 'database is locked'가 난다.
      - WAL: 쓰기 중에도 읽기를 동시에 진행(다중 독자 + 단일 기록자). 다중 접속 핵심.
      - busy_timeout: 잠금 만나면 즉시 실패하지 말고 대기(기본 5초 → 20초)로 충돌 흡수.
      - synchronous=NORMAL: WAL과 함께 쓰면 안전하면서 fsync 비용↓(무료 0.1 CPU에 유리).
    """
    if connection.vendor != "sqlite":
        return
    with connection.cursor() as cur:
        cur.execute("PRAGMA journal_mode=WAL;")       # 쓰기-읽기 동시성(파일 헤더에 영속, 멱등)
        cur.execute("PRAGMA synchronous=NORMAL;")     # WAL과 안전 조합, fsync 절감
        cur.execute("PRAGMA busy_timeout=20000;")     # 잠금 시 최대 20초 대기 후 포기
        cur.execute("PRAGMA foreign_keys=ON;")        # 무결성(SQLite 기본 OFF)


class SnsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "sns"

    def ready(self):
        # 새 DB 연결마다 PRAGMA 적용(연결 재사용 시에도 1회). 멱등·저비용.
        connection_created.connect(_tune_sqlite)
