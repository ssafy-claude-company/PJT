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
    f.gap_checked = True   # P7 범주적 완성 점검 보류를 테스트 기본 우회(전용 테스트만 False로 검증)
    f.percept_checked = True  # 지각 비대칭 점검(complete) 보류도 기본 우회(전용 테스트만 False로 검증)
    f.acceptance_checked = True  # 수용 계약 마감 게이트 보류도 기본 우회(전용 테스트만 False로 검증)
    f.authorship_checked = True  # 저작 다양성 게이트 보류도 기본 우회(전용 테스트만 False로 검증)
    f.decomp_checked = True  # 분해 점검 보류도 기본 우회(전용 테스트만 False로 검증)
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
                     "create_project", "create_task", "set_goal", "complete_task", "deploy",
                     "vote", "meet", "parallel_work"}   # Discord 심화 대화: 표결·회의(1R 동시 수집). 경쟁 구현은
                                       # 사용자 판단으로 제거(같은 모델 중복 비교 — 효과는 협업에서)


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
    # [일로 직업 획득 — 영속 이연] 채용 시점엔 *잠정*(런타임 라벨만) — 아직 영속(persist) 안 됨. 첫 실작업 때 영속.
    assert hired in f.tentative_roles and persisted.get(hired) is None
    # 1봇 1직업: 이미 직군('게임 기획자') 있는 봇에 다른 직군 추가 → 거부(겸직 폐지), 직군 그대로
    r2 = asyncio.run(t["recruit"].handler({"member": str(hired), "role": "레벨 디자이너"}))
    assert "거부" in r2["content"][0]["text"] and "1봇 1직업" in r2["content"][0]["text"]
    assert f.bot_info[hired] == "게임 기획자"
    # 남은 예비를 'UX 디자이너'로, 그 뒤 예비 소진 → 채용 불가 안내
    asyncio.run(t["recruit"].handler({"role": "UX 디자이너", "reason": "UX"}))
    r3 = asyncio.run(t["recruit"].handler({"role": "사운드", "reason": "x"}))
    assert "못 찾음" in r3["content"][0]["text"]


def test_일로직업획득_채용은잠정_첫실작업에_영속승격():
    """[일로 직업 획득 — 양산 근본 차단] 예비→직군 채용은 *잠정*(런타임 라벨만)이고, 그 봇이 *첫 실작업*(run/
    Write)을 한 순간에만 직군이 영속(persist jobs.json + Discord 부여 대기열)된다 — '직업=기억'을 문자 그대로.
    일 안 하면 영속 안 돼 '0-기억 직군'이 구조적으로 안 생긴다(양산 래칫·이중채용 충돌의 근본 차단)."""
    from src.permissions import make_pre_tool_use_hook, organt_allowed_tools
    g = FakeGuide()
    f = Flow(g, channel_id=500, guild_id=1, leader_id=11, bot_info={11: "백엔드", 13: "예비"})
    f.start_root("root")
    persisted = {}
    f.persist_role = lambda mid, role: persisted.__setitem__(mid, role)
    t = {x.name: x for x in make_guide_tools(f, 11, "leader")}
    asyncio.run(t["create_task"].handler({"members": ""}))
    # 예비 13을 게임 기획자로 채용 → 잠정(런타임 라벨만, 영속 X)
    asyncio.run(t["recruit"].handler({"role": "게임 기획자", "reason": "x"}))
    assert 13 in f.tentative_roles and persisted.get(13) is None    # 잠정·미영속
    assert f.bot_info[13] == "게임 기획자"                          # 런타임 라벨은 설정(이 흐름에서 활동 가능)
    # 13이 첫 실작업(run) → 권한 훅이 영속으로 승격
    class _A:
        def record(self, *a, **k):
            pass
    hook = make_pre_tool_use_hook(_A(), organt_allowed_tools(["mcp__guide__run"]),
                                  actor=13, role="게임 기획자", flow=f)
    asyncio.run(hook({"tool_name": "mcp__guide__run", "tool_input": {}}, "tid", None))
    assert persisted.get(13) == "게임 기획자"           # 첫 실작업으로 jobs.json 영속됨
    assert 13 not in f.tentative_roles                  # 잠정 해제(획득 완료)
    assert (13, "게임 기획자") in f.role_earned_queue    # Discord 역할 부여 대기열 등록(SYS가 비동기 드레인)
    # 영속은 1회만 — 두 번째 작업엔 재영속 안 함
    persisted.clear(); f.role_earned_queue.clear()
    asyncio.run(hook({"tool_name": "mcp__guide__run", "tool_input": {}}, "tid", None))
    assert persisted.get(13) is None and not f.role_earned_queue


