r"""디스코드 봇 대량 생성 헬퍼 (로컬 실행 전용) — 개발자 포털을 브라우저 자동화로 돌려 봇 N개를 만든다.

⚠ 꼭 읽으세요:
- 이 스크립트는 **당신의 PC에서, 당신의 디스코드 계정으로** 돌리는 도구입니다. 자격증명(이메일/비밀번호/2FA)은
  스크립트가 절대 다루지 않습니다 — 열리는 브라우저 창에서 **당신이 직접 로그인**합니다(캡차도 직접).
- 디스코드 개발자 포털 자동화는 **공식 지원이 아니며**(ToS 회색지대), 포털 UI가 바뀌면 아래 셀렉터가 깨집니다.
  한 계정당 **앱 개수 제한**(보통 ~25개, 인증 시 더 — 100개는 한 계정으로 안 될 수 있음)이 있습니다. 본인 책임하에.
- **이 클라우드 샌드박스에선 못 돌립니다**(브라우저·로그인 필요) — 반드시 로컬에서 실행하세요.
- 봇당 토큰을 '한 번' 보여줄 때만 캡처합니다(놓치면 그 봇은 토큰 Reset 다시). 2FA가 켜져 있으면 Reset마다
  코드 입력이 필요해(봇당 1회) — 그땐 스크립트가 멈추고 당신이 브라우저에 입력 후 Enter.
- **앱 생성(Create) 직후 '봇/사람 확인(캡차)'이 뜨면 바로 넘기지 않고 멈춰서 기다립니다** — 브라우저에서
  그 확인을 처리한 뒤 Enter를 누르면 계속됩니다(캡차가 없으면 멈추지 않고 빠르게 통과). 대량 생성 시
  디스코드가 중간부터 캡차를 띄우는 경우가 많은데, 그때 봇을 실패시키지 않고 당신이 통과시킬 수 있습니다.

준비(크로미움 다운로드 불필요 — PC에 설치된 크롬을 그대로 사용):
  pip install playwright
  # (playwright install chromium 안 해도 됨. 크롬이 없으면 그때만 설치.)

실행:
  python scripts/create_discord_bots.py 10                 # 봇 10개(슬롯 8번부터)
  python scripts/create_discord_bots.py 10 myteam 8        # 이름 접두사 myteam, .env 슬롯 8번부터
  → 설치된 '진짜 크롬'이 '저장 프로필'(.chrome_bot_profile)로 뜬다. **처음 한 번만 디스코드 로그인**하면
    이후 실행부턴 로그인이 유지된다(매번 로그인 X). 크로미움 다운로드도 필요 없다.

[옵션] 이미 디버그 모드로 켜둔 크롬에 '그대로 붙기'(로그인 세션 재사용):
  1) 크롬을 이렇게 띄운다(일반 창엔 보안상 못 붙음 — 반드시 이 플래그로):
       chrome.exe --remote-debugging-port=9222 --user-data-dir="%TEMP%\chrome-bot"
  2) 그 창에서 디스코드 로그인 → 환경변수 주고 실행:
       set CHROME_CDP=http://localhost:9222 && python scripts/create_discord_bots.py 10
산출물(현재 폴더):
  created_bots.env   # 'ORGANT_BOT_8=...' 형식 — 운영 서버 .env에 붙여넣으면 핫리로드가 자동 인식
  invite_urls.txt    # 봇별 '원터치 초대' 링크 — 클릭 한 번씩 서버에 추가(당신의 '클릭 한 번 개입')

봇 추가 후 흐름(요약): created_bots.env → 서버 .env 에 붙여넣기 → invite_urls.txt 링크 클릭(봇당 1회) → 끝.
이름(한국 이름)·직군(역할)은 운영 리스너가 알아서 붙입니다(이 스크립트는 토큰만 뽑음).
"""
import os
import random
import string
import sys
from pathlib import Path

PORTAL = "https://discord.com/developers/applications"
# 워커 봇 초대 권한(메시지·스레드·반응·기록) — src/discord_guide.py INVITE_PERMS와 동일.
INVITE_PERMS = 1024 + 2048 + 16384 + 65536 + 64 + 274877906944


def _rand(n: int = 4) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


def _read_app_id(page) -> str:
    """앱 생성 후 URL(.../applications/<APP_ID>/...)에서 application id(=봇 user id=초대용 client_id) 추출."""
    try:
        return page.url.split("/applications/")[1].split("/")[0].strip()
    except Exception:
        return ""


def _wait_app_page(page, timeout_ms: int = 12000) -> bool:
    """앱이 실제로 만들어져 'General Information' 페이지(URL에 숫자 application id)로 넘어갔는지 기다린다.
    True=넘어감(확인 통과 또는 없음), False=시간초과 — 보통 '봇/사람 확인(캡차)'이 막고 있는 상태다.
    이 신호로 '캡차 없으면 빠르게 통과, 있으면 멈춰서 사람이 처리'를 구분한다. 절대 예외를 던지지 않는다
    (시간초과든 다른 오류든 False) — 그래야 캡차가 항상 '대기 프롬프트'로 잡히고 루프가 멋대로 안 넘어간다."""
    try:
        page.wait_for_url(
            lambda url: "/applications/" in url
            and url.split("/applications/")[1].split("/")[0].strip().isdigit(),
            timeout=timeout_ms)
        return True
    except Exception:
        return False


