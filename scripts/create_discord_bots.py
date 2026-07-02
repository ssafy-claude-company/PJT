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

실행 — 두 가지 모드:
  (1) [반자동화·권장] 사람이 앱을 손으로 만들고, 스크립트는 '토큰만 수확':
        ① 평소 쓰는 일반 크롬에서 New Application→이름→캡차→Create 를 필요한 만큼 반복(생성만, 토큰 X).
        ② start_chrome_debug.bat 로 '진짜 크롬'을 디버그로 띄워 같은 계정 로그인.
        ③ python scripts/create_discord_bots.py harvest 11
           → 그 크롬의 내 앱들을 훑어 각 앱의 토큰만 Reset+Copy(토큰 단계엔 캡차 없음 → 자동).
           이미 수확한 앱은 harvested.txt에 기록돼 두 번 리셋 안 함(가동 중 봇 토큰 보호).
  (2) [완전 자동화] 생성까지 자동 시도(단, 앱 생성 캡차는 자동화 브라우저에서 막힐 수 있음):
        python scripts/create_discord_bots.py 10            # 봇 10개(슬롯 8번부터)
        python scripts/create_discord_bots.py 10 myteam 8   # 이름 접두사 myteam, .env 슬롯 8번부터
  → 디버그 크롬(start_chrome_debug.bat)에 먼저 붙고, 없으면 저장 프로필 크롬을 띄운다(로그인 1회 후 유지).

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
import re
import string
import sys
from pathlib import Path

PORTAL = "https://discord.com/developers/applications"
# 워커 봇 초대 권한(메시지·스레드·반응·기록) — organt_discord/discord_guide.py INVITE_PERMS와 동일.
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
    """Bot 페이지에서 토큰을 읽는다. ① 토큰이 이미 화면에 평문으로 보이면(포털 기본 표시) DOM 정규식으로 바로
    긁고(Reset 불필요), ② 없으면 '토큰 초기화/Reset Token'(한·영 정규식)으로 드러낸 뒤 다시 긁고, ③ '복사/Copy'
    →클립보드, ④ 최후엔 사람이 직접. **버튼 이름을 한국어/영어 모두 매칭**(한국어 포털 대응)하고, 어느 단계가
    실패해도 예외를 위로 안 던진다(루프가 멋대로 다음 봇으로 안 넘어가게)."""
    # 봇 페이지로 '직접 이동'(사이드바 'Bot' 링크 클릭에 의존하지 않음 — UI 변동/다국어에 강함).
    try:
        if app_id:
            page.goto(f"{PORTAL}/{app_id}/bot", wait_until="domcontentloaded")  # networkidle은 SPA에서 안 옴
        else:
            page.get_by_role("link", name="Bot").click(timeout=15000)
    except Exception:
        pass
    # 디스코드 토큰 = base64url 3토막(aaa.bbb.ccc). 화면 텍스트·input value에서 그 패턴을 집는다.
    token_re = r"[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{20,}"
    extract_js = ("() => { const re=/" + token_re + "/;"
                  " for (const el of document.querySelectorAll('input,textarea')) {"
                  "   const m=(el.value||'').match(re); if (m) return m[0]; }"
                  " const m=(document.body.innerText||'').match(re); return m ? m[0] : ''; }")

    def _scrape():
        try:
            return (page.evaluate(extract_js) or "").strip()
        except Exception:
            return ""

    # ① 토큰이 이미 화면에 평문으로 보이면 바로 긁는다(포털 기본 표시) — Reset 불필요. 렌더 지연 대비 재시도.
    for _ in range(6):
        tok = _scrape()
        if tok.count(".") >= 2:
            return tok
        page.wait_for_timeout(500)

    # ② 안 보이면 '토큰 초기화/Reset Token'으로 드러낸다 — **한·영 버튼 정규식 매칭**(한국어 포털 대응) +
    #    확인 모달 + 2FA(있으면 사람이 입력). 그 뒤 다시 긁는다.
    try:
        page.get_by_role("button", name=re.compile(r"Reset Token|토큰 초기화")).first.click(timeout=10000)
        try:
            page.get_by_role("button", name=re.compile(r"Yes, do it!|네, 확인|확인|초기화")).first.click(timeout=5000)
        except Exception:
            pass
    except Exception:
        pass
    try:
        if page.get_by_text(re.compile(r"Enter your 2FA|2단계 인증|인증 코드|2FA")).first.is_visible(timeout=3000):
            input("      ↳ 브라우저에 2FA 코드 입력 후 Enter ▶ ")
            try:
                page.get_by_role("button", name=re.compile(r"Yes, do it!|확인|초기화")).first.click(timeout=5000)
            except Exception:
                pass
    except Exception:
        pass
    for _ in range(8):
        tok = _scrape()
        if tok.count(".") >= 2:
            return tok
        page.wait_for_timeout(500)

    # ③ '복사/Copy' → 클립보드(권한 되면).
    try:
        page.get_by_role("button", name=re.compile(r"Copy|복사")).first.click(timeout=5000)
        tok = (page.evaluate("() => navigator.clipboard.readText()") or "").strip()
        if tok.count(".") >= 2:
            return tok
    except Exception:
        pass
    # 진단 덤프(자동 추출 실패 시): '무엇이 있었는지'를 **토큰 값 없이** 저장 — 셀렉터/원인 진단용(공유 가능).
    #   token_debug.txt = url·토큰표시여부·버튼라벨·input종류 (토큰 문자열은 안 적음). token_debug.png = 스크린샷.
    try:
        info = page.evaluate(
            "() => { const re=/" + token_re + "/;"
            " const labels=[...document.querySelectorAll('button,[role=button],a')]"
            "   .map(b=>(b.innerText||b.getAttribute('aria-label')||'').trim()).filter(Boolean).slice(0,50);"
            " const inputs=[...document.querySelectorAll('input,textarea')].map(e=>e.type||'text');"
            " const vis = re.test(document.body.innerText||'') ||"
            "   [...document.querySelectorAll('input,textarea')].some(e=>re.test(e.value||''));"
            " return {url: location.href, tokenVisible: vis, buttons: labels, inputs}; }")
        Path("token_debug.txt").write_text(
            f"url: {info.get('url')}\ntokenVisible(토큰이 화면에 떠 있나): {info.get('tokenVisible')}\n"
            f"inputs: {info.get('inputs')}\nbuttons: {info.get('buttons')}\n", encoding="utf-8")
        try:
            page.screenshot(path="token_debug.png")
        except Exception:
            pass
        print("      ↳ 진단 저장: token_debug.txt / token_debug.png (토큰 값은 안 적힘 — 이 두 개를 공유해 주세요)")
    except Exception:
        pass
    # ③ [최후] 사람이 직접. **여기서 멈추므로 '멋대로 다음 봇으로' 넘어가지 않는다.**
    return input("      ↳ 토큰 자동 읽기 실패 — 브라우저 Bot 페이지에서 토큰을 복사해 붙여넣고 Enter "
                 "(이 봇은 건너뛰려면 그냥 Enter) ▶ ").strip()


