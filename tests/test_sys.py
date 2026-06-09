"""재구현 검증(P2P 모델): Guide 도구 + 베턴 wake + 단일흐름."""
import asyncio

from src.guide_tools import Flow, make_guide_tools
from src.protocol import Kind
from src.sys_core import Sys


class FakeGuide:
    def __init__(self):
        self.calls = []

    async def post(self, ch, sender, content, reply_to=None):
        self.calls.append(("post", ch, sender, content))
        return "m1"

    async def create_project_channel(self, gid, name):
        self.calls.append(("create_channel", name))
        return 9001

    async def open_task(self, ch, status):
        self.calls.append(("open_task", ch, status.purpose))
        return "blk", "thr"

    async def update_status(self, ch, blk, status):
        self.calls.append(("update", status.status))
        return blk

    async def send_request(self, thr, sender, to, kind, body):
        self.calls.append(("req", sender, to, body))
        return "reqid"

    async def send_response(self, thr, sender, req, body):
        self.calls.append(("resp", sender, body))
        return "respid"


def _flow(g, leader=11):
    f = Flow(g, channel_id=500, guild_id=1, leader_id=leader, bot_info={11: "L", 12: "M"})
    f.start_root("root")
    return f


def _tools(f, me, role):
    return {t.name: t for t in make_guide_tools(f, me, role)}


def test_서브프로세스_사망_143은_일시오류로_재시도대상():
    """SDK 서브프로세스가 SIGTERM(143)/파이프끊김으로 죽으면 일시오류로 보고 resume 재시도해야 한다
    — 작업이 끝났는데 마무리 메시지만 깨져 에러가 최종 응답으로 올라오는 일 방지."""
    from src.organt import _is_transient_api_error
    assert _is_transient_api_error("API Error: Command failed with exit code 143 (exit code: 143)")
    assert _is_transient_api_error("API Error: Fatal error in message reader")
    assert _is_transient_api_error("API Error: 529 overloaded")
    assert not _is_transient_api_error("배포 완료. 라이브 URL: https://x")   # 정상 응답은 재시도 아님
    assert not _is_transient_api_error("API Error: invalid request 400")    # 비일시 오류는 재시도 아님


def test_member는_request_recruit_run():
    f = _flow(FakeGuide())
    assert {t.name for t in make_guide_tools(f, 12, "member")} == {"request", "recruit", "run"}


def test_leader는_project_task_도구():
    f = _flow(FakeGuide())
    names = {t.name for t in make_guide_tools(f, 11, "leader")}
    # 보고/답변 툴 없음(반환=Response). 흐름 도구(request·recruit·run)+리더 셋업·배포 도구.
    assert names == {"request", "recruit", "run",
                     "create_project", "create_task", "set_goal", "complete_task", "deploy"}


def test_리더_등록툴이_전부_허용목록에_있음():
    """make_guide_tools(leader)가 등록한 모든 guide 툴은 허용목록(FLOW_TOOLS+LEADER_TOOLS)에도 있어야 한다.
    등록만 되고 allowed_tools에서 빠지면 런타임에 권한거부된다(set_goal 누락 사고 재발 방지)."""
    from src.guide_tools import FLOW_TOOLS, LEADER_TOOLS
    f = _flow(FakeGuide())
    names = {t.name for t in make_guide_tools(f, 11, "leader")}
    allowed = set(FLOW_TOOLS) | set(LEADER_TOOLS)
    missing = {n for n in names if f"mcp__guide__{n}" not in allowed}
    assert not missing, f"허용목록(FLOW_TOOLS+LEADER_TOOLS)에서 빠진 리더 툴: {missing}"


def test_run_안전가드():
    f = _flow(FakeGuide())
    rt = {t.name: t for t in make_guide_tools(f, 11, "leader")}["run"]
    f.workspace = None
    assert "작업공간" in asyncio.run(rt.handler({"command": "echo hi"}))["content"][0]["text"]
    f.workspace = "/tmp"
    assert "거부" in asyncio.run(rt.handler({"command": "rm -rf /tmp/x"}))["content"][0]["text"]
    assert "거부" in asyncio.run(rt.handler({"command": "git commit -am x"}))["content"][0]["text"]


def test_run_파일작성_백도어_차단():
    """run으로 파일 작성(heredoc·cat>·tee)은 막힌다 — 산출물 작성은 Write/Edit로(권한·협의 게이트·기록 적용).
    이 백도어가 열려 있으면 리더가 위임 없이 전부 혼자 찍어내 독점하거나 협의 중 선구현이 가능했다."""
    f = _flow(FakeGuide())
    f.workspace = "/tmp"
    rt = {t.name: t for t in make_guide_tools(f, 12, "member")}["run"]
    for cmd in ("cat > server.js << 'EOF'\nx\nEOF", "echo hi | tee app.js", "cat>x.js"):
        out = asyncio.run(rt.handler({"command": cmd}))["content"][0]["text"]
        assert "거부" in out and "Write/Edit" in out, cmd
    ok = asyncio.run(rt.handler({"command": "echo built"}))["content"][0]["text"]   # 정상 실행은 통과
    assert "거부" not in ok and "built" in ok


def test_run_백그라운드_프로세스_그룹째_정리():
    """run이 백그라운드로 띄운 자식(서버 등)을 끝나면 그룹째 정리 → 포트/프로세스 누수 없음."""
    import os
    import time as _t
    f = _flow(FakeGuide())
    f.workspace = "/tmp"
    rt = {t.name: t for t in make_guide_tools(f, 11, "leader")}["run"]
    # 마커는 작업공간 내 상대경로로 기록(절대경로 '> /' 리다이렉트는 안전가드가 차단).
    name = f"organt_runtest_{os.getpid()}.pid"
    marker = f"/tmp/{name}"
    # 백그라운드로 오래 자는 자식을 띄우고 그 PID를 기록 → run 반환 뒤엔 죽어 있어야 함.
    out = asyncio.run(rt.handler({"command": f"sleep 30 & echo $! > {name}; echo started"}))
    text = out["content"][0]["text"]
    assert "[exit 0]" in text and "started" in text   # 거부 아닌 실제 실행 확인
    with open(marker) as fp:
        pid = int(fp.read().strip())
    os.remove(marker)

    def _running(p):  # 좀비(Z)는 죽은 것으로 간주 — 자원/포트를 더는 잡지 않음
        try:
            with open(f"/proc/{p}/stat") as fp:
                return fp.read().split(") ", 1)[1].split(" ", 1)[0] != "Z"
        except (FileNotFoundError, ProcessLookupError):
            return False

    for _ in range(40):       # init의 reaping을 잠깐 기다림(최대 ~2s)
        if not _running(pid):
            break
        _t.sleep(0.05)
    assert not _running(pid), f"백그라운드 자식(pid={pid})이 정리되지 않고 누수됨"


