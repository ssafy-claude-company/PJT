"""name_agents — 이름 없는 봇에 고유 이름 배정(멱등). 직군≠이름 분리의 데이터 측 보정.

  두뇌(디스코드) 봇은 이름이 비어 있어 직군이 정체성처럼 쓰였다 — 여기서 고유 이름을 채운다.
  이미 이름이 있으면 건드리지 않는다(스튜디오 채용·사용자 편집 보존). build에서 seed 뒤 실행.
"""
from django.core.management.base import BaseCommand

from sns.models import Agent
from sns.names import assign_name


class Command(BaseCommand):
    help = "이름 없는 봇에 고유 이름을 배정(멱등)."

    def handle(self, *args, **opts):
        taken = set(a.name for a in Agent.objects.exclude(name="") if (a.name or "").strip())
        n = 0
        for a in Agent.objects.filter(name="").order_by("bot_id"):
            a.name = assign_name(a.bot_id, taken)
            a.save(update_fields=["name"])
            n += 1
        self.stdout.write(self.style.SUCCESS(f"이름 배정 {n}명 (기존 이름 {len(taken) - n}명 보존)."))