def _grab_token(page, app_id: str = "") -> str:
    """Bot 페이지에서 'Reset Token' → 확인 → (2FA면 멈춤) → Copy로 토큰을 읽는다.
    **어느 단계가 실패해도 예외를 위로 던지지 않는다** — 그래야 루프가 멋대로 '다음 봇'으로 넘어가지(=재시작
    처럼 보이는) 않고, 마지막에 '직접 붙여넣기'에서 멈춰 사람이 처리할 수 있다. UI가 바뀌면 셀렉터만 고치면 됨."""
    # 봇 페이지로 '직접 이동'(사이드바 'Bot' 링크 클릭에 의존하지 않음 — UI 변동/다국어에 강함).
    try:
        if app_id:
            page.goto(f"{PORTAL}/{app_id}/bot")
            page.wait_for_load_state("networkidle", timeout=15000)
        else:
            page.get_by_role("link", name="Bot").click(timeout=15000)
    except Exception:
        pass
    # Reset Token (+ 확인 모달). 실패해도 안 던지고 폴백으로 간다.
    try:
        page.get_by_role("button", name="Reset Token").click(timeout=15000)
        try:
            page.get_by_role("button", name="Yes, do it!").click(timeout=5000)
        except Exception:
            pass
    except Exception:
        pass
    # 2FA 코드 입력이 뜨면 사람이 입력해야 한다(봇당 1회).
    try:
        if page.get_by_text("Enter your 2FA").is_visible(timeout=3000):
            input("      ↳ 브라우저에 2FA 코드 입력 후 Enter ▶ ")
            try:
                page.get_by_role("button", name="Yes, do it!").click(timeout=5000)
            except Exception:
                pass
    except Exception:
        pass
    # Copy 버튼 → 클립보드에서 토큰 읽기(컨텍스트에 clipboard 권한 부여돼 있어야 함).
    try:
        page.get_by_role("button", name="Copy").first.click(timeout=8000)
        tok = page.evaluate("() => navigator.clipboard.readText()")
        if tok and tok.count(".") >= 2:      # 디스코드 토큰은 'A.B.C' 꼴
            return tok.strip()
    except Exception:
        pass
    # 폴백: 자동 읽기 실패 → 사람이 직접. **여기서 멈추므로 '멋대로 다음 봇으로' 넘어가지 않는다.**
    return input("      ↳ 토큰 자동 읽기 실패 — 브라우저 Bot 페이지에서 토큰을 복사해 붙여넣고 Enter "
                 "(이 봇은 건너뛰려면 그냥 Enter) ▶ ").strip()