def test_recruit로_부족직군_풀인력_합류():
    """담당자가 고른 팀에 없던 풀 인력도 recruit로 현재 Task에 합류시킬 수 있다(동적 충원) — 직군 보유자
    (예비 아님)는 역할명만으로 합류한다('말로만 배정 차단'은 예비에만 적용)."""
    g = FakeGuide()
    f = Flow(g, channel_id=500, guild_id=1, leader_id=11, bot_info={11: "L", 12: "A", 13: "B"})
    f.start_root("root")
    t = {x.name: x for x in make_guide_tools(f, 11, "leader")}
    asyncio.run(t["create_project"].handler({"name": "p", "team": "12"}))   # 담당자가 팀을 12로 좁힘
    asyncio.run(t["create_task"].handler({"members": "12"}))                # 13은 팀 밖
    assert set(f.current.team) == {11, 12} and 13 not in f.current.team
    asyncio.run(t["recruit"].handler({"member": "B", "reason": "부족"}))     # 역할명 'B'로 풀에서 합류
    assert 13 in f.current.team


def test_예비인력_새직군_런타임채용_말로만배정차단():
    """'예비'(직군 미배정)는 기본 팀에 안 들어가고, recruit(role=…)로 '실제' 직군이 부여돼야 한다 — role 없이
    예비 채용/위임은 거부(말로만 배정 차단). **1봇 1직업: 이미 직군 있는 봇에 다른 직군(겸직)은 거부**된다."""
    g = FakeGuide()
    f = Flow(g, channel_id=500, guild_id=1, leader_id=11,
             bot_info={11: "백엔드", 12: "프론트엔드", 13: "예비", 14: "예비"})
    f.start_root("root")
    persisted = {}
    f.persist_role = lambda mid, role: persisted.__setitem__(mid, role)   # '기억'(직업 고정) 배선 검증용
    t = {x.name: x for x in make_guide_tools(f, 11, "leader")}
    asyncio.run(t["create_task"].handler({"members": ""}))      # 담당자가 안 좁히면 프로젝트팀(예비 제외)
    assert set(f.current.team) == {11, 12} and 13 not in f.current.team   # 예비는 제외
    # role 없이 예비 채용 시도 → 거부, 팀에 안 들어옴(말로만 배정 차단)
    rno = asyncio.run(t["recruit"].handler({"member": "13"}))
    assert "거부" in rno["content"][0]["text"] and "예비" in rno["content"][0]["text"]
    assert 13 not in f.current.team and f.bot_info[13] == "예비"
    # 새 직군이 필요 → 예비를 '게임 기획자'로 실제 채용(member 미지정 → 예비 자동 선발)
    r = asyncio.run(t["recruit"].handler({"role": "게임 기획자", "reason": "기획 필요"}))
    assert "직군으로 채용" in r["content"][0]["text"] and "게임 기획자" in r["content"][0]["text"]
    hired = next(i for i in (13, 14) if f.bot_info[i] == "게임 기획자")
    assert hired in f.current.team and f.bot_info[hired] == "게임 기획자"
    assert persisted.get(hired) == "게임 기획자"   # 채용한 직업이 '기억'(로스터 라벨)에 반영됨 → 다음 흐름 유지
    # 1봇 1직업: 이미 직군('게임 기획자') 있는 봇에 다른 직군 추가 → 거부(겸직 폐지), 직군 그대로
    r2 = asyncio.run(t["recruit"].handler({"member": str(hired), "role": "레벨 디자이너"}))
    assert "거부" in r2["content"][0]["text"] and "1봇 1직업" in r2["content"][0]["text"]
    assert f.bot_info[hired] == "게임 기획자"
    # 남은 예비를 'UX 디자이너'로, 그 뒤 예비 소진 → 채용 불가 안내
    asyncio.run(t["recruit"].handler({"role": "UX 디자이너", "reason": "UX"}))
    r3 = asyncio.run(t["recruit"].handler({"role": "사운드", "reason": "x"}))
    assert "못 찾음" in r3["content"][0]["text"]


def test_채용직업_기억_다음흐름_유지():
    """recruit로 부여한 직군은 _roster_labels에 기록돼, 새 흐름 시작 시 reset 후에도 유지된다 — '직업 고정·기억'
    (예비가 한 번 직업을 받으면 매 흐름 예비로 원복되지 않고 그 직업군을 누적·재사용)."""
    s = Sys(FakeGuide(), guild_id=1, organt_builder=None, bot_info={11: "백엔드", 13: "예비"})
    s._roster_labels.__setitem__(13, "게임 기획자")   # handle_user_input이 거는 persist_role과 동일 동작
    s.bot_info.clear(); s.bot_info.update(s._roster_labels)   # 새 흐름 reset 경로
    assert s.bot_info[13] == "게임 기획자" and s.bot_info[11] == "백엔드"   # 예비→게임기획자 유지


def test_개입_Task는_전원소집_안함():
    """개입(intervention) 흐름의 create_task도 담당자가 부른 담당만 모인다 — members로 고른 동료만(작은 수정에
    10명 소집 방지). 어느 흐름이든 팀은 자동 전원이 아니라 담당자가 동적 선정한다."""
    g = FakeGuide()
    f = Flow(g, channel_id=500, guild_id=1, leader_id=11,
             bot_info={11: "백엔드", 12: "프론트엔드", 13: "디자이너", 14: "QA"})
    f.start_root("root")
    f.intervention = {"id": "P-001"}        # 개입 표시
    f.project_channel = 500
    t = {x.name: x for x in make_guide_tools(f, 11, "leader")}
    asyncio.run(t["create_task"].handler({"members": "12"}))   # 프론트만 부름
    assert set(f.current.team) == {11, 12}   # 전원(13·14) 강제 합류 안 됨


def test_직군미배정_예비에게_위임_거부():
    """직군 미배정('예비') 봇에겐 request가 거부된다 — 말로 직군 주고 일 시키는 것을 구조적으로 차단."""
    g = FakeGuide()
    f = Flow(g, channel_id=500, guild_id=1, leader_id=11, bot_info={11: "백엔드", 13: "예비"})
    f.start_root("root")
    t = {x.name: x for x in make_guide_tools(f, 11, "leader")}
    asyncio.run(t["create_task"].handler({"members": ""}))
    r = asyncio.run(t["request"].handler({"to_id": "13", "kind": "Info", "body": "기획 해줘"}))
    txt = r["content"][0]["text"]
    assert "거부" in txt and "예비" in txt and "recruit" in txt   # recruit(role=)로 직군 먼저