def _open_context(pw):
    """디버그 크롬(CDP=로그인된 '진짜 크롬')에 붙거나, 안 되면 저장 프로필로 크롬을 띄운다.
    반환 (ctx, browser, using_cdp). 캡차가 걸리는 '앱 생성'은 진짜 크롬(CDP)이라야 통과된다."""
    cdp = os.environ.get("CHROME_CDP", "http://localhost:9222").strip()
    profile = os.environ.get("CHROME_PROFILE", str(Path.home() / ".organt_chrome_profile"))
    browser = ctx = None
    using_cdp = False
    if cdp:
        try:
            browser = pw.chromium.connect_over_cdp(cdp)
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            using_cdp = True
            print(f"[연결] 디버그 크롬에 붙음({cdp}) — 당신의 '진짜 크롬'/로그인 그대로 사용")
        except Exception:
            print("\n[중요] 디버그 크롬(포트 9222)에 못 붙었습니다.")
            print("  ⚠ 캡차는 '자동화로 띄운 브라우저'에선 풀어도 거부됩니다. 당신의 '진짜 크롬'에 붙이세요:")
            print("    1) start_chrome_debug.bat 더블클릭 → 뜬 크롬에서 디스코드 로그인")
            print("    2) 여기로 돌아와 Enter")
            if input("  Enter=디버그 크롬에 재연결 / s+Enter=자동화 브라우저로 진행 ▶ ").strip().lower() != "s":
                try:
                    browser = pw.chromium.connect_over_cdp(cdp)
                    ctx = browser.contexts[0] if browser.contexts else browser.new_context()
                    using_cdp = True
                    print(f"[연결] 디버그 크롬에 붙음({cdp})")
                except Exception:
                    print("  여전히 못 붙음 → 자동화 브라우저로 진행.")
    if ctx is None:
        launch_kw = dict(headless=False,
                         args=["--start-maximized", "--disable-blink-features=AutomationControlled"],
                         ignore_default_args=["--enable-automation"])
        try:
            ctx = pw.chromium.launch_persistent_context(profile, channel="chrome", **launch_kw)
        except Exception:
            print("크롬 실행 실패 — 번들 크로미움 시도(없으면: python -m playwright install chromium)")
            ctx = pw.chromium.launch_persistent_context(profile, **launch_kw)
        print(f"[실행] 자동화 크롬 — 저장 프로필: {profile}\n"
              f"       (캡차가 안 넘어가면: start_chrome_debug.bat로 '진짜 크롬'을 띄워 로그인 후 재실행)")
    try:
        ctx.grant_permissions(["clipboard-read", "clipboard-write"], origin="https://discord.com")
    except Exception:
        pass
    return ctx, browser, using_cdp


