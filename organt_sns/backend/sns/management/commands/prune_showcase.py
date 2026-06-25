"""prune_showcase — 공개 쇼케이스를 지정한 디스코드 채널 3개만 남기고 정리(멱등).

  사용자가 큐레이션한 3개 프로젝트(P-002/P-030/P-031 = 디스코드 채널
  1513804695896850542 / 1518585326023475291 / 1518678846067445870)만 공개 쇼케이스로 유지한다.
  - 사용자 생성 채널(U-/S-)과 사용자 소유 직원(owner!=null)은 절대 건드리지 않는다.
  - Event.project는 SET_NULL이라 캐스케이드가 안 됨 → 해당 이벤트를 명시 삭제한다.
  - 남은 3채널에서 활동하지 않는 '공개' 직원(owner=null)은 정리한다(시드 로스터를 3채널 팀으로 축소).
  build.sh가 seed 뒤 실행. Postgres 영속이라 한 번 정리되면 유지된다.
"""
from django.core.management.base import BaseCommand

from sns.models import Project, Agent, Event, GuideMessage

KEEP = {"P-002", "P-030", "P-031"}


class Command(BaseCommand):
    help = "공개 쇼케이스 채널을 지정 3개만 남기고 정리(멱등)."

    def handle(self, *args, **opts):
        doomed = list(Project.objects.filter(pid__startswith="P-").exclude(pid__in=KEEP))
        ids = [p.id for p in doomed]
        # 1) 채널에 묶인 데이터 삭제 — Event는 SET_NULL이라 수동, GuideMessage는 FK 아님(channel_id)
        n_ev = Event.objects.filter(project_id__in=ids).delete()[0] if ids else 0
        for p in doomed:
            GuideMessage.objects.filter(channel_id=p.id).delete()
        # 2) 채널 삭제 — CollabTask/Thread/Membership은 CASCADE로 함께 제거
        n_proj = len(doomed)
        Project.objects.filter(id__in=ids).delete()
        # 3) 남은 3채널에서 안 쓰이는 '공개' 직원 정리(owner=null만; 사용자 직원은 보존)
        used = set(Event.objects.exclude(actor__isnull=True).values_list("actor_id", flat=True))
        used |= set(Event.objects.exclude(target__isnull=True).values_list("target_id", flat=True))
        used |= set(Project.objects.exclude(leader__isnull=True).values_list("leader_id", flat=True))
        orphan = Agent.objects.filter(owner__isnull=True).exclude(pk__in=used).exclude(bot_id=0)
        n_ag = orphan.count()
        orphan.delete()
        self.stdout.write(self.style.SUCCESS(
            f"쇼케이스 정리: 채널 {n_proj}개·이벤트 {n_ev}건·미사용 공개직원 {n_ag}명 삭제 "
            f"(유지: {sorted(KEEP)})."))
