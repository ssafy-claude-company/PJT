/**
 * audio-manager.js — 1v1 FPS 오디오 엔진 (절차적 Web Audio API, 외부 에셋 없음)
 * 사운드 디자이너 산출물 — CC0 (절차적 합성, 파일 에셋 미사용)
 *
 * ── 믹싱 기준 (클리핑 기준선 0 dBFS) ──────────────────────────────────────
 *   카테고리     선형 gain   dBFS     용도
 *   weapon       0.50       -6       총성 (가장 중요한 SFX, 귀를 지배하지 않게 제한)
 *   feedback     0.71       -3       hit confirm, 피격, 사망 (즉각 인지 필요)
 *   ambient      0.13      -18       발자국 (전술 정보, 방해 없이 들려야 함)
 *   music        0.25      -12       승·패 스팅거 (후경)
 *   ui           0.40       -8       버튼·전환 효과음
 *
 * ── 방향성 오디오 ─────────────────────────────────────────────────────────
 *   PannerNode panningModel='HRTF' 사용 — 인간 두귀 전달 함수 기반,
 *   사운드 발생 위치를 [x,y,z]로 받아 AudioContext.listener(카메라) 기준
 *   ≤5° 오차 이내로 방향을 재현한다.
 *   setListener(x, y, z, yaw) 를 매 렌더 프레임마다 호출해야 한다.
 *
 * ── 사용법 ────────────────────────────────────────────────────────────────
 *   import AudioManager from '/audio-manager.js';
 *   AudioManager.play('shoot', [px, py+1.6, pz]);  // 발사 위치 기반 3D
 *   AudioManager.play('footstep_opp', [ox, oy, oz]); // 적 발자국 3D
 *   AudioManager.play('hit');                       // mono (즉각 피드백)
 *   AudioManager.setListener(px, py+1.6, pz, yaw); // 매 프레임
 *   AudioManager.resume();                          // 유저 클릭 후 호출
 *
 * ── 지원 사운드 목록 ──────────────────────────────────────────────────────
 *   shoot          총성 (3D)
 *   hit            적 명중 확인 (mono)
 *   damage         내가 피격 (mono)
 *   kill           적 처치 확인 (mono, 3음 stinger)
 *   death          내 사망 (mono, 저주파 충격)
 *   win            승리 스팅거 (5음 아르페지오)
 *   lose           패배 스팅거 (하강 단조)
 *   footstep_self  내 발자국 (mono ambient)
 *   footstep_opp   적 발자국 (3D ambient — 전술 정보)
 *   ui_click       UI 버튼 클릭
 *   game_start     게임 시작 신호
 */