def _close_ctx(ctx, browser, using_cdp):
    try:
        if using_cdp and browser is not None:
            browser.close()      # CDP: 연결만 끊김(당신 크롬 창은 유지)
        elif ctx is not None:
            ctx.close()          # 저장 프로필: 다음 실행 때 재사용(로그인 유지)
    except Exception:
        pass


def _write_outputs(created, start_idx):
    """수집 결과를 created_bots.env(.env 붙여넣기용) + invite_urls.txt(초대 링크)로 쓴다."""
    env_path, inv_path = Path("created_bots.env"), Path("invite_urls.txt")
    with env_path.open("w", encoding="utf-8") as f:
        f.write("# 운영 서버 .env에 붙여넣으세요(핫리로드가 자동 인식). 토큰은 비밀 — 커밋 금지.\n")
        for idx, (name, token, _aid) in enumerate([c for c in created if c[1]]):
            f.write(f"ORGANT_BOT_{start_idx + idx}={token}\n")
    with inv_path.open("w", encoding="utf-8") as f:
        f.write("# 각 링크를 클릭(봇당 1회)해 서버에 추가하세요.\n")
        for name, _t, app_id in created:
            if app_id:
                f.write(f"{name}: https://discord.com/oauth2/authorize?"
                        f"client_id={app_id}&scope=bot&permissions={INVITE_PERMS}\n")
    n_tok = sum(1 for _n, t, _a in created if t)
    print(f"\n완료: 토큰 {n_tok}개 / 앱 {len(created)}개")
    print(f"  - {env_path}  → 운영 서버 .env에 붙여넣기(슬롯 번호 겹치지 않게 확인)")
    print(f"  - {inv_path}  → 각 링크 클릭(봇당 1회)해 서버 추가")
    print("  토큰은 비밀입니다. created_bots.env는 커밋·공유 금지.")


def _list_app_ids(page) -> list:
    """포털 Applications 목록에서 내 앱들의 application id를 뽑는다(생성과 무관 — 캡차 없음).
    디스코드 포털은 SPA라 'networkidle'이 거의 안 와서(상시 연결) 그걸로 기다리면 타임아웃 →
    앱 링크가 뜰 때까지만 기다린다(안 떠도 진행)."""
    page.goto(PORTAL, wait_until="domcontentloaded")
    try:
        page.wait_for_selector("a[href*='/applications/']", timeout=15000)
    except Exception:
        pass
    try:
        hrefs = page.eval_on_selector_all(
            "a[href*='/applications/']", "els => els.map(e => e.getAttribute('href'))")
    except Exception:
        hrefs = []
    ids = []
    for h in hrefs or []:
        m = re.search(r"/applications/(\d+)", h or "")
        if m and m.group(1) not in ids:
            ids.append(m.group(1))
    return ids


