// E2E — 구조화 타임라인 회귀 가드(Playwright). run-e2e.sh가 서버·시드를 띄운 뒤 실행한다.
// 검사: 회의/표결 블록, 라운드 구분, 페이즈 구분선, 멎은요청 재시도, 채널 섹셔닝, 엔진 표시.
// CommonJS — 전역 playwright를 NODE_PATH로 해석(ESM은 NODE_PATH 미지원).
const { chromium } = require('playwright');

const BASE = process.env.BASE || 'http://127.0.0.1:8099';
const EXE = process.env.PW_CHROMIUM || undefined;   // 사전설치 크로미움 경로(선택)
let pass = 0;
const fails = [];
function ok(cond, msg) { if (cond) { pass++; console.log('  ✓ ' + msg); } else { fails.push(msg); console.log('  ✗ ' + msg); } }

(async () => {
  const browser = await chromium.launch(EXE ? { executablePath: EXE } : {});
  const page = await browser.newPage({ viewport: { width: 1180, height: 1500 } });
  try {
    // 인증 가드 통과용 토큰 주입(시드의 멤버 e2e).
    await page.goto(BASE + '/login', { waitUntil: 'networkidle' });
    await page.evaluate(() => localStorage.setItem('organt_token', 'tok_e2e'));

    // ── 채널 타임라인 ──────────────────────────────
    await page.goto(BASE + '/channels/E-1', { waitUntil: 'networkidle' });
    await page.waitForSelector('.collab-block', { timeout: 10000 });

    const meet = await page.$$eval('.collab-block.meeting .cb-row', e => e.length);
    ok(meet >= 6, `회의 블록 발언 ${meet}개(>=6)`);
    const speakers = await page.$$eval('.collab-block.meeting .cb-stack .cb-av', e => e.length);
    ok(speakers >= 3, `회의 참여자 스택 ${speakers}명(>=3)`);
    const rounds = await page.$$eval('.collab-block.meeting .cb-round', e => e.map(x => x.textContent.trim()));
    ok(rounds.length >= 2 && rounds.some(r => r.includes('1')) && rounds.some(r => r.includes('2')),
      `라운드 구분 ${JSON.stringify(rounds)}`);
    const votes = await page.$$eval('.collab-block.vote .cb-row', e => e.length);
    ok(votes >= 3, `표결 블록 발언 ${votes}개(>=3)`);

    const phases = await page.$$eval('.phase-sep', e => e.map(x => x.querySelector('.ph-pill') && x.querySelector('.ph-pill').textContent.trim()));
    ok(phases.includes('목표') && phases.includes('완료') && phases.includes('배포'),
      `페이즈 구분선 ${JSON.stringify(phases)}`);

    // 멎은 요청 — 바 노출 + '다시 맡기기' 클릭 → 재큐되어 바 사라짐
    ok(await page.$('.stuck-bar') !== null, '멎은 요청 바 노출');
    await page.click('.stuck-bar button');
    await page.waitForTimeout(1200);
    ok(await page.$('.stuck-bar') === null, '다시 맡기기 후 멎은 요청 바 사라짐(재큐)');

    // ── 홈: 엔진 표시 + 채널 섹셔닝 ─────────────────
    await page.goto(BASE + '/', { waitUntil: 'networkidle' });
    await page.waitForSelector('.sb-engine', { timeout: 6000 });
    ok(await page.$('.sb-engine.on') !== null, '협업 엔진 가동 중 표시(heartbeat)');
    const secs = await page.$$eval('.home .sec-h', e => e.map(x => x.textContent.replace(/\s+/g, ' ').trim()));
    ok(secs.some(s => s.includes('내 채널')) && secs.some(s => s.includes('공개 채널')),
      `채널 내/공개 섹셔닝 ${JSON.stringify(secs)}`);
  } catch (e) {
    fails.push('예외: ' + (e && e.message));
    console.error(e);
  } finally {
    await browser.close();
  }

  console.log(`\nE2E: ${pass} passed, ${fails.length} failed`);
  if (fails.length) { console.error('FAILED:\n - ' + fails.join('\n - ')); process.exit(1); }
  console.log('ALL PASS');
})();
