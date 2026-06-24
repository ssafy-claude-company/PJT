"""seed_if_empty — DB가 비어 있을 때만 데모 시드를 적재한다(멱등).

  build.sh가 매 배포 `loaddata seed`를 하면 사용자 데이터가 매번 초기화된다(Render 무료=상관없음,
  하지만 자체 서버 영속 DB에선 치명적). 이 커맨드는 Agent가 하나도 없을 때만 seed를 적재해,
  재시작·재배포가 사용자가 만든 봇·채널·요청을 지우지 않게 한다.
"""
from django.core.management import call_command
from django.core.management.base import BaseCommand

from sns.models import Agent


class Command(BaseCommand):
    help = "DB가 비어 있을 때만 seed fixture를 적재(멱등) — 영속 DB 보호."

    def handle(self, *args, **opts):
        n = Agent.objects.count()
        if n > 0:
            self.stdout.write(f"이미 데이터 있음(Agent {n}명) — 시드 건너뜀(사용자 데이터 보존).")
            return
        self.stdout.write("빈 DB — 데모 시드 적재…")
        call_command("loaddata", "seed")
        self.stdout.write(self.style.SUCCESS(f"시드 적재 완료(Agent {Agent.objects.count()}명)."))