def test_담당자_표식은_To수신자_동적():
    """담당자는 고정 직책이 아니라 흐름의 To 수신자(leader) — _prompt가 그 봇에게만 '(담당자)'를 붙이고,
    같은 봇이라도 다른 흐름(다른 leader)에선 직군만으로 한 직원으로 참여한다."""
    s = Sys(FakeGuide(), guild_id=1, organt_builder=None, bot_info={11: "백엔드", 12: "프론트엔드"})
    # 11이 담당자(To)일 때: 11은 '백엔드(담당자)', 12는 동료 목록에서 그냥 '프론트엔드'
    p_lead = s._prompt("x", Kind.WORK, "leader", 11, leader_id=11)
    assert "백엔드(담당자)" in p_lead
    p_mem = s._prompt("x", Kind.INFO, "member", 12, leader_id=11)
    assert "11(백엔드(담당자))" in p_mem and "역할: 프론트엔드" in p_mem
    # 12가 담당자(To)인 다른 흐름: 12가 '프론트엔드(담당자)', 11은 한 직원
    p_lead2 = s._prompt("x", Kind.WORK, "leader", 12, leader_id=12)
    assert "프론트엔드(담당자)" in p_lead2


def test_원문요청_프롬프트주입_탈중앙():
    """퍼실리테이터: '사용자 원문 요청'이 담당자·팀원 프롬프트에 그대로 주입된다 — 담당자 paraphrase를 거치며
    의도가 왜곡되는 중앙집권을 구조적으로 완화(팀원도 원문을 직접 봄). 원문 없으면 주입 안 함(하위호환)."""
    s = Sys(FakeGuide(), guild_id=1, organt_builder=None, bot_info={11: "백엔드", 12: "프론트엔드"})
    s._origin_request = "캐릭터 10개로 늘리고 이펙트 구분해줘"
    p_mem = s._prompt("프론트 성공기준 제안해줘", Kind.INFO, "member", 12, leader_id=11)
    p_lead = s._prompt("x", Kind.WORK, "leader", 11, leader_id=11)
    assert "사용자 원문 요청" in p_mem and "캐릭터 10개로 늘리고 이펙트 구분해줘" in p_mem   # 팀원도 원문 직접
    assert "캐릭터 10개로 늘리고 이펙트 구분해줘" in p_lead                               # 리더도 원문 그대로
    s._origin_request = ""
    assert "사용자 원문 요청" not in s._prompt("x", Kind.INFO, "member", 12, leader_id=11)


def test_예비_담당자는_자기직군_먼저채용_지시받음():
    """'예비'(직군 미배정) 봇이 담당자(To)로 호명되면, 프롬프트가 '먼저 recruit로 자기 직군을 부여해 한 직원으로
    참여하라'고 지시한다(사용자: 자길 예비로 두지 말고 프로젝트의 일원으로 참여). 또 팀은 자동 전원이 아니라
    담당자가 동적으로 짜라고 안내한다. 직군 보유 담당자에겐 '예비 먼저 채용' 지시가 안 나온다."""
    s = Sys(FakeGuide(), guild_id=1, organt_builder=None, bot_info={11: "예비", 12: "프론트엔드"})
    p_spare = s._prompt("x", Kind.WORK, "leader", 11, leader_id=11)
    assert "예비" in p_spare and "recruit(member=11" in p_spare and "자기 직군" in p_spare
    assert "팀은 당신이 동적으로 짠다" in p_spare           # 동적 팀 구성 안내는 담당자 프롬프트에 항상
    s2 = Sys(FakeGuide(), guild_id=1, organt_builder=None, bot_info={11: "백엔드", 12: "프론트엔드"})
    p_norm = s2._prompt("x", Kind.WORK, "leader", 11, leader_id=11)
    assert "자기 직군부터 정하라" not in p_norm and "팀은 당신이 동적으로 짠다" in p_norm


def test_owner는_work수신자_goal합의후():
    """새 모델(중앙집권 방지): create_task는 Purpose만 — Goal·owner 선배정 없음. Goal은 set_goal로 확정해야
    Work 위임 가능(선분배 금지), 그 Work를 받은 동료가 곧 그 Task의 owner가 된다(수신=소유)."""
    g = FakeGuide()
    f = Flow(g, channel_id=500, guild_id=1, leader_id=11, bot_info={11: "L", 12: "A백엔드", 13: "B프론트"})
    f.start_root("root")
    waked = []

    async def wake(to, b, k):
        waked.append(to)
        return "완료"

    f.wake = wake
    t = {x.name: x for x in make_guide_tools(f, 11, "leader")}
    asyncio.run(t["create_project"].handler({"name": "p", "team": "12,13"}))
    asyncio.run(t["create_task"].handler({"purpose": "서버", "members": "12,13"}))
    # 선배정 없음: owner·goal 비어 있음 (판 걸 때 분배 안 함)
    assert f.current.owner == 0 and f.current.status.owner == "" and not f.current.status.goal
    # Goal 미확정 상태에서 Work 위임은 거부(선분배 금지)
    blocked = asyncio.run(t["request"].handler({"to_id": "12", "kind": "Work", "body": "서버 만들어"}))
    assert "Goal" in blocked["content"][0]["text"] and f.current.owner == 0
    assert not any(c[0] == "req" for c in g.calls)               # 거부 → 게시 안 함
    # 팀 합의 결과를 리더가 set_goal로 확정 — 이 Task의 멤버 전원(12,13)을 Info로 물어야 통과(Task별·멤버별)
    f.current.participated.add(12)
    f.current.participated.add(13)
    asyncio.run(t["set_goal"].handler({"goal": "GET/POST /todos 동작"}))
    assert f.current.status.goal == "GET/POST /todos 동작"
    # 이제 Work 위임 → 받은 동료(12)가 owner가 됨 (수신=소유)
    asyncio.run(t["request"].handler({"to_id": "12", "kind": "Work", "body": "서버 만들어"}))
    assert f.current.owner == 12 and "A백엔드" in f.current.status.owner
    assert 12 in waked


def test_set_goal은_Task멤버_전원_의견받은뒤에만_Task별():
    """Goal은 'Task마다 그 담당 팀이 함께' 정한다(docs: Task.Team이 Goal을 정함) — 이 Task 멤버 전원을 Info로
    물은 뒤에만 set_goal 통과. 전역 1회로 끝내는 리더 독단/선지정 차단, Task가 바뀌면 추적도 리셋(Task별)."""
    g = FakeGuide()
    f = Flow(g, channel_id=500, guild_id=1, leader_id=11, bot_info={11: "L", 12: "백", 13: "프"})
    f.start_root("root")
    t = {x.name: x for x in make_guide_tools(f, 11, "leader")}
    asyncio.run(t["create_project"].handler({"name": "p", "team": "12,13"}))
    asyncio.run(t["create_task"].handler({"purpose": "서버", "members": "12,13"}))
    f.current.participated.add(12)    # 12만 물음
    r1 = asyncio.run(t["set_goal"].handler({"goal": "동작"}))
    assert "거부" in r1["content"][0]["text"] and not f.current.status.goal   # 13 미협의 → 거부
    f.current.participated.add(13)    # 13도 물음
    asyncio.run(t["set_goal"].handler({"goal": "동작"}))
    assert f.current.status.goal == "동작"                         # 전원 협의 → 통과
    # 다음 Task에선 추적 리셋(hist_start) → 이전 협의 재사용 불가
    f.current.verified = True
    asyncio.run(t["complete_task"].handler({"result": "ok"}))
    asyncio.run(t["create_task"].handler({"purpose": "프론트", "members": "13"}))
    r3 = asyncio.run(t["set_goal"].handler({"goal": "화면"}))     # 새 Task에서 13 다시 안 물음
    assert "거부" in r3["content"][0]["text"]                      # Task별로 다시 합의해야 함


