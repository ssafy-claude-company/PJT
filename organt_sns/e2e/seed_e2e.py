"""E2E 결정적 시드 — 구조화 타임라인(회의/표결/라운드/페이즈) + 멎은요청 + 섹셔닝 + 엔진.
manage.py shell < seed_e2e.py 로 실행(run-e2e.sh가 호출). 격리 DB(SQLITE_PATH)에 적재한다."""
import time
from sns.models import Project, Agent, GuideMessage, Event, Person, Membership, EngineHeartbeat

for pid in ("E-1", "E-2"):
    for p in Project.objects.filter(pid=pid):
        GuideMessage.objects.filter(channel_id=p.id).delete()
    Project.objects.filter(pid=pid).delete()
Person.objects.filter(handle="e2e").delete()

proj = Project.objects.create(pid="E-1", name="협동 던전 게임", visibility="public", status="live")
p2 = Project.objects.create(pid="E-2", name="포트폴리오 사이트", visibility="public", status="live")
person = Person.objects.create(handle="e2e", name="E2E", token="tok_e2e")
Membership.objects.create(person=person, project=proj, status="active")   # → '내 채널' 섹션


def agent(bid, name, role, leader=False):
    a, _ = Agent.objects.update_or_create(
        bot_id=bid, defaults=dict(name=name, role=role, is_leader=leader, visibility="public"))
    return a


lead = agent(910001, "김도윤", "게임 기획자", True)
be = agent(910002, "이서준", "백엔드")
fe = agent(910003, "박지호", "프론트엔드")
qa = agent(910004, "최유나", "QA")
proj.leader = lead
proj.save()

t0 = time.time() - 600
_mid = [0]


def gm(sender, body, mtype="plain", to=None, kind="", off=0, payload=None):
    _mid[0] += 1
    GuideMessage.objects.create(channel_id=proj.id, thread_id=proj.id, sender_id=sender, msg_type=mtype,
                                to_id=to, kind=kind, body=body, ts=t0 + off, payload=payload or {})


_seq = [970000]


def ev(kind, off, summary, payload=None):
    _seq[0] += 1
    Event.objects.create(seq=_seq[0], ts=t0 + off, kind=kind, actor=lead, project=proj,
                         summary=summary, payload=payload or {})


gm(0, "2인 협동 던전 탈출 게임 만들어줘. 혼자선 못 깨게.", mtype="request", kind="W", off=0,
   payload={"requester_name": "E2E"})
gm(lead.bot_id, "[Request]\nTo: <@910002>\nKind: Work\nBody: 서버 권위 모델로 2인 동기화 설계", mtype="request", to=be.bot_id, kind="W", off=20)
gm(lead.bot_id, "[회의 1R] 둘이 동시에 레버를 당겨야 문이 열리는 협동 강제가 핵심이에요.", off=60)
gm(be.bot_id, "[회의 1R] 서버 권위 모델을 제안합니다. 서버가 검증하면 치트·desync를 막아요.", off=70)
gm(fe.bot_id, "[회의 1R] Explorer는 좁은 시야, Oracle은 전체 맵 — 비대칭 2역할로 긴장감을.", off=80)
gm(qa.bot_id, "[회의 1R] 존재이유 테스트 필요 — 솔로로 깨지면 협동이 가짜입니다.", off=90)
gm(lead.bot_id, "[회의 2R] 레버 2개 동시 + Oracle 단서 퍼즐로 확정. 부정형 테스트를 수용기준에.", off=110)
gm(be.bot_id, "[회의 2R] 레버는 서버가 hold-release로, 둘 다 눌린 프레임에만 door_open 브로드캐스트.", off=120)
ev("goal_set", 130, "목표 확정", {"goal": "2인 협동 던전 — 솔로 클리어 불가, 레버 2개 동시, 배포 URL 200"})
gm(lead.bot_id, "[표] WebSocket — 지연 낮고 양방향이라 협동 타이밍에 맞아요.", off=150)
gm(be.bot_id, "[표] WebSocket — 폴링은 협동 동기화에 못 씁니다.", off=156)
gm(fe.bot_id, "[표] WebSocket — 클라 구현도 표준적이라 안전합니다.", off=162)
gm(qa.bot_id, "[표] WebSocket — 상태 로깅만 붙이면 검증도 쉬워요.", off=168)
gm(be.bot_id, "[Response]\nBody: 서버 권위 동기화 1차 구현 완료.", mtype="response", off=240)
ev("task_complete", 245, "서버 권위 동기화 완료", {"result": "server.js 레버 hold-release + door_open, 교차검증 2회 통과"})
ev("deploy", 255, "배포 완료", {"result": "https://organt-p-001.onrender.com"})
# 멎은 요청 — 픽 후 무응답·미완·5분 경과 → stuck-bar
gm(0, "사운드 이펙트도 추가해줘", mtype="request", kind="W", off=300,
   payload={"requester_name": "E2E", "picked": True, "picked_ts": t0 + 300})
# 방금 픽한(작업 중·최근) 요청 — live_status working(recent) → 라이브-스트립 + 개입 바 표시
gm(0, "멀티플레이 동기화 더 다듬어줘", mtype="request", kind="W", off=590,
   payload={"requester_name": "E2E", "picked": True, "picked_ts": t0 + 590})

GuideMessage.objects.create(channel_id=p2.id, thread_id=p2.id, sender_id=0, msg_type="request",
                            kind="W", body="포트폴리오 만들어줘", ts=t0, payload={})
EngineHeartbeat.beat("e2e")   # 엔진 '가동 중' 표시

print("E2E seeded: E-1(msgs %d) E-2 + member e2e + engine beat" % _mid[0])
