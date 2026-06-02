"""Task Rule — 작업 단위 (docs: Other/Rule/작업단위/Task.md).

Task = Goal 완수를 위한 작업 단위. 추상 상태기계이며, Discord 표현은
protocol.TaskStatus + DiscordGuide가 맡는다.

Flow: ①생성(Leader·Purpose) ②Team 모집 ③Goal 확정 ④Todo 생성 ⑤분배
      ⑥완료확인 ⑦Goal 완수판정(미달→④) ⑧보고
"""
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional

from .protocol import TaskStatus


class Phase(str, Enum):
    CREATED = "생성"
    RECRUITED = "팀모집"
    GOAL_SET = "목표확정"
    DISTRIBUTED = "분배"
    CHECK = "완료확인"
    REPORTED = "보고"


class TaskError(Exception):
    pass


@dataclass
class Todo:
    todo_id: int
    text: str
    assignee: Optional[int] = None
    done: bool = False


class Task:
    """Task Rule 상태기계."""

    def __init__(self, task_id, purpose: str, leader: int):
        self.task_id = str(task_id)
        self.purpose = purpose            # 문제(시작 시 부여, solution 아님)
        self.leader = leader              # drive/의사결정/Goal 판정
        self.team: List[int] = [leader]   # Leader도 Team 일원
        self.goal: Optional[str] = None   # 측정가능 목표(Team이 Purpose로 정함)
        self.todos: List[Todo] = []
        self.result: Optional[str] = None
        self.phase = Phase.CREATED
        self.goal_met = False
        self.rounds = 0                   # Goal 판정 라운드 수(loop 횟수)
        self._seq = 0
        self.history: List[Phase] = [Phase.CREATED]

    def _to(self, phase: Phase):
        self.phase = phase
        self.history.append(phase)

    # ② 팀 모집
    def recruit(self, organt_id: int):
        if self.phase not in (Phase.CREATED, Phase.RECRUITED):
            raise TaskError("팀 모집은 생성 직후 단계에서 한다.")
        if organt_id not in self.team:
            self.team.append(organt_id)
        if self.phase == Phase.CREATED:
            self._to(Phase.RECRUITED)

    # ③ Goal 확정
    def set_goal(self, goal: str):
        if self.phase not in (Phase.CREATED, Phase.RECRUITED):
            raise TaskError("Goal은 팀 모집 후 확정한다.")
        self.goal = goal
        self._to(Phase.GOAL_SET)

    # ④ Todo 생성 (loop 재투입 포함)
    def add_todo(self, text: str, assignee: Optional[int] = None) -> Todo:
        if self.phase not in (Phase.GOAL_SET, Phase.DISTRIBUTED, Phase.CHECK):
            raise TaskError("Todo는 Goal 확정 후 생성한다.")
        if self.phase == Phase.CHECK:       # 미달 후 재투입
            self._to(Phase.GOAL_SET)
        self._seq += 1
        todo = Todo(self._seq, text, assignee)
        self.todos.append(todo)
        return todo

    # ⑤ 분배
    def distribute(self, todo_id: int, assignee: int):
        self._todo(todo_id).assignee = assignee
        if assignee not in self.team:
            self.team.append(assignee)
        if self.phase == Phase.GOAL_SET:
            self._to(Phase.DISTRIBUTED)

    # ⑥ 완료(작업) → 모두 끝나면 완료확인
    def complete(self, todo_id: int):
        self._todo(todo_id).done = True
        if self.todos and all(t.done for t in self.todos):
            self._to(Phase.CHECK)

    # ⑦ Goal 완수판정 (Leader). 미달 → ④로 loop
    def judge_goal(self, achieved: bool):
        if self.phase != Phase.CHECK:
            raise TaskError("Goal 판정은 완료확인 단계에서 한다.")
        self.rounds += 1
        self.goal_met = achieved
        if not achieved:
            self._to(Phase.GOAL_SET)

    # ⑧ 보고
    def report(self, result: str):
        if self.phase != Phase.CHECK or not self.goal_met:
            raise TaskError("보고는 Goal 완수 판정 후 한다.")
        self.result = result
        self._to(Phase.REPORTED)

    def _todo(self, todo_id: int) -> Todo:
        for t in self.todos:
            if t.todo_id == todo_id:
                return t
        raise TaskError(f"Todo {todo_id} 가 없습니다.")

    def status(self, bot_info: Optional[Dict[int, str]] = None) -> TaskStatus:
        info = bot_info or {}
        group = [(f"<@{m}>", info.get(m, "")) for m in self.team]
        return TaskStatus(task_id=self.task_id, purpose=self.purpose,
                          status=self.phase.value, goal=self.goal or "",
                          group=group, result=self.result)