def test_Task팀은_담당자가_동적선정():
    """팀은 자동 전원 소집이 아니라 담당자가 일에 맞게 고른다(직군 고정 해결) — create_task(members)로 좁히거나,
    비우면 프로젝트팀(예비 제외) 기본. 빠져 있던 인력을 강제로 끌어오지 않는다(첫 Task도 마찬가지)."""
    g = FakeGuide()
    f = Flow(g, channel_id=500, guild_id=1, leader_id=11, bot_info={11: "L", 12: "백", 13: "프", 14: "디"})
    f.start_root("root")
    t = {x.name: x for x in make_guide_tools(f, 11, "leader")}
    asyncio.run(t["create_project"].handler({"name": "p", "team": "12,13"}))  # 담당자가 팀을 12,13으로 구성
    asyncio.run(t["create_task"].handler({"members": "12"}))      # 이 Task엔 12만 지정 → 12만(전원 강제 아님)
    assert set(f.current.team) == {11, 12} and 14 not in f.current.team
    f.current.participated.update({12})
    asyncio.run(t["set_goal"].handler({"purpose": "서버", "goal": "동작"}))
    f.current.verified = True
    asyncio.run(t["complete_task"].handler({"result": "ok"}))
    asyncio.run(t["create_task"].handler({"members": ""}))        # 비우면 프로젝트팀(11,12,13) 기본 — 14는 안 부름
    assert set(f.current.team) == {11, 12, 13} and 14 not in f.current.team


def test_create_task_빈껍데기_purpose는_팀이_set_goal로():
    """create_task는 Purpose를 비운 '빈 껍데기'로 연다(리더가 할 일 선지정 금지) — Purpose·Goal은 그 Task
    멤버 협의 후 set_goal(purpose, goal)로 함께 확정된다(분산: 무엇을 풀지도 팀이 정함)."""
    g = FakeGuide()
    f = _flow(g)                                   # leader 11, member 12
    t = {x.name: x for x in make_guide_tools(f, 11, "leader")}
    asyncio.run(t["create_task"].handler({"members": "12"}))
    assert f.current.status.purpose == "" and f.current.status.goal == ""   # 빈 껍데기(리더 선지정 없음)
    r0 = asyncio.run(t["set_goal"].handler({"purpose": "서버", "goal": "동작"}))
    assert "거부" in r0["content"][0]["text"]                                # 멤버 협의 전엔 거부
    f.current.participated.add(12)               # 팀 회의
    asyncio.run(t["set_goal"].handler({"purpose": "할 일 저장 문제 해결", "goal": "추가·삭제 시나리오 통과"}))
    assert f.current.status.purpose == "할 일 저장 문제 해결"                  # Purpose가 팀 회의로 채워짐
    assert f.current.status.goal == "추가·삭제 시나리오 통과"


def test_협의게이트_peer협의_인정_빈핑_불인정():
    """set_goal 합의 게이트 개선: (1) peer끼리 협의(member→member)도 합의로 인정 → 리더 허브 완화,
    (2) 빈 핑('응답 가능하신가요?')은 실질 협의로 안 침(허울뿐인 협의 차단)."""
    g = FakeGuide()
    f = Flow(g, channel_id=500, guild_id=1, leader_id=11, bot_info={11: "L", 12: "백", 13: "프"})
    f.start_root("root")

    async def wake(to, b, k):
        return "ok"

    f.wake = wake
    tL = {x.name: x for x in make_guide_tools(f, 11, "leader")}
    asyncio.run(tL["create_project"].handler({"name": "p", "team": "12,13"}))
    asyncio.run(tL["create_task"].handler({"members": "12,13"}))
    asyncio.run(tL["request"].handler({"to_id": "12", "kind": "Info", "body": "응답 가능하신가요?"}))
    assert 12 not in f.current.participated                       # 빈 핑은 협의로 불인정
    asyncio.run(tL["request"].handler({"to_id": "12", "kind": "Info", "body": "백엔드 도메인 목표·성공기준을 제안해줘"}))
    assert 12 in f.current.participated and 13 not in f.current.participated   # 실질 질문은 인정
    r = asyncio.run(tL["set_goal"].handler({"purpose": "p", "goal": "g"}))
    assert "거부" in r["content"][0]["text"]                       # 13 미참여 → 거부
    f.comm.request(11, 12, "w", Kind.WORK)                         # alive→12 (12가 요청 가능하게)
    tM = {x.name: x for x in make_guide_tools(f, 12, "member")}
    asyncio.run(tM["request"].handler({"to_id": "13", "kind": "Info", "body": "API 필드명 id/title로 맞출까요?"}))
    assert 13 in f.current.participated                           # peer 협의(12→13)로 13도 참여 인정


def test_무응답은_인프라로_취급_재배정_충원_안함():
    """단일흐름에선 한 명만 일하므로 동료 '실패'는 그 동료가 아니라 인프라(서브프로세스 크래시)다 →
    '다른 사람 재배정·새 채용'을 권하지 않는다(같은 환경이라 똑같이 실패 — '백엔드 6명' 루프의 뿌리)."""
    g = FakeGuide()
    f = _flow(g)                                   # leader 11, member 12

    async def wake(to, b, k):
        return "API Error: 529 overloaded"         # 서브프로세스 크래시/일시오류 모의

    f.wake = wake
    t = {x.name: x for x in make_guide_tools(f, 11, "leader")}
    asyncio.run(t["create_task"].handler({"members": "12"}))
    f.current.participated.add(12)
    asyncio.run(t["set_goal"].handler({"purpose": "p", "goal": "g"}))
    r = asyncio.run(t["request"].handler({"to_id": "12", "kind": "Work", "body": "구현"}))
    txt = r["content"][0]["text"]
    assert "인프라" in txt and "새로 뽑지" in txt and "보고" in txt   # 인프라로 취급, 재배정·충원 안 권함
    assert "recruit" not in txt and "재배정" not in txt              # 교체·충원을 권하지 않음