def main() -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright가 없습니다. 먼저:  pip install playwright && playwright install chromium")
        sys.exit(1)

    count = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    prefix = sys.argv[2] if len(sys.argv) > 2 else "organt-worker"
    start_idx = int(sys.argv[3]) if len(sys.argv) > 3 else 8     # .env 슬롯 시작번호(예비 8~)
    created = []   # (name, token, app_id)

    with sync_playwright() as pw:
        # (B) CHROME_CDP가 있으면 '디버그 모드로 켜둔 크롬'에 그대로 붙는다(로그인 세션 재사용).
        #     크롬을 다음처럼 띄워야 함(디버그 포트 없이 켜둔 일반 창엔 보안상 못 붙음):
        #       chrome.exe --remote-debugging-port=9222 --user-data-dir="%TEMP%\chrome-bot"
        #     그 창에서 디스코드 로그인 후, set CHROME_CDP=http://localhost:9222 로 실행.
        # (A) 없으면 '설치된 크롬'을 '저장 프로필'로 실행 → 처음 한 번만 로그인하면 이후 계속 유지.
        # 기본으로 localhost:9222(디버그 크롬)에 먼저 붙어 본다 → 당신이 띄워둔 그 창을 그대로 사용.
        # 없으면 아래에서 '저장 프로필'로 크롬을 새로 띄운다(로그인 1회 후 유지). 프로필 경로는 CWD와 무관하게 고정.
        cdp = os.environ.get("CHROME_CDP", "http://localhost:9222").strip()
        profile = os.environ.get("CHROME_PROFILE", str(Path.home() / ".organt_chrome_profile"))
        browser = ctx = None
        using_cdp = False
        if cdp:
            try:
                browser = pw.chromium.connect_over_cdp(cdp)
                ctx = browser.contexts[0] if browser.contexts else browser.new_context()
                using_cdp = True
                print(f"[연결] 디버그 크롬에 붙음({cdp}) — 로그인 세션 그대로 사용")
            except Exception:
                print(f"[알림] {cdp}에 붙을 디버그 크롬이 없음 → 저장 프로필로 크롬을 새로 띄웁니다.\n"
                       f"       (당신이 '띄워둔 창'을 쓰고 싶으면: 먼저 start_chrome_debug.bat 실행해 그 창에서 로그인 후 이 스크립트 재실행)")
        if ctx is None:
            try:                                                # 설치된 '크롬'을 저장 프로필로 실행
                ctx = pw.chromium.launch_persistent_context(
                    profile, headless=False, channel="chrome", args=["--start-maximized"])
            except Exception:                                   # 크롬 없으면 번들 크로미움
                print("크롬 실행 실패 — 번들 크로미움 시도(없으면: python -m playwright install chromium)")
                ctx = pw.chromium.launch_persistent_context(profile, headless=False)
            print(f"[실행] 크롬 실행 — 저장 프로필: {profile} (처음 한 번만 로그인하면 다음부턴 유지)")
        try:
            ctx.grant_permissions(["clipboard-read", "clipboard-write"], origin="https://discord.com")
        except Exception:
            pass
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(PORTAL)
        print("\n[1] 이 크롬 창에서 디스코드에 로그인돼 있어야 합니다(처음이면 지금 로그인·캡차·2FA).")
        input("    'Applications' 목록이 보이면 여기서 Enter ▶ ")

        for i in range(count):
            name = f"{prefix}-{start_idx + i}-{_rand()}"
            try:
                page.goto(PORTAL)
                page.get_by_role("button", name="New Application").click(timeout=20000)
                page.get_by_role("textbox").first.fill(name, timeout=10000)   # 이름 입력(모달)
                try:                                                          # ToS 체크박스(있으면)
                    page.get_by_role("checkbox").first.check(timeout=3000)
                except Exception:
                    pass
                page.get_by_role("button", name="Create").click(timeout=10000)
                # 'Create' 직후 디스코드가 '봇/사람 확인(캡차)'을 띄울 수 있다 — 바로 넘기지 않고 앱 페이지
                # (URL에 application id)로 넘어갈 때까지 기다린다. 빨리 넘어가면(캡차 없음) 그대로 진행하고,
                # 일정 시간 안 넘어가면(=확인이 막고 있음) **멈춰서** 당신이 브라우저에서 그 확인을 처리한 뒤
                # Enter → 처리되면 앱 페이지로 넘어갈 때까지 더 기다렸다가 계속. (빠른 생성 유지 + 캡차는 대기)
                if not _wait_app_page(page, timeout_ms=12000):
                    input(f"      ↳ '{name}' 생성 확인(봇/사람 캡차 등)이 떴으면 브라우저에서 처리한 뒤 Enter ▶ ")
                    _wait_app_page(page, timeout_ms=120000)
                page.wait_for_load_state("networkidle", timeout=20000)
                app_id = _read_app_id(page)
                token = _grab_token(page, app_id)
                created.append((name, token, app_id))
                print(f"    ({i + 1}/{count}) {name}  {'✓' if token else '⚠ 토큰 미수집(건너뜀)'}")
            except Exception as e:
                try:
                    page.screenshot(path=f"error_{i}.png")
                except Exception:
                    pass
                print(f"    ({i + 1}/{count}) {name}  ✗ 실패: {type(e).__name__}: {str(e)[:160]}\n"
                      f"        (error_{i}.png 스크린샷 확인 — 포털 UI가 바뀌었으면 셀렉터 조정 필요)")
                # **멋대로 다음 봇으로 넘어가지 않도록 여기서 멈춘다** — 사람이 화면(캡차/UI변동/로그인)을 보고 결정.
                if input("      ↳ Enter=다음 봇 계속 / s+Enter=중단하고 지금까지 저장 ▶ ").strip().lower() == "s":
                    break
        try:
            if using_cdp and browser is not None:
                browser.close()      # CDP: 연결만 끊김(당신 크롬 창은 유지)
            elif ctx is not None:
                ctx.close()          # 저장 프로필: 다음 실행 때 재사용(로그인 유지)
        except Exception:
            pass

    # 산출물: .env 토큰 줄 + 초대 링크
    env_path, inv_path = Path("created_bots.env"), Path("invite_urls.txt")
    with env_path.open("w", encoding="utf-8") as f:
        f.write("# 운영 서버 .env에 붙여넣으세요(핫리로드가 자동 인식). 토큰은 비밀 — 커밋 금지.\n")
        for idx, (name, token, _aid) in enumerate(created):
            if token:
                f.write(f"ORGANT_BOT_{start_idx + idx}={token}\n")
    with inv_path.open("w", encoding="utf-8") as f:
        f.write("# 각 링크를 클릭(봇당 1회)해 서버에 추가하세요.\n")
        for name, _t, app_id in created:
            if app_id:
                f.write(f"{name}: https://discord.com/oauth2/authorize?"
                        f"client_id={app_id}&scope=bot&permissions={INVITE_PERMS}\n")
    print(f"\n완료: {len(created)}개 생성")
    print(f"  - {env_path}  → 운영 서버 .env에 붙여넣기(핫리로드가 자동 연결)")
    print(f"  - {inv_path}  → 각 링크 클릭(봇당 1회)해 서버 추가")
    print("  토큰은 비밀입니다. created_bots.env는 커밋·공유 금지.")


if __name__ == "__main__":
    main()
