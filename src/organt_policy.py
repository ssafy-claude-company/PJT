"""Organt(LLM)를 SYS 정책(policy)으로 감싸는 어댑터.

SYS가 Organt를 깨울 때(plan/do_work/review) Organt(LLM)에게 구조화 출력을 요구하고
파싱해 SYS에 돌려준다. Leader Organt가 계획·판정을, 담당 Organt가 작업을 수행한다.
"""
import json
import re
from typing import Dict, List, Optional, Tuple

from .organt import Organt


def _extract_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except (ValueError, TypeError):
        return {}


class OrgantPolicy:
    """Leader Organt가 계획/판정, (옵션)담당 Organt가 작업 수행."""

    def __init__(self, leader: Organt, workers: Optional[Dict[int, Organt]] = None):
        self.leader = leader
        self.workers = workers or {}

    async def plan(self, purpose: str, team: List[int]) -> Tuple[str, List[Tuple[str, int]]]:
        others = [m for m in team if m != team[0]] or team
        prompt = (
            f"당신은 Task Leader입니다. Purpose(문제): {purpose}\n"
            f"팀원 ID 목록: {team} (첫 번째 {team[0]}이 Leader=당신)\n"
            f"측정 가능한 Goal과 2개의 Todo를 정하고, 각 Todo는 Leader가 아닌 팀원"
            f"({others})에게 배정하세요.\n"
            f'반드시 JSON만 출력: {{"goal":"...","todos":[{{"text":"...","assignee":<팀원ID>}}]}}'
        )
        data = _extract_json(await self.leader.handle(prompt))
        goal = data.get("goal") or "목표 미정"
        todos: List[Tuple[str, int]] = []
        for t in data.get("todos", []):
            try:
                todos.append((str(t.get("text", "")), int(t.get("assignee", team[0]))))
            except (ValueError, TypeError):
                continue
        if not todos:
            todos = [("작업 수행", team[0])]
        return goal, todos

    async def do_work(self, todo: str) -> str:
        worker = self.leader  # 단일 LLM 수행(데모). 필요시 self.workers[assignee] 사용 가능.
        prompt = (
            f"다음 작업을 실제로 수행하세요. 산출물이 파일이면 Write 툴로 작업공간에 "
            f"상대경로로 만드세요(절대경로 금지).\n작업: {todo}\n끝나면 결과를 한 줄로 보고하세요."
        )
        return (await worker.handle(prompt))[:300]

    async def review(self, goal: str, results: List[str]) -> Tuple[bool, str]:
        prompt = (
            f"당신은 Task Leader입니다. Goal: {goal}\n수행 결과들: {results}\n"
            f'Goal 완수 여부를 판정하세요. JSON만 출력: {{"achieved":true,"report":"성과 요약"}}'
        )
        data = _extract_json(await self.leader.handle(prompt))
        return bool(data.get("achieved", True)), str(data.get("report") or "보고")
