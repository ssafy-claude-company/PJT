"""SYS — 흐름 제어자 (docs: Architecture/Core.md).

User→SMS→SYS→Organt. SYS가 Rule(Communication 베턴 + Task)을 들고
Guide(Discord)로 구조화 메시지를 실어 나르며 Task 흐름을 제어한다.
Organt는 SYS가 깨울 때만 결정을 낸다(policy). 모든 흐름은 SYS가 제어한다.

흐름(Task.md + Communication.md 통합, Thread 안에서):
  1. open_task: 채널에 [Task-XXX] 상태블록 + Thread 생성
  2. SYS가 Leader를 깨움 (origin→Leader Work Request)
  3. Leader가 Goal·Todo 계획 → 상태블록 갱신
  4. Todo마다 Leader→담당 Work Request → 담당 작업 → Response(close) → 베턴 복귀
  5. 완료확인 → Goal 판정(미달시 추가 Todo loop) → Leader가 origin에 보고(close) → 종료
"""
from typing import Dict, List, Optional, Tuple

from .communication import CommunicationManager
from .discord_guide import DiscordGuide
from .protocol import Kind
from .task_rule import Phase, Task

ORIGIN = 0  # User/SMS 시작점(흐름의 origin)


class Sys:
    """SYS 제어자: Rule + Guide를 조율해 Task 흐름을 구동한다."""

    def __init__(self, guide: DiscordGuide, channel_id: int,
                 bot_info: Optional[Dict[int, str]] = None):
        self.guide = guide
        self.channel_id = channel_id
        self.bot_info = bot_info or {}
        self.flow_log: List[dict] = []   # 흐름 모니터링(SYS 책임)

    def _log(self, event, **f):
        self.flow_log.append({"event": event, **f})

    async def run_task(self, task_id, purpose: str, leader: int,
                       team: List[int], policy, max_rounds: int = 2) -> dict:
        # origin(User/SYS)은 system 봇으로 발화한다.
        self.guide.register_organt(ORIGIN, self.guide.system)

        task = Task(task_id, purpose, leader)
        for m in team:
            if m != leader:
                task.recruit(m)
        comm = CommunicationManager(ORIGIN)

        # 1. 채널 상태블록 + Thread
        block_id, thread_id = await self.guide.open_task(self.channel_id, task.status(self.bot_info))
        self._log("task_open", task=task.task_id, thread=thread_id)

        async def refresh():
            await self.guide.update_status(self.channel_id, block_id, task.status(self.bot_info))

        # 2. Leader 깨움 (origin→Leader Work Request, Thread 내)
        root = await self.guide.send_request(thread_id, ORIGIN, leader, Kind.WORK, f"[Task] {purpose}")
        comm.request(ORIGIN, leader, root, Kind.WORK)
        self._log("wake_leader", leader=leader)

        # 3. Leader 계획 (Goal + Todo)
        goal, todos = await policy.plan(purpose, list(task.team))
        task.set_goal(goal)
        await refresh()
        self._log("goal_set", goal=goal)

        all_results: List[str] = []
        rounds = 0
        while True:
            rounds += 1
            # 4. Todo 생성·분배 (잘못된 배정은 팀으로 보정)
            nonleader = [m for m in task.team if m != leader] or [leader]
            pending = []
            for text, assignee in todos:
                if assignee not in task.team:
                    assignee = nonleader[0]
                t = task.add_todo(text, assignee)
                task.distribute(t.todo_id, assignee)
                pending.append((t, assignee))
            await refresh()  # 분배

            # 5. 수행: 담당이 타인이면 Thread 내 베턴(Request→작업→Response),
            #    담당이 Leader 본인이면 이미 활성이므로 직접 수행(베턴 홉 없음).
            for t, assignee in pending:
                if assignee == leader:
                    result = await policy.do_work(t.text)
                    all_results.append(result)
                    task.complete(t.todo_id)
                    self._log("todo_done", todo=t.text, by=assignee, mode="self")
                    continue
                req = await self.guide.send_request(thread_id, leader, assignee, Kind.WORK, t.text)
                comm.request(leader, assignee, req, Kind.WORK)               # 담당 wake
                result = await policy.do_work(t.text)
                all_results.append(result)
                await self.guide.send_response(thread_id, assignee, req, result)
                comm.respond(assignee, "accept", result)                    # 베턴 복귀(Leader)
                task.complete(t.todo_id)
                self._log("todo_done", todo=t.text, by=assignee)
            await refresh()  # 완료확인

            # 5. Goal 판정
            achieved, report = await policy.review(goal, all_results)
            task.judge_goal(achieved)
            self._log("judge", achieved=achieved, round=rounds)
            if achieved or rounds >= max_rounds:
                break
            await refresh()  # 미달 → 목표확정으로 loop
            _, todos = await policy.plan(purpose, list(task.team))

        # 6. Leader가 origin Request에 Response(close) → 흐름 시작점 복귀·종료
        await self.guide.send_response(thread_id, leader, root, report)
        comm.respond(leader, "accept", report)
        if task.phase == Phase.CHECK and task.goal_met:
            task.report(report)
        else:
            task.result = report  # 미달 종료(엣지)
        await refresh()  # 보고 + result
        self._log("task_report", done=comm.done, phase=task.phase.value)

        return {"task": task, "comm": comm, "thread_id": thread_id,
                "block_id": block_id, "report": report}