def test_연속실패는_충원루프_차단():
    """무응답/타임아웃이 '연속'되면(시스템 일시불안정) '더 채용 말라'로 바뀐다 — 타임아웃 백엔드를 계속
    새로 뽑던 충원 루프(백엔드 6명 사태) 차단. 정상 응답이 한 번 오면 카운터 리셋."""
    g = FakeGuide()
    f = _flow(g)
    state = {"fail": True}

    async def wake(to, b, k):
        return "API Error: timeout" if state["fail"] else "완료"

    f.wake = wake
    t = {x.name: x for x in make_guide_tools(f, 11, "leader")}
    asyncio.run(t["create_task"].handler({"members": "12"}))
    f.current.participated.add(12)
    asyncio.run(t["set_goal"].handler({"purpose": "p", "goal": "g"}))
    r1 = asyncio.run(t["request"].handler({"to_id": "12", "kind": "Work", "body": "1차"}))
    assert "인프라" in r1["content"][0]["text"] and f.consec_fail == 1   # 1회: 인프라로 취급(교체·충원 안 권함)
    assert "새로 뽑지" in r1["content"][0]["text"]
    r2 = asyncio.run(t["request"].handler({"to_id": "12", "kind": "Work", "body": "2차"}))
    assert "환경" in r2["content"][0]["text"] and "새로 뽑" in r2["content"][0]["text"]  # 2회+: 환경 불안정 보고
    assert f.consec_fail == 2
    # consec_fail>=2 → recruit 자체가 '하드 차단'(안내가 아니라 거부) — 백엔드 6명 충원 구조적으로 불가
    rc = asyncio.run(t["recruit"].handler({"role": "백엔드", "reason": "충원"}))
    assert "채용 보류" in rc["content"][0]["text"]
    # 정상 응답이 한 번 오면 consec_fail 리셋 → 다시 채용 가능
    state["fail"] = False
    asyncio.run(t["request"].handler({"to_id": "12", "kind": "Work", "body": "3차"}))
    assert f.consec_fail == 0


def test_continue전_고아베턴_복구():
    """위임 도중 리더 턴이 끝나 베턴이 동료에 굳으면(고아), continue가 리더를 다시 띄우기 전에 베턴을
    리더로 강제 복구한다 — '활성=동료'로 모든 요청이 거부되는 '두 흐름' 버그 방지."""
    import types
    g = FakeGuide()
    s = Sys(g, guild_id=1, organt_builder=None, bot_info={11: "L", 12: "M"}, workspace="/ws", max_continue=3)
    calls = []

    async def fake_run_turn(flow, oid, body, kind, role):
        calls.append(role)
        if len(calls) == 1:                        # seg1: 위임 도중 끝남 → 베턴이 동료(12)에 굳음
            flow.current = types.SimpleNamespace(
                task_id="t1", status=types.SimpleNamespace(status="진행", result=None))
            flow.comm.request(11, 12, "leak", Kind.WORK)        # alive→12(고아 프레임)
            return "작업 중 (⚠ 턴 한도 도달 — 미완)"
        assert flow.comm.alive == 11, f"continue 진입 시 베턴이 리더가 아님: {flow.comm.alive}"  # 복구됨
        flow.current = None
        return "완료"

    s.run_turn = fake_run_turn
    asyncio.run(s.handle_user_input(500, 11, "큰 작업", root_id="r"))
    assert len(calls) == 2
    assert any(e["event"] == "baton_recover_continue" and e.get("recovered") for e in s.flow_log)


def test_request_동료_깨우고_베턴복귀():
    g = FakeGuide()
    f = _flow(g)
    waked = []

    async def wake(to, b, k):
        waked.append((to, b, k))
        return f"{b} 처리완료"

    f.wake = wake
    tools = _tools(f, 11, "leader")
    asyncio.run(tools["create_task"].handler({"purpose": "p", "members": "12"}))
    f.current.participated.add(12)   # 목표 합의 전 팀 Info 협의
    asyncio.run(tools["set_goal"].handler({"goal": "g"}))     # Work 위임은 Goal 확정 후 가능
    res = asyncio.run(tools["request"].handler({"to_id": "12", "kind": "Work", "body": "백엔드"}))
    assert len(waked) == 1 and waked[0][0] == 12 and waked[0][2] == Kind.WORK   # 동료 깨움
    assert "백엔드" in waked[0][1] and "Goal: g" in waked[0][1]   # 원 요청 + Goal 계약을 안고 전달
    assert f.comm.alive == 11                        # 응답 후 베턴 복귀
    assert "처리완료" in res["content"][0]["text"]
    assert any(c[0] == "req" for c in g.calls) and any(c[0] == "resp" for c in g.calls)


def test_request_자기자신_거부_게시안함():
    g = FakeGuide()
    f = _flow(g)
    f.wake = lambda *a: None
    tools = _tools(f, 11, "leader")
    asyncio.run(tools["create_task"].handler({"purpose": "p", "goal": "g"}))
    r = asyncio.run(tools["request"].handler({"to_id": "11", "kind": "Work", "body": "x"}))
    assert "거부" in r["content"][0]["text"]
    assert not any(c[0] == "req" for c in g.calls)   # 검증 실패 → 게시 안 함


def test_단일Task_순차_생성과_완료마감():
    g = FakeGuide()
    f = _flow(g)
    tools = _tools(f, 11, "leader")
    asyncio.run(tools["create_task"].handler({"purpose": "백엔드", "goal": "API 동작"}))
    # 현재 Task 미완이면 새 Task 거부(단일흐름 — 한 번에 하나, 고아 '진행' 방지)
    blocked = asyncio.run(tools["create_task"].handler({"purpose": "프론트", "goal": "화면 연동"}))
    assert "단일흐름" in blocked["content"][0]["text"] and len(f.tasks) == 1
    # 현재 Task 완료 마감 → 다음 Task 허용 (run 검증돼야 마감 가능 — 허위완료 가드)
    f.current.verified = True
    r = asyncio.run(tools["complete_task"].handler({"result": "백엔드 완료"}))
    assert "완료" in r["content"][0]["text"] and f.current is None
    asyncio.run(tools["create_task"].handler({"purpose": "프론트", "goal": "화면 연동"}))
    assert len(f.tasks) == 2 and f.tasks[0].task_id != f.tasks[1].task_id   # task_id 유니크
    f.current.verified = True
    r2 = asyncio.run(tools["complete_task"].handler({"result": "프론트 완료"}))
    assert f.tasks[1].status.status == "완료" and "프론트 완료" in f.tasks[1].status.result
    assert f.current is None
    # 현재 Task 없으면 request 거부(게시 안 함)
    f.wake = lambda *a: None
    rr = asyncio.run(tools["request"].handler({"to_id": "12", "kind": "Work", "body": "x"}))
    assert "진행 중인 Task가 없습니다" in rr["content"][0]["text"]