def test_네이티브도구_거부에_Organt_대체도구_안내():
    """봇(Claude)이 본능적으로 집는 네이티브 도구(Bash/Agent/TaskList…)를 거부할 때 '대신 이걸 써라'를
    안내한다 — 라이브: '권한 밖 도구' 거부 359건(대부분 Bash), Bash 거부의 74%가 run으로 복귀 못 하고
    표류. 친절한 redirect로 즉시 올바른 도구로 유도(본능을 이기지 말고 받아서 돌린다)."""
    from src.permissions import make_pre_tool_use_hook, organt_allowed_tools

    class _A:
        def record(self, *a, **k):
            pass
    hook = make_pre_tool_use_hook(_A(), organt_allowed_tools(["mcp__guide__run"]), actor=12, role="백엔드")
    r = asyncio.run(hook({"tool_name": "Bash", "tool_input": {"command": "ls"}}, "tid", None))
    out = r["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny" and "run" in out["permissionDecisionReason"]   # Bash → run
    r2 = asyncio.run(hook({"tool_name": "Agent", "tool_input": {}}, "tid", None))
    assert "request" in r2["hookSpecificOutput"]["permissionDecisionReason"]                   # Agent → request
    r3 = asyncio.run(hook({"tool_name": "FooBar", "tool_input": {}}, "tid", None))             # 미지 도구는
    assert r3["hookSpecificOutput"]["permissionDecision"] == "deny"                            # 종전대로 거부(안 깨짐)


def test_채용직업_기억_다음흐름_유지():
    """recruit로 부여한 직군은 _roster_labels에 기록돼, 새 흐름 시작 시 reset 후에도 유지된다 — '직업 고정·기억'
    (예비가 한 번 직업을 받으면 매 흐름 예비로 원복되지 않고 그 직업군을 누적·재사용)."""
    s = Sys(FakeGuide(), guild_id=1, organt_builder=None, bot_info={11: "백엔드", 13: "예비"})
    s._roster_labels.__setitem__(13, "게임 기획자")   # handle_user_input이 거는 persist_role과 동일 동작
    s.bot_info.clear(); s.bot_info.update(s._roster_labels)   # 새 흐름 reset 경로
    assert s.bot_info[13] == "게임 기획자" and s.bot_info[11] == "백엔드"   # 예비→게임기획자 유지


def test_예비담당자_Task전_자기직군_확정():
    """'예비' 담당자는 Task를 열기 전에 recruit(member=자신, role=…)로 자기 직군부터 정할 수 있다 — 이래야
    '예비'인 채로 프로젝트/Task를 열어 화면에 '예비'로 박히지 않는다(사용자가 본 '담당자가 예비로 들어옴' 차단).
    단 '다른 사람' 채용은 종전대로 Task가 먼저 있어야 한다(자기직군만 Task 전 허용)."""
    g = FakeGuide()
    f = Flow(g, channel_id=500, guild_id=1, leader_id=11, bot_info={11: "예비", 12: "백엔드"})
    f.start_root("root")
    persisted = {}
    f.persist_role = lambda mid, role: persisted.__setitem__(mid, role)
    t = {x.name: x for x in make_guide_tools(f, 11, "leader")}
    # Task 없음 + 자기 자신 + role → 자기 직군 확정(허용)
    r = asyncio.run(t["recruit"].handler({"member": "11", "role": "게임 기획자"}))
    assert "자기 직군 확정" in r["content"][0]["text"]
    assert f.bot_info[11] == "게임 기획자" and persisted.get(11) == "게임 기획자"   # 기억에도 반영
    # Task 없이 '다른 사람' 채용은 여전히 거부(Task 먼저)
    r2 = asyncio.run(t["recruit"].handler({"member": "12", "role": "QA"}))
    assert "진행 중인 Task가 없습니다" in r2["content"][0]["text"] and f.bot_info[12] == "백엔드"


def test_PM혼자_Task_차단():
    """프로젝트에 동료가 있는데 리더 혼자만 멤버로 Task를 열면 거부 — 'PM 혼자 Task'(팀 버리고 단독작업·독식)
    차단. members로 동료를 넣으면 통과. 동료가 없는 1인 프로젝트는 솔로 허용(거짓양성 없음)."""
    g = FakeGuide()
    f = Flow(g, channel_id=500, guild_id=1, leader_id=11, bot_info={11: "백엔드", 12: "프론트엔드"})
    f.start_root("root")
    t = {x.name: x for x in make_guide_tools(f, 11, "leader")}
    r = asyncio.run(t["create_task"].handler({"members": "11"}))          # 동료 있는데 리더만 → 거부
    assert "단독 Task 거부" in r["content"][0]["text"] and f.current is None
    asyncio.run(t["create_task"].handler({"members": "12"}))              # 동료 넣으면 통과
    assert f.current is not None and set(f.current.team) == {11, 12}
    # 동료 없는 1인 프로젝트는 솔로 허용
    f1 = Flow(g, channel_id=501, guild_id=1, leader_id=11, bot_info={11: "백엔드"})
    f1.start_root("root")
    t1 = {x.name: x for x in make_guide_tools(f1, 11, "leader")}
    asyncio.run(t1["create_task"].handler({"members": ""}))
    assert f1.current is not None


def test_같은직군_증원_자유채용_허용():
    """같은 직군이어도 필요에 따라 증원 채용을 허용한다 — role 중복/실패상태로 거부하지 않는다(사용자 지적:
    중요한 직군은 더 뽑을 수 있어야 함). 반복 채용의 진짜 원인(무응답=서브프로세스 행)은 워커 턴 타임아웃으로
    끊었으므로, 채용 자체를 막지 않는다."""
    g = FakeGuide()
    f = Flow(g, channel_id=500, guild_id=1, leader_id=11,
             bot_info={11: "백엔드", 12: "프론트엔드", 13: "예비", 14: "예비"})
    f.start_root("root")
    t = {x.name: x for x in make_guide_tools(f, 11, "leader")}
    asyncio.run(t["create_project"].handler({"name": "p", "team": "12"}))   # 팀: 백엔드(11)+프론트(12)
    asyncio.run(t["create_task"].handler({"members": "12"}))
    # 이미 프론트엔드(12)가 있어도 같은 직군 증원 → 허용
    r = asyncio.run(t["recruit"].handler({"role": "프론트엔드", "reason": "프론트 과중 — 증원"}))
    assert "직군으로 채용" in r["content"][0]["text"]
    hired = next(i for i in (13, 14) if f.bot_info[i] == "프론트엔드")
    assert hired in f.current.team and f.bot_info[hired] == "프론트엔드"      # 같은 직군 2명째 합류


def test_워커_턴_타임아웃은_인프라실패로(monkeypatch):
    """워커(비-리더) 턴이 행(무응답)이면 turn_timeout 후 'API Error: timeout'(인프라 실패)로 반환 — 단일흐름
    영구정지 차단(관측: 24분 좀비). 리더 턴은 흐름 전체를 품으므로 타임아웃 안 함(정상 반환 그대로)."""
    monkeypatch.setattr("src.sys_core.build_guide_server", lambda *a, **k: object())

    class _Hang:
        async def handle(self, prompt):
            await asyncio.sleep(5)      # 서브프로세스 행 흉내
            return "done"

    class _Quick:
        async def handle(self, prompt):
            return "리더 결과"

    g = FakeGuide()
    f = Flow(g, channel_id=1, guild_id=1, leader_id=11, bot_info={11: "백엔드", 12: "프론트엔드"})
    f.start_root("root")
    s = Sys(g, guild_id=1, organt_builder=lambda oid, srv, role, flow=None: _Hang(),
            bot_info={11: "백엔드", 12: "프론트엔드"})
    s.turn_timeout = 0.2
    out = asyncio.run(s.run_turn(f, 12, "b", Kind.INFO, "member"))      # 워커 행 → 타임아웃
    assert out.lower().startswith("api error") and "timeout" in out.lower()
    s.organt_builder = lambda oid, srv, role, flow=None: _Quick()       # 리더는 정상 반환
    assert asyncio.run(s.run_turn(f, 11, "b", Kind.WORK, "leader")) == "리더 결과"


def test_무진행_워치독_행은취소_진행중은보호():
    """흐름 워치독: last_activity가 idle_timeout 동안 안 바뀌면(무진행=행) 리더 task를 취소(리더-행 구멍 메움).
    진행 중(last_activity 갱신)이면 idle_timeout보다 오래 걸려도 안 끊는다 — 고정 타임아웃이 아니라 무진행 기준."""
    import time as _t
    s = Sys(FakeGuide(), guild_id=1, organt_builder=None, bot_info={11: "L"})
    s.idle_timeout = 0.5

    class _F:
        pass

    # (1) 행: last_activity 과거 고정 → 무진행 → 취소됨
    fhang = _F(); fhang.last_activity = _t.monotonic() - 100

    async def _hang():
        await asyncio.sleep(10); return "done"

    async def _run_hang():
        return await s._await_with_idle_watchdog(asyncio.create_task(_hang()), fhang)

    cancelled = False
    try:
        asyncio.run(_run_hang())
    except asyncio.CancelledError:
        cancelled = True
    assert cancelled

    # (2) 진행 중: last_activity 계속 갱신 → timeout(0.5s)보다 오래(1.5s) 걸려도 완료
    fact = _F(); fact.last_activity = _t.monotonic()

    async def _active():
        for _ in range(15):
            await asyncio.sleep(0.1); fact.last_activity = _t.monotonic()
        return "ok"

    async def _run_active():
        return await s._await_with_idle_watchdog(asyncio.create_task(_active()), fact)

    assert asyncio.run(_run_active()) == "ok"


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


def test_원문요청_흐름별격리_동시흐름_교차오염없음():
    """[교차오염 차단] 동시 흐름이 두 개 돌 때, 각 흐름의 봇 프롬프트엔 '자기 흐름의 사용자 원문'만
    주입돼야 한다. 과거엔 원문이 SYS 전역 단일 필드(self._origin_request)였어서, 흐름 A가 진행 중인데
    흐름 B 개입이 도착하면 전역이 덮어써져 흐름 A의 봇이 '흐름 B의 원문'을 진짜 의도로 받았다(라이브:
    웹 프로젝트 리더가 게임 개입 원문 '게임성을 강화해'를 받아 게임을 짓기 시작 → 웹 흐름에 게임 난입).
    이제 원문은 흐름 객체에 박제되고 _prompt가 흐름의 것을 읽으므로 전역이 덮어써져도 격리된다."""
    s = Sys(FakeGuide(), guild_id=1, organt_builder=None, bot_info={11: "프론트엔드", 12: "게임 기획자"})
    fa = _flow(s.guide, leader=11)            # 흐름 A: 웹
    fa.origin_request = "지방선거 공공데이터로 웹 사이트 만들어줘"
    fb = _flow(s.guide, leader=12)            # 흐름 B: 게임
    fb.origin_request = "게임성을 강화해 사운드 이펙트 디자인 다 챙겨"
    # 흐름 B 개입이 전역 필드를 덮어쓴 상태(가장 최근 개입) — 과거 버그의 트리거 조건 재현
    s._origin_request = "게임성을 강화해 사운드 이펙트 디자인 다 챙겨"
    # 흐름 A의 봇 프롬프트: 전역이 게임으로 덮였어도 '웹 원문'만 보여야 한다
    pa = s._prompt("x", Kind.WORK, "leader", 11, leader_id=11, flow=fa)
    assert "지방선거 공공데이터로 웹 사이트 만들어줘" in pa
    assert "게임성을 강화해" not in pa        # ← 핵심: 게임 원문이 웹 흐름에 새지 않음
    # 흐름 B의 봇 프롬프트: 자기 게임 원문을 본다
    pb = s._prompt("x", Kind.WORK, "leader", 12, leader_id=12, flow=fb)
    assert "게임성을 강화해" in pb and "지방선거" not in pb


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
    f.gap_checked = True; f.decomp_checked = True   # P7 범주·분해 점검 보류 우회(이 테스트 범위 밖)
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
    f.gap_checked = True; f.decomp_checked = True   # P7 범주·분해 점검 보류(이 테스트는 participated 게이트 검증)
    f.percept_checked = True   # 지각 비대칭 점검 보류 우회(범위 밖)
    f.acceptance_checked = True   # 수용 계약 게이트 보류 우회(범위 밖)
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


def test_set_goal_유화적_타흐름점유멤버는_협의면제_교착차단():
    """[유화적 전원협의 — 무한 루프 차단] 미참여 멤버가 '지금 타 흐름에 점유(busy_elsewhere)'돼 도달 불가하면
    set_goal이 그 멤버 협의를 면제하고 진행한다 — 라이브 P-002 114305-1: 프론트 4명 중 1명이 내내 타 프로젝트
    (P-013)를 리드 중이라 1봇=1흐름 배타로 P-002엔 못 와 협의 33회 거부·200분 교착. 가용 멤버는 전원 협의하되,
    도달 불가 멤버 때문에 영영 못 막히게(유화적)."""
    from src.communication import Engagement
    g = FakeGuide()
    f = Flow(g, channel_id=500, guild_id=1, leader_id=11, bot_info={11: "L", 12: "백엔드", 13: "프론트엔드"})
    f.start_root("root")
    f.gap_checked = True; f.percept_checked = True; f.acceptance_checked = True; f.decomp_checked = True
    eng = Engagement()
    f.comm.attach_engagement(eng, scope="P-THIS")
    logged = []
    f.log = lambda ev, **kw: logged.append((ev, kw))
    t = {x.name: x for x in make_guide_tools(f, 11, "leader")}
    asyncio.run(t["create_project"].handler({"name": "p", "team": "12,13"}))
    asyncio.run(t["create_task"].handler({"purpose": "서버", "members": "12,13"}))
    eng.engage(13, "P-OTHER")                        # 13(프론트)은 내내 다른 흐름 점유 — 도달 불가
    f.current.participated.add(12)                    # 가용한 12는 협의 완료, 13만 미참여(점유)
    r = asyncio.run(t["set_goal"].handler({"goal": "동작"}))
    assert f.current.status.goal == "동작"             # 도달 불가 도메인 면제 → 진행(교착 차단)
    assert "면제" in r["content"][0]["text"]            # 면제 안내
    assert any(ev == "set_goal_consensus_coverage" and "프론트엔드" in kw.get("uncovered_busy", []) for ev, kw in logged)


def test_set_goal_같은직군_잉여는_합의면제_에코방지():
    """[동질 모델 원리] 같은 Claude·같은 직군 봇 둘은 0 다양성(에코)이라 합의엔 직군당 1명이면 충분. 같은
    직군 잉여(그 도메인에 이미 참여자 있음)는 합의 면제(에코·과대소집·합의편향 방지) — 잉여는 병렬 실행용.
    라이브: meet 57%가 같은 직군 중복(백엔드×3). 단, *다른* 도메인 누락은 에코가 아니라 진짜 공백 → 거부."""
    g = FakeGuide()
    f = Flow(g, channel_id=500, guild_id=1, leader_id=11, bot_info={11: "L", 12: "백엔드", 13: "백엔드", 14: "프론트엔드"})
    f.start_root("root")
    f.gap_checked = True; f.percept_checked = True; f.acceptance_checked = True; f.decomp_checked = True
    logged = []; f.log = lambda ev, **kw: logged.append((ev, kw))
    t = {x.name: x for x in make_guide_tools(f, 11, "leader")}
    asyncio.run(t["create_project"].handler({"name": "p", "team": "12,13,14"}))
    asyncio.run(t["create_task"].handler({"members": "12,13,14"}))
    f.current.participated.update({12, 14})                       # 백엔드 1명(12)+프론트(14); 백엔드 13은 잉여
    r = asyncio.run(t["set_goal"].handler({"goal": "동작"}))
    assert f.current.status.goal == "동작"                         # 직군 커버(백엔드·프론트 각 1명) → 통과, 13 불필요
    assert any(ev == "set_goal_consensus_coverage" and 13 in kw.get("redundant", []) for ev, kw in logged)
    # 대조: 프론트(14) 미참여면 프론트 도메인 *미커버* → 거부(에코 아님, 진짜 도메인 누락)
    g2 = FakeGuide()
    f2 = Flow(g2, channel_id=501, guild_id=1, leader_id=11, bot_info={11: "L", 12: "백엔드", 13: "백엔드", 14: "프론트엔드"})
    f2.start_root("r2"); f2.gap_checked = True; f2.percept_checked = True; f2.acceptance_checked = True; f2.decomp_checked = True
    t2 = {x.name: x for x in make_guide_tools(f2, 11, "leader")}
    asyncio.run(t2["create_project"].handler({"name": "p2", "team": "12,13,14"}))
    asyncio.run(t2["create_task"].handler({"members": "12,13,14"}))
    f2.current.participated.update({12, 13})                      # 백엔드 2명만 — 프론트 도메인 누락
    r2 = asyncio.run(t2["set_goal"].handler({"goal": "동작"}))
    assert "거부" in r2["content"][0]["text"] and "프론트엔드" in r2["content"][0]["text"]   # 프론트 미커버 → 거부


def test_set_goal_가용한_미참여멤버는_여전히_협의요구():
    """유화적 면제는 '타 흐름 점유'에만 적용 — 지금 가용(reachable)한 미참여 멤버는 여전히 협의해야 통과한다
    (최대한 다 받기는 유지). 점유도 아닌데 면제하면 '한 명만 묻고 확정'하는 리더 독단이 부활한다."""
    from src.communication import Engagement
    g = FakeGuide()
    f = Flow(g, channel_id=500, guild_id=1, leader_id=11, bot_info={11: "L", 12: "백엔드", 13: "프론트엔드"})
    f.start_root("root")
    f.gap_checked = True; f.percept_checked = True; f.acceptance_checked = True; f.decomp_checked = True
    eng = Engagement()
    f.comm.attach_engagement(eng, scope="P-THIS")    # 13은 어디에도 점유 안 됨(가용)
    t = {x.name: x for x in make_guide_tools(f, 11, "leader")}
    asyncio.run(t["create_project"].handler({"name": "p", "team": "12,13"}))
    asyncio.run(t["create_task"].handler({"purpose": "서버", "members": "12,13"}))
    f.current.participated.add(12)                    # 12만 협의, 13은 가용한데 미협의
    r = asyncio.run(t["set_goal"].handler({"goal": "동작"}))
    assert "거부" in r["content"][0]["text"] and not f.current.status.goal   # 13 가용·미협의 → 거부


def test_set_goal_분해점검_다도메인_1회보류_단일도메인_스킵():
    """[하이브리드 — 중앙 고수준 분해 + 지역 자율] 목표가 ≥2 독립 도메인에 걸치면 set_goal이 1회 보류하고
    '도메인별 Task로 나눠 각 전문가가 owner+검증'을 유도(검증갭·오케스트레이터 단일점 지능 병목 차단 —
    외부 연구의 hybrid). 단일 도메인은 분해 무의미라 스킵. 매직넘버 아님(도메인 ≥2 구조신호), 1회 보류 후 통과."""
    g = FakeGuide()
    f = Flow(g, channel_id=500, guild_id=1, leader_id=11, bot_info={11: "L", 12: "백엔드", 13: "VFX 전문가"})
    f.start_root("root")
    f.gap_checked = True; f.percept_checked = True; f.acceptance_checked = True   # decomp만 검증(나머지 보류 우회)
    logged = []; f.log = lambda ev, **kw: logged.append((ev, kw))
    t = {x.name: x for x in make_guide_tools(f, 11, "leader")}
    asyncio.run(t["create_project"].handler({"name": "p", "team": "12,13"}))
    asyncio.run(t["create_task"].handler({"purpose": "서버+VFX", "members": "12,13"}))
    f.current.participated.update({12, 13})
    r1 = asyncio.run(t["set_goal"].handler({"goal": "스킬 시스템"}))
    assert "분해 점검" in r1["content"][0]["text"] and not f.current.status.goal   # 2도메인 → 1회 보류
    assert any(ev == "set_goal_decomp_check" for ev, kw in logged)
    r2 = asyncio.run(t["set_goal"].handler({"goal": "스킬 시스템"}))               # 재호출
    assert f.current.status.goal == "스킬 시스템"                                 # 1회뿐 — 통과
    # 단일 도메인(백엔드×2 = 1도메인)은 보류 없이 통과 — 새 흐름
    f2 = Flow(g, channel_id=501, guild_id=1, leader_id=11, bot_info={11: "L", 12: "백엔드", 13: "백엔드"})
    f2.start_root("r2"); f2.gap_checked = True; f2.percept_checked = True; f2.acceptance_checked = True
    t2 = {x.name: x for x in make_guide_tools(f2, 11, "leader")}
    asyncio.run(t2["create_project"].handler({"name": "p2", "team": "12,13"}))
    asyncio.run(t2["create_task"].handler({"members": "12,13"}))
    f2.current.participated.update({12, 13})
    r3 = asyncio.run(t2["set_goal"].handler({"goal": "백엔드만"}))
    assert f2.current.status.goal == "백엔드만"                                   # 1도메인 → 보류 없이 통과


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
    f.percept_checked = True   # 지각 비대칭 점검 보류 우회(이 테스트는 팀 동적선정 검증)
    f.acceptance_checked = True   # 수용 계약 게이트 보류 우회(범위 밖)
    asyncio.run(t["complete_task"].handler({"result": "ok"}))
    asyncio.run(t["create_task"].handler({"members": ""}))        # 비우면 프로젝트팀(11,12,13) 기본 — 14는 안 부름
    assert set(f.current.team) == {11, 12, 13} and 14 not in f.current.team


def test_create_task_기본팀은_직군당_1명_비대차단():
    """[팀 비대 차단 — 라이브 2026-06-14: 역할 드리프트로 백엔드 5명이 기본 팀에 다 들어와 set_goal
    전원협의×비대로 meet 4회·6 잠수·136분 미수렴]. members= 없이 create_task하면 기본 팀은 **직군당 1명**
    (실행 핵심·단일 owner 보편 이치) — 같은 직군 중복은 기본에서 제외(recruit/members=로 추가). 명시
    members=는 중복도 그대로 존중(리더가 일부러 고른 것)."""
    from collections import Counter
    g = FakeGuide()
    f = Flow(g, channel_id=500, guild_id=1, leader_id=11,
             bot_info={11: "L", 12: "백엔드", 13: "백엔드", 14: "백엔드", 15: "프론트엔드", 16: "프론트엔드", 17: "QA"})
    f.start_root("root"); f.project_team = [11, 12, 13, 14, 15, 16, 17]
    t = {x.name: x for x in make_guide_tools(f, 11, "leader")}
    asyncio.run(t["create_task"].handler({}))                     # members 없음 → 기본팀(직군당 1명)
    c = Counter(f._info(m) for m in f.current.team if m != 11)
    assert c["백엔드"] == 1 and c["프론트엔드"] == 1 and c["QA"] == 1   # 백엔드 3→1, 프론트 2→1
    assert len(f.current.team) == 4                                # 리더 + 3직군 각 1명(비대 차단)
    # 명시 members=는 중복 직군도 존중 — 새 흐름으로 확인
    f2 = Flow(g, channel_id=501, guild_id=1, leader_id=11, bot_info={11: "L", 12: "백엔드", 13: "백엔드"})
    f2.start_root("r2"); f2.project_team = [11, 12, 13]
    t2 = {x.name: x for x in make_guide_tools(f2, 11, "leader")}
    asyncio.run(t2["create_task"].handler({"members": "12,13"}))  # 백엔드 2명 명시
    assert set(f2.current.team) == {11, 12, 13}                   # 명시하면 중복도 그대로(자율)


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
    f.current.cross_checks = f.current.cross_check_offdomain = 1                    # 검증 분업 게이트(별도 테스트)와 무관한 의도 보존
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


def test_프로젝트_등록과_채널개입_라우팅(tmp_path):
    """create_project → 식별번호 등록 + 흐름 임시 폴더(new-…)가 **p-00n-슬러그로 개명**(신원=번호 —
    사용자 제안). 등록된 채널에 다시 명령 → '개입'으로 라우팅되어 그 id-작업공간을 그대로 잇는다."""
    import os as _os
    base = str(tmp_path)
    g = FakeGuide()
    s = Sys(g, guild_id=1, organt_builder=None, bot_info={11: "L", 12: "M"}, workspace=base)
    f = Flow(g, channel_id=500, guild_id=1, leader_id=11, bot_info={11: "L", 12: "M"})
    f.workspace = _os.path.join(base, "new-1")
    _os.makedirs(f.workspace)

    def _reg(ch, name):                          # Sys가 흐름에 거는 것과 같은 배선(개명 결과 채택)
        pid = s._register_project(ch, name, f.workspace, f.leader)
        f.workspace = s.projects[int(ch)]["workspace"]
        return pid
    f.register_project = _reg
    f.start_root("root")
    t = {x.name: x for x in make_guide_tools(f, 11, "leader")}
    asyncio.run(t["create_project"].handler({"name": "스네이크", "team": "12"}))   # 채널 9001 생성
    pid = s.projects[9001]["id"]
    ws = s.projects[9001]["workspace"]
    assert pid.startswith("P-") and ws.endswith(f"{pid.lower()}-스네이크")        # 신원=번호 개명
    assert f.workspace == ws and _os.path.isdir(ws)                              # 흐름도 채택·실재
    assert not _os.path.exists(_os.path.join(base, "new-1"))                     # 임시 이름 소멸

    captured = {}
    async def fake_run_turn(flow, oid, body, kind, role):
        captured["flow"], captured["body"] = flow, body
        return "done"
    s.run_turn = fake_run_turn
    asyncio.run(s.handle_user_input(9001, 11, "즉사 버그 고쳐", root_id=None))   # 등록 채널 명령
    fl = captured["flow"]
    assert fl.intervention and fl.project_id == pid                              # 개입으로 인식
    assert fl.project_channel == 9001 and fl.workspace == ws                     # id-작업공간 유지
    assert "개입" in captured["body"] and "즉사 버그 고쳐" in captured["body"]
    # 미등록 채널의 신규 흐름은 시작부터 고유 임시 폴더(루트 노출 차단 — 타 프로젝트 안 보임)
    asyncio.run(s.handle_user_input(777, 11, "새 일", root_id=None))
    nw = captured["flow"].workspace
    assert captured["flow"].intervention is None
    assert _os.path.basename(nw).startswith("new-") and _os.path.dirname(nw) == base


def test_프로젝트_레지스트리_영속과_중복방지(tmp_path):
    """레지스트리를 디스크에 영속 → 프로세스가 끝나도 '원래 프로젝트'에 개입 가능. 같은 이름은 재사용."""
    p = str(tmp_path / "projects.json")
    s1 = Sys(FakeGuide(), guild_id=1, organt_builder=None, bot_info={11: "L"}, projects_path=p)
    pid = s1._register_project(9001, "스네이크", "/ws", 11)
    # 같은 이름은 새 채널이어도 식별번호 '그대로 유지' + 채널만 갱신(번호 증가/중복 금지)
    assert s1._register_project(9999, "스네이크", "/ws2", 11) == pid
    assert 9999 in s1.projects and 9001 not in s1.projects     # 채널만 현재 것으로 이동
    assert s1.projects[9999]["id"] == pid and s1.projects[9999]["workspace"] == "/ws"   # 연장=기존 폴더 유지
    # 새 프로세스(새 Sys)가 같은 파일 로드 → 갱신된 채널·식별번호 그대로 복원
    s2 = Sys(FakeGuide(), guild_id=1, organt_builder=None, bot_info={11: "L"}, projects_path=p)
    assert 9999 in s2.projects and s2.projects[9999]["id"] == pid
    assert s2.projects[9999]["workspace"] == "/ws"   # 연장=기존 작품 폴더 그대로(덮지 않음)


def test_신규끼리_같은리더는_큐_다른리더는_병렬_큐는_접수안내():
    """[신규×신규 완화] 신규 요청은 고유 스코프라 서로 직렬화되지 않는다 — 직렬의 근거는 스코프가
    아니라 전역 점유(같은 리더)다. 같은 리더면 큐(+'⏸ 접수됨' 안내 즉시 표시 — 침묵하는 큐 금지),
    다른 리더면 새 프로젝트 둘이 동시에 뜬다(라이브: 두 리더 병렬 의도가 main 직렬에 좌절+무표시)."""
    g = FakeGuide()
    s = Sys(g, guild_id=1, organt_builder=None, bot_info={11: "L", 12: "기획"},
            workspace="/tmp/ws-x")
    gate = asyncio.Event()
    started = []

    async def fake_run_turn(flow, oid, body, kind, role):
        started.append(oid)
        await gate.wait()
        flow.current = None
        return "ok"
    s.run_turn = fake_run_turn

    async def scenario():
        t1 = asyncio.ensure_future(s.handle_user_input(500, 11, "첫 신규", root_id="r1"))
        await asyncio.sleep(0.05)
        out2 = await s.handle_user_input(500, 11, "같은 리더 신규", root_id="r2")
        assert out2["mode"] == "queued"                       # 같은 리더 → 점유로 큐
        assert any(c[0] == "post" and "⏸ 접수됨" in str(c[3]) for c in g.calls)   # 침묵하지 않는다
        t3 = asyncio.ensure_future(s.handle_user_input(500, 12, "다른 리더 신규", root_id="r3"))
        await asyncio.sleep(0.05)
        assert started == [11, 12]                            # 다른 리더는 동시 진행(병렬)
        gate.set()
        await t1
        await t3
        assert started.count(11) == 2                         # 큐는 종료 후 드레인으로 실행됨
    asyncio.run(scenario())


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
    """새 최상위 요청은 '고유 세션 스코프'로 시작한다 — 이전 흐름의 세션 파일을 아예 읽지 않으므로
    '이미 했다' 앵커링이 구조적으로 차단된다(과거의 전역 삭제 방식을 스코프 분리가 대체).
    프로젝트가 등록되면 흐름 마감 때 그 스코프로 승격(리네임)돼 다음 개입이 기억을 잇는다."""
    sd = tmp_path
    (sd / "organt_state_old-scope_11.json").write_text("{}")        # 이전 흐름의 세션(읽히면 안 됨)
    s = Sys(FakeGuide(), guild_id=1, organt_builder=None, bot_info={11: "L"},
            workspace="/ws", session_dir=str(sd))
    captured = {}

    async def fake_run_turn(flow, oid, body, kind, role):
        captured["scope"] = flow.session_scope
        flow.current = None
        return "ok"
    s.run_turn = fake_run_turn
    asyncio.run(s.handle_user_input(500, 11, "새 요청", root_id=None))
    assert captured["scope"].startswith("new-")                     # 고유 스코프 — 옛 세션과 무관
    assert (sd / "organt_state_old-scope_11.json").exists()         # 옛 파일은 건드리지도 않음


def test_개입은_세션유지_위임기억보존(tmp_path):
    """[근본] 등록된 프로젝트 '개입(이어서/수정)'에선 세션을 지우지 않는다 — 리더·동료가 진행 중이던 팀·위임·
    owner 기억(resume용 session_id)을 잃고 처음부터 다시 계획하는 걸 막는다(=리더가 직전 위임을 무시하고
    팀을 일부만 다시 불러 혼자 마무리하던 행동의 근본 차단). 새 요청에만 reset, 개입엔 keep."""
    sd = tmp_path
    (sd / "organt_state_11.json").write_text('{"session_id": "S11"}')
    (sd / "organt_state_12.json").write_text('{"session_id": "S12"}')
    s = Sys(FakeGuide(), guild_id=1, organt_builder=None, bot_info={11: "L", 12: "M"},
            workspace="/ws", session_dir=str(sd))
    s.projects[900] = {"id": "P-001", "name": "게임", "channel": 900,
                       "workspace": "/ws", "leader": 11, "summary": ""}

    captured = {}

    async def fake_rt(flow, oid, body, kind, role):
        captured["body"] = body
        return "done"

    s.run_turn = fake_rt
    asyncio.run(s.handle_user_input(900, 11, "이어서 진행해", root_id=None))   # 등록 채널 개입
    assert {p.name for p in sd.glob("organt_state_*.json")} == {
        "organt_state_11.json", "organt_state_12.json"}            # 세션 보존(기억 유지)
    assert not any(e["event"] == "reset_sessions" for e in s.flow_log)   # 개입엔 reset 안 함
    assert any(e["event"] == "intervention_keep_sessions" for e in s.flow_log)
    assert "이어지는 작업" in captured["body"]                      # 본문이 '이어가기'를 지시


def test_개입_미완Task_영속과_되살리기_담당자가_이어감(tmp_path):
    """[근본] 흐름이 미완 Task를 남기고 끝나면 프로젝트에 스냅샷 영속 → 다음 개입에서 같은 블록·스레드·owner·
    팀으로 되살려 flow.current로 재부착한다(사용자가 Task명 안 불러도 '더 진행해'가 그 Task를 이어감 —
    담당자가 판단). 되살린 직후 검증 누계는 0(verified=False)이라 완료 전 run 재검증을 강제. 완료로 마감하면
    open_task는 비워진다."""
    g = FakeGuide()
    s = Sys(g, guild_id=1, organt_builder=None, bot_info={11: "L", 12: "백엔드"},
            workspace="/ws", session_dir=str(tmp_path),
            projects_path=str(tmp_path / "projects.json"))
    s.projects[901] = {"id": "P-002", "name": "게임", "channel": 901,
                       "workspace": "/ws", "leader": 11, "summary": ""}

    # 1) 흐름이 미완 Task를 만들고(완료 안 함) 끝남 → open_task 영속
    async def fake_make_task(flow, oid, body, kind, role):
        t = _tools(flow, 11, "leader")
        await t["create_task"].handler({"members": "12"})       # 진행 Task 생성, 미완 채로 둠
        flow.current.status.goal = "스킬 3종 동작"
        flow.current.owner = 12
        flow.current.status.owner = "백엔드"
        return "스킬1까지 구현, 나머지 미완"
    s.run_turn = fake_make_task
    asyncio.run(s.handle_user_input(901, 11, "스킬 추가해", root_id=None))
    snap = s.projects[901].get("open_task")
    assert snap and snap["owner"] == 12 and snap["goal"] == "스킬 3종 동작"   # 미완 Task 영속됨
    saved_tid = snap["task_id"]

    # 2) 다음 개입 '더 진행해' → 같은 Task로 되살아나 flow.current에 재부착(담당자가 이어감)
    captured = {}

    async def fake_resume(flow, oid, body, kind, role):
        if "task" not in captured and flow.current is not None:   # 첫 호출(개입 본문)만 캡처(이어가기 프롬프트로 덮어쓰기 방지)
            captured.update(task=flow.current.task_id, owner=flow.current.owner,
                            team=list(flow.current.team), block=flow.current.block_id,
                            verified=flow.current.verified, body=body)
        return "이어서 마무리"
    s.run_turn = fake_resume
    asyncio.run(s.handle_user_input(901, 11, "더 진행해", root_id=None))
    assert captured["task"] == saved_tid and captured["owner"] == 12        # 같은 Task·owner 재부착
    assert 11 in captured["team"] and 12 in captured["team"]                # 팀도 그대로(일부만 부르지 않음)
    assert captured["verified"] is False                                    # 검증 초기화 → 완료 전 재검증 강제
    assert "진행 중이던 Task 복원됨" in captured["body"] and saved_tid in captured["body"]
    assert any(e["event"] == "open_task_restored" for e in s.flow_log)

    # 3) 되살린 Task를 완료로 마감 → open_task 비워짐
    async def fake_complete(flow, oid, body, kind, role):
        flow.current.verified = True
        flow.current.owner = 0                                   # 리더 직접 완료(owner_delivered 게이트 우회)
        flow.percept_checked = True                            # percept 게이트 우회(마감 메커니즘 테스트 — 실에셋 검증은 별도)
        flow.acceptance_checked = True                         # 수용 계약 게이트 우회(범위 밖)
        t = _tools(flow, 11, "leader")
        await t["complete_task"].handler({"result": "스킬 3종 완성"})
        return "완료"
    s.run_turn = fake_complete
    asyncio.run(s.handle_user_input(901, 11, "마저 끝내", root_id=None))
    assert s.projects[901].get("open_task") is None                         # 완료 → 비움


def test_프로젝트_리더_봇부재시_자동재배정_프로젝트유지(tmp_path):
    """[프로젝트↔봇 결합 해제 2026-06-15] 프로젝트 리더 봇이 로스터에서 빠지면(해고·예비환원·미연결)
    _valid_leader가 가용 봇으로 자동 재배정 → 봇을 자유롭게 빼도 기존 프로젝트가 안 깨진다. 유효한
    리더는 그대로(불필요 재배정 없음). 게임 기획자(자연 리더 역할) 우선. 멀티봇 협업 구조엔 무영향."""
    g = FakeGuide()
    s = Sys(g, guild_id=1, organt_builder=None,
            bot_info={11: "게임 기획자", 12: "백엔드", 13: "프론트엔드"},
            workspace="/ws", session_dir=str(tmp_path),
            projects_path=str(tmp_path / "projects.json"))
    s.projects[901] = {"id": "P-001", "name": "게임", "channel": 901, "workspace": "/ws", "leader": 99}
    new_lead = s._valid_leader(s.projects[901])   # 99=해고된 봇(bot_info에 없음) → 재배정
    assert new_lead == 11                          # 기획자 우선 재배정
    assert s.projects[901]["leader"] == 11         # 영속(프로젝트 유지)
    assert any(e["event"] == "project_leader_reassigned" for e in s.flow_log)
    s.projects[901]["leader"] = 12                 # 유효(연결된) 리더로 교체
    assert s._valid_leader(s.projects[901]) == 12  # 연결돼 있으면 그대로(불필요 재배정 안 함)


def test_open_task_복원은_프로젝트팀을_좁히지_않는다(tmp_path):
    """[라이브 버그 회귀 가드 — 사용자 관측] 미완 Task 복원이 project_team을 그 Task에 낀 일부 멤버로
    '대입'하면, 같은 프로젝트에서 일하던 팀원(그 Task엔 안 낀)이 이후 request에서 '이 프로젝트 팀이
    아님'으로 거부됐다(팀 안에 있는데도 거부 → 구조적 불안정). 복원은 union이어야 한다 — 좁히지 않고
    넓히기만 한다(리더 항상 포함)."""
    g = FakeGuide()
    s = Sys(g, guild_id=1, organt_builder=None,
            bot_info={11: "L", 12: "백엔드", 13: "프론트엔드", 14: "디자이너"},
            workspace="/ws", session_dir=str(tmp_path),
            projects_path=str(tmp_path / "projects.json"))
    s.projects[901] = {"id": "P-009", "name": "게임", "channel": 901,
                       "workspace": "/ws", "leader": 11, "summary": ""}

    # 1) 미완 Task 생성 — 이 Task 팀은 12만(13 프론트·14 디자이너는 이 Task엔 안 낌, 그러나 프로젝트 팀원)
    async def make(flow, oid, body, kind, role):
        t = _tools(flow, 11, "leader")
        await t["create_task"].handler({"members": "12"})
        flow.current.status.goal = "g"
        flow.current.owner = 12
        flow.current.status.owner = "백엔드"
        assert 13 in flow.project_team and 14 in flow.project_team   # 처음엔 전체 직군 보유자
        return "미완"
    s.run_turn = make
    asyncio.run(s.handle_user_input(901, 11, "시작", root_id=None))

    # 2) 복원 — project_team이 [11,12]로 축소되면(옛 대입 버그) 13·14가 사라져 이후 거부됨
    captured = {}
    async def grab(flow, oid, body, kind, role):
        if "pt" not in captured:
            captured["pt"] = list(flow.project_team)
            captured["team"] = list(flow.current.team) if flow.current else []
        return "이어감"
    s.run_turn = grab
    asyncio.run(s.handle_user_input(901, 11, "더 진행해", root_id=None))
    assert set(captured["team"]) == {11, 12}                     # 되살린 Task 팀은 일부
    assert 13 in captured["pt"] and 14 in captured["pt"], f"복원이 프로젝트 팀을 축소함: {captured['pt']}"
    assert 11 in captured["pt"] and 12 in captured["pt"]


def test_직업기억_디스크영속_재시작에도_직군유지(tmp_path):
    """[근본] recruit로 예비가 받은 직군(게임 기획자)을 jobs.json에 영속 → 프로세스 재시작(새 Sys) 뒤에도
    '예비'로 원복되지 않고 그 직군 유지. (매번 다른 봇이 게임 기획자로 뽑히던 churn의 디스크 차원 해결)"""
    import json
    jp = tmp_path / "jobs.json"
    s = Sys(FakeGuide(), guild_id=1, organt_builder=None, bot_info={11: "백엔드", 12: "예비"},
            workspace="/ws", session_dir=str(tmp_path), jobs_path=str(jp))
    s._persist_job(12, "게임 기획자")                       # recruit가 부르는 콜백(예비→직군)
    assert jp.exists() and json.load(open(jp, encoding="utf-8"))["jobs"]["12"] == "게임 기획자"
    # '재시작' 시뮬: 같은 jobs_path로 새 Sys — roster는 12를 '예비'로 주지만 디스크에서 직군 복원
    s2 = Sys(FakeGuide(), guild_id=1, organt_builder=None, bot_info={11: "백엔드", 12: "예비"},
             workspace="/ws", session_dir=str(tmp_path), jobs_path=str(jp))
    assert s2.bot_info[12] == "게임 기획자"                  # 예비로 원복 안 됨
    assert s2._roster_labels[12] == "게임 기획자"            # 흐름 시작 원복 라벨에도 반영(지속)


def test_개입_리더재지정_To로_담당자_이양(tmp_path):
    """[사용자 요청] 개입 시 [Request] To로 현 리더와 다른 봇을 명시하면 그 봇이 그 프로젝트의 새 담당자가
    된다(게임 프로젝트인데 백엔드가 담당자로 고정되던 문제 — 기획자 등으로 이양). 같은 리더면 변화 없음."""
    g = FakeGuide()
    s = Sys(g, guild_id=1, organt_builder=None, bot_info={11: "백엔드", 12: "게임 기획자"},
            workspace="/ws", session_dir=str(tmp_path),
            projects_path=str(tmp_path / "projects.json"))
    s.projects[900] = {"id": "P-001", "name": "게임", "channel": 900,
                       "workspace": "/ws", "leader": 11, "summary": ""}
    captured = {}

    async def fake_rt(flow, oid, body, kind, role):
        captured["leader"] = flow.leader
        return "done"
    s.run_turn = fake_rt
    asyncio.run(s.handle_user_input(900, 12, "이건 기획자 너가 담당해", root_id=None))   # To=12(현 리더 11과 다름)
    assert s.projects[900]["leader"] == 12                       # 레지스트리 담당자 이양
    assert captured["leader"] == 12                              # 이번 흐름도 12가 담당
    assert any(e["event"] == "leader_reassigned" for e in s.flow_log)
    # 같은 담당자(현 리더=12)로 다시 개입 → 재지정 이벤트 없음(불필요한 변경 안 함)
    s.flow_log.clear()
    asyncio.run(s.handle_user_input(900, 12, "이어서", root_id=None))
    assert not any(e["event"] == "leader_reassigned" for e in s.flow_log)


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
    f.current.cross_checks = f.current.cross_check_offdomain = 1                    # 검증 분업 게이트(별도 테스트)와 무관한 의도 보존
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
    t and setattr(f.current, "cross_checks", 1)   # 검증 분업 게이트(별도 테스트)와 무관한 의도 보존
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
    f.current.cross_checks = f.current.cross_check_offdomain = 1                    # 검증 분업 게이트(별도 테스트)와 무관한 의도 보존
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


# --- 레지스트리의 리클레임 내구성: 시드(seeded 마커) + Discord 채널 토픽(영속 진실원) ---

class TopicGuide(FakeGuide):
    """채널 토픽을 흉내내는 가짜 Guide — set/get_channel_topic 기록·반환."""
    def __init__(self, topics=None):
        super().__init__()
        self.topics = {int(k): v for k, v in (topics or {}).items()}

    async def get_channel_topics(self, gid):
        return dict(self.topics)

    async def set_channel_topic(self, ch, topic):
        self.calls.append(("topic", int(ch), topic))
        self.topics[int(ch)] = topic
        return True


def _seed(tmp_path, projects, n=None):
    sp = tmp_path / "projects.seed.json"
    sp.write_text(__import__("json").dumps(
        {"n": n or len(projects), "projects": projects}, ensure_ascii=False), encoding="utf-8")
    return str(sp)


def test_시드복원은_seeded마커와_함께_적재(tmp_path):
    """logs/projects.json이 없으면(리클레임) 커밋 시드에서 복원하되 'seeded' 마커를 남긴다 —
    reconcile이 마커를 보고 '토픽 > 시드' 우선순위를 적용할 수 있게(셸 cp 복원의 대체)."""
    seed = _seed(tmp_path, {"9001": {"id": "P-001", "name": "스네이크", "channel": 9001,
                                     "workspace": "/ws", "leader": 11, "summary": ""}})
    pp = tmp_path / "projects.json"
    s = Sys(TopicGuide(), guild_id=1, organt_builder=None, bot_info={11: "L"},
            projects_path=str(pp), seed_path=seed)
    assert s.projects[9001]["seeded"] is True and s.projects[9001]["leader"] == 11
    assert pp.exists()                                   # logs에 물질화(마커 포함)
    # 디스크(logs)가 있으면 시드는 안 본다(런타임이 최신)
    s2 = Sys(TopicGuide(), guild_id=1, organt_builder=None, bot_info={11: "L"},
             projects_path=str(pp), seed_path=_seed(tmp_path, {}))
    assert 9001 in s2.projects


def test_reconcile_토픽이_시드를_이기고_런타임디스크는_그대로(tmp_path):
    """부팅 reconcile 우선순위(런타임 디스크 > 토픽 > 시드): 시드로 복원된 항목은 토픽(리더 재지정이
    반영된 영속 진실원)이 덮고, 런타임 디스크 항목은 토픽이 못 덮는다 + 토픽만 있는 프로젝트는 복원."""
    seed = _seed(tmp_path, {"9001": {"id": "P-001", "name": "스네이크", "channel": 9001,
                                     "workspace": "/ws", "leader": 11, "summary": ""}})
    g = TopicGuide(topics={
        9001: "[ORGANT:P-001] leader=12 | ws=/ws | name=스네이크",      # 재지정된 리더(12)
        9003: "[ORGANT:P-007] leader=12 | ws=/game | name=협동 게임",   # 디스크·시드에 없던 등록
        9004: "그냥 사람이 적은 토픽",                                   # 무관 토픽은 무시
    })
    s = Sys(g, guild_id=1, organt_builder=None, bot_info={11: "L", 12: "M"},
            projects_path=str(tmp_path / "projects.json"), seed_path=seed)
    asyncio.run(s.reconcile_projects_from_discord())
    assert s.projects[9001]["leader"] == 12              # 토픽 > 시드 (리더 재지정 원복 안 됨)
    assert "seeded" not in s.projects[9001]
    assert s.projects[9003]["id"] == "P-007"             # 토픽에서 등록 복원
    assert s._proj_n >= 7                                # 식별번호 카운터도 따라감(중복 발급 방지)
    assert 9004 not in s.projects
    # 런타임 디스크(마커 없음)는 토픽이 못 덮는다
    s2 = Sys(TopicGuide(topics={9001: "[ORGANT:P-001] leader=12 | ws=/ws | name=스네이크"}),
             guild_id=1, organt_builder=None, bot_info={11: "L", 12: "M"},
             projects_path=str(tmp_path / "projects.json"))
    s2.projects[9001]["leader"] = 11                     # 런타임 상태(디스크가 진실원)
    asyncio.run(s2.reconcile_projects_from_discord())
    assert s2.projects[9001]["leader"] == 11


def test_등록과_리더재지정이_채널토픽에_기록(tmp_path):
    """_register_project(이동 포함)·리더 재지정 때 토픽이 갱신돼야 리클레임 후 복원이 가능하다."""
    g = TopicGuide()
    s = Sys(g, guild_id=1, organt_builder=None, bot_info={11: "L", 12: "M"},
            projects_path=str(tmp_path / "projects.json"))

    async def scenario():
        s._register_project(9001, "스네이크", "/ws", 11)
        await asyncio.sleep(0)                           # best-effort 태스크 실행 양보
        s._register_project(9002, "스네이크", "/ws2", 11)   # 같은 이름 → 채널 이동
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert g.topics.get(9001) == ""                      # 옛 채널 토픽은 비움(유령 등록 방지)
    parsed = Sys.parse_project_topic(g.topics.get(9002, ""))
    assert parsed and parsed["id"] == "P-001" and parsed["leader"] == 11
    # 토픽 포맷 왕복(기록한 걸 그대로 읽을 수 있어야 복원이 성립) — 공백·파이프 포함도 보존
    p = {"id": "P-009", "leader": 12, "workspace": "/w s", "name": "이름 | 파이프포함"}
    back = Sys.parse_project_topic(Sys._topic_for(p))
    assert back == {"id": "P-009", "leader": 12, "workspace": "/w s", "name": "이름 | 파이프포함"}


# --- 직군 '변형(중복) 생성' 게이트: VFX류가 흐름마다 새 이름으로 불어나던 중복 생성 오류의 근본 차단 ---

def test_직군_변형생성_게이트_재사용유도와_명시적신설():
    """기존 직군의 변형 이름(VFX 전문가 ↔ VFX 아티스트)으로 recruit하면 생성하지 않고 멈춰 세운다.
    같은 이름은 재사용(증원)이라 통과, 변형은 보류(기존 이름 재사용 안내), 정말 다른 일을 하는
    새 직군이면 new_role='yes'로 명시적 신설 — 시스템이 정답 이름을 정하지 않는다(하드코딩 아님)."""
    g = FakeGuide()
    f = Flow(g, channel_id=500, guild_id=1, leader_id=11,
             bot_info={11: "백엔드", 12: "VFX 전문가", 13: "예비", 14: "예비"})
    f.start_root("root")
    t = {x.name: x for x in make_guide_tools(f, 11, "leader")}
    asyncio.run(t["create_task"].handler({"members": ""}))
    r = asyncio.run(t["recruit"].handler({"role": "VFX 아티스트", "reason": "이펙트"}))
    assert "중복 의심" in r["content"][0]["text"] and "VFX 전문가" in r["content"][0]["text"]
    assert all(v != "VFX 아티스트" for v in f.bot_info.values())   # 변형 직군이 생기지 않음
    # 기존 이름 그대로 → 재사용·증원 통과(같은 직군 채용 자유 정책 유지)
    r2 = asyncio.run(t["recruit"].handler({"role": "VFX 전문가", "reason": "증원"}))
    assert "직군으로 채용" in r2["content"][0]["text"]
    # 정말 다른 일을 하는 새 직군 → 명시적 신설(new_role='yes')로 통과
    r3 = asyncio.run(t["recruit"].handler({"role": "VFX 아티스트", "new_role": "yes", "reason": "다른 일"}))
    assert "직군으로 채용" in r3["content"][0]["text"]


def test_직군게이트_비교풀에_서버_커스텀역할_포함():
    """비교 풀은 현재 팀 라벨만이 아니라 '서버 커스텀 역할 전체' — 토큰 유실/오프라인으로 로스터에 없는
    봇이 보유한 직군('VFX 전문가')과도 변형 충돌을 잡는다(직군 역할은 서버에 영속이므로 그것이 진실원).
    정확히 같은 이름은 다른 역할과 토큰이 겹쳐도 재사용으로 즉시 통과한다(오차단 금지)."""
    class RoleGuide(FakeGuide):
        async def get_custom_role_names(self, gid):
            return ["VFX 전문가", "게임 비주얼 디자이너", "게임 기획자"]

    f = Flow(RoleGuide(), channel_id=500, guild_id=1, leader_id=11,
             bot_info={11: "백엔드", 13: "예비", 14: "예비"})
    f.start_root("root")
    t = {x.name: x for x in make_guide_tools(f, 11, "leader")}
    asyncio.run(t["create_task"].handler({"members": ""}))
    r = asyncio.run(t["recruit"].handler({"role": "VFX 디자이너", "reason": "이펙트"}))
    assert "중복 의심" in r["content"][0]["text"] and "VFX 전문가" in r["content"][0]["text"]
    # '게임 기획자'는 서버에 이미 있는 이름 그대로 → '게임 비주얼 디자이너'와 토큰('게임')이 겹쳐도 통과
    r2 = asyncio.run(t["recruit"].handler({"role": "게임 기획자", "reason": "기획"}))
    assert "직군으로 채용" in r2["content"][0]["text"]


# ── 타임아웃 결함 수정: 하트비트(일하는 워커 보호) + 인프라 타임아웃 '이어가기' ──────────────

def test_하트비트_일하는워커는_침묵타임아웃_안걸림():
    """워커가 turn_timeout보다 오래 걸려도, 도구 활동으로 last_activity를 갱신하는 한 끊기지 않는다
    (벽시계 고정 타임아웃이 일하는 owner를 잘라 좀비·미완을 만들던 결함의 근본 교정)."""
    import time as _t
    g = FakeGuide()
    f = Flow(g, channel_id=1, guild_id=1, leader_id=11, bot_info={11: "L", 12: "M"})
    f.start_root("root")

    class _Worker:
        def __init__(self, flow):
            self.flow = flow

        async def handle(self, prompt):
            for _ in range(12):                 # 총 ~1.2s > turn_timeout(0.5) — 그래도 활동으로 보호
                await asyncio.sleep(0.1)
                self.flow.last_activity = _t.monotonic()   # 도구 활동 흉내(하트비트)
            return "끝까지 완료"

    s = Sys(g, guild_id=1, organt_builder=lambda oid, srv, role, flow=None: _Worker(flow),
            bot_info={11: "L", 12: "M"})
    s.turn_timeout = 0.5
    import src.sys_core as sc
    _orig = sc.build_guide_server
    sc.build_guide_server = lambda *a, **k: object()
    try:
        out = asyncio.run(s.run_turn(f, 12, "b", Kind.WORK, "member"))
    finally:
        sc.build_guide_server = _orig
    assert out == "끝까지 완료"                  # >turn_timeout 걸렸지만 하트비트로 안 잘림


def test_하트비트_무활동워커는_침묵으로_끊김():
    """반대로, 도구 활동이 전혀 없는(진짜 행) 워커는 turn_timeout 침묵 후 'API Error: timeout'으로 끊긴다."""
    g = FakeGuide()
    f = Flow(g, channel_id=1, guild_id=1, leader_id=11, bot_info={11: "L", 12: "M"})
    f.start_root("root")

    class _Hang:
        async def handle(self, prompt):
            await asyncio.sleep(10)             # 무활동(last_activity 갱신 0) → 행
            return "done"

    s = Sys(g, guild_id=1, organt_builder=lambda oid, srv, role, flow=None: _Hang(),
            bot_info={11: "L", 12: "M"})
    s.turn_timeout = 0.3
    import src.sys_core as sc
    _orig = sc.build_guide_server
    sc.build_guide_server = lambda *a, **k: object()
    try:
        out = asyncio.run(s.run_turn(f, 12, "b", Kind.WORK, "member"))
    finally:
        sc.build_guide_server = _orig
    assert out.lower().startswith("api error") and "timeout" in out.lower()


def test_인프라타임아웃이라도_작업했으면_이어가기():
    """워커가 작업을 하다(act_count↑) 무활동으로 끊긴 인프라 타임아웃은 '실패'가 아니라 '이어가기'로
    처리된다 — owner_incomplete=True(작업 보존·complete 차단) + 같은 owner '이어서' 재위임 안내."""
    g = FakeGuide()
    f = _flow(g)

    async def wake(to, b, k):
        f.act_count += 1                        # owner가 실제로 일했음(파일/실행)
        return "API Error: timeout — 동료 무응답(행)"

    f.wake = wake
    tools = _tools(f, 11, "leader")
    asyncio.run(tools["create_task"].handler({"purpose": "p", "members": "12"}))
    f.current.participated.add(12)
    asyncio.run(tools["set_goal"].handler({"goal": "g"}))
    r = asyncio.run(tools["request"].handler({"to_id": "12", "kind": "Work", "body": "구현"}))
    assert f.current.owner_incomplete is True               # 이어가기로 표시(작업 유실 방지)
    assert "이어서" in r["content"][0]["text"]              # 같은 owner 재위임 안내
    f.current.verified = True                                # run 검증은 됐다 쳐도
    rc = asyncio.run(tools["complete_task"].handler({"result": "끝"}))
    assert "거부" in rc["content"][0]["text"]               # 미완이라 완료 거부(허위완료 차단)


def test_미완게이트는_크래시나_무작업응답으로_안풀림():
    """타임아웃 미완(owner_incomplete)은 'owner의 실작업을 담은 정상 응답'만이 해제한다 — 후속 요청이
    크래시(일시오류)나 실작업 없는 응답으로 끝나도 게이트가 풀리지 않는다(과거 정상 인도가 있었어도
    미완인 채 complete가 통과되던 구멍 차단)."""
    g = FakeGuide()
    f = _flow(g)
    st = {"mode": "timeout"}

    async def wake(to, b, k):
        if st["mode"] == "timeout":
            f.act_count += 1                    # 작업하다 무활동으로 끊김(이어가기 대상)
            return "API Error: timeout — 동료 무응답(행)"
        if st["mode"] == "crash":
            return "API Error: 500 overloaded"  # 타임아웃 아닌 크래시(일시오류)
        return "이미 다 했습니다"                  # 실작업 없는 응답(착수·증거 없음)

    f.wake = wake
    tools = _tools(f, 11, "leader")
    asyncio.run(tools["create_task"].handler({"members": "12"}))
    f.current.participated.add(12)
    asyncio.run(tools["set_goal"].handler({"goal": "g"}))
    asyncio.run(tools["request"].handler({"to_id": "12", "kind": "Work", "body": "구현"}))
    assert f.current.owner_incomplete is True               # 작업하다 끊김 → 미완(이어가기)
    st["mode"] = "crash"
    asyncio.run(tools["request"].handler({"to_id": "12", "kind": "Work", "body": "이어서"}))
    assert f.current.owner_incomplete is True               # 크래시는 완료의 증거가 아님 — 미완 유지
    st["mode"] = "idle"
    asyncio.run(tools["request"].handler({"to_id": "12", "kind": "Work", "body": "이어서 마무리"}))
    assert f.current.owner_incomplete is True               # 실작업 없는 응답도 미완 유지
    f.current.verified = True
    f.current.owner_delivered = True                        # 과거 정상 인도가 있었다 쳐도
    rc = asyncio.run(tools["complete_task"].handler({"result": "끝"}))
    assert "거부" in rc["content"][0]["text"]               # 이어가기 완료 전엔 마감 불가

    async def wake_done(to, b, k):
        f.act_count += 1                                    # owner가 실작업으로 마저 끝냄
        return "남은 부분 구현·검증 완료"

    f.wake = wake_done
    asyncio.run(tools["request"].handler({"to_id": "12", "kind": "Work", "body": "이어서 끝내기"}))
    assert f.current.owner_incomplete is False              # 실작업 담은 정상 응답 → 게이트 해제
    rc2 = asyncio.run(tools["complete_task"].handler({"result": "끝"}))
    assert "마감" in rc2["content"][0]["text"]              # 이제 완료 허용


def test_크래시응답은_인도아님_재요청은_Redo아님():
    """크래시(일시오류) 응답은 '완료 인도(accept)'로 기록되지 않는다 — 직후 같은 동료 재요청이
    Redo(직전 산출물 보완)로 둔갑해 한도를 태우거나 owner에게 '결함 보완' 프레임으로 잘못 전달되지 않는다."""
    g = FakeGuide()
    f = _flow(g)
    st = {"fail": True}

    async def wake(to, b, k):
        if st["fail"]:
            return "API Error: 529 overloaded"              # 서브프로세스 크래시 모의
        f.act_count += 1
        return "구현·검증 완료"

    f.wake = wake
    tools = _tools(f, 11, "leader")
    asyncio.run(tools["create_task"].handler({"members": "12"}))
    f.current.participated.add(12)
    asyncio.run(tools["set_goal"].handler({"goal": "g"}))
    asyncio.run(tools["request"].handler({"to_id": "12", "kind": "Work", "body": "구현"}))   # 크래시
    assert not f.comm.delivered_work(11, 12)                # 크래시는 인도가 아님(accept 아님)
    assert any(ev[0] == "respond" and ev[4] == "failed" for ev in f.comm.history)
    st["fail"] = False
    asyncio.run(tools["request"].handler({"to_id": "12", "kind": "Work", "body": "다시 부탁"}))
    assert not any(ev[0] == "redo" for ev in f.comm.history)   # 크래시 후 재요청 = 새 위임(Redo 아님)
    assert f.current.owner_delivered is True                   # 정상 인도 성립


def test_직군보유자_자기직군_덮어쓰기_거부_1봇1직업():
    """Task 전 '자기 직군' recruit는 예비(무직) 담당자 전용이다 — 예비가 남아 있는데 직군 보유 봇이
    '무관한' 직군으로 자기를 재채용하면 거부한다(1봇 1직업·전문화 기억 보호; 라이브에서 디자이너가
    '게임 기획자'로 자기 직군을 덮어써 영속까지 오염되던 버그). 같은 직군 재확인은 무해 통과."""
    g = FakeGuide()
    f = Flow(g, channel_id=500, guild_id=1, leader_id=11,
             bot_info={11: "L", 12: "M", 13: "예비"})   # 예비가 남아 있음 → 전직 예외 미적용
    f.start_root("root")
    persisted = {}
    f.persist_role = lambda mid, role: persisted.__setitem__(mid, role)
    t = _tools(f, 11, "leader")
    r = asyncio.run(t["recruit"].handler({"member": "", "role": "게임 기획자", "reason": "전직"}))
    assert "거부" in r["content"][0]["text"] and "1봇 1직업" in r["content"][0]["text"]
    assert f.bot_info[11] == "L" and 11 not in persisted      # 라벨·영속 기억 모두 안 바뀜
    r2 = asyncio.run(t["recruit"].handler({"member": "11", "role": "L", "reason": "재확인"}))
    assert "이미" in r2["content"][0]["text"] and f.bot_info[11] == "L"   # 같은 직군은 무해 통과


def test_겸직_예외_예비없으면_허용_유사직군도_허용_한도2():
    """겸직(직군 추가)은 사용자 정책의 예외 둘 중 하나일 때만 허용된다 — ① 풀에 예비가 0명(어쩔 수
    없음) ② 새 직군이 기존과 '비슷한 일'(도메인 토큰 공유). 허용 시 교체가 아니라 '추가'다(기존
    전문화 기억 유지 — 라벨·직업 기억이 '주직군·부직군'). 봇당 최대 2개(직군 스택 재발 방지)."""
    # ① 예비 0명 — 무관한 직군이라도 겸직 허용(기존 직군 유지 + 추가)
    g = FakeGuide()
    f = Flow(g, channel_id=500, guild_id=1, leader_id=11, bot_info={11: "L", 12: "백엔드"})
    f.start_root("root")
    persisted = {}
    f.persist_role = lambda mid, role: persisted.__setitem__(mid, role)
    t = _tools(f, 11, "leader")
    asyncio.run(t["create_task"].handler({"members": "12"}))
    r = asyncio.run(t["recruit"].handler({"member": "12", "role": "QA", "reason": "재배치"}))
    assert "겸직" in r["content"][0]["text"]
    assert f.bot_info[12] == "백엔드·QA" and persisted.get(12) == "백엔드·QA"   # 기존 유지 + 추가
    # 이미 보유한 직군 재요청 → 변경 없이 무해 통과
    r_dup = asyncio.run(t["recruit"].handler({"member": "12", "role": "QA", "reason": "재확인"}))
    assert f.bot_info[12] == "백엔드·QA"
    # 한도: 직군 2개 보유자에게 셋째 직군(예비 0명이어도) → 거부
    r_cap = asyncio.run(t["recruit"].handler({"member": "12", "role": "사운드", "reason": "추가"}))
    assert "한도" in r_cap["content"][0]["text"] and f.bot_info[12] == "백엔드·QA"
    # ② 예비가 있어도 '비슷한 일'(기존 직군명 재사용 — 토큰 공유)이면 겸직 허용
    g2 = FakeGuide()
    f2 = Flow(g2, channel_id=500, guild_id=1, leader_id=11,
              bot_info={11: "L", 12: "디자이너", 13: "게임 비주얼 디자이너", 14: "예비"})
    f2.start_root("root")
    t2 = _tools(f2, 11, "leader")
    asyncio.run(t2["create_task"].handler({"members": "12,13"}))
    r2 = asyncio.run(t2["recruit"].handler({"member": "12", "role": "게임 비주얼 디자이너", "reason": "통합"}))
    assert "겸직" in r2["content"][0]["text"]
    assert f2.bot_info[12] == "디자이너·게임 비주얼 디자이너"   # 주직군 유지 + 부직군 추가


def test_위임은_도구호출_취소에도_완주_detached결과_전달():
    """CLI가 request 도구 호출을 포기(취소)해도 위임 자체는 끝까지 완주한다 — 프레임이 정상 닫혀
    베턴이 복귀하고 owner 인도가 성립하며, 완주 결과는 detached_results로 남아 SYS가 이어가기
    리더에게 전달한다(라이브 관측: 도구 포기가 '이중 활성'·'비동기 작업 중' 오인을 만들던 결함 차단)."""
    g = FakeGuide()
    f = _flow(g)

    async def wake(to, b, k):
        f.act_count += 1
        await asyncio.sleep(0.2)        # 일하는 중(이 사이 도구 호출이 포기됨)
        return "구현·검증 완료"

    f.wake = wake
    t = _tools(f, 11, "leader")

    async def scenario():
        await t["create_task"].handler({"members": "12"})
        f.current.participated.add(12)
        await t["set_goal"].handler({"goal": "g"})
        h = asyncio.ensure_future(t["request"].handler({"to_id": "12", "kind": "Work", "body": "구현"}))
        await asyncio.sleep(0.05)
        h.cancel()                      # CLI의 도구 호출 포기 모의
        try:
            await h
        except asyncio.CancelledError:
            pass
        assert any(not x.done() for x in f.inflight_tasks)   # 완주 태스크는 계속 살아 있음
        await asyncio.gather(*list(f.inflight_tasks), return_exceptions=True)
        assert f.comm.alive == 11                            # 프레임 닫혀 베턴 복귀(단일활성 일관)
        assert f.current.owner_delivered is True             # 인도 성립(작업 유실 없음)
        assert f.detached_results and "완료" in f.detached_results[0]

    asyncio.run(scenario())


def test_drain_inflight_완주대기_결과전달():
    """SYS는 이어가기 전에 완주 중인 위임(detach 포함)을 끝까지 기다리고, 도착한 결과를 이어가기
    본문으로 돌려준다 — 일하는 owner를 드레인으로 자르지 않는다(단일활성·작업 보존)."""
    s = Sys(FakeGuide(), guild_id=1, organt_builder=None, bot_info={11: "L"})
    f = _flow(FakeGuide())

    async def scenario():
        async def slow():
            await asyncio.sleep(0.1)
            f.detached_results.append("M → 남은 부분 구현 완료")
        task = asyncio.ensure_future(slow())
        f.inflight_tasks.add(task)
        task.add_done_callback(f.inflight_tasks.discard)
        out = await s._drain_inflight(f)
        assert task.done() and "구현 완료" in out and not f.detached_results
        assert await s._drain_inflight(f) == ""              # 남은 게 없으면 빈 문자열

    asyncio.run(scenario())


def test_SYS_자동이어가기_미완위임을_시스템이_완주시킴():
    """[구조적 이어가기] 위임이 '구조적 미완'(턴한도/타임아웃)으로 끊기면 — 리더(LLM)의 판단·기억에
    맡기지 않고 — SYS가 표준 request 파이프라인으로 같은 owner에게 '이어서'를 자동 발사해 완성본을
    받아낸다. 리더는 완성 결과를 받아 판정(검증·마감)만 한다(리더가 '비동기 작업' 오인으로 폴링하며
    이어가기 예산을 태우던 결함의 구조적 차단 — 프롬프트 의존 제거)."""
    g = FakeGuide()
    f = _flow(g)
    st = {"n": 0}

    async def wake(to, b, k):
        st["n"] += 1
        if st["n"] == 1:
            f.act_count += 1
            return "절반 구현 (⚠ 턴 한도 도달 — 작업이 미완일 수 있음)"   # 1차: 구조적 미완
        f.act_count += 1
        assert "SYS 자동 이어가기" in b                                  # SYS가 보낸 이어가기 본문
        return "남은 부분 구현·검증 완료"

    f.wake = wake
    s = Sys(g, guild_id=1, organt_builder=None, bot_info={11: "L", 12: "M"})
    t = _tools(f, 11, "leader")
    asyncio.run(t["create_task"].handler({"members": "12"}))
    f.current.participated.add(12)
    asyncio.run(t["set_goal"].handler({"goal": "g"}))
    asyncio.run(t["request"].handler({"to_id": "12", "kind": "Work", "body": "구현"}))
    assert f.current.owner_incomplete is True                            # 1차 미완 확인
    out = asyncio.run(s._auto_continue_owner(f, 11))
    assert f.current.owner_incomplete is False                           # SYS가 완주시킴
    assert f.current.owner_delivered is True                             # 인도 성립 → 리더는 판정만
    assert "완료" in out and st["n"] == 2


def test_SYS_자동이어가기_무진행이면_중단():
    """자동 이어가기는 '진행이 전혀 없는데 미완 유지'(환경 문제·크래시 반복)면 같은 호출을 반복해
    박지 않는다 — 무한 재시도 대신 리더/사용자 보고 경로로 넘긴다."""
    g = FakeGuide()
    f = _flow(g)
    st = {"n": 0}

    async def wake(to, b, k):
        st["n"] += 1
        if st["n"] == 1:
            f.act_count += 1
            return "절반 (⚠ 턴 한도 도달 — 작업이 미완일 수 있음)"
        return "API Error: 500 overloaded"     # 이어가기가 크래시(무진행) — 미완은 보존 게이트로 유지

    f.wake = wake
    s = Sys(g, guild_id=1, organt_builder=None, bot_info={11: "L", 12: "M"})
    t = _tools(f, 11, "leader")
    asyncio.run(t["create_task"].handler({"members": "12"}))
    f.current.participated.add(12)
    asyncio.run(t["set_goal"].handler({"goal": "g"}))
    asyncio.run(t["request"].handler({"to_id": "12", "kind": "Work", "body": "구현"}))
    asyncio.run(s._auto_continue_owner(f, 11, limit=5))
    assert f.current.owner_incomplete is True and st["n"] <= 3           # 무진행 반복 안 함


def test_SYS_자동위임_리더가_위임0건_헛돌면_owner에게_직접발사():
    """[헛돎 발생 차단 2026-06-15] 리더가 designated owner(스냅샷 복원 등)에게 위임 0건이고 솔로 독식
    차단(leader_runs>3)에만 막혀 헛돌면, SYS가 직접 그 owner에게 '첫 위임'을 발사한다 — _auto_continue_owner는
    '위임된 뒤 미완'만 잡으므로 '위임 0건'인 정체는 구조적 빈틈이었다(라이브: 신예준 P-014 거부11·위임0·헛돎).
    헛돎을 한도 종결로 사후 차단하지 않고 발생 자체에서 막는다. 위임 한 번 나가면 work_delegated>0이라 재발사 X."""
    g = FakeGuide()
    f = _flow(g)
    st = {"n": 0, "body": ""}

    async def wake(to, b, k):
        st["n"] += 1; st["body"] = b
        f.act_count += 1
        return "남은 부분 구현·검증 완료"

    f.wake = wake
    s = Sys(g, guild_id=1, organt_builder=None, bot_info={11: "L", 12: "M"})
    t = _tools(f, 11, "leader")
    asyncio.run(t["create_task"].handler({"members": "12"}))
    f.current.participated.add(12)
    asyncio.run(t["set_goal"].handler({"goal": "g"}))
    f.current.owner = 12          # 스냅샷 복원 모사: owner 지정됐으나
    f.leader_runs = 4             # 위임 0건 + 솔로 독식 차단 발동(>3) = 헛돎 정체
    assert f.current.work_delegated == 0
    out = asyncio.run(s._auto_delegate_owner(f, 11))
    assert st["n"] == 1                                  # SYS가 owner에게 위임 발사
    assert "SYS 자동 위임" in st["body"]                  # 자동 위임 본문 전달
    assert "자동 위임" in out                             # 결과 반환(침묵 금지)
    assert any(e["event"] == "sys_auto_delegate" for e in s.flow_log)
    st["n"] = 0                                          # 위임 나갔으니(work_delegated>0) 재발사 X
    assert asyncio.run(s._auto_delegate_owner(f, 11)) == "" and st["n"] == 0


def test_SYS_자동위임_정상흐름엔_무동작():
    """자동 위임은 헛돎 정체(owner 지정 + 위임0 + leader_runs>3)에서만 발동 — 그 외엔 무동작(정상 흐름 방해 X)."""
    g = FakeGuide()
    f = _flow(g)
    s = Sys(g, guild_id=1, organt_builder=None, bot_info={11: "L", 12: "M"})
    t = _tools(f, 11, "leader")
    asyncio.run(t["create_task"].handler({"members": "12"}))
    f.current.participated.add(12)
    asyncio.run(t["set_goal"].handler({"goal": "g"}))
    assert asyncio.run(s._auto_delegate_owner(f, 11)) == ""   # owner 미지정 → 무동작
    f.current.owner = 12; f.leader_runs = 2                   # 아직 안 헛돎(leader_runs 낮음)
    assert asyncio.run(s._auto_delegate_owner(f, 11)) == ""   # → 무동작


def test_요청자_자기활동은_owner인도로_안침():
    """[구조 신호 정확성] 위임 측정창에서 '요청자(리더) 자신의 활동'(detach 뒤 모델 쪽 폴링 run 등)은
    owner 인도 신호(owner_acted)로 치지 않는다 — 이중 활성 잔재가 허위완료 게이트를 뚫지 못하게.
    또한 미착수(premature)는 구조적 미완 마커를 세워 SYS 자동 이어가기의 대상이 된다."""
    g = FakeGuide()
    f = _flow(g)

    async def wake(to, b, k):
        f.act_count += 1                          # 측정창에 활동 1회가 있었지만...
        f.act_by[11] = f.act_by.get(11, 0) + 1    # ...그건 요청자(리더 11) 자신의 것
        return "네, 곧 시작하겠습니다"
    f.wake = wake
    t = _tools(f, 11, "leader")
    asyncio.run(t["create_task"].handler({"members": "12"}))
    f.current.participated.add(12)
    asyncio.run(t["set_goal"].handler({"goal": "g"}))
    r = asyncio.run(t["request"].handler({"to_id": "12", "kind": "Work", "body": "구현"}))
    assert f.current.owner_delivered is False                 # 리더 노이즈는 인도가 아님
    assert f.current.owner_incomplete is True                 # 미착수 = 구조적 미완(자동 이어가기 대상)
    assert "산출물을 만들지" in r["content"][0]["text"]


def test_강제배포는_완료Task가_있을때만(tmp_path, monkeypatch):
    """[품질 게이트] SYS 강제배포는 '완료된 Task가 있고 미완 Task가 안 남은' 흐름에서만 발동한다 —
    미완·실패 산출물이 흐름 종료마다 자동으로 라이브를 덮던 것 차단."""
    import types
    (tmp_path / "package.json").write_text("{}")
    for k, v in (("GH_PAT", "x"), ("GH_USER", "u"), ("RENDER_KEY", "k"),
                 ("RENDER_OWNER", "o")):
        monkeypatch.setenv(k, v)
    deployed = {"n": 0}
    monkeypatch.setattr("src.deploy.deploy_sync",
                        lambda *a: (deployed.__setitem__("n", deployed["n"] + 1), "https://URL")[1])
    s = Sys(FakeGuide(), guild_id=1, organt_builder=None, bot_info={11: "L"})
    f = _flow(FakeGuide())
    f.project_id = "P-009"                                     # 등록 프로젝트만 슬롯을 가진다
    f.workspace = str(tmp_path)
    f.current = object()                                       # ① 미완 Task 남음 → 배포 금지
    assert asyncio.run(s._ensure_deploy(f, 11, "r")) == "r" and deployed["n"] == 0
    f.current = None
    f.tasks = []                                               # ② 완료 Task 없음 → 배포 금지
    assert asyncio.run(s._ensure_deploy(f, 11, "r")) == "r" and deployed["n"] == 0
    f.tasks = [types.SimpleNamespace(status=types.SimpleNamespace(status="완료"))]
    out = asyncio.run(s._ensure_deploy(f, 11, "r"))            # ③ 완료 있음 → 강제배포 발동
    assert deployed["n"] == 1 and "배포" in out


def test_read_thread_시간순과_평문개입_포함():
    """read_thread는 시간순(과거→최신)으로 돌려준다(discord 기본 최신→과거를 뒤집음 — '마지막 요청'
    판정의 전제). include_plain=True면 평문도 Request(to=None)로 감싼다 — 등록 프로젝트 채널의
    평문 개입을 부팅 복구가 잡을 수 있게(라이브에서 평문 '이어서 계속해'가 복구 누락되던 구멍)."""
    import types
    from src.discord_guide import DiscordGuide

    def _m(mid, author, content):
        return types.SimpleNamespace(id=mid, author=types.SimpleNamespace(id=author),
                                     content=content, mentions=[], reference=None)

    class _Ch:
        def __init__(self, msgs):
            self._m = msgs

        async def history(self, limit=50):
            for x in reversed(self._m):       # discord history 기본: 최신→과거
                yield x

    class _Client:
        def __init__(self, ch):
            self._ch = ch

        def get_channel(self, cid):
            return self._ch

    msgs = [_m(1, 9, "하나"), _m(2, 9, "이어서 계속해")]      # 시간순 원본
    g = DiscordGuide(_Client(_Ch(msgs)))
    out = asyncio.run(g.read_thread(5, include_plain=True))
    assert [r.body for r in out] == ["하나", "이어서 계속해"]   # 시간순 보장 + 평문 래핑
    assert out[-1].to_id is None and out[-1].from_id == 9
    assert asyncio.run(g.read_thread(5)) == []                 # 기본값은 구조화 메시지만


def test_직무기준_주입과_초안요청():
    """[직군 고도화 — 하드코딩 없음] 직무 기준이 있는 직군은 프롬프트에 자기검수 기준으로 주입되고,
    없는 직군은 '스스로 작성'을 한 번 요청받는다 — QA·백엔드·런타임 직군 전부 같은 메커니즘."""
    s = Sys(FakeGuide(), guild_id=1, organt_builder=None,
            bot_info={11: "백엔드", 12: "QA", 13: "백엔드·QA"})
    s.role_profiles["백엔드"] = "엣지·경계값을 시뮬로 직접 재현해 검증한다"
    p11 = s._prompt("b", Kind.WORK, "member", 11, 11)
    assert "엣지·경계값을 시뮬로" in p11                       # 기준 보유 → 주입
    p12 = s._prompt("b", Kind.WORK, "member", 12, 11)
    assert "[직무기준] QA" in p12 and "직무 기준 작성" in p12   # 기준 없음 → 초안 요청
    p13 = s._prompt("b", Kind.WORK, "member", 13, 11)
    assert "엣지·경계값을 시뮬로" in p13 and "[직무기준] QA" in p13   # 겸직: 보유분 주입+부족분 요청


def test_직무기준_흡수_영속_본문제거(tmp_path):
    """보고 속 [직무기준] 블록은 SYS가 흡수한다 — 메모리·디스크(role_profiles.json)로 영속하고
    본문에서는 제거돼 요청자에게 깨끗한 보고만 전달된다(사용자 디스코드를 오염시키지 않음).
    재기동 시 디스크에서 복원되고, 리클레임으로 잃으면 전문가가 첫 작업 때 다시 쓴다(자가 재생)."""
    import json as _json
    s = Sys(FakeGuide(), guild_id=1, organt_builder=None, bot_info={11: "L"},
            session_dir=str(tmp_path))
    out = asyncio.run(s._absorb_role_profiles(
        "구현·검증 완료 보고입니다.\n[직무기준] QA\n실플레이 시나리오를 끝까지 재현한다\n경계값을 직접 친다\n[/직무기준]"))
    assert out == "구현·검증 완료 보고입니다."                  # 본문에서 블록 제거
    assert "실플레이 시나리오" in s.role_profiles["QA"]         # 메모리 흡수
    saved = _json.load(open(tmp_path / "role_profiles.json", encoding="utf-8"))
    assert "경계값" in saved["profiles"]["QA"]                  # 디스크 영속
    assert any(e["event"] == "role_profile_saved" for e in s.flow_log)
    s2 = Sys(FakeGuide(), guild_id=1, organt_builder=None, bot_info={11: "L"},
             session_dir=str(tmp_path))
    assert "실플레이 시나리오" in s2.role_profiles["QA"]        # 재기동 복원


def test_create_project는_id기반_작업공간과_배포슬롯(tmp_path):
    """[신원=번호 — 사용자 제안] 프로젝트의 폴더와 배포 슬롯은 리더 작명이 아니라 식별번호가
    보증한다 — 일반명사 이름이 충돌해도(라이브: 'public-data-website' 3연쇄) 폴더·슬롯이 안 섞인다."""
    import os as _os
    from src.guide_tools import deploy_service_name
    base = str(tmp_path)
    g = FakeGuide()
    s = Sys(g, guild_id=1, organt_builder=None, bot_info={11: "L"}, workspace=base)
    f = Flow(g, channel_id=500, guild_id=1, leader_id=11, bot_info={11: "L"})
    f.workspace = _os.path.join(base, "new-7")
    _os.makedirs(f.workspace)

    def _reg(ch, name):
        pid = s._register_project(ch, name, f.workspace, f.leader)
        f.workspace = s.projects[int(ch)]["workspace"]
        return pid
    f.register_project = _reg
    f.start_root("root")
    t = _tools(f, 11, "leader")
    asyncio.run(t["create_project"].handler({"name": "Public-Data-Website", "team": ""}))
    assert _os.path.basename(f.workspace).startswith(f.project_id.lower())   # 폴더 신원=번호
    assert _os.path.isdir(f.workspace)
    assert deploy_service_name(f, "내맘대로이름") == f"organt-{f.project_id.lower()}"  # 슬롯 신원=번호(작명 무시)


def test_프로젝트_등록은_원요청링크를_영속(tmp_path):
    """[졸업 라우팅의 전제] 등록은 '프로젝트를 탄생시킨 원요청 메시지 id'(origin_msg)를 영속한다 —
    부팅 복구가 졸업한 원요청을 재발사하지 않고 프로젝트 채널 개입으로 잇는 연결 고리.
    같은 채널 재등록은 기존 origin을 보존하고, 비어 있을 때만 백필한다."""
    s = Sys(FakeGuide(), guild_id=1, organt_builder=None, bot_info={11: "L"},
            session_dir=str(tmp_path), workspace=str(tmp_path))
    pid = s._register_project(500, "마법진 디펜스", str(tmp_path / "ws"), 11,
                              purpose="디펜스 게임", origin_msg="650442")
    assert s.projects[500]["origin_msg"] == "650442"
    s._register_project(500, "마법진 디펜스", str(tmp_path / "ws"), 11,
                        purpose="x", origin_msg="999999")        # 재등록은 기존 origin 보존
    assert s.projects[500]["origin_msg"] == "650442"
    s.projects[500]["origin_msg"] = ""                           # 구세대 등록(링크 없음) 백필 경로
    s._register_project(500, "마법진 디펜스", str(tmp_path / "ws"), 11, origin_msg="650442")
    assert s.projects[500]["origin_msg"] == "650442" and s.projects[500]["id"] == pid


def test_발언_안전망은_침묵절단하지_않는다():
    """[회의 품질] 발언 클립은 폭주만 막고, 잘리면 '잘렸다'고 표기한다 — 종전 하드컷([:300])이
    '3~5줄' 지시를 지킨 발언까지 단어 중간에서 침묵 절단해(라이브: 전 발언이 307~308자 박제,
    '…프론트엔'에서 끊김) 채널 기록과 다음 발언자의 토론 문맥을 함께 훼손하던 것 교정."""
    from src.guide_tools import _speech_clip
    assert _speech_clip("  짧은 발언  ") == "짧은 발언"            # 무손실 + 트림
    long = "가" * 2000
    out = _speech_clip(long)
    assert out.startswith("가" * 1500) and "2000자" in out and "잘림" in out   # 명시 마커
    assert _speech_clip("나" * 1500) == "나" * 1500               # 경계는 무손실
    assert _speech_clip(None) == ""


def test_진행중_프로젝트의_채널은_재등록이_못_옮긴다(tmp_path):
    """[채널 하이재킹 가드] 같은 작품(이름·목적 유사)을 다른 채널에서 다시 등록해도, 미완 Task가
    영속된 '진행 중' 프로젝트의 채널·open_task는 원래 자리를 지킨다 — 라이브: 동면 복구 재발사가
    새 채널을 파고 create_project → 원래 작업 채널에서 신원·토픽이 떨어져 나가 '기존 채널이 죽고
    새 채널에서 처음부터'가 됐다(사용자 지적). 미완 Task가 없으면(쉬는 작품) 기존처럼 이동 허용."""
    s = Sys(FakeGuide(), guild_id=1, organt_builder=None, bot_info={11: "L"},
            session_dir=str(tmp_path), workspace=str(tmp_path))
    pid = s._register_project(500, "마법진 디펜스", str(tmp_path / "ws"), 11, purpose="디펜스 게임")
    s.projects[500]["open_task"] = {"task_id": "065442-1"}      # 진행 중 표식(크래시-세이프 스냅샷)
    assert s._register_project(900, "마법진 디펜스", str(tmp_path / "w2"), 11,
                               purpose="디펜스 게임") == pid    # 신원은 돌려주되
    assert 500 in s.projects and s.projects[500]["channel"] == 500   # 채널은 원래 자리
    assert 900 not in s.projects
    assert s.projects[500]["open_task"]["task_id"] == "065442-1"     # 미완 Task 보존
    s.projects[500]["open_task"] = None                          # 쉬는 작품(마감 완료)이면
    assert s._register_project(900, "마법진 디펜스", str(tmp_path / "w2"), 11,
                               purpose="디펜스 게임") == pid
    assert s.projects[900]["channel"] == 900 and 500 not in s.projects   # 기존 이동 동작 유지


def test_교차검증_같은직군은_에코_다른도메인_독립검증_요구():
    """[독립 검증 = 다른 도메인 (동질 모델 원리)] 같은 Claude·같은 직군 검증자는 에코(같은 관점=같은 맹점)라
    독립 검증이 아니다. owner와 다른 도메인의 도달 가능한 검증자가 있으면 그 독립 검증을 요구하고(같은 직군만
    검증하면 보류), 다른 도메인 동료가 없으면(단일도메인) 같은 직군 검증으로 폴백(교착 방지)."""
    g = FakeGuide()
    f = _flow(g)
    f.bot_info[12] = "백엔드"; f.bot_info[13] = "백엔드"; f.bot_info[14] = "프론트엔드"  # 12=owner, 13=같은직군, 14=다른도메인
    f.project_team += [13, 14]
    t = _tools(f, 11, "leader")
    asyncio.run(t["create_task"].handler({"members": "12,13,14"}))
    f.current.participated.update({12, 13, 14})
    asyncio.run(t["set_goal"].handler({"goal": "g"}))
    f.current.owner, f.current.owner_delivered, f.current.verified = 12, True, True
    f.act_by[12] = 5; f.act_by[13] = 1; f.act_by[14] = 1                       # 기여 게이트 통과
    f.current.cross_checks = 1; f.current.cross_check_offdomain = 0            # 같은 직군(13)만 검증 = 에코
    r1 = asyncio.run(t["complete_task"].handler({"result": "끝"}))
    assert "완료 거부" in r1["content"][0]["text"] and "다른 도메인" in r1["content"][0]["text"]   # 독립 검증 요구
    assert f.current is not None
    f.current.cross_check_offdomain = 1                                        # 다른 도메인(14)이 독립 검증
    r2 = asyncio.run(t["complete_task"].handler({"result": "끝"}))
    assert f.current is None                                                   # 독립 검증 후 마감


def test_complete_task_최대성_기준이_교차검증에_주입_PHASE3():
    """[최대화 — PHASE 3 lynchpin] flow.standard(최대 표준)가 설정되면 마감 교차검증 메시지에 '최대성 기준
    대조'가 주입된다 — 검증자(다른 도메인)가 *돌아가나*가 아니라 *실제 최대만큼인가*를 워크스페이스 실측으로
    대조. P-018식 얕은 마감(표준=AI·웹인데 산출=서버만)을 마감 단계에서 잡는 지점."""
    g = FakeGuide()
    f = _flow(g)
    f.bot_info[12] = "백엔드"; f.bot_info[13] = "프론트엔드"     # owner=12, off-domain 검증자=13
    f.project_team += [13]
    t = _tools(f, 11, "leader")
    asyncio.run(t["create_task"].handler({"members": "12,13"}))
    f.current.participated.update({12, 13})
    asyncio.run(t["set_goal"].handler({"goal": "공공데이터 AI 웹사이트",
                                       "standard": "최대 표준: 학습모델·인터랙티브 프론트·시각화",
                                       "interfaces": "백→프 JSON 포맷 {city,aqi,grade}"}))
    assert "학습모델" in f.current.standard and "JSON 포맷" in f.current.interfaces   # 표준·인터페이스 영속
    f.current.owner, f.current.owner_delivered, f.current.verified = 12, True, True
    f.act_by[12] = 5; f.act_by[13] = 1
    f.current.cross_checks = 0                                  # 검증 0 → 게이트 보류
    txt = asyncio.run(t["complete_task"].handler({"result": "끝"}))["content"][0]["text"]
    assert "완료 거부" in txt and "최대성 기준" in txt and "학습모델" in txt   # 표준이 검증에 주입(below-max)
    assert "통합 검증" in txt and "JSON 포맷" in txt             # 인터페이스 계약 검증(L2)도 주입(사일로 차단)


def test_교차검증_의무_제3멤버가_있으면_단독마감_불가():
    """[교차 검증 의무 — Rule/Task.md 6, 범용 이치의 하드 제한(사용자 확정)] owner 아닌 멤버의
    검증 참여 없이는 완수 선언 불가(제3멤버가 있는 한 우회 없음 — 재호출도 거부). 라이브 P-009:
    단독 마감이 브라우저 렉·적 돌진 등 사용성 결함을 통과시킴(사용자가 첫 발견). 검증 응답이
    돌아오면 게이트는 자동으로 열린다. 제3멤버가 정말 없는 팀만 예외(단독 마감 마커가 기록에)."""
    g = FakeGuide()
    f = _flow(g)
    f.bot_info[13] = "잠수"
    f.project_team.append(13)
    t = _tools(f, 11, "leader")
    asyncio.run(t["create_task"].handler({"members": "12,13"}))
    f.current.participated.add(12)
    f.current.participated.add(13)
    asyncio.run(t["set_goal"].handler({"goal": "1) 기능 A\n2) 기능 B\n3) 기능 C\n4) 기능 D"}))
    f.current.owner, f.current.owner_delivered, f.current.verified = 12, True, True
    f.act_by[12] = 5                                               # owner는 실작업 있음
    r1 = asyncio.run(t["complete_task"].handler({"result": "끝"}))
    txt1 = r1["content"][0]["text"]
    assert "완료 거부" in txt1 and f.current is not None           # 거부
    assert "각 부분이 '존재하나'가 아니라" in txt1                 # 항목 '수'·'존재' 아닌 '체험'으로 각 부분 검증(RFC-011 M2)
    assert "실작업·검증 참여 0" in txt1                            # 잠수 멤버(13) 가시화
    r2 = asyncio.run(t["complete_task"].handler({"result": "끝"}))
    assert "완료 거부" in r2["content"][0]["text"] and f.current is not None   # 재호출도 거부(우회 없음)
    f.current.cross_checks = f.current.cross_check_offdomain = 1                                     # 검증 응답 도착
    f.act_by[13] = 1                                               # 검증자(13)가 실제로 run 검증함(기여 게이트 통과)
    r3 = asyncio.run(t["complete_task"].handler({"result": "끝"}))
    assert f.current is None and "거부" not in r3["content"][0]["text"]        # 게이트 자동 개방
    assert "단독 마감" not in f.tasks[0].status.result             # 교차 검증 마감 — 마커 없음
    # 제3멤버가 없는 팀(leader+owner뿐) → 예외 허용 + 단독 마감 마커
    asyncio.run(t["create_task"].handler({"members": "12"}))
    f.current.participated.add(12)
    asyncio.run(t["set_goal"].handler({"goal": "g2"}))
    f.current.owner, f.current.owner_delivered, f.current.verified = 12, True, True
    r4 = asyncio.run(t["complete_task"].handler({"result": "끝"}))
    assert f.current is None and "거부" not in r4["content"][0]["text"]
    assert "단독 마감" in f.tasks[1].status.result                 # 침묵 강행 불가 — 기록에 보임


def test_검증게이트에_owner직군_직무기준이_루브릭으로_주입된다():
    """[RFC-008 P0] 교차 검증 거부 시, owner 산출물 도메인의 직무 기준(craft profile)을 검증 루브릭으로
    제공한다 — QA가 '작동하는가'(holistic)가 아니라 '이 기준 대비 충분한가'를 차원별로 보게(rubric-guided
    judge가 인간 일치를 +20pt; 측정 가능한 기능만 보면 품질이 빠지는 Holmström-Milgrom 함정의 처방).
    craft profile이 없으면 루브릭은 비고(검증자가 먼저 기준을 쓰는 기존 경로), 겸직은 직군별로 합친다."""
    g = FakeGuide()
    f = _flow(g)
    f.bot_info[12] = "백엔드·QA"          # 겸직 owner
    f.bot_info[13] = "프론트"
    f.project_team.append(13)
    f.craft_of = lambda job: {"백엔드": "엣지·경계값을 시뮬로 직접 재현해 검증한다",
                              "QA": "실플레이 시나리오를 끝까지 재현한다"}.get(str(job).strip(), "")
    t = _tools(f, 11, "leader")
    asyncio.run(t["create_task"].handler({"members": "12,13"}))
    f.current.participated.add(12); f.current.participated.add(13)
    asyncio.run(t["set_goal"].handler({"goal": "g"}))
    f.current.owner, f.current.owner_delivered, f.current.verified = 12, True, True
    r = asyncio.run(t["complete_task"].handler({"result": "끝"}))
    txt = r["content"][0]["text"]
    assert "완료 거부(교차 검증" in txt
    assert "검증 루브릭" in txt and "백엔드·QA" in txt          # 산출물 도메인 명시
    assert "엣지·경계값을 시뮬로" in txt and "실플레이 시나리오" in txt   # 겸직 두 직군 craft 합쳐 주입
    # craft profile이 없는 owner → 루브릭 비고(기존 거부 메시지는 유지)
    f.current.cross_checks = f.current.cross_check_offdomain = 1                                    # 게이트 통과시켜 새 Task로
    f.act_by[13] = 1                                             # 검증자(13)가 실제로 run 검증함(기여 게이트 통과)
    asyncio.run(t["complete_task"].handler({"result": "끝"}))
    asyncio.run(t["create_task"].handler({"members": "13"}))
    f.current.participated.add(13)
    asyncio.run(t["set_goal"].handler({"goal": "g2"}))
    f.bot_info[13] = "프론트"; f.project_team.append(99); f.bot_info[99] = "디자이너"
    f.current.team.append(99)
    f.current.owner, f.current.owner_delivered, f.current.verified = 13, True, True   # 프론트=craft 없음
    r2 = asyncio.run(t["complete_task"].handler({"result": "끝"}))
    assert "완료 거부(교차 검증" in r2["content"][0]["text"] and "검증 루브릭" not in r2["content"][0]["text"]


def test_팀기여의무_부른직군_실작업0이면_증거명시필요_RFC009():
    """[팀 기여 의무 — 증거/명시 통과(2026-06-15 라이브 교정)] 교차 검증(cross_checks)과 **독립**. 팀에
    부른 직군이 회의 발언만 하고 실작업·검증 0(act_by==0)이면 완료를 보류한다. soft '1회 보류 후 재호출
    통과'는 마감 관성에 무력했으므로(라이브 3/3 반사적 통과로 폴리시 또 빠짐), percept와 같은 원리로
    강화 — 잠수 직군이 실제로 기여(idle 해소)하거나 '[기여 불필요]'로 의식적 명시해야 통과, **반사적
    재호출로는 안 닫힌다**. 라이브 P-010: VFX·디자이너·사운드 등 폴리시 직군이 실구현 0인 채 마감돼
    "단순 나열 웹·타격감 없는 게임"이 됨(발언≠기여). 무한 반려 아님(명시 탈출구 — 판단은 리더). ① Work 위임
    ② 팀에서 빼기 ③ 재호출 통과 — 1회만 보류(무한 반려 금지, 판단은 리더)."""
    g = FakeGuide()
    f = _flow(g)
    f.bot_info[13] = "QA"; f.bot_info[14] = "VFX"
    f.project_team += [13, 14]
    t = _tools(f, 11, "leader")
    asyncio.run(t["create_task"].handler({"members": "12,13,14"}))
    for m in (12, 13, 14):
        f.current.participated.add(m)
    asyncio.run(t["set_goal"].handler({"goal": "타격감 있는 횡스크롤 게임"}))
    f.current.owner, f.current.owner_delivered, f.current.verified = 12, True, True
    f.act_by[12] = 5                       # owner 실구현
    f.act_by[13] = 2                       # QA가 실제로 run 검증 → cross_checks를 올린 주체
    f.current.cross_checks = f.current.cross_check_offdomain = 1             # 교차 검증은 통과 상태
    # 14(VFX)는 act_by==0: 회의 발언만 함 → 폴리시가 작품에 반영 안 됨
    r1 = asyncio.run(t["complete_task"].handler({"result": "끝"}))
    txt1 = r1["content"][0]["text"]
    assert "완료 보류(팀 기여 의무" in txt1 and f.current is not None      # 보류(증거/명시 필요)
    assert "VFX" in txt1                                                 # 잠수 직군 지목
    assert "request(Work)" in txt1 and "팀에서 빼" in txt1 and "[기여 불필요]" in txt1  # 3선택지(③=명시 마커)
    assert f.current.contrib_checked is False                           # 보류는 통과 아님 → 미마킹
    r2 = asyncio.run(t["complete_task"].handler({"result": "끝"}))       # 반사적 재호출 → 여전히 보류
    assert f.current is not None and "완료 보류(팀 기여 의무" in r2["content"][0]["text"]  # no-op 차단
    r3 = asyncio.run(t["complete_task"].handler({"result": "[기여 불필요] VFX는 이 작품에 불요"}))  # 의식적 명시
    assert f.current is None                                            # 명시 통과·마감(판단은 리더)


def test_팀기여의무_잠수직군이_실제기여하면_명시없이_통과_RFC009():
    """기여 게이트의 정상 경로: 보류 후 잠수 직군에게 실제로 Work를 맡겨 그가 일하면(act_by>0) idle이
    해소돼 명시 없이도 통과 — '실제 기여'가 곧 증거. 게이트의 목적(폴리시가 작품에 반영되게)이 충족되면
    마찰 없이 닫힌다(증거 통과형). 반사적 재호출만 막고, 실제로 한 일은 막지 않는다."""
    g = FakeGuide()
    f = _flow(g)
    f.bot_info[13] = "VFX 전문가"; f.project_team.append(13)
    t = _tools(f, 11, "leader")
    asyncio.run(t["create_task"].handler({"members": "12,13"}))
    f.current.participated.add(12); f.current.participated.add(13)
    asyncio.run(t["set_goal"].handler({"goal": "타격감 있는 게임"}))
    f.current.owner, f.current.owner_delivered, f.current.verified = 12, True, True
    f.act_by[12] = 5; f.act_by[13] = 0; f.current.cross_checks = f.current.cross_check_offdomain = 1
    r1 = asyncio.run(t["complete_task"].handler({"result": "끝"}))       # VFX 잠수 → 보류
    assert "완료 보류(팀 기여 의무" in r1["content"][0]["text"] and f.current is not None
    f.act_by[13] = 3                                                     # VFX가 실제로 기여(idle 해소)
    r2 = asyncio.run(t["complete_task"].handler({"result": "VFX 타격감 반영 완료"}))  # 명시 없이도 통과
    assert f.current is None                                            # 실제 기여가 증거 → 마감


def test_팀기여의무_게이트는_잠수직군_회의발언을_되돌린다_RFC009():
    """[RFC-009 2단계 정수 — 발언→책임] 기여 게이트가 잠수 직군 '본인의 회의 발언'을 collab_notes
    (화자 귀속 미니츠 '[NR] 직군: 발언')에서 끌어와 그대로 보여준다 — '당신이 회의에서 한 말이
    산출물에 들어갔나?'(발언≠구현). 직군 키워드 없이 본인 발언만 에코. 별도 '발언→Task' 게이트
    없이 1단계 back-pressure + collab_notes 동봉으로 발언→구현 루프가 닫히는 것을 게이트가 환기."""
    g = FakeGuide()
    f = _flow(g)
    f.bot_info[14] = "VFX"
    f.project_team.append(14)
    t = _tools(f, 11, "leader")
    asyncio.run(t["create_task"].handler({"members": "12,14"}))
    f.current.participated.add(12); f.current.participated.add(14)
    asyncio.run(t["set_goal"].handler({"goal": "타격감 있는 게임"}))
    # 회의록: VFX가 발언했으나(화자 귀속) 실작업은 0 — 백엔드 발언은 오귀속 안 돼야
    f.current.collab_notes = ("[회의] 스펙 (2R)\n[1R] 백엔드: 상태머신 5단계\n"
                              "[1R] VFX: 타격감은 히트스톱+화면진동이 핵심")
    f.current.owner, f.current.owner_delivered, f.current.verified = 12, True, True
    f.act_by[12] = 5                      # owner만 실작업, VFX(14)는 act_by==0
    f.current.cross_checks = f.current.cross_check_offdomain = 1
    r = asyncio.run(t["complete_task"].handler({"result": "끝"}))
    txt = r["content"][0]["text"]
    assert "완료 보류(팀 기여 의무" in txt and "회의 발언 대조" in txt
    assert "히트스톱+화면진동" in txt              # VFX 본인 발언을 그대로 되돌림
    assert "상태머신" not in txt                   # 백엔드 발언은 잠수자(VFX)에 오귀속 안 됨


# ── RFC-011: 상용 품질 구조(현실 기준·체험대조 검증·취향 축적) ────────────────────────────
def test_워커도구에_WebSearch_포함_RFC011():
    """[RFC-011 M1] 워커 기본 도구에 WebSearch/WebFetch가 있어야 '훌륭한 예'를 상상이 아니라
    실제로 검색해 대조한다(취향 천장 ~0.5 → 외부 레퍼런스가 '상용 수준'의 기준)."""
    from src.main import WORKER_BASE_TOOLS
    assert "WebSearch" in WORKER_BASE_TOOLS and "WebFetch" in WORKER_BASE_TOOLS


def test_범주점검_보류가_WebSearch_실제예시_요구_RFC011():
    """[RFC-011 M1] P7 범주적 완성 점검 보류는 '훌륭한 예를 떠올려'가 아니라 'WebSearch로 실제로
    찾아' 대조하라고 요구한다(상상=자기 산출 기준 → '평범=충분' 수렴 차단)."""
    g = FakeGuide()
    f = _flow(g)
    f.gap_checked = False                          # P7 보류를 실제로 발동
    t = _tools(f, 11, "leader")
    asyncio.run(t["create_task"].handler({"members": "12"}))
    f.current.participated.add(12)
    txt = asyncio.run(t["set_goal"].handler({"goal": "g"}))["content"][0]["text"]
    assert "확정 보류" in txt and "WebSearch" in txt and "실제 훌륭한 예" in txt   # 외부 실레퍼런스 대조 요구(최대화)


def test_set_goal_누적사용자취향_품질기준으로_재생_RFC011():
    """[RFC-011 M3] 흐름에 누적된 사용자 취향(반복 비평)을 set_goal이 '진짜 품질 기준'으로 되돌린다 —
    사용자 자신의 말이라 직군·키워드 하드코딩 0. 피드백이 없으면 그 노트는 안 붙는다."""
    g = FakeGuide()
    f = _flow(g)
    f.user_feedback = [{"ts": 1, "text": "이펙트 구림 캐릭터 디자인 구림"},
                       {"ts": 2, "text": "기본공격 없어서 지루"}]
    t = _tools(f, 11, "leader")
    asyncio.run(t["create_task"].handler({"members": "12"}))
    f.current.participated.add(12)
    txt = asyncio.run(t["set_goal"].handler({"goal": "1) 개선 A"}))["content"][0]["text"]
    assert "누적 사용자 취향" in txt
    assert "이펙트 구림" in txt and "기본공격" in txt        # 사용자 말이 그대로 기준으로
    # 누적 취향이 없으면(빈 프로젝트) 그 노트는 붙지 않는다
    f2 = _flow(g)
    t2 = _tools(f2, 11, "leader")
    asyncio.run(t2["create_task"].handler({"members": "12"}))
    f2.current.participated.add(12)
    txt2 = asyncio.run(t2["set_goal"].handler({"goal": "g"}))["content"][0]["text"]
    assert "누적 사용자 취향" not in txt2


def test_교차검증_체험대조_요구하고_누적취향_주입_RFC011():
    """[RFC-011 M2] 교차검증 거부 메시지는 'presence(요소 존재·에러0·기동)는 좋음의 증거 아님'을
    명시하고, '체험+WebSearch 예시대조'를 요구하며, 누적 사용자 취향을 검증에 주입한다."""
    g = FakeGuide()
    f = _flow(g)
    f.user_feedback = [{"ts": 1, "text": "브금 없음 사운드 애매"}]
    f.bot_info[13] = "QA"; f.project_team.append(13)
    t = _tools(f, 11, "leader")
    asyncio.run(t["create_task"].handler({"members": "12,13"}))
    f.current.participated.add(12); f.current.participated.add(13)
    asyncio.run(t["set_goal"].handler({"goal": "1) A\n2) B"}))
    f.current.owner, f.current.owner_delivered, f.current.verified = 12, True, True
    f.act_by[12] = 5
    txt = asyncio.run(t["complete_task"].handler({"result": "요소 다 존재, JS 에러 0"}))["content"][0]["text"]
    assert "완료 거부" in txt
    assert "'작동'이지 '좋음'" in txt                       # presence-only 반려(M2)
    assert "WebSearch로 실제로 찾아 대조" in txt             # 체험·예시대조(M1+M2)
    assert "스크린샷" in txt and "눈으로 보고" in txt          # 자율 비전 검증 — DOM 존재가 아니라 '실제로 보이는 것'(M2')
    assert "사용자가 반복해 지적한 것" in txt and "브금 없음" in txt   # 누적 취향 주입(M3)


def test_record_user_feedback_프로젝트에_누적_dedup_바운드_RFC011():
    """[RFC-011 M3] 사용자 발화를 그 프로젝트에 누적(연속 동일 dedup, 미등록 채널 skip, 최근 50 바운드,
    영속 호출). 누적이 set_goal·검증의 품질 앵커가 된다(배포→플레이→비평 회차마다 기준 상승)."""
    from types import SimpleNamespace
    saved = []
    stub = SimpleNamespace(projects={500: {"id": "P-010"}},
                           _save_projects=lambda: saved.append(1))
    Sys.record_user_feedback(stub, 500, "이펙트 구림 사운드 애매함")
    Sys.record_user_feedback(stub, 500, "이펙트 구림 사운드 애매함")   # 연속 동일 → dedup
    Sys.record_user_feedback(stub, 500, "기본공격이 없어 지루")
    fb = stub.projects[500]["feedback"]
    assert [x["text"] for x in fb] == ["이펙트 구림 사운드 애매함", "기본공격이 없어 지루"]
    assert saved                                            # 영속 호출됨
    Sys.record_user_feedback(stub, 999, "x")               # 미등록 채널 → skip
    assert 999 not in stub.projects
    for i in range(60):                                    # 용량 바운드(최근 50)
        Sys.record_user_feedback(stub, 500, f"비평{i}")
    assert len(stub.projects[500]["feedback"]) == 50


def test_팀기여의무_전원_실작업하면_보류없음_RFC009():
    """[팀 기여 의무 — RFC-009 음성 케이스] 팀 전원(리더·owner 제외)이 실작업·검증을 했으면(act_by>0)
    기여 게이트는 발동하지 않는다 — 폴리시 직군도 실제로 만들면 즉시 통과(부른 직군이 기여하면 OK)."""
    g = FakeGuide()
    f = _flow(g)
    f.bot_info[13] = "QA"; f.bot_info[14] = "VFX"
    f.project_team += [13, 14]
    t = _tools(f, 11, "leader")
    asyncio.run(t["create_task"].handler({"members": "12,13,14"}))
    for m in (12, 13, 14):
        f.current.participated.add(m)
    asyncio.run(t["set_goal"].handler({"goal": "타격감 있는 게임"}))
    f.current.owner, f.current.owner_delivered, f.current.verified = 12, True, True
    f.act_by[12] = 5; f.act_by[13] = 2; f.act_by[14] = 3   # VFX도 실제로 이펙트 구현함
    f.current.cross_checks = f.current.cross_check_offdomain = 1
    r = asyncio.run(t["complete_task"].handler({"result": "끝"}))
    assert f.current is None and "보류" not in r["content"][0]["text"]    # 즉시 마감


def test_의견수렴_안내는_meet_권장한다_라이브0퍼센트채택():
    """[meet 채택 유도 — 라이브 분석: meet/vote 0% 채택, 리더가 1:1 Info로만 폴링→앵커링·합의 미기록]
    create_task와 set_goal(미협의) 안내가 의견 수렴을 **meet(회의)**로 권장한다(1:1 Info 순차가 아니라).
    합의가 또렷·빠르고 회의록(collab_notes)이 자동으로 남아 구현자에게 전달된다."""
    g = FakeGuide()
    f = _flow(g)
    t = _tools(f, 11, "leader")
    rc = asyncio.run(t["create_task"].handler({"members": "12"}))["content"][0]["text"]
    assert "meet" in rc and "회의" in rc                       # meet 권장
    assert ("앵커링" in rc) or ("회의록" in rc)                 # 이유 명시
    rg = asyncio.run(t["set_goal"].handler({"goal": "g"}))["content"][0]["text"]  # 12 미협의 → 거부
    assert "확정 거부" in rg and "meet" in rg                  # 거부 안내도 meet 권장


def test_setgoal_품질차원_팀구성유도_폴리시채용_환기_RFC009():
    """[RFC-009 3단계 — 상류 폴리시 의식] set_goal 안내가 ① 팀 구성에서 품질 축을 유도(팀 직군을
    나열해 각 도메인 품질을 '완성'의 축으로) ② 폴리시 직군이 팀에 있는지 보고 없으면 recruit하라고
    환기 — '게임이면 VFX' 같은 직군 키워드 하드코딩 없이(작품 종류 판단은 리더)."""
    g = FakeGuide()
    f = _flow(g)
    f.bot_info[13] = "VFX 전문가"; f.project_team.append(13)
    t = _tools(f, 11, "leader")
    asyncio.run(t["create_task"].handler({"members": "12,13"}))
    f.current.participated.add(12); f.current.participated.add(13)
    txt = asyncio.run(t["set_goal"].handler({"goal": "게임"}))["content"][0]["text"]
    assert "품질 차원" in txt
    assert "VFX 전문가" in txt                  # 팀 직군 나열(구성에서 품질 축 유도)
    assert "recruit" in txt and "폴리시" in txt  # 없으면 채용 환기


def test_setgoal_발산수렴_완성재정의_RFC010():
    """[RFC-010 P3·P5] set_goal 안내가 ① 자명한 1개로 수렴 말고 복수 접근안 비교(발산→수렴) ②
    '작동=완성'이 아니라 '써보니 좋다'가 완성(작동≠좋음, 마감 전 실플레이 비평+1회 개선)을 환기."""
    g = FakeGuide()
    f = _flow(g)
    t = _tools(f, 11, "leader")
    asyncio.run(t["create_task"].handler({"members": "12"}))
    f.current.participated.add(12)
    txt = asyncio.run(t["set_goal"].handler({"goal": "게임"}))["content"][0]["text"]
    assert "발산→수렴" in txt or "2~3개" in txt        # P3 복수안 비교
    assert "작동≠좋음" in txt and "써보니 좋다" in txt   # P5 완성 재정의(경험 기반)
    # P6(범용): 장르 예시 대비 '범주적 부재' 점검 + 신규 구축/recruit — 특정 범주(사운드 등) 미지정(하드코딩 없음)
    assert "범주적 부재" in txt and "신규 구축" in txt
    assert "recruit" in txt and "훌륭한 예" in txt        # 장르 예시 대비(리더가 범주 도출 — 시스템이 안 박음)
    assert "사운드" not in txt                            # 범용: 시스템이 특정 범주를 프라이밍하지 않음


def test_교차검증_경험적_비평_요구_RFC010():
    """[RFC-010 P1·P2] 교차검증 거부가 '코드만 읽지 말고 실제 실행·플레이 + 재밌나/아쉽나 비평'을 요구하고
    '만든 사람 아닌 다른 멤버'(자기검증 무효)를 못박는다 — 라이브 QA 0런(코드만 읽음)·노잼 구멍 처방."""
    g = FakeGuide()
    f = _flow(g)
    f.bot_info[13] = "QA"; f.project_team.append(13)
    t = _tools(f, 11, "leader")
    asyncio.run(t["create_task"].handler({"members": "12,13"}))
    f.current.participated.add(12); f.current.participated.add(13)
    asyncio.run(t["set_goal"].handler({"goal": "게임"}))
    f.current.owner, f.current.owner_delivered, f.current.verified = 12, True, True
    f.act_by[12] = 5                              # cross_checks=0 → 교차검증 게이트 발동
    txt = asyncio.run(t["complete_task"].handler({"result": "끝"}))["content"][0]["text"]
    assert "완료 거부(교차 검증" in txt
    assert "실제로 실행" in txt and "써보니 좋은가" in txt   # P1 경험적 비평(범용 — '재밌나' 게임 프라이밍 제거)
    assert "만든 사람이 아닌" in txt               # P2 분리된 검증자(자기검증 무효)


def test_setgoal_범주적완성_점검_1회보류_RFC010_P7():
    """[RFC-010 P7 — recognition→action 강제] set_goal 확정 전 1회(흐름당), 장르 예시 대비 '통째로 없는
    범주'(사운드 등)를 goal에 구축 대상으로 반영하거나 사유하게 보류한다 — 라이브: P6 넛지로 사운드를 grep
    점검만 하고 구현 0(인지≠행동). 1회 보류 후 재호출 통과(막지 않되 의식적 결정). participated 통과 후 발동."""
    g = FakeGuide()
    f = _flow(g)
    f.gap_checked = False            # 이 테스트는 P7 보류를 검증(_flow 기본 우회 해제)
    t = _tools(f, 11, "leader")
    asyncio.run(t["create_task"].handler({"members": "12"}))
    f.current.participated.add(12)
    r1 = asyncio.run(t["set_goal"].handler({"goal": "게임"}))            # 1회차: P7 보류
    assert "확정 보류(최대화 기준 점검" in r1["content"][0]["text"] and not f.current.status.goal
    assert "훌륭한 예" in r1["content"][0]["text"] and "구축" in r1["content"][0]["text"]   # 범용: 장르 예시 대비 + 구축
    assert "사운드" not in r1["content"][0]["text"]      # 시스템이 특정 범주를 지정·프라이밍하지 않음(하드코딩 없음)
    r2 = asyncio.run(t["set_goal"].handler({"goal": "게임 + 사운드 구축"}))   # 재호출: 통과
    assert f.current.status.goal == "게임 + 사운드 구축" and f.gap_checked is True   # 확정 + 흐름당 1회 마킹


def test_set_goal_최대화표준_standard_영속_PHASE1():
    """[최대화 — PHASE 1.2] set_goal의 standard 인자가 flow.current.standard에 영속 — 목적함수가 '요청 문자
    최소'가 아니라 '가용 외부자원으로 만들 수 있는 *최대*'임을 박는 외부 앵커(마감 검증이 이 최대 대비 갭으로
    판정). gap_check 메시지도 '최대화 기준'으로 재구성(임계값 만족 아님)."""
    g = FakeGuide()
    f = _flow(g)
    logged = []; f.log = lambda ev, **kw: logged.append((ev, kw))
    t = _tools(f, 11, "leader")
    asyncio.run(t["create_task"].handler({"members": "12"}))
    f.current.participated.add(12)
    # 핵심: standard는 *리더 단독 덮어쓰기*가 아니라 *도메인별 기여의 합집합*(누적) — 품질 바가 한 명에 인질 안 됨
    asyncio.run(t["set_goal"].handler({"goal": "공공데이터 AI 웹사이트", "standard": "AI 도메인 최대: 학습모델+평가지표"}))
    asyncio.run(t["set_goal"].handler({"goal": "공공데이터 AI 웹사이트", "standard": "프론트 도메인 최대: 인터랙티브 시각화"}))
    assert "학습모델" in f.current.standard and "시각화" in f.current.standard   # 두 도메인 기여가 *누적*(합집합)
    assert any(ev == "set_goal_standard_set" for ev, kw in logged)


def test_지각비대칭_증거명시통과_반사적재호출은_불가():
    """[지각 비대칭 — 실제 자원/명시 통과(2026-06-15 P-015 라이브 재강화)] soft '1회 보류 후 통과'는 마감
    관성에 무력했고, 그 뒤 외부소싱(WebFetch) 증거도 '레퍼런스 읽기'를 통과시켜 합성 placeholder가 샜다
    (P-015: 사운드=오실레이터, 에셋 0인데 WebFetch 11회). 증거를 '실제 에셋 파일 통합'으로 강화 — 작업공간에
    코드 아닌 실재 에셋이 있거나 '[지각차원 없음]' 의식적 명시가 있어야 통과하고, **반사적 재호출·읽기로는
    안 닫힌다**. 도메인 중립(에셋=실재물 파일), 무한 반려 아님(명시 탈출구 상시 — 판단은 리더)."""
    g = FakeGuide()
    f = _flow(g)
    f.percept_checked = False        # 이 테스트는 지각 비대칭 게이트를 검증(_flow 기본 우회 해제)
    # 작업공간에 실제 에셋 파일 없음(_flow는 workspace=None) — 합성 placeholder 상황
    t = _tools(f, 11, "leader")
    asyncio.run(t["create_task"].handler({"members": "12"}))
    f.current.participated.add(12)
    asyncio.run(t["set_goal"].handler({"goal": "게임 동작"}))   # gap_checked=True라 P7 통과
    f.current.verified = True
    r1 = asyncio.run(t["complete_task"].handler({"result": "playSound로 효과음 구현"}))
    assert "지각 비대칭" in r1["content"][0]["text"] and f.current is not None   # 보류(마감 안 됨)
    assert "WebSearch" in r1["content"][0]["text"] and "recruit" in r1["content"][0]["text"]  # 실자원·전문성 경로
    assert "듣거나 느껴야" in r1["content"][0]["text"]      # 지각 불가 차원 개념(도메인 중립 표현)
    assert "사운드" not in r1["content"][0]["text"]        # 특정 범주 프라이밍 없음(하드코딩 0)
    assert f.percept_checked is False                      # 보류는 통과 아님 → 아직 미마킹
    # 반사적 재호출(증거·명시 없음)은 여전히 보류 — soft no-op 차단(라이브 +0초 패스스루의 정확한 교정점)
    r2 = asyncio.run(t["complete_task"].handler({"result": "재확인 — 그냥 통과 시도"}))
    assert "지각 비대칭" in r2["content"][0]["text"] and f.current is not None   # 여전히 안 닫힘
    # 의식적 명시([지각차원 없음] 첫 줄)로만 통과 — 판단은 리더
    r3 = asyncio.run(t["complete_task"].handler({"result": "[지각차원 없음] 전부 화면·코드로 검증 가능한 퍼즐"}))
    assert "지각 비대칭" not in r3["content"][0]["text"] and f.current is None   # 명시 통과·마감
    assert f.percept_checked is True                       # 통과 시점에 마킹


def test_지각비대칭_실에셋있으면_명시없이_통과(tmp_path):
    """실제 제작 자원 파일(사운드·이미지 등 코드 아닌 에셋)이 작업공간에 있으면 percept 게이트는 명시
    없이도 통과 — '합성 placeholder가 아니라 실재 자원을 받아 통합했다'가 곧 증거(레퍼런스 *읽기*와 구분 —
    P-015 허점 교정). 진짜 자원을 받은 정상 경로는 마찰 없이 닫히고, 코드만(에셋 0)인 placeholder만 막힌다."""
    g = FakeGuide()
    f = _flow(g)
    f.percept_checked = False
    f.workspace = str(tmp_path)
    (tmp_path / "sfx_hit.mp3").write_bytes(b"\x00\x01ID3")   # 실제 에셋 파일(다운로드·통합 모사)
    t = _tools(f, 11, "leader")
    asyncio.run(t["create_task"].handler({"members": "12"}))
    f.current.participated.add(12)
    asyncio.run(t["set_goal"].handler({"goal": "게임 동작"}))
    f.current.verified = True
    r = asyncio.run(t["complete_task"].handler({"result": "효과음 CC0 mp3 받아 통합"}))
    assert "지각 비대칭" not in r["content"][0]["text"] and f.current is None   # 실에셋 증거로 통과·마감
    assert f.percept_checked is True


def test_수용계약_포착과_마감바인딩_회의전문성_코드도달_강제():
    """[수용 계약 — 회의 전문성이 '코드'에 도달했는가] 회의가 합의한 '좋음'의 구체 기준(set_goal acceptance)이
    마감에 구속된다 — 각 항목 충족 증거('[수용기준 검증]' 회계) 또는 의식적 드롭/N·A 명시가 있어야 통과하고,
    반사적 재호출로는 안 닫힌다(percept·contrib와 동 원리). 라이브 P-015: 회의 제안 6개 중 코드 반영 0인데
    마감('플레이하면 감이 없다')의 정확한 차단점. 도메인 중립(기준은 팀 자작), 자율 보존(드롭/N·A 상시)."""
    g = FakeGuide()
    f = _flow(g)
    f.acceptance_checked = False        # 이 테스트는 수용 계약 게이트를 검증(_flow 기본 우회 해제)
    t = _tools(f, 11, "leader")
    asyncio.run(t["create_task"].handler({"members": "12"}))
    f.current.participated.add(12)
    # set_goal에 acceptance(수용 기준) 박기 — 회의 제안이 구속력 있는 계약이 됨(포착·누적)
    asyncio.run(t["set_goal"].handler({"goal": "게임 동작", "acceptance": "처치 시 히트스톱 80ms"}))
    asyncio.run(t["set_goal"].handler({"goal": "게임 동작", "acceptance": "콤보 사운드 스택"}))   # 누적
    assert "히트스톱" in (f.current.acceptance or "") and "콤보" in (f.current.acceptance or "")   # 포착·누적됨
    f.current.verified = True
    # ① 계약 있는데 항목별 회계(마커) 없이 마감 시도 → 보류(합의 기준을 되돌려 대조 강제)
    r1 = asyncio.run(t["complete_task"].handler({"result": "다 됐음"}))
    assert "수용 계약" in r1["content"][0]["text"] and f.current is not None   # 보류(마감 안 됨)
    assert "히트스톱" in r1["content"][0]["text"]            # 합의 기준을 되돌려 '코드 도달' 확인 강제
    # ② 반사적 재호출(마커 없음)도 여전히 보류 — soft no-op 차단(P-015 +0초 패스스루 교정)
    r2 = asyncio.run(t["complete_task"].handler({"result": "그냥 통과 시도"}))
    assert "수용 계약" in r2["content"][0]["text"] and f.current is not None
    assert f.acceptance_checked is False                    # 보류는 통과 아님 → 아직 미마킹
    # ③ '[수용기준 검증]' 헤더 + 항목별 회계(충족·증거/드롭)로만 통과 — 판단은 리더
    r3 = asyncio.run(t["complete_task"].handler(
        {"result": "[수용기준 검증] 히트스톱: app.js 구현·run 확인 / 콤보 사운드: [드롭] 다음 흐름으로"}))
    assert "수용 계약" not in r3["content"][0]["text"] and f.current is None   # 회계로 통과·마감
    assert f.acceptance_checked is True


def test_수용계약_미정의시_구체기준_요구_또는_NA명시로만_통과():
    """수용 계약이 아예 없으면(set_goal에 acceptance 미입력) 마감은 '좋음(상용)의 구체 기준'을 요구한다 —
    훌륭한 예 대조로 기준을 세워 회계하거나, 정말 품질 기준이랄 게 없는 단순 산출물이면 '[수용기준 N/A]'로
    의식적 명시해야 통과(반사적 재호출 불가). 단순 요청을 죽이지 않는 명시 탈출구 — 판단은 리더."""
    g = FakeGuide()
    f = _flow(g)
    f.acceptance_checked = False
    t = _tools(f, 11, "leader")
    asyncio.run(t["create_task"].handler({"members": "12"}))
    f.current.participated.add(12)
    asyncio.run(t["set_goal"].handler({"goal": "동작"}))   # acceptance 미입력
    f.current.verified = True
    r1 = asyncio.run(t["complete_task"].handler({"result": "끝"}))
    assert "수용 계약 미정의" in r1["content"][0]["text"] and f.current is not None  # 구체 기준 요구
    assert "훌륭한 예" in r1["content"][0]["text"]          # 외부 실재 대조로 기준 도출 유도
    # N/A 의식적 명시로만 통과(단순 산출물 — 판단은 리더)
    r2 = asyncio.run(t["complete_task"].handler({"result": "[수용기준 N/A] 내부 유틸 스크립트라 체감 품질 차원 없음"}))
    assert "수용 계약" not in r2["content"][0]["text"] and f.current is None
    assert f.acceptance_checked is True


def test_저작다양성_한직군독점이면_보류_단일도메인명시나_분산으로_통과():
    """[메커니즘② 저작 다양성 — 도메인 전문가 저작 강제] 파일 저작이 한 직군에 ≥80% 집중되면(P-017: 백엔드
    혼자 20중 19 → 단일 app.js 모놀리스, 도메인 전문가 부재) 마감을 1회 보류하고 외부 예시 대조를 강제한다 —
    '[단일도메인]' 명시(정말 단일 도메인일 때)나 빠진 도메인 전문가 recruit로만 통과. 반사적 재호출론 안 됨.
    도메인 중립(특정 직군 하드코딩 0), 출구 게이트, 분산 저작이면 미발동(깊은 P-002=7직군은 안 걸림)."""
    g = FakeGuide()
    f = _flow(g)
    f.authorship_checked = False        # 이 테스트는 저작 다양성 게이트를 검증(_flow 기본 우회 해제)
    t = _tools(f, 11, "leader")
    asyncio.run(t["create_task"].handler({"members": "12"}))
    f.current.verified = True
    f.current.leader_writes = 1          # has_product=True(리더 직접구현 산출물 존재), owner=0이라 owner게이트 우회
    f.current.cross_checks = f.current.cross_check_offdomain = 1           # 교차검증 게이트 우회(별도 테스트)
    f.current.contrib_checked = True     # 기여 게이트 우회(별도 테스트)
    # P-017 패턴: 백엔드 1명이 저작 독점(19/20=95%)
    f.writes_by_role = {"백엔드": 19, "프론트엔드": 1}
    r1 = asyncio.run(t["complete_task"].handler({"result": "AI 웹 완성"}))
    assert "저작 다양성" in r1["content"][0]["text"] and f.current is not None   # 보류(마감 안 됨)
    assert "백엔드" in r1["content"][0]["text"] and "95%" in r1["content"][0]["text"]   # 집중 직군·집중도 명시
    assert "recruit" in r1["content"][0]["text"]            # 빠진 도메인 전문가 채용 경로 안내
    # 반사적 재호출(마커 없음)도 여전히 보류
    r2 = asyncio.run(t["complete_task"].handler({"result": "그냥 통과 시도"}))
    assert "저작 다양성" in r2["content"][0]["text"] and f.current is not None
    assert f.authorship_checked is False                   # 보류는 통과 아님
    # '[단일도메인]' 의식적 명시로만 통과(정말 단일 도메인일 때)
    r3 = asyncio.run(t["complete_task"].handler({"result": "[단일도메인] 순수 백엔드 배치 스크립트라 한 직군이 적정"}))
    assert "저작 다양성" not in r3["content"][0]["text"] and f.current is None   # 명시 통과·마감
    assert f.authorship_checked is True


def test_저작다양성_여러직군_분산저작이면_미발동():
    """저작이 여러 직군에 분산되면(깊은 P-002=7직군 각자 도메인 모듈) 게이트가 발동하지 않는다 — 정상 협업은
    마찰 0. '한 명이 다 씀'만 잡고 '여러 전문가가 각자 씀'은 통과(거짓양성 없음)."""
    g = FakeGuide()
    f = _flow(g)
    f.authorship_checked = False
    t = _tools(f, 11, "leader")
    asyncio.run(t["create_task"].handler({"members": "12"}))
    f.current.verified = True
    f.current.leader_writes = 1
    f.current.cross_checks = f.current.cross_check_offdomain = 1
    f.current.contrib_checked = True
    # P-002 패턴: 여러 직군이 분산 저작(최대 점유 ~32%)
    f.writes_by_role = {"백엔드": 6, "VFX 전문가": 5, "프론트엔드": 4, "사운드 디자이너": 3}
    r = asyncio.run(t["complete_task"].handler({"result": "분산 협업 완성"}))
    assert "저작 다양성" not in r["content"][0]["text"] and f.current is None   # 미발동·정상 마감
    assert f.authorship_checked is True


def test_기여미흡_명시마감은_기록과_로그에_남는다_RFC009():
    """[게이트 강화 — 침묵 강행 불가] 잠수 직군이 실작업 0인 채 기여 게이트를 '[기여 불필요]' 명시로
    통과해 마감하면(옵션③), '[기여 미흡: … 실작업 0 — 리더 판단 마감]'이 Task 결과에 박히고
    task_contrib_overridden 로그가 남는다 — 막진 않되(리더 자율) 사후 분석·사용자·학습이 한눈에 보게
    (단독 마감 마커와 같은 정신). 단 통과는 의식적 명시여야 한다(반사적 재호출 불가 — 라이브 교정)."""
    g = FakeGuide()
    f = _flow(g)
    f.bot_info[13] = "VFX 전문가"; f.project_team.append(13)
    logs = []
    f.log = lambda ev, **k: logs.append((ev, k))
    t = _tools(f, 11, "leader")
    asyncio.run(t["create_task"].handler({"members": "12,13"}))
    f.current.participated.add(12); f.current.participated.add(13)
    asyncio.run(t["set_goal"].handler({"goal": "타격감 있는 게임"}))
    f.current.owner, f.current.owner_delivered, f.current.verified = 12, True, True
    f.act_by[12] = 5; f.act_by[13] = 0          # owner 실작업, VFX 잠수
    f.current.cross_checks = f.current.cross_check_offdomain = 1
    r1 = asyncio.run(t["complete_task"].handler({"result": "끝"}))   # 1회차: 보류
    assert "완료 보류(팀 기여 의무" in r1["content"][0]["text"] and f.current is not None
    assert "기록에 남습니다" in r1["content"][0]["text"]              # 재호출 통과 경고
    r2 = asyncio.run(t["complete_task"].handler({"result": "[기여 불필요] VFX 불필요 판단"}))   # 2회차: 명시 통과
    assert f.current is None                                          # 마감됨(리더 자율)
    assert "기여 미흡" in f.tasks[0].status.result and "VFX 전문가" in f.tasks[0].status.result
    assert any(ev == "task_contrib_overridden" for ev, _ in logs)    # 로그에 영속


def test_협의기록은_Work위임에_동봉되고_스냅샷에_생존한다(tmp_path):
    """[스펙 증발 방지] 회의·표결 합의(collab_notes)는 ① 이후 모든 Work 위임 본문에 자동 동봉되고
    ② Task 스냅샷에 영속돼 재개 후 위임에도 살아있다 — 라이브 P-009: 9직군이 회의로 정한 스펙이
    구현자에게 전달되지 않아(리더 요약 의존·재개 스코프 단절) 품질로 이어지지 못함."""
    g = FakeGuide()
    f = _flow(g)
    waked = []

    async def wake(to, b, k):
        waked.append(b)
        return "구현 완료 보고"
    f.wake = wake
    t = _tools(f, 11, "leader")
    asyncio.run(t["create_task"].handler({"members": "12"}))
    f.current.participated.add(12)
    asyncio.run(t["set_goal"].handler({"goal": "g"}))
    f.current.collab_notes = "[회의] 상태머신 5단계 합의\n[표결] 스택=Node+TF.js"
    asyncio.run(t["request"].handler({"to_id": "12", "kind": "Work", "body": "구현해줘"}))
    assert any("[팀 협의 기록" in b and "상태머신 5단계" in b for b in waked)   # 위임 동봉
    s = Sys(g, guild_id=1, organt_builder=None, bot_info={11: "L", 12: "B"}, session_dir=str(tmp_path))
    snap = s._task_snapshot(f, f.current)
    assert "상태머신 5단계" in snap["collab_notes"]                            # 스냅샷 생존


def test_배포명은_프로젝트별_결정적(monkeypatch):
    """[멀티 프로젝트] 배포 서비스명은 '프로젝트 신원'에서만 결정적으로 유도된다 — 미등록 흐름은
    슬롯이 없다(사용자 설계: 배포는 프로젝트마다). 과거의 DEPLOY_NAME env·인자·기본 폴백은
    미등록 배포를 공유 슬롯(P-002 라이브 겸용)으로 보내 덮어쓰기 위험을 남겨 제거됨."""
    from src.guide_tools import deploy_service_name
    monkeypatch.setenv("DEPLOY_NAME", "todo-organt-demo")      # env가 있어도 어디서도 안 읽는다
    f = _flow(FakeGuide())
    f.project_id, f.project_name = "P-003", "Cell Grow Game"
    assert deploy_service_name(f, "agent-random-name") == "organt-p-003"   # 신원=번호(작명·인자 무시)
    f.project_name = "세포 키우기"                                                   # 한글 → 식별번호 폴백
    assert deploy_service_name(f) == "organt-p-003"
    f2 = _flow(FakeGuide())                                                          # 미등록 흐름
    assert deploy_service_name(f2, "x") == ""                  # 슬롯 없음 — env·인자 폴백 폐지
    assert deploy_service_name(f2, "My App!") == ""


def test_deploy도구는_미등록흐름을_등록안내로_거부():
    """[배포=프로젝트] 미등록 흐름의 deploy 호출은 자격증명·작업공간 검사 전에 거부되고,
    create_project 등록 경로를 안내한다 — 공유 슬롯 덮어쓰기가 도구 수준에서 구조적으로 불가."""
    f = _flow(FakeGuide())
    t = _tools(f, 11, "leader")
    r = asyncio.run(t["deploy"].handler({"name": "my-random-slot"}))
    text = r["content"][0]["text"]
    assert "배포 불가" in text and "create_project" in text


def test_세션_스코프분리_프로젝트간_기억오염_구조차단(tmp_path):
    """[병렬·멀티 프로젝트] 세션 파일이 흐름 스코프별로 분리된다 — 개입은 그 프로젝트 스코프를
    resume(기억 유지)하고, 다른 프로젝트와는 파일 자체가 달라 교차 오염이 구조적으로 불가능하다
    (과거의 '다른 프로젝트면 리셋' 가드를 대체 — 병렬 동시 흐름에서도 안전)."""
    g = FakeGuide()
    s = Sys(g, guild_id=1, organt_builder=None, bot_info={11: "L"},
            workspace="/tmp/ws-x", session_dir=str(tmp_path))
    s.projects = {
        100: {"id": "P-00A", "name": "a", "channel": 100, "workspace": str(tmp_path), "leader": 11, "summary": ""},
        200: {"id": "P-00B", "name": "b", "channel": 200, "workspace": str(tmp_path), "leader": 11, "summary": ""},
    }
    scopes = []

    async def fake_run_turn(flow, oid, body, kind, role):
        scopes.append(flow.session_scope)
        flow.current = None
        return "ok"
    s.run_turn = fake_run_turn
    asyncio.run(s.handle_user_input(100, 11, "A 개입", root_id=None))
    asyncio.run(s.handle_user_input(200, 11, "B 개입", root_id=None))
    asyncio.run(s.handle_user_input(100, 11, "A 재개입", root_id=None))
    assert scopes == ["P-00A", "P-00B", "P-00A"]                    # 프로젝트별 고정 스코프(기억 유지·격리)


def test_Skill강화_경험_흡수_주입_상한(tmp_path):
    """[Skill 강화 v1] 보고의 [경험] 블록을 흡수해 직군별로 누적(상한 유지)·디스크 영속하고,
    다음 작업 프롬프트에 '최근 경험'으로 주입한다 — '일하며 쌓인 경험'이 다음 작업의
    '일하기 전 학습'이 되는 순환(압축은 기억 증류 고도화의 몫)."""
    import json as _json
    s = Sys(FakeGuide(), guild_id=1, organt_builder=None, bot_info={11: "QA"},
            session_dir=str(tmp_path))
    out = asyncio.run(s._absorb_role_profiles(
        "검증 완료.\n[경험] QA\n소켓 e2e는 서버 기동 1.5초 대기 후가 안정적\n[/경험]"))
    assert out == "검증 완료."                                   # 본문에서 블록 제거
    assert "1.5초" in s.role_experience["QA"][0]                 # 누적
    saved = _json.load(open(tmp_path / "role_profiles.json", encoding="utf-8"))
    assert "1.5초" in saved["experience"]["QA"][0]               # 디스크 영속
    for i in range(20):                                          # 상한(_EXP_KEEP) 유지
        asyncio.run(s._absorb_role_profiles(f"r\n[경험] QA\n교훈{i}\n[/경험]"))
    assert len(s.role_experience["QA"]) == s._EXP_KEEP
    p = s._prompt("b", Kind.WORK, "member", 11, 11)
    assert "최근 경험" in p and "교훈19" in p                     # 다음 작업에 주입
    assert "[경험] QA" in p                                      # 경험 남기기 안내
    s2 = Sys(FakeGuide(), guild_id=1, organt_builder=None, bot_info={11: "QA"},
             session_dir=str(tmp_path))
    assert s2.role_experience["QA"]                              # 재기동 복원


def test_수면_기억증류_경험이_기준으로_압축(tmp_path):
    """[수면 — 기억 증류] 유휴 시 경험이 쌓인 직군의 '전문가 본인'이 경험을 일반화해 직무 기준을
    개선하고, 증류된 경험 로그는 비워진다(자기계발 보강 — Feature.md). 증류는 별도 세션(state_tag)
    이라 작업 기억을 오염시키지 않는다."""
    g = FakeGuide()
    calls = {}

    class _Distiller:
        async def handle(self, prompt):
            calls["prompt"] = prompt
            return "[직무기준] QA\n개선된 기준: 소켓 e2e는 기동 대기 후 검증한다\n실플레이를 끝까지 재현한다\n[/직무기준]"

    def builder(oid, srv, role, flow=None, state_tag=None):
        calls["state_tag"] = state_tag
        return _Distiller()

    s = Sys(g, guild_id=1, organt_builder=builder, bot_info={11: "백엔드·QA"},
            session_dir=str(tmp_path))
    s.role_profiles["QA"] = "기존 기준"
    s.role_experience["QA"] = [f"교훈{i}" for i in range(6)]
    assert s.pick_distill_job() == "QA"                       # 경험 임계 도달 직군 선정
    ok = asyncio.run(s.distill_role("QA"))
    assert ok is True
    assert "소켓 e2e" in s.role_profiles["QA"]                # 기준이 개선본으로 교체
    assert s.role_experience["QA"] == []                      # 원석 비움
    assert calls["state_tag"] == "distill_11"                 # 작업 세션과 분리
    assert "교훈3" in calls["prompt"] and "기존 기준" in calls["prompt"]
    assert any(e["event"] == "role_distilled" for e in s.flow_log)
    assert s.pick_distill_job() is None                       # 증류 후 대상 없음


def test_vote_표결_집계와_협의인정():
    """[Discord 심화 대화] vote는 멤버 전원의 선택·근거를 한 호출로(독립·동시) 수집·집계한다 —
    표결 참여는 set_goal 협의로 인정되고, 집계 후에도 리더가 활성(단일활성 형식 유지)."""
    g = FakeGuide()
    f = _flow(g)

    async def wake(to, b, k):
        assert "[표결" in b and "선택지" in b and "독립" in b   # 표는 독립 수집(앵커링 방지)
        return "[표] Canvas\n성능과 단순성"
    f.wake = wake
    t = _tools(f, 11, "leader")
    asyncio.run(t["create_task"].handler({"members": "12"}))
    r = asyncio.run(t["vote"].handler({"question": "렌더 방식?", "options": "Canvas;SVG", "members": ""}))
    txt = r["content"][0]["text"]
    assert "Canvas: 1관점" in txt and "SVG: 0관점" in txt          # 집계
    assert 12 in f.current.participated                        # 표결 = 실질 협의 인정
    assert f.comm.alive == 11                                  # 베턴 복귀(단일활성 일관)


def test_meet_1라운드_독립fork_2라운드부터_문맥토론():
    """[Discord 심화 대화 × 병렬] meet 1라운드는 전원의 '독립 의견'을 동시에 수집한다(서로의 발언을
    보지 않음 — 앵커링 방지·회의 비용 절감). 2라운드부터는 직전 발언들을 보며 직렬로 토론한다
    (품질의 원천인 순차 문맥은 유지). 참여는 협의로 인정되고 베턴은 리더로 복귀한다."""
    g = FakeGuide()
    f = Flow(g, channel_id=500, guild_id=1, leader_id=11, bot_info={11: "L", 12: "백엔드", 13: "QA"})
    f.start_root("root")
    seen = []

    async def wake(to, b, k):
        seen.append((to, b))
        return f"{to}의 입장: 근거와 함께"
    f.wake = wake
    t = _tools(f, 11, "leader")
    asyncio.run(t["create_task"].handler({"members": "12,13"}))
    r = asyncio.run(t["meet"].handler({"topic": "저장 방식", "members": "", "rounds": "2"}))
    txt = r["content"][0]["text"]
    assert "[회의록]" in txt and "12의 입장" in txt and "13의 입장" in txt
    r1 = [b for _, b in seen if "1라운드" in b]
    r2 = [b for _, b in seen if "2라운드" in b]
    assert len(r1) == 2 and all("독립 의견" in b and "지금까지의 발언" not in b for b in r1)
    assert len(r2) == 2 and all("[1R]" in b for b in r2)       # 2라운드는 1라운드 발언을 본다
    assert {12, 13} <= f.current.participated
    assert f.comm.alive == 11


def test_병렬_다른프로젝트는_동시진행_같은스코프는_큐(tmp_path):
    """[병렬 작업 v1] 흐름 내 단일활성(베턴)은 불변 — 완화는 '다른 프로젝트의 흐름 동시 진행'만.
    같은 스코프는 직렬 큐. 동시 진행은 '리더가 서로 다른 봇'일 때 성립한다(전역 점유 — 한 직원은
    한 번에 한 흐름). 종료 시 큐에서 비충돌 항목을 드레인."""
    g = FakeGuide()
    s = Sys(g, guild_id=1, organt_builder=None, bot_info={11: "L", 12: "기획"},
            workspace="/tmp/ws-x", session_dir=str(tmp_path))
    s.projects = {
        100: {"id": "P-00A", "name": "a", "channel": 100, "workspace": str(tmp_path), "leader": 11, "summary": ""},
        200: {"id": "P-00B", "name": "b", "channel": 200, "workspace": str(tmp_path), "leader": 12, "summary": ""},
    }
    gate_a = asyncio.Event()
    order = []

    async def fake_run_turn(flow, oid, body, kind, role):
        order.append(body)
        if "A 작업" in body:
            await gate_a.wait()                       # A를 잡아둔 채 B 진입을 관찰
        flow.current = None
        return "ok"
    s.run_turn = fake_run_turn

    async def scenario():
        t_a = asyncio.ensure_future(s.handle_user_input(100, 11, "A 작업", root_id=None))
        await asyncio.sleep(0.05)
        t_b = asyncio.ensure_future(s.handle_user_input(200, 12, "B 작업", root_id=None))
        await asyncio.sleep(0.05)
        assert "A 작업" in order[0] and "B 작업" in order[1]   # B는 A 진행 중에도 동시 진입(다른 프로젝트·다른 리더)
        r_a2 = await s.handle_user_input(100, 11, "A 추가", root_id=None)
        assert r_a2["mode"] == "queued"                # 같은 스코프(P-00A)는 큐
        gate_a.set()
        await t_a                                      # A 종료 → 드레인이 'A 추가' 실행
        await t_b
        assert any("A 추가" in b for b in order)       # 드레인으로 실행됨
    asyncio.run(scenario())


def test_같은스코프_동시진입_레이스_봉쇄(tmp_path):
    """[안정성] 같은 프로젝트 채널에 메시지 2개가 '동시에' 도착해도 흐름은 1개만 생긴다 —
    스코프 선점이 첫 await 이전이라 두 번째는 반드시 큐로(개입 복원 await 사이로 끼어들던
    중복 진입 창 봉쇄)."""
    g = FakeGuide()
    s = Sys(g, guild_id=1, organt_builder=None, bot_info={11: "L"},
            workspace="/tmp/ws-x", session_dir=str(tmp_path))
    s.projects = {100: {"id": "P-00A", "name": "a", "channel": 100,
                        "workspace": str(tmp_path), "leader": 11, "summary": ""}}
    gate = asyncio.Event()
    runs = []

    async def fake_run_turn(flow, oid, body, kind, role):
        runs.append(body)
        await gate.wait()
        flow.current = None
        return "ok"
    s.run_turn = fake_run_turn

    async def scenario():
        t1 = asyncio.ensure_future(s.handle_user_input(100, 11, "첫 메시지", root_id=None))
        t2 = asyncio.ensure_future(s.handle_user_input(100, 11, "둘째 메시지", root_id=None))
        await asyncio.sleep(0.05)
        assert len(runs) == 1                       # 흐름은 하나만 떴다
        assert len(s.queue) == 1                    # 둘째는 큐
        gate.set()
        await t1
        await t2
        assert len(runs) == 2                       # 종료 후 드레인으로 둘째 실행
    asyncio.run(scenario())


# ───────────────── 전역 점유(Engagement) — '흐름 수 상한'을 대체하는 구조적 병렬 안전 ─────────────────


def test_전역점유_타흐름_동료는_Kind불문_차단_응답시_즉시해제():
    """한 직원(봇)은 한 시점에 한 흐름에만 참여한다 — Work는 물론 Info도 타 흐름 점유 중엔 차단
    (같은 봇이 두 채널에서 동시에 일하는 '이중 존재' 방지; 흐름 안의 Info는 종전대로). 응답을
    마친 봇은 그 즉시 회사 풀로 돌아가 다른 흐름이 쓸 수 있다."""
    import pytest
    from src.communication import BusyInOtherFlow, CommunicationManager, Engagement
    eng = Engagement()
    a = CommunicationManager(0)
    a.attach_engagement(eng, "P-A")
    b = CommunicationManager(0)
    b.attach_engagement(eng, "P-B")
    a.request(0, 11, "ra", Kind.WORK)                  # A 리더 점유
    a.request(11, 13, "r1", Kind.WORK)                 # 13은 A에서 작업 중
    b.request(0, 12, "rb", Kind.WORK)                  # 리더가 다르면 흐름은 동시 진행
    assert eng.holder(11) == "P-A" and eng.holder(13) == "P-A" and eng.holder(12) == "P-B"
    with pytest.raises(BusyInOtherFlow):
        b.check_request(12, 13, Kind.WORK)
    with pytest.raises(BusyInOtherFlow):
        b.check_request(12, 13, Kind.INFO)
    a.respond(13, "accept")                            # 응답 완료 → 즉시 해제
    assert eng.holder(13) is None
    b.request(12, 13, "r2", Kind.INFO)                 # 이제 B가 쓸 수 있다
    assert eng.holder(13) == "P-B"
    b.respond(13, "accept")
    a.respond(11, "accept")
    b.respond(12, "accept")
    assert eng.holder(11) is None and eng.holder(12) is None   # 흐름 종료 → 전원 해제


def test_전역점유_상신_강제정리도_해제대칭():
    """escalate(타임아웃·복구의 강제 close)도 respond와 같은 지점에서 점유를 해제한다 —
    복구 경로에서 봇이 '바쁨'으로 영구히 굳는 누수가 구조적으로 없다."""
    from src.communication import CommunicationManager, Engagement
    eng = Engagement()
    a = CommunicationManager(0)
    a.attach_engagement(eng, "P-A")
    a.request(0, 11, "ra", Kind.WORK)
    a.request(11, 13, "r1", Kind.WORK)
    a.escalate("타임아웃 정리")
    assert eng.holder(13) is None and eng.holder(11) == "P-A"   # 13만 풀리고 리더는 계속
    a.escalate("종료 정리")
    assert a.done and eng.holder(11) is None                    # origin 복귀 → 전원 해제


def test_전역점유_유령점유_자가치유():
    """장부는 인메모리 + 조회 시 스코프 생존 검사 — 끝난/죽은 흐름의 점유는 holder() 조회 순간
    스스로 지워진다(예외로 해제가 누락돼도 봇이 영구 '바쁨'으로 굳지 않음)."""
    from src.communication import Engagement
    eng = Engagement(is_live=lambda s: s == "LIVE")
    eng.engage(7, "DEAD")
    assert eng.holder(7) is None                       # 죽은 스코프 → 자가 치유
    assert not eng.busy_elsewhere(7, "LIVE")
    eng.engage(7, "LIVE")
    assert eng.busy_elsewhere(7, "OTHER") and not eng.busy_elsewhere(7, "LIVE")
    eng.release_scope("LIVE")
    assert eng.holder(7) is None


def test_전역점유_같은리더_두프로젝트는_자연직렬_해제시_드레인(tmp_path):
    """같은 봇이 리더인 두 프로젝트는 흐름 수 상한 없이도 자연히 직렬화된다(한 직원은 한 번에 한
    흐름) — 임의 숫자 cap을 대체하는 구조적 안전. 점유가 풀리면(흐름 종료) 큐가 이어서 실행된다.
    (max_flows 기본 0=무제한에서도 모든 것이 큐로 가지 않음을 함께 증명 — 게이트는 '>0일 때만' 상한.)"""
    g = FakeGuide()
    s = Sys(g, guild_id=1, organt_builder=None, bot_info={11: "L"},
            workspace="/tmp/ws-x", session_dir=str(tmp_path))
    s.projects = {
        100: {"id": "P-00A", "name": "a", "channel": 100, "workspace": str(tmp_path), "leader": 11, "summary": ""},
        200: {"id": "P-00B", "name": "b", "channel": 200, "workspace": str(tmp_path), "leader": 11, "summary": ""},
    }
    gate_a = asyncio.Event()
    order = []

    async def fake_run_turn(flow, oid, body, kind, role):
        order.append(body)
        if "A 작업" in body:
            await gate_a.wait()
        flow.current = None
        return "ok"
    s.run_turn = fake_run_turn

    async def scenario():
        t_a = asyncio.ensure_future(s.handle_user_input(100, 11, "A 작업", root_id=None))
        await asyncio.sleep(0.05)
        assert s.engaged.holder(11) == "P-00A"         # 예약 블록에서 리더 선점
        r_b = await s.handle_user_input(200, 11, "B 작업", root_id=None)
        assert r_b["mode"] == "queued"                 # 다른 프로젝트라도 같은 리더면 큐(자연 직렬)
        gate_a.set()
        await t_a                                      # A 종료 → 점유 해제 → 드레인이 B 실행
        assert any("B 작업" in b for b in order)
        assert s.engaged.holder(11) is None
    asyncio.run(scenario())


def test_request도구_타흐름점유는_거부아닌_대안안내():
    """타 흐름이 점유한 동료에게 request하면 무서운 '규약 거부'가 아니라 [동료 점유] + 지금 가용한
    같은 직군 동료·채용 안내(+재시도 금지)가 온다. 점유가 풀리면 같은 요청이 즉시 통한다."""
    from src.communication import Engagement
    eng = Engagement()
    fa = Flow(FakeGuide(), channel_id=500, guild_id=1, leader_id=11, bot_info={11: "L", 13: "QA"})
    fa.comm.attach_engagement(eng, "P-A")
    fa.start_root("ra")
    fa.comm.request(11, 13, "r1", Kind.WORK)           # 13은 A에서 작업 중
    fb = Flow(FakeGuide(), channel_id=600, guild_id=1, leader_id=12,
              bot_info={12: "기획", 13: "QA", 14: "QA", 15: "예비"})
    fb.comm.attach_engagement(eng, "P-B")
    fb.start_root("rb")
    woken = []

    async def wake(to, b, k):
        woken.append(to)
        return "확인했습니다"
    fb.wake = wake
    t = {x.name: x for x in make_guide_tools(fb, 12, "leader")}
    asyncio.run(t["create_task"].handler({"members": "13,14"}))
    r = asyncio.run(t["request"].handler({"to_id": "13", "kind": "Info", "body": "QA 가능?"}))
    txt = r["content"][0]["text"]
    assert "[동료 점유]" in txt and "P-A" in txt        # 어느 흐름이 점유 중인지
    assert "14" in txt and "재시도" in txt              # 가용한 같은 직군(14) 안내 + 폴링 금지
    assert woken == []                                  # 점유된 동료를 깨우지 않았다
    fa.comm.respond(13, "accept")                       # A에서 응답 → 즉시 회사 풀로
    asyncio.run(t["request"].handler({"to_id": "13", "kind": "Info", "body": "이제 QA 가능?"}))
    assert woken == [13]                                # 풀리면 같은 동료에게 즉시 통한다


def test_수면증류_흐름참여_전문가는_스킵_가용하면_진행(tmp_path):
    """[병렬×수면] 증류 조건은 '시스템 유휴'가 아니라 '그 전문가 유휴' — 흐름에 묶인 전문가는
    스킵하고(전체-유휴 조건이면 장기 프로젝트 중 증류가 영영 굶는다), 한가해지면 진행한다.
    증류가 끝나면 점유도 해제된다."""
    calls = []

    class FakeOrgant:
        async def handle(self, prompt):
            calls.append(prompt)
            return "[직무기준] QA\n빠른 재현 → 최소 수정 → 회귀 확인\n[/직무기준]"

    def builder(mid, server, role, flow=None, state_tag=None):
        return FakeOrgant()

    s = Sys(FakeGuide(), guild_id=1, organt_builder=builder, bot_info={21: "QA"},
            session_dir=str(tmp_path))
    s.role_experience["QA"] = [f"경험{i}" for i in range(5)]
    f = Flow(FakeGuide(), channel_id=1, guild_id=1, leader_id=21, bot_info={21: "QA"})
    s.active_flows["P-X"] = f                          # 살아있는 흐름이
    s.engaged.engage(21, "P-X")                        # 그 전문가를 점유 중
    assert asyncio.run(s.distill_role("QA")) is False and calls == []   # → 스킵
    s.active_flows.pop("P-X")                          # 흐름 종료(유령 점유는 자가 치유)
    assert asyncio.run(s.distill_role("QA")) is True and len(calls) == 1  # 유휴 → 증류
    assert s.engaged.holder(21) is None                # 증류 점유도 해제됨


# ───────────────── 병렬 Info fork-join — 표결·회의 1라운드 '독립 의견'의 동시 수집 ─────────────────


def test_표결_판정자_사본은_침묵절단되지_않는다():
    """[잘림 사건의 잔재 — 회귀 가드] 리더가 표결을 판정할 때 받는 '각자의 선택·근거'가 종전
    [:150] 하드컷으로 단어 중간에서 동강났다(채널 발언은 _speech_clip으로 고쳤는데 판정자
    사본이 빠짐). 근거는 전문이 전달되거나, 안전망(400)을 넘으면 '잘렸다'는 표기가 붙어야
    한다 — 침묵 절단 금지."""
    from src.communication import Engagement
    eng = Engagement()
    f = Flow(FakeGuide(), channel_id=500, guild_id=1, leader_id=11,
             bot_info={11: "L", 12: "백엔드", 13: "QA"})
    f.comm.attach_engagement(eng, "P-A")
    f.start_root("root")
    mid = "성능과 메모리 곡선을 함께 고려하면 캔버스가 우세합니다. " * 6   # 150자 초과, 400자 이하
    long = "근거가 아주 깁니다. " * 60                                      # 400자 초과(안전망 발동)

    async def wake(to, b, k):
        return f"[표] Canvas\n{mid if to == 12 else long}"
    f.wake = wake
    t = {x.name: x for x in make_guide_tools(f, 11, "leader")}
    asyncio.run(t["create_task"].handler({"members": "12,13"}))
    r = asyncio.run(t["vote"].handler({"question": "렌더?", "options": "Canvas;SVG", "members": ""}))
    txt = r["content"][0]["text"]
    assert mid.strip()[:200] in txt          # 150자 넘는 근거가 통째로 전달(종전엔 150에서 동강)
    assert "안전망에서 잘림" in txt           # 400자 초과는 자르되 '잘렸다'고 표기(침묵 금지)


def test_표결_동시수집_점유와_해제():
    """[병렬 fork-join] 표결은 멤버들을 '동시에' 깨워 독립 의견을 모은다(겹침 실측) — 수집 동안
    가지 봇은 전역 점유돼 타 흐름이 못 집어가고, 조인 후 즉시 회사 풀로 돌아간다."""
    from src.communication import Engagement
    eng = Engagement()
    f = Flow(FakeGuide(), channel_id=500, guild_id=1, leader_id=11,
             bot_info={11: "L", 12: "백엔드", 13: "QA"})
    f.comm.attach_engagement(eng, "P-A")
    f.start_root("root")
    running = {"now": 0, "peak": 0, "held": []}

    async def wake(to, b, k):
        assert "[표결" in b and "독립" in b
        running["now"] += 1
        running["peak"] = max(running["peak"], running["now"])
        running["held"].append(eng.holder(to))     # 수집 중 점유 확인
        await asyncio.sleep(0.02)
        running["now"] -= 1
        return "[표] Canvas\n성능 근거"
    f.wake = wake
    t = {x.name: x for x in make_guide_tools(f, 11, "leader")}
    asyncio.run(t["create_task"].handler({"members": "12,13"}))
    r = asyncio.run(t["vote"].handler({"question": "렌더?", "options": "Canvas;SVG", "members": ""}))
    txt = r["content"][0]["text"]
    assert "Canvas: 2관점" in txt
    assert running["peak"] == 2                    # 진짜 동시 수집(직렬이면 1)
    assert running["held"] == ["P-A", "P-A"]       # 가지 봇은 수집 동안 점유 중
    assert eng.holder(12) is None and eng.holder(13) is None   # 조인 후 즉시 해제
    assert f.comm.alive == 11                      # 베턴은 리더 그대로(단일활성 형식 유지)


def test_표결_동시폭은_운영노브로_직렬화_가능(monkeypatch):
    """ORGANT_FORK_FAN=1이면 fork 수집이 종전의 직렬과 동일하게 돈다(토큰 속도 운영 노브)."""
    monkeypatch.setenv("ORGANT_FORK_FAN", "1")
    f = Flow(FakeGuide(), channel_id=500, guild_id=1, leader_id=11,
             bot_info={11: "L", 12: "백엔드", 13: "QA"})
    f.start_root("root")
    running = {"now": 0, "peak": 0}

    async def wake(to, b, k):
        running["now"] += 1
        running["peak"] = max(running["peak"], running["now"])
        await asyncio.sleep(0.02)
        running["now"] -= 1
        return "[표] SVG\n근거"
    f.wake = wake
    t = {x.name: x for x in make_guide_tools(f, 11, "leader")}
    asyncio.run(t["create_task"].handler({"members": "12,13"}))
    asyncio.run(t["vote"].handler({"question": "렌더?", "options": "Canvas;SVG", "members": ""}))
    assert running["peak"] == 1                    # 노브로 완전 직렬


def test_표결_타흐름점유_멤버는_부분조인으로_제외():
    """[병렬 fork-join] 타 흐름이 점유한 멤버는 수집에서 빠지고 사유가 기록된다 — 일부 멤버 때문에
    표결 전체가 막히거나 행으로 굳지 않는다(부분 조인). 남의 점유는 건드리지 않는다."""
    from src.communication import Engagement
    eng = Engagement()
    f = Flow(FakeGuide(), channel_id=500, guild_id=1, leader_id=11,
             bot_info={11: "L", 12: "백엔드", 13: "QA"})
    f.comm.attach_engagement(eng, "P-A")
    f.start_root("root")
    eng.engage(13, "P-B")                          # 13은 다른 흐름에서 작업 중
    woken = []

    async def wake(to, b, k):
        woken.append(to)
        return "[표] SVG\n근거"
    f.wake = wake
    t = {x.name: x for x in make_guide_tools(f, 11, "leader")}
    asyncio.run(t["create_task"].handler({"members": "12,13"}))
    r = asyncio.run(t["vote"].handler({"question": "렌더?", "options": "Canvas;SVG", "members": ""}))
    txt = r["content"][0]["text"]
    assert woken == [12]                           # 점유 멤버는 깨우지 않음
    assert "SVG: 1관점" in txt and "P-B" in txt      # 부분 집계 + 제외 사유 표기
    assert eng.holder(13) == "P-B"                 # 남의 점유 보존


def test_fork수집중_신규request와_중첩수집은_대기():
    """[fork 동시성 가드] fork 중엔 베턴이 리더에 머물러, CLI의 같은 턴 병렬 도구 호출(vote+request,
    vote+meet)이 수집 가지와 같은 동료를 이중으로 깨울 수 있었다(직렬 vote 시절엔 alive 이동이 자연
    차단 — 재감사에서 발견). 수집 중 신규 요청/중첩 수집은 '[대기]'로 막히고, 조인 후 즉시 풀린다."""
    f = Flow(FakeGuide(), channel_id=500, guild_id=1, leader_id=11,
             bot_info={11: "L", 12: "백엔드", 13: "QA"})
    f.start_root("root")
    gate = asyncio.Event()

    async def wake(to, b, k):
        await gate.wait()
        return "[표] A\n근거"
    f.wake = wake
    t = {x.name: x for x in make_guide_tools(f, 11, "leader")}
    asyncio.run(t["create_task"].handler({"members": "12,13"}))

    async def scenario():
        vote_t = asyncio.ensure_future(t["vote"].handler(
            {"question": "Q", "options": "A;B", "members": ""}))
        await asyncio.sleep(0.02)
        assert f.fork_active == 1
        r1 = await t["request"].handler({"to_id": "12", "kind": "Info", "body": "딴 질문"})
        assert "[대기]" in r1["content"][0]["text"]            # 수집 중 신규 요청 차단
        r2 = await t["meet"].handler({"topic": "T", "members": "", "rounds": "1"})
        assert "[대기]" in r2["content"][0]["text"]            # 중첩 수집 차단
        gate.set()
        out = await vote_t
        assert "A: 2관점" in out["content"][0]["text"]           # 수집은 정상 완주
        assert f.fork_active == 0                              # 조인 후 가드 해제
    asyncio.run(scenario())


def test_경험_의무섹션_없음은_흡수에서_버려짐(tmp_path):
    """[학습 플라이휠] [경험]은 보고의 고정 섹션(의무형 — 선택형은 라이브 0% vs 의무형 100%)이되,
    '없음'은 탈출구라 흡수 단계에서 구조적으로 버려진다 — 다음 프롬프트 주입·증류 원료가 억지
    채움 노이즈로 오염되지 않는다."""
    s = Sys(FakeGuide(), guild_id=1, organt_builder=None, bot_info={11: "QA"},
            session_dir=str(tmp_path))
    note = s._craft_note(11)
    assert "고정 섹션" in note and "생략 금지" in note and "'없음'" in note   # 의무형 + 탈출구 안내
    out = asyncio.run(s._absorb_role_profiles("검증 끝.\n[경험] QA\n없음\n[/경험]"))
    assert out == "검증 끝." and not s.role_experience.get("QA")            # '없음'은 저장 안 됨
    asyncio.run(s._absorb_role_profiles("[경험] QA\n소켓 e2e는 1.5초 대기 후 안정\n[/경험]"))
    assert s.role_experience["QA"] == ["소켓 e2e는 1.5초 대기 후 안정"]      # 실교훈만 축적


def test_프로젝트_Context가_개입프롬프트에_주입(tmp_path):
    """[Project.Context 복원 — docs Project.md 'Organts는 Context를 숙지한다'] 직전 흐름의 마감
    요약(summary)이 다음 개입의 리더 프롬프트에 참고 블록으로 주입된다(기록만 되고 읽는 곳이
    없던 단절 해소). 요약이 비어 있으면 블록 자체가 없다."""
    g = FakeGuide()
    s = Sys(g, guild_id=1, organt_builder=None, bot_info={11: "L"},
            workspace="/tmp/ws-x", session_dir=str(tmp_path))
    s.projects = {100: {"id": "P-00A", "name": "a", "channel": 100, "workspace": str(tmp_path),
                        "leader": 11, "summary": "핵심 결정: 렌더는 Canvas 채택, 룸 기반 멀티 구조"}}
    bodies = []

    async def fake_run_turn(flow, oid, body, kind, role):
        bodies.append(body)
        flow.current = None
        return "ok"
    s.run_turn = fake_run_turn
    asyncio.run(s.handle_user_input(100, 11, "이어서 개선해", root_id=None))
    assert "프로젝트 최근 맥락" in bodies[0] and "Canvas 채택" in bodies[0]
    assert "이번 요청이 우선" in bodies[0]                                   # 앵커링 방향 단서
    s.projects[100]["summary"] = ""
    asyncio.run(s.handle_user_input(100, 11, "또 개선해", root_id=None))
    assert "프로젝트 최근 맥락" not in bodies[1]                             # 빈 요약이면 블록 없음


def test_프로젝트_목표원문_등록·개입주입(tmp_path):
    """[Project.Context 완성] 프로젝트 등록 때 '그 흐름을 시작시킨 사용자 원문'을 purpose로 영속하고,
    이후 모든 개입 프롬프트에 [프로젝트 목표]로 주입한다 — 재개 흐름이 마지막 미완 Task만 닫고
    '멀티·배포가 남은 프로젝트'를 종료 보고하던 시야 협착(라이브 관측)의 구조적 차단."""
    g = FakeGuide()
    s = Sys(g, guild_id=1, organt_builder=None, bot_info={11: "L"},
            workspace="/tmp/ws-x", session_dir=str(tmp_path))
    bodies = []

    async def fake_run_turn(flow, oid, body, kind, role):
        bodies.append(body)
        if flow.register_project and not flow.project_id:
            flow.workspace = str(tmp_path)
            flow.project_id = flow.register_project(900, "세포게임")   # 리더가 create_project 한 셈
        flow.current = None
        return "1차 작업 완료"
    s.run_turn = fake_run_turn
    원문 = "온라인 세포 키우기 게임 만들어줘 스페이스바 분열·먹이·지뢰·멀티까지"
    asyncio.run(s.handle_user_input(500, 11, 원문, root_id=None))
    assert s.projects[900].get("purpose") == 원문                    # 원문이 영속됨
    asyncio.run(s.handle_user_input(900, 11, "이어서 진행해", root_id=None))
    assert "[프로젝트 목표" in bodies[1] and "지뢰·멀티" in bodies[1]  # 개입마다 목표 주입
    assert "Task 하나의 마감이 프로젝트의 끝이 아닙니다" in bodies[1]


def test_Task_체크포인트_전이마다_영속_마감시_해제(tmp_path):
    """[크래시-세이프 Task 스냅샷] 미완 Task는 흐름 '종료'가 아니라 전이(생성→목표→owner→마감)마다
    레지스트리에 영속된다 — 동면·강제종료처럼 마감 코드가 못 도는 죽음에도 복구가 '같은 Task'를
    잇는다(새 Task 둔갑·'진행' 박제 방지 — 라이브 관측의 구조적 차단)."""
    g = FakeGuide()
    s = Sys(g, guild_id=1, organt_builder=None, bot_info={11: "L", 12: "백엔드"},
            workspace=str(tmp_path), session_dir=str(tmp_path))
    s.projects = {500: {"id": "P-00A", "name": "a", "channel": 500, "workspace": str(tmp_path),
                        "leader": 11, "summary": ""}}
    f = Flow(g, channel_id=500, guild_id=1, leader_id=11, bot_info={11: "L", 12: "백엔드"})
    f.start_root("root")
    f.gap_checked = True   # P7 범주점검 보류 우회(체크포인트 검증 범위 밖)
    f.percept_checked = True   # 지각 비대칭 점검 보류 우회(범위 밖)
    f.acceptance_checked = True   # 수용 계약 게이트 보류 우회(범위 밖)
    f.project_channel = 500
    f.workspace = str(tmp_path)
    f.checkpoint_task = lambda: s._checkpoint_open_task(f)

    async def wake(to, b, k):
        return "의견: 코어 루프가 끝까지 돌면 성공으로 봅니다"
    f.wake = wake
    t = {x.name: x for x in make_guide_tools(f, 11, "leader")}
    asyncio.run(t["create_task"].handler({"members": "12"}))
    snap = s.projects[500]["open_task"]
    assert snap and snap["task_id"] == f.current.task_id          # 생성 '즉시' 영속(흐름 종료 전)
    asyncio.run(t["request"].handler({"to_id": "12", "kind": "Info", "body": "이 Task 성공기준 의견 줘"}))
    asyncio.run(t["set_goal"].handler({"purpose": "p", "goal": "측정가능 g"}))
    assert s.projects[500]["open_task"]["goal"] == "측정가능 g"    # 목표 확정 영속
    asyncio.run(t["request"].handler({"to_id": "12", "kind": "Work", "body": "구현해줘"}))
    assert s.projects[500]["open_task"]["owner"] == 12             # owner 확정 영속
    f.current.verified = True                                      # (인도 게이트는 별도 테스트가 커버)
    f.current.owner_delivered = True
    f.current.owner_incomplete = False
    f.current.cross_checks = f.current.cross_check_offdomain = 1                    # 검증 분업 게이트(별도 테스트)와 무관한 의도 보존
    asyncio.run(t["complete_task"].handler({"result": "끝"}))
    assert s.projects[500]["open_task"] is None                    # 마감 즉시 해제(유령 복원 방지)


def test_배포검증_라이브가_산출물과_다르면_성공선언_불가(tmp_path):
    """[완료 = 증명된 완료] deploy는 URL 응답(200)만으론 성공을 말할 수 없다 — 라이브가 '방금 만든
    그 파일'을 서빙하는지 바이트 대조까지 통과해야 한다. 스테일 배포(옛 빌드 서빙)가 '배포 완료'로
    보고되던 부류(라이브 관측 — 사용자 재보고로 발견)의 도구 레벨 차단."""
    from src.deploy import _verify_live_assets
    pub = tmp_path / "public"
    pub.mkdir()
    (pub / "app.js").write_bytes(b"NEW BUILD v2")
    (pub / "index.html").write_bytes(b"<html>v2</html>")
    live = {"app.js": b"OLD BUILD v1", "index.html": b"<html>v2</html>"}

    def fetch(u):
        return live[u.rsplit("/", 1)[-1]]
    bad = _verify_live_assets("https://x.example", str(tmp_path), tries=2, wait=0, fetch=fetch)
    assert len(bad) == 1 and "app.js" in bad[0] and "≠" in bad[0]      # 스테일 파일 정확히 적발
    live["app.js"] = b"NEW BUILD v2"                                    # 전파 완료 시나리오
    assert _verify_live_assets("https://x.example", str(tmp_path), tries=1, wait=0, fetch=fetch) == []
    def fetch_fail(u):
        raise OSError("timeout")
    bad2 = _verify_live_assets("https://x.example", str(tmp_path), tries=1, wait=0, fetch=fetch_fail)
    assert len(bad2) == 2 and "조회 실패" in bad2[0]                    # 조회 불가도 성공 선언 불가
    assert _verify_live_assets("https://x.example", str(tmp_path / "없음"), fetch=fetch) == []  # public 없음=생략


def test_상태가시화_시작게시_종결확정_무알림수정(tmp_path):
    """[Rule/Status — 상태 가시화] 흐름 시작 시 System Bot(sender=0)이 상태 메시지 1개를 올리고,
    갱신·종결은 그 메시지의 '수정'으로만 한다(알림 0). 완료 흐름은 '✅ 완료'로, 미완 Task가 남은
    흐름은 '⏸ 중단'으로 확정된다. edit 능력이 없는 가이드에선 통째로 생략(거짓 계기판 금지)."""

    class EditableGuide(FakeGuide):
        def __init__(self):
            super().__init__()
            self.edits = []

        async def edit_message(self, ch, mid, content):
            self.edits.append((ch, mid, content))

    g = EditableGuide()
    s = Sys(g, guild_id=1, organt_builder=None, bot_info={11: "L"},
            workspace="/tmp/ws-x", session_dir=str(tmp_path))

    async def fake_run_turn(flow, oid, body, kind, role):
        flow.current = None
        return "끝"
    s.run_turn = fake_run_turn
    asyncio.run(s.handle_user_input(500, 11, "세포 게임 멀티 마저 해줘", root_id=None))
    status_posts = [c for c in g.calls if c[0] == "post" and "● 작업 중" in str(c[3])]
    assert len(status_posts) == 1 and status_posts[0][2] == 0        # System Bot(sender=0)이 1개 게시
    assert "세포 게임 멀티" in status_posts[0][3]                     # 요청 요약 표기
    assert g.edits and "✅ 완료" in g.edits[-1][2]                    # 종결은 '수정'으로 확정

    # edit 능력 없는 가이드(기존 FakeGuide) → 상태 메시지 생략(기존 동작 보존)
    g2 = FakeGuide()
    s2 = Sys(g2, guild_id=1, organt_builder=None, bot_info={11: "L"},
             workspace="/tmp/ws-x", session_dir=str(tmp_path))
    s2.run_turn = fake_run_turn
    asyncio.run(s2.handle_user_input(500, 11, "작은 일", root_id=None))
    assert not any("● 작업 중" in str(c[3]) for c in g2.calls if c[0] == "post")


def test_상태텍스트_살아있음_신호_구성():
    """상태 본문은 '무엇을·언제 시작·지금 누가·마지막 활동'을 담되, 시각은 Discord 동적
    타임스탬프(<t:유닉스:R>)여야 한다 — 클라이언트가 상대시간을 계속 갱신하므로 컨테이너가
    멈춰 edit이 끊겨도 표시가 늙는다(수정 시점 계산 'N초 전' 고정 문자열은 박제 시
    '마지막 활동 1초 전' 거짓 생존 신호가 되던 결함 — 사용자 관측)."""
    import re as _re
    import time as _t
    s = Sys(FakeGuide(), guild_id=1, organt_builder=None, bot_info={11: "기획", 12: "백엔드"})
    f = Flow(FakeGuide(), channel_id=1, guild_id=1, leader_id=11, bot_info={11: "기획", 12: "백엔드"})
    f.start_root("r")
    f.status_req = "온라인 세포 키우기 게임"
    f.last_activity = _t.monotonic() - 14
    txt = s._status_text(f, _t.monotonic() - 23 * 60)
    assert "● 작업 중" in txt and "온라인 세포" in txt and "지금: 기획" in txt
    stamps = [int(x) for x in _re.findall(r"<t:(\d+):R>", txt)]
    assert len(stamps) == 2, f"시작·마지막활동 동적 타임스탬프 2개여야 함: {txt}"
    now = _t.time()
    assert abs((now - 23 * 60) - stamps[0]) < 5      # 시작 ≈ 23분 전 (벽시계 유닉스)
    assert abs((now - 14) - stamps[1]) < 5           # 마지막 활동 ≈ 14초 전
    assert "초 전" not in txt and "분째" not in txt   # 고정 상대문자열 금지(박제=거짓말 차단)
    fin = s._status_text(f, _t.monotonic(), final="⏸ 중단(미완 Task 이어가기 가능)")
    assert fin.startswith("⏸ 중단") and "온라인 세포" in fin


def test_팀밖_거부는_팀내_같은직군_대안과_명단을_동봉():
    """[정보가 있는 거부 — 원인 교정] 리더가 풀과 프로젝트 팀을 혼동해 팀 밖 동료를 반복 호출하던
    문제(라이브 7회 우회)의 뿌리는 '거부만 하고 올바른 대안을 안 알려준 것' — 거부에 팀 내 같은
    직군 동료와 현재 팀 명단을 동봉해 첫 거부에서 바로 교정되게 한다(자동 합류·양산 없이)."""
    f = Flow(FakeGuide(), channel_id=500, guild_id=1, leader_id=11,
             bot_info={11: "L", 12: "프론트엔드", 13: "프론트엔드", 14: "QA"})
    f.start_root("root")
    f.project_team = [11, 12, 14]                    # 13(프론트)은 풀에만 있고 팀 밖

    async def wake(to, b, k):
        return "ok"
    f.wake = wake
    t = {x.name: x for x in make_guide_tools(f, 11, "leader")}
    asyncio.run(t["create_task"].handler({"members": "12,14"}))
    r = asyncio.run(t["request"].handler({"to_id": "13", "kind": "Info", "body": "도와줘"}))
    txt = r["content"][0]["text"]
    assert "이 프로젝트 팀이 아닙니다" in txt
    assert "팀 내 동료" in txt and "id 12" in txt     # 같은 직군(프론트)의 팀 내 대안(id 포함)
    assert "현재 프로젝트 팀" in txt and "재시도 금지" in txt


def test_이어가기_본문에_팀·소유_시스템사실_재주입(tmp_path):
    """[기억 구멍 무력화] 외부 절단으로 리더 세션에서 직전 턴이 증발해도, 이어가기 본문에 SYS가
    팀·Owner·Goal·프로젝트 팀 명단을 재주입한다 — '참여 중인가요?' 재확인·팀 밖 호출 반복의 차단."""
    g = FakeGuide()
    s = Sys(g, guild_id=1, organt_builder=None, bot_info={11: "L", 12: "백엔드"},
            workspace="/tmp/ws-x", session_dir=str(tmp_path))
    bodies = []
    calls = {"n": 0}

    async def fake_run_turn(flow, oid, body, kind, role):
        bodies.append(body)
        calls["n"] += 1
        if role != "leader":
            return "(owner 진행 중)"
        if calls["n"] == 1:                          # 1세그먼트: Task를 연 채 끝남 → 이어가기 유발
            from src.guide_tools import TaskRef
            from src.protocol import TaskStatus
            st = TaskStatus(task_id="T-1", purpose="p", status="진행", goal="측정가능 g",
                            owner="백엔드", group=[])
            flow.current = TaskRef(task_id="T-1", thread_id="th", block_id="b",
                                   status=st, team=[11, 12], owner=12)
            flow.project_team = [11, 12]
            return "1차(미완)"
        flow.current = None                          # 2세그먼트(이어가기): 마감
        return "완료"
    s.run_turn = fake_run_turn
    asyncio.run(s.handle_user_input(500, 11, "큰 작업", root_id=None))
    cont = bodies[1]                                 # 이어가기 본문
    assert "[시스템 기록 — 현재 Task T-1]" in cont
    assert "Owner: 백엔드" in cont and "측정가능 g" in cont
    assert "[프로젝트 팀 전체]" in cont and "구성원이 아닙니다" in cont


def test_수면은_정리자_예산과_통합지시_위생증류(tmp_path):
    """[수면 = 정리자(인간 수면의 통합·솎아냄)] ① 증류 프롬프트에 구조 예산(원칙 최대 8개·1,000자)과
    '추가가 아니라 통합' 지시가 들어간다. ② 기준이 비대(>1,100자)하면 새 경험이 없어도 '정리 전용'
    수면이 발동한다 — 더 많이가 아니라 더 선명하게."""
    prompts = []

    class FakeOrgant:
        async def handle(self, prompt):
            prompts.append(prompt)
            return "[직무기준] QA\n핵심 원칙으로 통합·정리됨\n[/직무기준]"

    def builder(mid, server, role, flow=None, state_tag=None):
        return FakeOrgant()

    s = Sys(FakeGuide(), guild_id=1, organt_builder=builder, bot_info={21: "QA"},
            session_dir=str(tmp_path))
    s.role_profiles["QA"] = "- 비대한 원칙\n" * 140         # 1,260자(>1,100 발동선, 경험은 0)
    assert "QA" in s.pick_distill_jobs()                     # 위생 증류 후보로 떠오름
    assert asyncio.run(s.distill_role("QA")) is True         # 경험 0이어도 정리 전용 수면 실행
    p = prompts[0]
    assert "정리 전용" in p                                   # 새 경험 없음 → 다이어트 모드 명시
    assert "원칙 최대 8개" in p and "1,000자" in p            # 구조 예산
    assert "기존 원칙에 합쳐" in p                            # 기본 동사 = 통합(추가 아님)
    assert s.role_profiles["QA"] == "핵심 원칙으로 통합·정리됨"  # 다이어트 반영


def test_기준_하드캡은_줄단위_절단(tmp_path):
    """절단 사고 방지 — 1,500자 초과 기준은 문장 중간이 아니라 마지막 완전한 줄까지만 흡수한다
    (반쪽 원칙이 매 턴 주입되는 데이터 오염 차단)."""
    s = Sys(FakeGuide(), guild_id=1, organt_builder=None, bot_info={11: "QA"},
            session_dir=str(tmp_path))
    long_line = "- " + "가" * 120
    body = "\n".join(long_line for _ in range(20))           # 2,400자+
    asyncio.run(s._absorb_role_profiles(f"[직무기준] QA\n{body}\n[/직무기준]"))
    saved = s.role_profiles["QA"]
    assert len(saved) <= 1500
    assert saved.endswith(long_line)                          # 마지막이 '완전한 줄'


def test_유사프로젝트_존재시_신설전_정보공급(tmp_path):
    """[공급 원칙] 새 요청이 기존 프로젝트와 유사하면 리더 프롬프트에 그 사실을 공급한다 —
    같은 요청의 재전송이 이름 짓기 운에 따라 중복 신설되던 비결정성(라이브 P-006)의 교정.
    판단(재사용/신설)은 리더 몫, 정보만 구조가."""
    g = FakeGuide()
    s = Sys(g, guild_id=1, organt_builder=None, bot_info={11: "L"},
            workspace="/tmp/ws-x", session_dir=str(tmp_path))
    s.projects = {900: {"id": "P-005", "name": "공공데이터 웹사이트", "channel": 900,
                        "workspace": "/tmp/x", "leader": 11, "summary": "",
                        "purpose": "공공 데이터를 하나 받아와서 이를 활용한 웹 사이트 만들어줘"}}
    bodies = []

    async def fake_run_turn(flow, oid, body, kind, role):
        bodies.append(body)
        flow.current = None
        return "ok"
    s.run_turn = fake_run_turn
    asyncio.run(s.handle_user_input(500, 11, "공공 데이터를 받아와서 활용한 웹 사이트 만들어줘", root_id=None))
    assert "[유사 프로젝트 존재" in bodies[0] and "P-005" in bodies[0]
    assert "새 작품으로 등록됩니다" in bodies[0]              # 신규가 기본 — 임의 재사용 금지 안내
    asyncio.run(s.handle_user_input(501, 11, "스네이크 게임 만들어줘", root_id=None))
    assert "[유사 프로젝트 존재" not in bodies[1]             # 무관한 요청엔 없음


def test_이름충돌_다른작품은_하이재킹_금지_자동고유화(tmp_path):
    """[신원 가드] 이름은 라벨이지 신원이 아니다 — 일반명사 이름이 우연히 일치해도 목표 원문이
    다르면 기존 프로젝트(채널·작업공간·배포 슬롯)를 차지하지 않고 이름을 고유화해 신규 등록한다
    (라이브: 지진 사이트가 같은 영문명으로 대기질 P-006을 하이재킹). 진짜 연장(원문 유사)은 재사용."""
    s = Sys(FakeGuide(), guild_id=1, organt_builder=None, bot_info={11: "L"},
            session_dir=str(tmp_path))
    s._origin_request = "공공 데이터 대기질 미세먼지 사이트 만들어줘"
    pid1 = s._register_project(100, "public-data-website", "/ws/a", 11,
                               purpose="공공 데이터 대기질 미세먼지 사이트 만들어줘")
    # 같은 이름 + '다른 작품'(지진) → 차지 금지, 자동 고유화로 신규 등록
    pid2 = s._register_project(200, "public-data-website", "/ws/b", 11,
                               purpose="지진 데이터를 받아 이펙트 화려한 시각화 사이트 만들어줘")
    assert pid2 != pid1                                        # 신규 식별번호
    assert s.projects[100]["id"] == pid1                       # 원 프로젝트 무사(채널·ws 보존)
    assert s.projects[100]["workspace"] == "/ws/a"
    assert s.projects[200]["name"].startswith("public-data-website-")   # 라벨 고유화
    # 같은 이름 + '진짜 연장'(원문 유사) → 종전대로 재사용(채널 이동)
    pid3 = s._register_project(300, "public-data-website", "/ws/c", 11,
                               purpose="공공 대기질 미세먼지 데이터 사이트 개선해줘")
    assert pid3 == pid1 and s.projects[300]["id"] == pid1      # 재사용(이동)


def test_배포_진행중_재호출은_대기_새배포_트리거_금지():
    """[배포 폴링 차단] 빌드가 길어지면 리더가 deploy를 재호출해 '점검'하려 하는데, 재호출은 새
    배포를 또 트리거(빌드 리셋)하는 자기 영속 루프가 된다(라이브: [안내][배포] 1분 간격 도배 +
    같은 턴 4연발). 흐름당 동시 1회 — 진행 중 재호출·병렬 호출은 [대기]로 즉답한다."""
    import src.guide_tools as gt
    f = Flow(FakeGuide(), channel_id=500, guild_id=1, leader_id=11, bot_info={11: "L"})
    f.start_root("root")
    f.workspace = "/tmp/ws-x"
    f.project_id = "P-009"                       # 등록 프로젝트만 배포 슬롯을 가진다(미등록은 즉시 거부)
    t = {x.name: x for x in make_guide_tools(f, 11, "leader")}
    calls = {"n": 0}
    gate = asyncio.Event()

    def fake_deploy_sync(ws, name, *a):
        calls["n"] += 1
        return f"배포 성공 ✅ {name}"

    async def scenario(monkey_ds):
        import src.deploy as dp
        orig = dp.deploy_sync
        dp.deploy_sync = monkey_ds
        try:
            import anyio

            async def slow_to_thread(fn, *a):
                await gate.wait()                      # 1번째 배포를 잡아둔 채
                return fn(*a)
            orig_run = anyio.to_thread.run_sync
            anyio.to_thread.run_sync = slow_to_thread
            try:
                import os as _os
                _os.environ.setdefault("GH_PAT", "x"); _os.environ.setdefault("GH_USER", "x")
                _os.environ.setdefault("RENDER_KEY", "x"); _os.environ.setdefault("RENDER_OWNER", "x")
                t1 = asyncio.ensure_future(t["deploy"].handler({"name": "site"}))
                await asyncio.sleep(0.02)
                r2 = await t["deploy"].handler({"name": "site"})       # 진행 중 재호출
                assert "[대기]" in r2["content"][0]["text"]            # 새 배포 트리거 없이 즉답
                gate.set()
                out = await t1
                assert "배포 성공" in out["content"][0]["text"]
                assert calls["n"] == 1                                 # 실제 배포는 1회뿐
            finally:
                anyio.to_thread.run_sync = orig_run
        finally:
            dp.deploy_sync = orig
    asyncio.run(scenario(fake_deploy_sync))


def test_직군밖_Work는_전문가가_반려_리더는_채용지시_받음():
    """[전문화의 구조 채널] 도메인 적합성은 키워드 하드코딩이 아니라 '받는 전문가'가 판정한다 —
    owner가 보고 첫 줄에 [직군밖] 필요직군 을 적으면: 실패·미완이 아닌 올바른 반려로 분류되고,
    소유가 해제되며, 리더는 'recruit로 채용해 맡기라'는 구조 지시를 받는다(관계없는 직군이 일을
    흡수하던 경로 차단 — 라이브: ML이 백엔드에 묶여 감)."""
    f = Flow(FakeGuide(), channel_id=500, guild_id=1, leader_id=11,
             bot_info={11: "L", 12: "백엔드"})
    f.start_root("root")

    async def wake(to, b, k):
        assert "[직군밖]" in b and "반려하세요" in b              # 위임 계약에 반려권 명시
        return "[직군밖] AI 엔지니어\n이 모델 설계는 ML 전문성이 필요합니다."
    f.wake = wake
    t = {x.name: x for x in make_guide_tools(f, 11, "leader")}
    asyncio.run(t["create_task"].handler({"members": "12"}))
    f.current.status.goal = "ML 모델로 혼잡도 예측"
    r = asyncio.run(t["request"].handler({"to_id": "12", "kind": "Work", "body": "모델 만들어줘"}))
    txt = r["content"][0]["text"]
    assert "[직군밖 반려]" in txt and "recruit(role='AI 엔지니어')" in txt   # 채용 구조 지시
    assert "떠넘기지 마세요" in txt
    assert f.current.owner == 0                                   # 소유 해제(채용 전문가가 새 owner)
    assert not f.current.owner_delivered and not f.current.owner_incomplete
    assert f.consec_fail == 0                                     # 반려 ≠ 실패
    assert f.comm.alive == 11                                     # 베턴 정상 복귀


def test_범용직군_채용은_정책으로_거부():
    """[전문화 정책 — 사용자 결정] 풀스택·제너럴리스트류 범용 직군 채용은 거부된다 — 범용은 모든
    일을 흡수해 전문 채용을 막고(라이브: 1봇 22건 집중) 병렬의 병목이 된다."""
    f = Flow(FakeGuide(), channel_id=500, guild_id=1, leader_id=11,
             bot_info={11: "L", 12: "백엔드", 13: "예비"})
    f.start_root("root")
    t = {x.name: x for x in make_guide_tools(f, 11, "leader")}
    asyncio.run(t["create_task"].handler({"members": "12"}))
    for bad in ("풀스택 개발자", "Full-Stack Engineer", "만능 개발자"):
        r = asyncio.run(t["recruit"].handler({"role": bad, "member": "13"}))
        assert "채용 거부(전문화 정책)" in r["content"][0]["text"], bad
    r = asyncio.run(t["recruit"].handler({"role": "AI 엔지니어", "member": "13"}))
    assert "합류" in r["content"][0]["text"]                      # 전문 직군은 정상 채용


def test_신규요청은_같은이름이라도_신설_P번호명시만_재사용(tmp_path):
    """[신원 재사용 권한 — 주소 지정의 이치(사용자 사건)] 메인 채널의 '새 요청'은 이름이 기존
    작품과 같아도 신설(자동 고유화)된다 — 단어 유사+같은 이름 작명이 기존 P-009의 신원·작업공간·
    채널을 통째로 가져가던 사고 차단. 기존 작품 재사용은 ① 그 프로젝트 채널 개입(reuse_ok=None)
    ② 원문에 P-번호 명시(reuse_ok={'P-00n'})로만."""
    s = Sys(FakeGuide(), guild_id=1, organt_builder=None, bot_info={11: "L"},
            session_dir=str(tmp_path), workspace=str(tmp_path))
    pid1 = s._register_project(500, "디펜스 게임", str(tmp_path / "a"), 11, purpose="마법진 디펜스")
    # 신규 요청(명시 P-번호 없음) + 같은 이름 → 신설(고유화), 기존 채널·신원 불변
    pid2 = s._register_project(900, "디펜스 게임", str(tmp_path / "b"), 11,
                               purpose="2인 협동 디펜스", reuse_ok=set())
    assert pid2 != pid1 and s.projects[500]["id"] == pid1 and s.projects[500]["channel"] == 500
    assert s.projects[900]["id"] == pid2 and s.projects[900]["name"] != "디펜스 게임"   # 이름 고유화
    # 원문에 P-번호 명시 → 그 프로젝트만 재사용 허용(채널 이동 — 기존 동작)
    pid3 = s._register_project(901, "디펜스 게임", str(tmp_path / "c"), 11,
                               purpose="마법진 디펜스 확장", reuse_ok={pid1})
    assert pid3 == pid1 and s.projects[901]["id"] == pid1 and 500 not in s.projects


def test_병렬Work_동시실행_리스_조인_owner():
    """[RFC-006 Work-fork v1] 독립 영역 Work 2건이 '동시에' 실행되고(두 wake가 서로를 기다려야
    풀리는 게이트로 증명), 가지 동안 쓰기 리스가 활성·조인 시 해제되며, 조인 합본·owner(첫 수신자)·
    participated·work_delegated가 직렬 request와 일관되게 기록된다 — 병렬 실행+직렬 통합(RFC-005 P1)."""
    g = FakeGuide()
    f = _flow(g)
    f.bot_info[13] = "프론트"
    f.project_team.append(13)
    f.workspace = "/tmp/ws-p"
    started, gate = [], asyncio.Event()

    async def wake(to, b, k):
        assert "쓰기 영역(리스)" in b and "보고 계약" in b      # Work 계약 동봉
        assert f.write_lease.get(to)                            # 가지 동안 리스 활성
        started.append(to)
        if len(started) == 2:
            gate.set()
        await asyncio.wait_for(gate.wait(), 5)                  # 둘 다 시작해야 풀림 = 동시 실행 증명
        f.act_by[to] = f.act_by.get(to, 0) + 1                  # 실작업 흔적
        return f"[결과] 완료/{to} [변경] x [검증] ok [리스크] 없음"
    f.wake = wake
    t = _tools(f, 11, "leader")
    asyncio.run(t["create_task"].handler({"members": "12,13"}))
    f.current.participated.add(12); f.current.participated.add(13)
    asyncio.run(t["set_goal"].handler({"goal": "측정가능 g"}))
    import json as _j
    r = asyncio.run(t["parallel_work"].handler({"assignments": _j.dumps([
        {"to": "12", "files": "server.js", "body": "서버"},
        {"to": "13", "files": "public/app.js,public/style.css", "body": "프론트"}])}))
    txt = r["content"][0]["text"]
    assert "[병렬 조인 — 2건]" in txt and "완료/12" in txt and "완료/13" in txt
    assert sorted(started) == [12, 13]                          # 둘 다 실제 깨어남
    assert not f.write_lease                                    # 조인=리스 해제
    assert f.current.owner == 12 and f.current.owner_delivered  # 첫 수신자=owner(기존 규칙 일관)
    assert f.current.work_delegated == 2 and getattr(f, "fork_active", 0) == 0


def test_병렬Work_영역겹침과_전제위반은_거부():
    """[토큰 중립 조건 ⓐ 기계 강제] 영역 일치/포함이면 거부(겹침=통합 충돌→Redo=토큰 손실 — 직렬로).
    goal 미확정·1건·빈 files도 거부(병렬의 전제: 합의된 목표 + 영역 분리 + 2건 이상)."""
    g = FakeGuide()
    f = _flow(g)
    f.bot_info[13] = "프론트"
    f.project_team.append(13)
    f.workspace = "/tmp/ws-p2"
    t = _tools(f, 11, "leader")
    asyncio.run(t["create_task"].handler({"members": "12,13"}))
    import json as _j
    mk = lambda files2: _j.dumps([{"to": "12", "files": "public/app.js", "body": "a"},
                                  {"to": "13", "files": files2, "body": "b"}])
    r0 = asyncio.run(t["parallel_work"].handler({"assignments": mk("public/x.js")}))
    assert "Goal 확정 전" in r0["content"][0]["text"]            # goal 미확정 거부
    f.current.participated.add(12); f.current.participated.add(13)
    asyncio.run(t["set_goal"].handler({"goal": "g"}))
    r1 = asyncio.run(t["parallel_work"].handler({"assignments": mk("public/app.js")}))
    assert "영역 겹침 거부" in r1["content"][0]["text"]          # 동일 파일
    r2 = asyncio.run(t["parallel_work"].handler({"assignments": mk("public")}))
    assert "영역 겹침 거부" in r2["content"][0]["text"]          # 포함 관계(폴더⊃파일)
    r3 = asyncio.run(t["parallel_work"].handler({"assignments": _j.dumps(
        [{"to": "12", "files": "a.js", "body": "x"}])}))
    assert "2건부터" in r3["content"][0]["text"]                 # 1건 거부


def test_협의명단은_스냅샷에_영속되고_복원된다(tmp_path):
    """[재협의 루프 차단] participated(협의 완료 명단)가 스냅샷에 없으면 재개마다 set_goal 게이트가
    전원 재협의를 강제 — 라이브 P-010 개입: 동면 재개 5회 동안 리더가 같은 협의 질문을 5회 반복
    (스레드 통독으로 발견). 협의는 '사실'이라 영속이 옳다(검증 누계는 의도적으로 0 재시작 — 별개)."""
    g = FakeGuide()
    f = _flow(g)
    t = _tools(f, 11, "leader")
    asyncio.run(t["create_task"].handler({"members": "12"}))
    f.current.participated.add(12)
    f.current.standard = "최대 표준: IQAir급 게이지·24h 예측 차트·건강 권고"   # [최대화] 바
    f.current.interfaces = "백→프 JSON {city,aqi,grade}"                     # [협업] 계약
    s = Sys(g, guild_id=1, organt_builder=None, bot_info={11: "L", 12: "B"}, session_dir=str(tmp_path))
    snap = s._task_snapshot(f, f.current)
    assert snap["participated"] == [12]                      # 영속
    assert snap["standard"] and snap["interfaces"]           # [최대화/협업] 스냅샷에 영속(동면 너머 바·계약 유지 — 라이브 버그 수정)
    f2 = _flow(FakeGuide())
    proj = {"id": "P-X", "open_task": snap}
    asyncio.run(s._restore_open_task(f2, proj))
    assert 12 in f2.current.participated                     # 복원 → 재개 후 set_goal 재협의 불요
    assert "IQAir" in f2.current.standard and "JSON" in f2.current.interfaces   # 복원 → 동면 재개에도 최대 바·계약 유지


def test_활동기반_이어가기예산_진행세그는_소모없음():
    """[활동 기반 예산] 직전 세그먼트에 실작업(act_count 증가)이 있으면 이어가기 예산을 소모하지
    않는다 — 예산의 목적은 '무진행 루프 차단'이지 '대형 작업 총량 제한'이 아니다(라이브 P-010:
    동면 재개+재협의가 예산 12를 태워 '진행 중' 작업이 마감 직전 절단). max_continue=2여도 진행
    세그먼트 3개를 지나 완주하고, 무진행만 누적돼 한도에서 닫힌다."""
    import types
    g = FakeGuide()
    s = Sys(g, guild_id=1, organt_builder=None, bot_info={11: "L"}, workspace="/ws", max_continue=2)
    calls = []

    async def fake_run_turn(flow, oid, body, kind, role):
        calls.append(1)
        if len(calls) <= 3:                            # 세그 1~3: 미완이지만 매번 실작업 진행
            flow.current = types.SimpleNamespace(
                task_id="t1", status=types.SimpleNamespace(status="진행", result=None))
            flow.act_count += 1                        # 진행 증거
            return "작업 중 (⚠ 턴 한도 도달 — 미완)"
        flow.current = None                            # 4번째에 완주
        return "완료"

    s.run_turn = fake_run_turn
    asyncio.run(s.handle_user_input(500, 11, "큰 작업", root_id="r"))
    assert len(calls) == 4                             # 예산 2를 넘는 진행 세그먼트도 절단되지 않음
    ci = [e for e in s.flow_log if e["event"] == "continue_incomplete"]
    assert all(e.get("progressed") for e in ci) and all(e.get("attempt") == 0 for e in ci)

    g2 = FakeGuide()
    s2 = Sys(g2, guild_id=1, organt_builder=None, bot_info={11: "L"}, workspace="/ws", max_continue=2)
    calls2 = []

    async def stuck_run_turn(flow, oid, body, kind, role):
        calls2.append(1)
        flow.current = types.SimpleNamespace(
            task_id="t1", thread_id="th", block_id="blk", team=[], owner=0,
            participated=set(), collab_notes="",
            status=types.SimpleNamespace(status="진행", result=None, purpose="", goal="", owner=""))
        return "작업 중 (⚠ 턴 한도 도달 — 미완)"       # 무진행(실작업 0) 반복

    s2.run_turn = stuck_run_turn
    asyncio.run(s2.handle_user_input(500, 11, "정체 작업", root_id="r"))
    assert len(calls2) == 3                            # 첫 턴 + 무진행 이어가기 2회에서 한도 종결

    # 교대 시나리오: 무진행↔진행이 번갈아도 '진행 시 리셋' 덕에 연속 한도(2)에 안 걸린다
    g3 = FakeGuide()
    s3 = Sys(g3, guild_id=1, organt_builder=None, bot_info={11: "L"}, workspace="/ws", max_continue=2)
    calls3 = []

    async def alt_run_turn(flow, oid, body, kind, role):
        calls3.append(1)
        if len(calls3) <= 4:
            flow.current = types.SimpleNamespace(
                task_id="t1", thread_id="th", block_id="blk", team=[], owner=0,
                participated=set(), collab_notes="",
                status=types.SimpleNamespace(status="진행", result=None, purpose="", goal="", owner=""))
            if len(calls3) % 2 == 0:
                flow.act_count += 1                    # 짝수 세그만 진행(교대)
            return "작업 중 (⚠ 턴 한도 도달 — 미완)"
        flow.current = None
        return "완료"

    s3.run_turn = alt_run_turn
    asyncio.run(s3.handle_user_input(500, 11, "교대 작업", root_id="r"))
    assert len(calls3) == 5                            # 연속 2 무진행이 없으므로 완주(리셋 검증)


def test_검증위임에_owner도메인_루브릭_자동주입():
    """[RFC-008 P0 보강] owner 인도 후 '다른 멤버'에게 가는 Work(=검증 위임)에 owner 산출물 도메인의
    직무 기준이 루브릭으로 자동 동봉된다 — 라이브 P-010 1차에서 루브릭이 거부 메시지에만 있어 0회
    발동(검증이 카운트되면 게이트 미통과)한 구멍 교정. 검증자가 'owner 도메인 기준에 충분한가'로
    채점하게. owner 본인 재위임·owner 미인도 시엔 주입 안 함."""
    g = FakeGuide()
    f = _flow(g)
    f.bot_info[12] = "백엔드"
    f.bot_info[13] = "QA"
    f.project_team.append(13)
    f.craft_of = lambda job: "엣지·경계값을 시뮬로 직접 재현" if str(job).strip() == "백엔드" else ""
    waked = []

    async def wake(to, b, k):
        waked.append((to, b))
        return "검증 보고"
    f.wake = wake
    t = _tools(f, 11, "leader")
    asyncio.run(t["create_task"].handler({"members": "12,13"}))
    f.current.participated.add(12)
    f.current.participated.add(13)
    asyncio.run(t["set_goal"].handler({"goal": "g"}))
    f.current.owner = 12
    f.current.owner_delivered = True                   # owner(백엔드) 인도 완료
    asyncio.run(t["request"].handler({"to_id": "13", "kind": "Work", "body": "검증해줘"}))   # 검증 위임(QA에게)
    body13 = [b for to, b in waked if to == 13][-1]
    assert "산출물 품질 기준" in body13 and "엣지·경계값을 시뮬로" in body13   # owner(백엔드) 도메인 기준 주입(검증/후속구현 양쪽 커버)
    waked.clear()
    asyncio.run(t["request"].handler({"to_id": "12", "kind": "Work", "body": "보완"}))        # owner 본인 재위임
    body12 = [b for to, b in waked if to == 12][-1]
    assert "산출물 품질 기준" not in body12             # owner 자신에겐 안 붙음




def test_리더독식_Task도_교차검증_의무(tmp_path):
    """[발견1 교정 2026-06-13] owner 없이 리더가 직접 구현한 Task(leader_writes>0)도 제3자 검증을
    면제하지 않는다 — '누가 만들었든 제3자 검증'은 보편 이치(코드리뷰 연구). 종전엔 owner==0이면
    교차검증 게이트가 건너뛰어 리더 독식이 검증 0으로 마감되던 구멍(P-009/P-010 리더 run 독식 경로)."""
    g = FakeGuide()
    f = _flow(g)
    f.bot_info[13] = "프론트"
    f.project_team.append(13)
    t = _tools(f, 11, "leader")
    asyncio.run(t["create_task"].handler({"members": "13"}))
    f.current.participated.add(13)
    asyncio.run(t["set_goal"].handler({"goal": "g"}))
    # 리더가 owner 없이 직접 구현(leader_writes>0), owner는 0
    f.current.owner = 0
    f.current.leader_writes = 2
    f.current.verified = True
    r1 = asyncio.run(t["complete_task"].handler({"result": "끝"}))
    assert "완료 거부(교차 검증" in r1["content"][0]["text"] and f.current is not None   # 리더 독식도 검증 의무
    # 타 멤버(13)가 검증 참여 → cross_checks 증가 → 게이트 통과
    f.current.cross_checks = f.current.cross_check_offdomain = 1
    f.act_by[13] = 1                                    # 검증자(13)가 실제로 run 검증함(기여 게이트 통과)
    r2 = asyncio.run(t["complete_task"].handler({"result": "끝"}))
    assert f.current is None                            # 검증 후 마감 통과


# ── 배포 풀 자가관리 (무료 티어 한도로 인한 작업 멈춤 차단) ────────────────────────
def test_배포풀_자가정리_고아만_오래된순_삭제_참조링크_보존(monkeypatch):
    """한도 임박 시 '현 채널이 참조하지 않는 고아'만 오래된 순으로 삭제해 슬롯을 확보하고,
    keep-set(참조 중 링크)은 절대 건드리지 않는다. (라이브 P-019: 풀이 차서 배포가 멈춤)"""
    from src import deploy
    svcs = []
    for i in range(3):                                  # 참조 중(keep) — 보존돼야 함
        svcs.append({"service": {"id": f"keep{i}", "name": f"organt-p-00{i}",
                                 "serviceDetails": {"url": f"https://organt-p-00{i}.onrender.com"},
                                 "createdAt": f"2026-06-0{i + 1}"}})
    for i in range(22):                                 # 고아 — 오래된 것부터 삭제 대상
        svcs.append({"service": {"id": f"orph{i:02d}", "name": f"old-test-{i:02d}",
                                 "serviceDetails": {"url": f"https://old-test-{i:02d}.onrender.com"},
                                 "createdAt": f"2026-05-{i + 1:02d}"}})
    deleted_ids = []

    def fake_http(method, url, token, *a, **k):
        if method == "GET" and "/services?" in url:
            return 200, svcs
        if method == "DELETE":
            deleted_ids.append(url.rsplit("/", 1)[-1])
            return 204, {}
        return 200, {}

    monkeypatch.setattr(deploy, "_http", fake_http)
    keep = {"organt-p-000", "organt-p-001", "organt-p-002"}
    gone = deploy._free_slots("rk", keep, want_free=2, cap=25)   # 25/25 → free=0 → 고아 2개 확보
    assert len(gone) == 2 and all(g.startswith("old-test-") for g in gone)
    assert "old-test-00" in gone and "old-test-01" in gone        # 가장 오래된 두 고아
    assert not any(d.startswith("keep") for d in deleted_ids)     # keep-set은 절대 삭제 안 함


def test_배포풀_슬롯충분하면_정리안함(monkeypatch):
    """슬롯이 남으면(한도 미임박) 아무것도 삭제하지 않는다 — 보수적 자가관리."""
    from src import deploy
    svcs = [{"service": {"id": f"s{i}", "name": f"svc-{i}",
                         "serviceDetails": {"url": f"https://svc-{i}.onrender.com"},
                         "createdAt": "2026-06-01"}} for i in range(10)]
    deleted = []

    def fake_http(method, url, token, *a, **k):
        if method == "GET":
            return 200, svcs
        if method == "DELETE":
            deleted.append(url)
            return 204, {}
        return 200, {}

    monkeypatch.setattr(deploy, "_http", fake_http)
    gone = deploy._free_slots("rk", set(), want_free=2, cap=25)   # 10/25 → 슬롯 충분
    assert gone == [] and deleted == []


def test_등록레지스트리_참조서비스명_추출(tmp_path):
    """keep-set = projects.json이 아직 참조하는 onrender 서비스명(남아있는 채널의 링크)."""
    from src import deploy
    p = tmp_path / "projects.json"
    p.write_text('{"projects":{"1":{"summary":"라이브: https://organt-p-016.onrender.com 확인"},'
                 '"2":{"summary":"https://organt-cell-grow-online.onrender.com 배포완료"},'
                 '"3":{"summary":""}}}')
    keep = deploy._referenced_services(str(p))
    assert keep == {"organt-p-016", "organt-cell-grow-online"}