def _harvest(ctx, start_idx, baseline=False):
    """[반자동화] 사람이 미리 만든 앱들의 '토큰만' 수확한다 — Reset/Copy엔 캡차가 없어 자동화 가능.
    ⚠ 토큰 Reset은 '이미 가동 중인 봇' 토큰도 무효화한다. 그래서 harvested.txt='건드리지 않을 app id'
    목록을 두고, baseline으로 기존 앱을 먼저 등록한 뒤 '새로 만든 앱만' 수확한다."""
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(PORTAL)
    print("\n[수확 모드] 이 크롬이 디스코드에 로그인돼 있어야 합니다.")
    input("    'Applications' 목록이 보이면 Enter ▶ ")
    done_path = Path("harvested.txt")
    already = set(done_path.read_text(encoding="utf-8").split()) if done_path.exists() else set()
    ids = _list_app_ids(page)
    if baseline:   # 기존 앱 전부를 '보호 목록'에 등록 — 이후 수확이 이들을 건드리지 않음(가동 중 봇 토큰 보호)
        with done_path.open("w", encoding="utf-8") as f:
            for i in ids:
                f.write(i + "\n")
        print(f"    기존 앱 {len(ids)}개를 보호 목록(harvested.txt)에 등록했습니다 — 수확이 이들은 건드리지 않음.")
        print("    이제 새 앱을 손으로 만든 뒤:  python create_discord_bots.py harvest 11")
        return []
    new_ids = [i for i in ids if i not in already]
    print(f"    앱 {len(ids)}개 발견 · 보호(기등록) {len(already)}개 · 수확 대상 {len(new_ids)}개")
    if not already:
        print("    ⚠ 보호 목록이 비어 있습니다 — 가동 중인 봇이 섞여 있으면 그 토큰이 깨집니다!")
        print("       권장: 먼저 'python create_discord_bots.py harvest baseline'로 기존 앱 보호 후 새 앱만 생성·수확.")
    if not new_ids:
        print("    수확할 새 앱이 없습니다(이미 다 했거나 목록이 안 보임 — 로그인 확인).")
        return []
    if input(f"    {len(new_ids)}개 앱의 토큰을 Reset+수확합니다. 새로 만든 앱만 맞나요? 계속하려면 y ▶ ").strip().lower() != "y":
        print("    취소했습니다.")
        return []
    harvested = []
    for n, app_id in enumerate(new_ids):
        try:
            token = _grab_token(page, app_id)     # /applications/<id>/bot 직행 → Reset+Copy(캡차 없음)
            harvested.append((f"organt-{app_id[:6]}", token, app_id))
            if token:
                with done_path.open("a", encoding="utf-8") as f:
                    f.write(app_id + "\n")          # 성공분만 기록 → 실패분은 다음 실행에 재시도
            print(f"    ({n + 1}/{len(new_ids)}) app {app_id}  {'✓' if token else '⚠ 미수집'}")
        except Exception as e:
            print(f"    ({n + 1}/{len(new_ids)}) app {app_id}  ✗ {type(e).__name__}: {str(e)[:120]}")
            if input("      Enter=다음 / s+Enter=중단 ▶ ").strip().lower() == "s":
                break
    return harvested


def main() -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright가 없습니다. 먼저:  pip install playwright")
        sys.exit(1)

    # 반자동화 '수확' 모드: 사람이 앱을 손으로 만들어 두면(캡차) 스크립트가 토큰만 긁는다.
    #   ① 먼저 기존 앱 보호:  python create_discord_bots.py harvest baseline
    #   ② 새 앱 손으로 생성 후: python create_discord_bots.py harvest 11   (11 = .env 시작 슬롯)
    if len(sys.argv) > 1 and sys.argv[1].lower() == "harvest":
        sub = sys.argv[2] if len(sys.argv) > 2 else ""
        baseline = sub.lower() == "baseline"
        start_idx = int(sub) if sub.isdigit() else 11
        with sync_playwright() as pw:
            ctx, browser, using_cdp = _open_context(pw)
            created = _harvest(ctx, start_idx, baseline=baseline)
            _close_ctx(ctx, browser, using_cdp)
        if not baseline:
            _write_outputs(created, start_idx)
        return

    count = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    prefix = sys.argv[2] if len(sys.argv) > 2 else "organt-worker"
    start_idx = int(sys.argv[3]) if len(sys.argv) > 3 else 8     # .env 슬롯 시작번호(예비 8~)
    created = []   # (name, token, app_id)

    with sync_playwright() as pw:
        ctx, browser, using_cdp = _open_context(pw)
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
                app_id = _read_app_id(page)   # _wait_app_page가 이미 앱 페이지(URL) 확인 — networkidle 불필요
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
        _close_ctx(ctx, browser, using_cdp)

    _write_outputs(created, start_idx)


if __name__ == "__main__":
    main()