def test_허위완료_차단_run검증_후에만_마감():
    """run으로 한 번도 검증 안 한 Task는 complete_task 거부(허위완료 차단). run 후엔 허용."""
    f = _flow(FakeGuide())
    f.workspace = "/tmp"
    tools = _tools(f, 11, "leader")
    asyncio.run(tools["create_task"].handler({"purpose": "백엔드", "goal": "API 동작"}))
    # 실행 전 마감 시도 → 거부(허위완료 금지), Task는 여전히 진행 중
    r = asyncio.run(tools["complete_task"].handler({"result": "다 했어요"}))
    assert "거부" in r["content"][0]["text"] and "실행" in r["content"][0]["text"]
    assert f.current is not None and f.current.status.status != "완료"
    # run으로 실제 실행 → verified=True, 시스템이 영수증(실제 출력) 캡처
    asyncio.run(tools["run"].handler({"command": "echo ok"}))
    assert f.current.verified is True and f.current.run_count == 1 and f.current.evidence
    # 마감 허용 — 결과엔 에이전트 '보고' 옆에 시스템 실행기록(실제 출력)이 떼어낼 수 없게 묶인다
    r2 = asyncio.run(tools["complete_task"].handler({"result": "검증 후 완료"}))
    assert "완료" in r2["content"][0]["text"] and f.current is None
    res = f.tasks[-1].status.result
    assert "검증 후 완료" in res and "시스템 실행기록" in res and "exit=0" in res


def test_close_flow_정상_clean_close():
    s = Sys(FakeGuide(), guild_id=1, organt_builder=None, bot_info={11: "L"})
    f = _flow(s.guide)                          # comm: [origin→11], alive=11
    s._close_flow(f, 11, "결과")
    assert f.comm.done                          # 리더가 alive → 정상 close


def test_close_flow_비정상베턴_강제드레인():
    s = Sys(FakeGuide(), guild_id=1, organt_builder=None, bot_info={11: "L", 12: "M"})
    f = _flow(s.guide)
    f.comm.request(11, 12, "leak", Kind.WORK)   # 닫히지 않은 프레임 → alive=12(비정상)
    assert not f.comm.done and f.comm.alive == 12
    s._close_flow(f, 11, "결과")                # 강제 드레인
    assert f.comm.done                          # 교착 없이 종료


def test_프로젝트_등록과_채널개입_라우팅():
    """create_project → 식별번호 등록+채널 앵커. 등록된 채널에 다시 명령 → '개입'으로 라우팅(맥락 유지)."""
    g = FakeGuide()
    s = Sys(g, guild_id=1, organt_builder=None, bot_info={11: "L", 12: "M"}, workspace="/ws")
    f = Flow(g, channel_id=500, guild_id=1, leader_id=11, bot_info={11: "L", 12: "M"})
    f.workspace = "/ws"
    f.register_project = lambda ch, name: s._register_project(ch, name, f.workspace, f.leader)
    f.start_root("root")
    t = {x.name: x for x in make_guide_tools(f, 11, "leader")}
    asyncio.run(t["create_project"].handler({"name": "스네이크", "team": "12"}))   # 채널 9001 생성
    pid = s.projects[9001]["id"]
    assert pid.startswith("P-") and s.projects[9001]["workspace"] == "/ws"   # 내부 등록(채널 앵커는 안 박음)

    captured = {}
    async def fake_run_turn(flow, oid, body, kind, role):
        captured["flow"], captured["body"] = flow, body
        return "done"
    s.run_turn = fake_run_turn
    asyncio.run(s.handle_user_input(9001, 11, "즉사 버그 고쳐", root_id=None))   # 등록 채널 명령
    fl = captured["flow"]
    assert fl.intervention and fl.project_id == pid                              # 개입으로 인식
    assert fl.project_channel == 9001 and fl.workspace == "/ws"                  # 기존 맥락 유지
    assert "개입" in captured["body"] and "즉사 버그 고쳐" in captured["body"]
    # 미등록 채널은 일반 신규 흐름(개입 아님)
    asyncio.run(s.handle_user_input(777, 11, "새 일", root_id=None))
    assert captured["flow"].intervention is None and captured["flow"].workspace == "/ws"


def test_프로젝트_레지스트리_영속과_중복방지(tmp_path):
    """레지스트리를 디스크에 영속 → 프로세스가 끝나도 '원래 프로젝트'에 개입 가능. 같은 이름은 재사용."""
    p = str(tmp_path / "projects.json")
    s1 = Sys(FakeGuide(), guild_id=1, organt_builder=None, bot_info={11: "L"}, projects_path=p)
    pid = s1._register_project(9001, "스네이크", "/ws", 11)
    # 같은 이름은 새 채널이어도 식별번호 '그대로 유지' + 채널만 갱신(번호 증가/중복 금지)
    assert s1._register_project(9999, "스네이크", "/ws2", 11) == pid
    assert 9999 in s1.projects and 9001 not in s1.projects     # 채널만 현재 것으로 이동
    assert s1.projects[9999]["id"] == pid and s1.projects[9999]["workspace"] == "/ws2"
    # 새 프로세스(새 Sys)가 같은 파일 로드 → 갱신된 채널·식별번호 그대로 복원
    s2 = Sys(FakeGuide(), guild_id=1, organt_builder=None, bot_info={11: "L"}, projects_path=p)
    assert 9999 in s2.projects and s2.projects[9999]["id"] == pid
    assert s2.projects[9999]["workspace"] == "/ws2"


def test_단일흐름_진행중_명령은_큐잉():
    g = FakeGuide()
    s = Sys(g, guild_id=1, organt_builder=None, bot_info={11: "L"})
    s.active_flow = Flow(g, 500, 1, 11, {11: "L"})    # 활성(미완) 흐름
    out = asyncio.run(s.handle_user_input(500, 11, "두번째 명령", root_id=None))
    assert out["mode"] == "queued"                    # 버리지 않고 큐에 적재
    assert s.queue and s.queue[0][2] == "두번째 명령"


def test_턴한도_미완이면_같은세션으로_이어서_완료():
    """리더가 턴 한도로 Task를 못 닫고 끝나면 SYS가 이어서 재호출해 완료까지 끌고 간다(中断 아님)."""
    import types
    g = FakeGuide()
    s = Sys(g, guild_id=1, organt_builder=None, bot_info={11: "L"}, workspace="/ws", max_continue=4)
    calls = []

    async def fake_run_turn(flow, oid, body, kind, role):
        calls.append(body)
        if len(calls) == 1:                            # 1차: Task 열어둔 채 턴 한도로 끊김
            flow.current = types.SimpleNamespace(
                task_id="t1", status=types.SimpleNamespace(status="진행", result=None))
            return "작업 중... (⚠ 턴 한도 도달 — 작업이 미완일 수 있음)"
        flow.current = None                            # 2차(이어서): 마감
        return "완료"

    s.run_turn = fake_run_turn
    asyncio.run(s.handle_user_input(500, 11, "큰 작업", root_id=None))
    assert len(calls) == 2 and "이어서 계속" in calls[1]          # 연속 실행 프롬프트로 재호출됨
    assert any(e["event"] == "continue_incomplete" for e in s.flow_log)