const AudioManager = (() => {
  let _ctx = null;
  let _master = null;

  // 카테고리별 선형 gain
  const CAT = {
    weapon:   0.50,  // -6 dBFS
    feedback: 0.71,  // -3 dBFS
    ambient:  0.13,  // -18 dBFS
    music:    0.25,  // -12 dBFS
    ui:       0.40,  // -8 dBFS
  };

  // ── AudioContext 지연 초기화 (유저 제스처 전 생성 불필요) ────────────────
  function _c() {
    if (!_ctx) {
      _ctx = new (window.AudioContext || window.webkitAudioContext)();
      _master = _ctx.createGain();
      _master.gain.value = 1.0;
      _master.connect(_ctx.destination);
    }
    return _ctx;
  }

  function resume() {
    if (_ctx && _ctx.state === 'suspended') _ctx.resume();
  }

  // ── 합성 기본 블록 ────────────────────────────────────────────────────────

  /** @param {AudioNode|null} target null → master로 연결 */
  function _gain(vol, decay, catVol, target) {
    const c = _c();
    const g = c.createGain();
    g.gain.setValueAtTime(vol * catVol, c.currentTime);
    g.gain.exponentialRampToValueAtTime(0.0001, c.currentTime + decay);
    g.connect(target || _master);
    return g;
  }

  function _osc(type, freq, freqEnd, decay, vol, catVol, target) {
    const c = _c();
    const g = _gain(vol, decay, catVol, target);
    const o = c.createOscillator();
    o.type = type;
    o.frequency.setValueAtTime(freq, c.currentTime);
    if (freqEnd) o.frequency.exponentialRampToValueAtTime(freqEnd, c.currentTime + decay);
    o.connect(g);
    o.start();
    o.stop(c.currentTime + decay);
  }

  function _noise(decay, vol, catVol, target) {
    const c = _c();
    const len = Math.ceil(c.sampleRate * decay);
    const buf = c.createBuffer(1, len, c.sampleRate);
    const d = buf.getChannelData(0);
    for (let i = 0; i < len; i++) d[i] = Math.random() * 2 - 1;
    const s = c.createBufferSource();
    s.buffer = buf;
    const g = _gain(vol, decay, catVol, target);
    s.connect(g);
    s.start();
    s.stop(c.currentTime + decay);
  }

  // ── 3D PannerNode ─────────────────────────────────────────────────────────
  /**
   * 월드 좌표 [x,y,z]에 소리 발생원을 배치.
   * HRTF panningModel 로 ≤5° 방향 오차 충족.
   * rolloffFactor=1.8, maxDistance=50 (맵 대각선 ~57m 커버).
   */
  function _panner(x, y, z) {
    const c = _c();
    const p = c.createPanner();
    p.panningModel   = 'HRTF';
    p.distanceModel  = 'exponential';
    p.refDistance    = 2;
    p.maxDistance    = 50;
    p.rolloffFactor  = 1.8;
    p.coneInnerAngle = 360;
    p.coneOuterAngle = 360;
    p.coneOuterGain  = 0;
    if (p.positionX) {
      p.positionX.value = x;
      p.positionY.value = y;
      p.positionZ.value = z;
    } else {
      p.setPosition(x, y, z);  // 구형 API 폴백
    }
    p.connect(_master);
    return p;
  }

  // ── 리스너(카메라) 위치·방향 업데이트 ────────────────────────────────────
  /**
   * 매 렌더 프레임 호출 필수.
   * @param {number} x   카메라 world X (px)
   * @param {number} y   카메라 world Y (py + 1.6)
   * @param {number} z   카메라 world Z (pz)
   * @param {number} yaw 플레이어 yaw 라디안
   */
  function setListener(x, y, z, yaw) {
    const c = _c();
    const L = c.listener;
    const fx = -Math.sin(yaw);
    const fz = -Math.cos(yaw);
    if (L.positionX) {
      L.positionX.value = x;
      L.positionY.value = y;
      L.positionZ.value = z;
      L.forwardX.value  = fx;
      L.forwardY.value  = 0;
      L.forwardZ.value  = fz;
      L.upX.value = 0;
      L.upY.value = 1;
      L.upZ.value = 0;
    } else {
      L.setPosition(x, y, z);
      L.setOrientation(fx, 0, fz, 0, 1, 0);
    }
  }

  // ── 발자국 쿨다운 (AudioContext.currentTime 기준) ──────────────────────
  let _lastSelfStep = 0;
  let _lastOppStep  = 0;

  // ── 사운드 구현 ────────────────────────────────────────────────────────────
  const _sounds = {

    /**
     * 총성 — 3D positional
     * @param {number[]} pos [x, y, z] 발사 위치 (camera eye level)
     */
    shoot(pos) {
      const pan = pos ? _panner(pos[0], pos[1], pos[2]) : null;
      const cv  = CAT.weapon;
      _noise(0.20, 0.40, cv, pan);
      _osc('sawtooth', 310, 65, 0.20, 0.28, cv, pan);
    },

    /** 내가 상대를 명중 — 즉각 mono 피드백 */
    hit() {
      _osc('square', 900, 200, 0.09, 0.60, CAT.feedback, null);
    },

    /** 내가 데미지 받음 */
    damage() {
      _osc('triangle', 140, 72, 0.30, 0.50, CAT.feedback, null);
      _noise(0.09, 0.20, CAT.feedback, null);
    },

    /** 킬 확인 — 3음 상승 stinger */
    kill() {
      const cv = CAT.feedback;
      [0, 80, 165].forEach((t, i) => {
        setTimeout(() =>
          _osc('sine', [440, 660, 880][i], [440, 660, 880][i], 0.14, 0.38, cv, null),
        t);
      });
    },

    /** 사망 — 저주파 충격 + 하강 */
    death() {
      const cv = CAT.feedback;
      _noise(0.48, 0.65, cv, null);
      _osc('sine', 90, 32, 0.68, 0.55, cv, null);
      setTimeout(() => _osc('sine', 55, 28, 0.40, 0.25, cv, null), 180);
    },

    /** 승리 스팅거 — C5→E5→G5→C6→E6 상승 장조 아르페지오 */
    win() {
      const cv    = CAT.music;
      const notes = [523, 659, 784, 1047, 1319];
      [0, 110, 225, 370, 550].forEach((t, i) => {
        setTimeout(() =>
          _osc('sine', notes[i], notes[i] * 1.008, 0.45, 0.42, cv, null),
        t);
      });
      _noise(0.22, 0.18, cv, null);
      setTimeout(() => _noise(0.16, 0.12, cv, null), 370);
    },

    /** 패배 스팅거 — E4→C4→G3 하강 단조 */
    lose() {
      const cv    = CAT.music;
      const notes = [330, 261, 196];
      [0, 170, 380].forEach((t, i) => {
        setTimeout(() =>
          _osc('sawtooth', notes[i], notes[i] * 0.88, 0.62, 0.32, cv, null),
        t);
      });
    },

    /**
     * 내 발자국 — ambient mono (자신 위치 = 리스너 위치이므로 3D 불필요)
     * 320ms 최소 보폭 간격 (AudioContext.currentTime 기준)
     */
    footstep_self() {
      const now = _c().currentTime;
      if (now - _lastSelfStep < 0.32) return;
      _lastSelfStep = now;
      _noise(0.062, 0.07, CAT.ambient, null);
      _osc('sine', 120, 58, 0.068, 0.12, CAT.ambient, null);
    },

    /**
     * 적 발자국 — 3D positional (전술 정보 — 소리로 적 방향 파악)
     * 300ms 쿨다운 (서버 틱 노이즈 방지)
     * @param {number[]} pos [x, y, z] 적 현재 위치
     */
    footstep_opp(pos) {
      const now = _c().currentTime;
      if (now - _lastOppStep < 0.30) return;
      _lastOppStep = now;
      const pan = pos ? _panner(pos[0], pos[1], pos[2]) : null;
      const cv  = CAT.ambient;
      _noise(0.072, 0.08, cv, pan);
      _osc('sine', 108, 52, 0.075, 0.11, cv, pan);
    },

    /** UI 버튼 클릭 */
    ui_click() {
      _osc('sine', 1200, 900, 0.07, 0.42, CAT.ui, null);
    },

    /** 게임 시작 신호음 */
    game_start() {
      const cv = CAT.ui;
      _osc('sine', 440, 880, 0.12, 0.32, cv, null);
      setTimeout(() => _osc('sine', 880, 1760, 0.10, 0.28, cv, null), 145);
    },
  };

  // ── 공개 API ──────────────────────────────────────────────────────────────
  return {
    /** AudioContext 사전 초기화 (선택적, 유저 제스처 핸들러에서 호출 가능) */
    init: _c,

    /** suspended 상태 해제 — 모든 play() 내부에서도 자동 호출됨 */
    resume,

    /**
     * 사운드 재생
     * @param {string}         name 사운드 이름 (위 목록 참고)
     * @param {number[]|null}  pos  [x, y, z] 발생 위치 (3D 사운드만 유효)
     */
    play(name, pos) {
      resume();
      if (_sounds[name]) {
        _sounds[name](pos);
      } else {
        console.warn('[AudioManager] unknown sound:', name);
      }
    },

    /**
     * 리스너(카메라) 위치·방향 갱신 — 매 렌더 프레임 호출 필수
     * @param {number} x   world X
     * @param {number} y   world Y (eye height: py + 1.6)
     * @param {number} z   world Z
     * @param {number} yaw 플레이어 yaw 라디안
     */
    setListener,
  };
})();

export default AudioManager;
