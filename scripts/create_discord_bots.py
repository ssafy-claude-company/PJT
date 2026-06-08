"""디스코드 봇 대량 생성 헬퍼 (로컬 실행 전용) — 개발자 포털을 브라우저 자동화로 돌려 봇 N개를 만든다.

⚠ 꼭 읽으세요:
- 이 스크립트는 **당신의 PC에서, 당신의 디스코드 계정으로** 돌리는 도구입니다. 자격증명(이메일/비밀번호/2FA)은
  스크립트가 절대 다루지 않습니다 — 열리는 브라우저 창에서 **당신이 직접 로그인**합니다(캡차도 직접).
- 디스코드 개발자 포털 자동화는 **공식 지원이 아니며**(ToS 회색지대), 포털 UI가 바뀌면 아래 셀렉터가 깨집니다.
  한 계정당 **앱 개수 제한**(보통 ~25개, 인증 시 더 — 100개는 한 계정으로 안 될 수 있음)이 있습니다. 본인 책임하에.
- **이 클라우드 샌드박스에선 못 돌립니다**(브라우저·로그인 필요) — 반드시 로컬에서 실행하세요.
- 봇당 토큰을 '한 번' 보여줄 때만 캡처합니다(놓치면 그 봇은 토큰 Reset 다시). 2FA가 켜져 있으면 Reset마다
  코드 입력이 필요해(봇당 1회) — 그땐 스크립트가 멈추고 당신이 브라우저에 입력 후 Enter.

준비:
  pip install playwright
  playwright install chromium

실행:
  python scripts/create_discord_bots.py 10                 # 봇 10개(슬롯 8번부터)
  python scripts/create_discord_bots.py 10 myteam 8        # 이름 접두사 myteam, .env 슬롯 8번부터
산출물(현재 폴더):
  created_bots.env   # 'ORGANT_BOT_8=...' 형식 — 운영 서버 .env에 붙여넣으면 핫리로드가 자동 인식
  invite_urls.txt    # 봇별 '원터치 초대' 링크 — 클릭 한 번씩 서버에 추가(당신의 '클릭 한 번 개입')

봇 추가 후 흐름(요약): created_bots.env → 서버 .env 에 붙여넣기 → invite_urls.txt 링크 클릭(봇당 1회) → 끝.
이름(한국 이름)·직군(역할)은 운영 리스너가 알아서 붙입니다(이 스크립트는 토큰만 뽑음).
"""
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


def _grab_token(page) -> str:
    """Bot 페이지에서 'Reset Token' → 확인 → (2FA면 멈춤) → Copy 버튼으로 클립보드 복사해 토큰을 읽는다.
    UI가 바뀌면 이 함수의 셀렉터만 고치면 된다. 자동 읽기 실패 시 직접 붙여넣기로 폴백."""
    from playwright.sync_api import TimeoutError as PWTimeout
    page.get_by_role("link", name="Bot").click(timeout=20000)        # 좌측 사이드바 'Bot'
    page.get_by_role("button", name="Reset Token").click(timeout=15000)
    try:
        page.get_by_role("button", name="Yes, do it!").click(timeout=5000)  # 확인 모달
    except PWTimeout:
        pass
    # 2FA 코드 입력이 뜨면 사람이 입력해야 한다(봇당 1회).
    try:
        if page.get_by_text("Enter your 2FA").is_visible(timeout=3000):
            input("      ↳ 브라우저에 2FA 코드 입력 후 Enter ▶ ")
            try:
                page.get_by_role("button", name="Yes, do it!").click(timeout=5000)
            except PWTimeout:
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
    # 폴백: 자동 읽기 실패 → 사람이 브라우저에서 토큰 복사해 붙여넣기.
    return input("      ↳ 토큰 자동 읽기 실패. 브라우저에서 토큰을 복사해 붙여넣고 Enter ▶ ").strip()


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
        browser = pw.chromium.launch(headless=False)            # 헤드풀: 직접 로그인 보이게
        ctx = browser.new_context(permissions=["clipboard-read", "clipboard-write"])
        page = ctx.new_page()
        page.goto(PORTAL)
        print("\n[1] 열린 브라우저에서 디스코드에 로그인하세요(캡차·2FA 포함).")
        input("    로그인 끝나고 'Applications' 목록이 보이면 여기서 Enter ▶ ")

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
                page.wait_for_load_state("networkidle", timeout=20000)
                app_id = _read_app_id(page)
                token = _grab_token(page)
                created.append((name, token, app_id))
                print(f"    ({i + 1}/{count}) {name}  ✓")
            except Exception as e:
                try:
                    page.screenshot(path=f"error_{i}.png")
                except Exception:
                    pass
                print(f"    ({i + 1}/{count}) {name}  ✗ 실패: {type(e).__name__}: {str(e)[:120]}\n"
                      f"        (error_{i}.png 스크린샷 확인 — 포털 UI가 바뀌었으면 _grab_token 셀렉터 조정)")
        browser.close()

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