def test_새요청마다_세션초기화_앵커링차단(tmp_path):
    """새 최상위 요청 시작 시 organt_state_*.json를 지워 '이미 했다' 앵커링을 구조적으로 막는다."""
    sd = tmp_path
    (sd / "organt_state_11.json").write_text("{}")
    (sd / "organt_state_12.json").write_text("{}")
    s = Sys(FakeGuide(), guild_id=1, organt_builder=None, bot_info={11: "L"},
            workspace="/ws", session_dir=str(sd))

    async def fake_rt(flow, oid, body, kind, role):
        return "done"

    s.run_turn = fake_rt
    asyncio.run(s.handle_user_input(500, 11, "새 요청", root_id=None))
    assert not list(sd.glob("organt_state_*.json"))   # 세션 파일 초기화됨
    assert any(e["event"] == "reset_sessions" for e in s.flow_log)


def test_위임자에게_되묻기는_확인요청반환_에러아님():
    """직속 위임자에게 Info로 되물으면 '재진입 불가' 에러 대신 확인요청을 위임자에게 반환(협업 가능)."""
    g = FakeGuide()
    f = _flow(g)                                       # leader 11, member 12; start_root → alive=11
    tools11 = _tools(f, 11, "leader")
    asyncio.run(tools11["create_task"].handler({"purpose": "p", "goal": "g", "members": "12"}))
    f.comm.request(11, 12, "r1", Kind.WORK)            # 11→12 위임 → alive=12, 12의 직속위임자=11
    tools12 = _tools(f, 12, "member")
    r = asyncio.run(tools12["request"].handler(
        {"to_id": "11", "kind": "Info", "body": "필드명 X 맞나요?"}))
    txt = r["content"][0]["text"]
    assert "확인요청" in txt and "위임자" in txt and "거부" not in txt   # 더는 거부 에러가 아님
    assert f.pending_clarify == {"from": 12, "to": 11, "q": "필드명 X 맞나요?"}


def test_위임자측_확인요청_질문으로_표면화():
    """깨운 동료가 확인요청을 남기고 반환하면, 위임자에게 그 질문이 응답으로 떠올라 답·재위임하게 된다."""
    g = FakeGuide()
    f = _flow(g)
    tools11 = _tools(f, 11, "leader")
    asyncio.run(tools11["create_task"].handler({"purpose": "p", "members": "12"}))
    f.current.participated.add(12)   # 목표 합의 전 팀 Info 협의
    asyncio.run(tools11["set_goal"].handler({"goal": "g"}))   # Work 위임 전 Goal 확정

    async def wake(to, body, kind):                    # 12가 위임자(11)에게 확인요청 남기고 반환했다고 모의
        f.pending_clarify = {"from": 12, "to": 11, "q": "필드명 X 맞나요?"}
        return "(짧게 반환)"

    f.wake = wake
    r = asyncio.run(tools11["request"].handler({"to_id": "12", "kind": "Work", "body": "X 구현"}))
    txt = r["content"][0]["text"]
    assert "확인요청 from" in txt and "필드명 X 맞나요?" in txt   # 질문이 위임자 응답으로 표면화
    assert f.pending_clarify is None                            # 표면화하며 소거


def test_재위임은_Redo로_바운드_정당한첫위임은_허용():
    """docs Communication.md §5: 이미 '완료 응답'까지 받은 산출물을 같은 owner에게 또 Work로 보내면
    '새 위임'이 아니라 Redo(직전 결함 보완)로 처리되고, 한계를 넘으면 거부된다(반사적 중복요청 차단·보완은 허용)."""
    g = FakeGuide()
    f = _flow(g)
    waked = []

    async def wake(to, b, k):
        waked.append((to, b))
        f.act_count += 1   # owner가 위임 도중 실제로 작업(run/Write)했다고 모의 → '검증된 인도'(허위완료 가드 통과)
        return "완료"

    f.wake = wake
    tools = _tools(f, 11, "leader")
    asyncio.run(tools["create_task"].handler({"purpose": "p", "members": "12"}))
    f.current.participated.add(12)   # 목표는 팀 합의 산물
    asyncio.run(tools["set_goal"].handler({"goal": "GET/POST /todos 동작"}))
    # 1) 첫 Work 위임(정상) → owner=12, '완료 응답'까지 닫혀 delivered로 기록됨
    asyncio.run(tools["request"].handler({"to_id": "12", "kind": "Work", "body": "구현"}))
    assert f.comm.delivered_work(11, 12) and f.current.owner == 12
    # 위임 본문은 Goal을 계약으로 안고 owner에게 전달된다(리더 스펙 리파인이 아니라 목표가 계약)
    assert any("Goal" in b for _, b in waked)
    # 2) 같은 owner에 또 Work × 2 → Redo로 처리(여전히 깨워 '보완' 가능), history에 redo 2건
    asyncio.run(tools["request"].handler({"to_id": "12", "kind": "Work", "body": "결함A 고쳐"}))
    asyncio.run(tools["request"].handler({"to_id": "12", "kind": "Work", "body": "결함B 고쳐"}))
    assert sum(1 for ev in f.comm.history if ev[0] == "redo") == 2
    # 3) 한계(2) 초과 → 거부(반복 위임 차단), 동료를 더 깨우지 않음
    n_before = len(waked)
    r = asyncio.run(tools["request"].handler({"to_id": "12", "kind": "Work", "body": "또 고쳐"}))
    assert "한도" in r["content"][0]["text"] and len(waked) == n_before
    # 4) 새 Task를 열면 추적이 초기화 → 같은 동료라도 다시 '첫 위임'(다른 산출물)
    f.current.verified = True
    asyncio.run(tools["complete_task"].handler({"result": "ok"}))
    asyncio.run(tools["create_task"].handler({"purpose": "p2", "members": "12"}))
    assert not f.comm.delivered_work(11, 12)


def test_같은턴_병렬중복요청은_합쳐서_재호출안함():
    """같은 턴에 같은 동료에게 같은 요청을 다발로 보내면(병렬 중복), 동료를 다시 깨우지 않고 직전
    응답을 재사용한다 — 반사적 중복 wake를 구조적으로 차단(서로 다른 동료 병렬요청은 직렬화·거부 아님)."""
    g = FakeGuide()
    f = _flow(g)                                   # leader 11, member 12; alive=11
    tools = _tools(f, 11, "leader")
    asyncio.run(tools["create_task"].handler({"purpose": "p", "members": "12"}))
    f.current.participated.add(12)
    asyncio.run(tools["set_goal"].handler({"goal": "g"}))
    waked = []

    async def wake(to, b, k):
        waked.append(to)
        return "동료응답"

    f.wake = wake
    r1 = asyncio.run(tools["request"].handler({"to_id": "12", "kind": "Info", "body": "질문"}))
    assert waked == [12] and "동료응답" in r1["content"][0]["text"]   # 1차: 동료 깸·응답 캐시
    r2 = asyncio.run(tools["request"].handler({"to_id": "12", "kind": "Info", "body": "질문"}))
    assert waked == [12]                                              # 2차: 다시 깨우지 않음(합침)
    assert "재사용" in r2["content"][0]["text"] and "동료응답" in r2["content"][0]["text"]


