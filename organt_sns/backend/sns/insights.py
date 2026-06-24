"""협업 인사이트 생성 (F1302 심화) — 실제 협업 이벤트 스트림을 컨텍스트로 LLM 요약.

데이터 기반(미니 RAG): 프로젝트의 최근 협업 이벤트를 프롬프트 컨텍스트로 주입 → 사실 기반 요약.
LLM 미설정/오류 시 규칙기반 폴백(이벤트 통계 → 자연어 템플릿)으로 *항상* 결과를 반환한다.
"""
from collections import Counter

from django.conf import settings

from .ai import chat, AINotConfigured

KIND_KO = {
    "delegation": "위임", "consultation": "자문", "work": "작업",
    "verification": "교차검증", "goal_set": "목표합의", "deploy": "배포",
    "learn": "학습", "recruit": "채용", "distill": "증류", "halt": "수렴경보",
}


def _ko(kind):
    return KIND_KO.get(kind, kind)


def _gather(project):
    """프로젝트 협업 통계·최근 이벤트 컨텍스트를 모은다."""
    evs = list(project.events.select_related("actor", "target")[:60])
    kinds = Counter(e.kind for e in evs)
    roles = Counter(e.actor.role for e in evs if e.actor and e.actor.role)
    tasks = list(project.tasks.all())
    deploy = sum(t.deploy_count for t in tasks)
    xcheck = sum(t.cross_checks for t in tasks)
    lines = []
    for e in evs[:28]:
        who = e.actor.role if e.actor and e.actor.role else "에이전트"
        to = f"→{e.target.role}" if e.target and e.target.role else ""
        lines.append(f"- [{_ko(e.kind)}] {who}{to}: {(e.summary or '')[:70]}")
    return {
        "events": evs, "kinds": kinds, "roles": roles,
        "deploy": deploy, "xcheck": xcheck, "context": "\n".join(lines),
        "stat_str": ", ".join(f"{_ko(k)} {v}" for k, v in kinds.most_common()),
    }


def project_briefing(project):
    """프로젝트 협업 브리핑 dict 반환: {generated, model, text, stats}."""
    g = _gather(project)
    stats = {
        "event_count": len(g["events"]),
        "by_kind": {_ko(k): v for k, v in g["kinds"].most_common()},
        "roles": [{"role": r, "count": n} for r, n in g["roles"].most_common(6)],
        "deploy_count": g["deploy"], "cross_checks": g["xcheck"],
    }
    try:
        text = chat([
            {"role": "system", "content":
                "너는 Organt(AI 직원들이 단일 흐름 베턴으로 협업하는 회사)의 협업 분석가다. "
                "주어진 협업 이벤트 로그를 근거로, 이 프로젝트에서 AI 직원들이 어떻게 협업했는지 "
                "3~4문장 한국어로 요약하라. 위임·자문·교차검증·배포의 흐름과 어떤 직군이 무엇을 했는지를 "
                "담되, 로그에 없는 내용은 지어내지 말고 사실 기반으로 간결하게 써라."},
            {"role": "user", "content":
                f"프로젝트: {project.pid} {project.name}\n"
                f"통계: {g['stat_str']} / 교차검증 {g['xcheck']}회·배포 {g['deploy']}회\n\n"
                f"최근 협업 이벤트:\n{g['context']}"},
        ])
        return {"generated": True, "model": getattr(settings, "AI_MODEL", ""),
                "text": text, "stats": stats}
    except (AINotConfigured, Exception):  # 미설정·네트워크·응답 오류 모두 폴백
        return {"generated": False, "model": "rule-based-fallback",
                "text": _fallback_text(project, g), "stats": stats}


def _fallback_text(project, g):
    top_roles = ", ".join(f"{r}({n})" for r, n in g["roles"].most_common(4)) or "AI 직원들"
    return (f"{project.name or project.pid} 프로젝트에서 AI 직원들이 단일 흐름 베턴 협업을 진행했습니다. "
            f"최근 {len(g['events'])}건의 협업 이벤트가 기록되었으며, 주요 활동은 {g['stat_str'] or '작업 중심'} 입니다. "
            f"참여 직군은 {top_roles}이고, 교차검증 {g['xcheck']}회·배포 {g['deploy']}회를 거쳐 품질을 확보했습니다.")