def test_owner_미착수면_허위완료_차단_실작업후_완료허용():
    """사용자가 잡은 '허위 완료' 차단: owner에게 Work를 위임했는데 owner가 아무 실작업(run/Write) 없이
    곧장 반환(착수 전/계획만, response 사실상 빈)하면 — ① request가 '대신 구현·완료 말라'고 안내하고
    ② owner_delivered=False라 complete_task가 거부된다(owner 일하는 중/응답 전 리더 대리 허위완료 금지).
    owner가 실제로 일하면(act_count↑) owner_delivered=True가 되어 완료가 허용된다. 미착수는 delivered로
    기록 안 돼 재위임이 Redo 한도에 안 걸린다(실제 첫 인도 기회 보장)."""
    g = FakeGuide()
    f = _flow(g)
    worked = {"on": False}

    async def wake(to, b, k):
        if worked["on"]:
            f.act_count += 1                 # owner가 실제로 run/Write 함(훅이 집계하는 신호를 모의)
            return "구현하고 run으로 검증 완료"
        return "네, 곧 시작하겠습니다"          # 착수 전 — 실작업 0회

    f.wake = wake
    t = {x.name: x for x in make_guide_tools(f, 11, "leader")}
    asyncio.run(t["create_task"].handler({"members": "12"}))
    f.current.participated.add(12)
    asyncio.run(t["set_goal"].handler({"purpose": "프론트", "goal": "public/ 동작"}))
    # 1) owner가 실작업 없이 반환 → '대신 하지 말라' 안내, owner_delivered=False, delivered 기록 안 됨
    r = asyncio.run(t["request"].handler({"to_id": "12", "kind": "Work", "body": "public/ 구현"}))
    assert f.current.owner == 12 and f.current.owner_delivered is False
    assert "대신 구현" in r["content"][0]["text"]
    assert not f.comm.delivered_work(11, 12)        # 미착수 → delivered 아님(재위임은 첫 인도)
    # 2) 이 상태로 complete 시도 → 거부(허위 완료 차단) — 리더가 verified를 채워도 owner 인도 전엔 못 닫음
    f.current.verified = True
    rc = asyncio.run(t["complete_task"].handler({"result": "리더가 대신 완료"}))
    assert "완료 거부" in rc["content"][0]["text"] and f.current is not None
    # 3) owner가 실제로 일하고 응답 → owner_delivered=True → 완료 허용
    worked["on"] = True
    asyncio.run(t["request"].handler({"to_id": "12", "kind": "Work", "body": "public/ 구현"}))
    assert f.current.owner_delivered is True
    assert not any(ev[0] == "redo" for ev in f.comm.history)   # 첫 인도라 Redo 아님
    rc2 = asyncio.run(t["complete_task"].handler({"result": "owner 검증 완료"}))
    assert "마감" in rc2["content"][0]["text"] and f.current is None


def test_미완owner_Task는_완료거부_이어가기는_Redo아님():
    """owner가 '턴 한도'로 미완 반환하면 그 Task는 완료 거부(허위완료→다음Task churn 차단). 같은 owner
    재위임은 '이어가기'라 Redo 아님(미완은 delivered로 안 침 → 횟수 제한 무관)."""
    g = FakeGuide()
    f = _flow(g)
    st = {"n": 0}

    async def wake(to, b, k):
        st["n"] += 1
        if st["n"] == 1:
            return "작업 중 (⚠ 턴 한도 도달 — 작업이 미완일 수 있음)"   # 1차: 턴 한도로 미완 반환
        f.act_count += 1                                            # 2차(이어가기): owner가 실제로 마저 작업
        return "완료"

    f.wake = wake
    t = {x.name: x for x in make_guide_tools(f, 11, "leader")}
    asyncio.run(t["create_task"].handler({"members": "12"}))
    f.current.participated.add(12)
    asyncio.run(t["set_goal"].handler({"purpose": "p", "goal": "g"}))
    asyncio.run(t["request"].handler({"to_id": "12", "kind": "Work", "body": "구현"}))   # 1차 → 미완 반환
    assert f.current.owner_incomplete is True and not f.comm.delivered_work(11, 12)
    f.current.verified = True
    r = asyncio.run(t["complete_task"].handler({"result": "끝"}))
    assert "완료 거부" in r["content"][0]["text"] and "미완" in r["content"][0]["text"]   # 미완 → 완료 거부
    asyncio.run(t["request"].handler({"to_id": "12", "kind": "Work", "body": "이어서"}))   # 이어가기(완료 반환)
    assert not any(ev[0] == "redo" for ev in f.comm.history)        # 이어가기는 Redo 아님
    assert f.current.owner_incomplete is False                      # 완료 반환 → 미완 해제
    assert f.current.owner_delivered is True                        # 실작업 인도됨 → 완료 가능
    r2 = asyncio.run(t["complete_task"].handler({"result": "끝"}))
    assert "마감" in r2["content"][0]["text"] and f.current is None   # 이제 완료 마감 허용


def test_되묻기후_재위임은_Redo아님():
    """owner가 '되묻기(clarify)'만 하고 반환하면 미완이므로, 위임자가 다시 맡기는 건 '첫 구현'이지 Redo가 아니다."""
    g = FakeGuide()
    f = _flow(g)
    calls = {"n": 0}

    async def wake(to, b, k):
        calls["n"] += 1
        if calls["n"] == 1:                      # 1차: 되묻기만 남기고 반환(미완)
            f.pending_clarify = {"from": 12, "to": 11, "q": "필드명?"}
            return "(짧게 반환)"
        return "완료"                            # 2차: 실제 완료

    f.wake = wake
    tools = _tools(f, 11, "leader")
    asyncio.run(tools["create_task"].handler({"purpose": "p", "members": "12"}))
    f.current.participated.add(12)
    asyncio.run(tools["set_goal"].handler({"goal": "g"}))
    asyncio.run(tools["request"].handler({"to_id": "12", "kind": "Work", "body": "구현"}))   # 되묻기 → 미완
    assert not f.comm.delivered_work(11, 12)                       # 완료 아님 → delivered 기록 안 됨
    r = asyncio.run(tools["request"].handler({"to_id": "12", "kind": "Work", "body": "구현(답 반영)"}))
    assert not any(ev[0] == "redo" for ev in f.comm.history)       # 재위임이지만 Redo 아님
    assert "응답" in r["content"][0]["text"]
