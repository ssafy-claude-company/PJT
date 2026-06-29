// ═══════════════════════════════════════════════════════════════════════════════
// public/vfx.js  —  VFX System for 1v1 FPS
// VFX 전문가 산출물 | ES Module, zero external deps beyond Three.js (r158)
//
// API (see bottom of file for integration guide):
//   import VFXSystem from '/vfx.js';
//   const vfxSys = new VFXSystem({ THREE, scene, renderer, camera });
//   vfxSys.onShoot()                      — muzzle flash PointLight burst
//   vfxSys.triggerShake('hit'|'death')    — camera shake (synced to damage events)
//   vfxSys.triggerKillVignette(isKill)    — gold(kill) / red(death) screen flash
//   vfxSys.renderWithBloom(scene, cam, dt)— replaces renderer.render(); applies bloom + shake
//   vfxSys.setSize(w, h)                  — call on canvas resize
// ═══════════════════════════════════════════════════════════════════════════════

// ─── VFX Configuration — ALL tuning in one place, no magic numbers elsewhere ──
export const VFX_CFG = {
  // Muzzle flash dynamic PointLight (camera-space, warm yellow burst on every shot)
  MUZZLE_LIGHT_INTENSITY: 5.5,   // peak intensity
  MUZZLE_LIGHT_RADIUS:    6.0,   // world-unit falloff radius
  MUZZLE_LIGHT_MS:         65,   // ms before intensity → 0

  // Postprocessing bloom — inline WebGL pipeline, no external library
  // Pass order is FIXED (documented below in _buildBloom — changing order changes output):
  //   [0] ScenePass   → sceneRT          (full-res, main scene render)
  //   [1] Threshold   → extractRT        (½-res, bright pixels only)
  //   [2] HBlur-1     → blurRTA          (½-res, horizontal 7-tap gaussian)
  //   [3] VBlur-1     → blurRTB          (½-res, vertical  7-tap gaussian)
  //   [4] HBlur-2     → blurRTA          (½-res, 2nd pass: wider glow radius)
  //   [5] VBlur-2     → blurRTB          (½-res, final bloom texture)
  //   [6] Composite   → canvas           (scene + bloom additive blend)
  BLOOM_THRESHOLD: 0.62,  // luminance cutoff for extraction (0=everything, 1=only clipped white)
  BLOOM_STRENGTH:  1.35,  // additive bloom weight in composite pass

  // Camera shake — screen-space offset, driven by player state events
  SHAKE_HIT_AMP:   0.034, // world-unit amplitude on damage hit
  SHAKE_DEATH_AMP: 0.072, // amplitude on death (2× stronger)
  SHAKE_DECAY:     6.0,   // exponential decay rate (1/sec); 6.0 ≈ 500ms effective duration
  SHAKE_FREQ:     44.0,   // oscillation frequency (rad/sec)

  // Kill/death screen vignette (CSS div overlay, synced to server 'kill' event)
  KILL_VIG_HOLD:   120,   // ms at full opacity before fade begins
  KILL_VIG_FADE:   480,   // ms fade-out duration

  // Debug toggles — set to false to disable an effect individually (code stays, QA-friendly)
  DBG_BLOOM:           true,   // false = bypass bloom pipeline, plain renderer.render
  DBG_CAMERA_SHAKE:    true,   // false = no camera shake
  DBG_MUZZLE_LIGHT:    true,   // false = no PointLight burst on shoot
  DBG_KILL_VIGNETTE:   true,   // false = no kill/death screen flash
};

// ═══════════════════════════════════════════════════════════════════════════════
// VFXSystem — main class
// ═══════════════════════════════════════════════════════════════════════════════
export default class VFXSystem {
  /**
   * @param {object} opts
   * @param {*}  opts.THREE    — the Three.js namespace (import * as THREE from '/three.module.js')
   * @param {*}  opts.scene    — THREE.Scene (main game scene)
   * @param {*}  opts.renderer — THREE.WebGLRenderer
   * @param {*}  opts.camera   — THREE.PerspectiveCamera (player camera)
   */
  constructor({ THREE, scene, renderer, camera }) {
    this._T        = THREE;
    this._scene    = scene;
    this._renderer = renderer;
    this._camera   = camera;

    // ── Muzzle flash PointLight (camera-space; follows player look direction) ──
    // Positioned near gun barrel: camera-local (0.19, -0.1, -0.4)
    // Added as camera child so world-space follows camera rotation automatically
    this._muzzleLight = new THREE.PointLight(
      0xffffaa,                        // warm yellow
      0,                               // start at 0 — only active on shoot
      VFX_CFG.MUZZLE_LIGHT_RADIUS,
      2.0                              // decay exponent (quadratic falloff)
    );
    this._muzzleLight.position.set(0.19, -0.1, -0.4);
    camera.add(this._muzzleLight);
    this._muzzleLightTO = null;

    // ── Camera shake state ──
    this._shakeAmt   = 0;   // current amplitude (world units), decays to 0 each frame
    this._shakePhase = 0;   // phase accumulator for multi-frequency oscillation

    // ── Kill vignette DOM element (defined in index.html) ──
    this._killVigEl = document.getElementById('kill-vignette');
    this._kvTO1 = null;
    this._kvTO2 = null;

    // ── Bloom pipeline (inline WebGL; built once at construction) ──
    this._bloom = this._buildBloom();
  }

  // ──────────────────────────────────────────────────────────────────────────────
  // Public API — per-event calls
  // ──────────────────────────────────────────────────────────────────────────────

  /**
   * Muzzle flash PointLight burst — call from doShoot() immediately on fire.
   * Briefly illuminates walls/floor/opponent with a warm yellow flash.
   */
  onShoot() {
    if (!VFX_CFG.DBG_MUZZLE_LIGHT) return;
    this._muzzleLight.intensity = VFX_CFG.MUZZLE_LIGHT_INTENSITY;
    clearTimeout(this._muzzleLightTO);
    this._muzzleLightTO = setTimeout(
      () => { this._muzzleLight.intensity = 0; },
      VFX_CFG.MUZZLE_LIGHT_MS
    );
  }

  /**
   * Camera shake — call with 'hit' on damage, 'death' on player death.
   * Applies additive offset to camera.position (respects current base pos).
   * @param {'hit'|'death'} type
   */
  triggerShake(type) {
    if (!VFX_CFG.DBG_CAMERA_SHAKE) return;
    const amp = type === 'death' ? VFX_CFG.SHAKE_DEATH_AMP : VFX_CFG.SHAKE_HIT_AMP;
    // Take the larger amplitude if multiple events overlap
    this._shakeAmt = Math.max(this._shakeAmt, amp);
  }

  /**
   * Kill/death screen vignette (CSS div overlay, event-driven).
   * @param {boolean} isKill — true = gold centre flash (I killed); false = red edge flash (I died)
   */
  triggerKillVignette(isKill) {
    if (!VFX_CFG.DBG_KILL_VIGNETTE || !this._killVigEl) return;
    const el = this._killVigEl;
    el.style.transition = '';
    el.style.background = isKill
      ? 'radial-gradient(ellipse at center, rgba(255,220,30,0.28) 0%, rgba(255,140,0,0.48) 100%)'
      : 'radial-gradient(ellipse at center, transparent 30%, rgba(220,0,0,0.72) 100%)';
    el.style.opacity = '1';
    clearTimeout(this._kvTO1); clearTimeout(this._kvTO2);
    this._kvTO1 = setTimeout(() => {
      el.style.transition = `opacity ${VFX_CFG.KILL_VIG_FADE}ms ease-out`;
      el.style.opacity    = '0';
    }, VFX_CFG.KILL_VIG_HOLD);
    this._kvTO2 = setTimeout(() => {
      el.style.transition = '';
    }, VFX_CFG.KILL_VIG_HOLD + VFX_CFG.KILL_VIG_FADE);
  }

  // ──────────────────────────────────────────────────────────────────────────────
  // Public API — per-frame (call ONCE per frame, replaces renderer.render)
  // ──────────────────────────────────────────────────────────────────────────────

  /**
   * Main per-frame call — applies camera shake then runs bloom pipeline.
   * Must be called AFTER camera.position.set() so shake is additive offset.
   * Replaces `renderer.render(scene, camera)` in the game loop.
   * @param {THREE.Scene}             scene
   * @param {THREE.PerspectiveCamera} camera
   * @param {number}                  dt — delta time in seconds
   */
  renderWithBloom(scene, camera, dt) {
    // ── Camera shake — additive offset applied after base position is set ──
    if (VFX_CFG.DBG_CAMERA_SHAKE && this._shakeAmt > 0.0005) {
      this._shakePhase += dt * VFX_CFG.SHAKE_FREQ;
      this._shakeAmt   *= Math.exp(-VFX_CFG.SHAKE_DECAY * dt);
      if (this._shakeAmt < 0.0005) {
        this._shakeAmt = 0;
      } else {
        // Two-frequency mix → organic, non-periodic feel (avoids visible regularity)
        const p  = this._shakePhase;
        const sx = (Math.sin(p * 1.0) * 0.65 + Math.sin(p * 2.7) * 0.35) * this._shakeAmt;
        const sy = (Math.sin(p * 1.5) * 0.65 + Math.sin(p * 3.1) * 0.35) * this._shakeAmt * 0.5;
        camera.position.x += sx;
        camera.position.y += sy;
      }
    }

    // ── Bloom pipeline (or plain render if DBG_BLOOM=false) ──
    this._bloom.render(scene, camera);
  }

  /**
   * Call on canvas resize to keep render targets matched to viewport.
   * @param {number} w — new canvas width  (pixels)
   * @param {number} h — new canvas height (pixels)
   */
  setSize(w, h) {
    this._bloom.setSize(w, h);
  }

  // ──────────────────────────────────────────────────────────────────────────────
  // Private — bloom pipeline builder
  // ──────────────────────────────────────────────────────────────────────────────

  _buildBloom() {
    const T        = this._T;
    const renderer = this._renderer;

    // ── Bypass — used when DBG_BLOOM is false ──────────────────────────────────
    if (!VFX_CFG.DBG_BLOOM) {
      return {
        render(scene, camera) { renderer.setRenderTarget(null); renderer.render(scene, camera); },
        setSize() {},
      };
    }

    const W = window.innerWidth;
    const H = window.innerHeight;
    const bW = W >> 1;   // half-width  (blur passes at ½ resolution = 4× faster)
    const bH = H >> 1;   // half-height

    // ── Render targets ─────────────────────────────────────────────────────────
    // sceneRT: full-res HDR scene texture (HalfFloat for wider luminance range)
    const sceneRT   = new T.WebGLRenderTarget(W,  H,  { type: T.HalfFloatType, depthBuffer: true,  stencilBuffer: false });
    // bloom targets: half-res, no depth needed
    const extractRT = new T.WebGLRenderTarget(bW, bH, { depthBuffer: false });
    const blurRTA   = new T.WebGLRenderTarget(bW, bH, { depthBuffer: false });
    const blurRTB   = new T.WebGLRenderTarget(bW, bH, { depthBuffer: false });

    // ── Fullscreen quad infrastructure (shared across all passes) ──────────────
    const orthoCamera = new T.OrthographicCamera(-1, 1, 1, -1, 0, 1);
    const orthoScene  = new T.Scene();
    const quad        = new T.Mesh(new T.PlaneGeometry(2, 2), null);
    quad.frustumCulled = false;
    orthoScene.add(quad);

    // Shared vertex shader (all passes use UV → clip-space, no transforms)
    const VS = `
      varying vec2 vUv;
      void main() { vUv = uv; gl_Position = vec4(position, 1.0); }
    `;

    // ── Pass [1] — Luminance threshold ────────────────────────────────────────
    // Extracts only pixels bright enough to bloom.
    // Targets: emissive center-pillar stripes, muzzle flash particles, hot reflections.
    const threshMat = new T.ShaderMaterial({
      depthTest: false, depthWrite: false,
      uniforms: {
        tDiffuse: { value: null },
        thresh:   { value: VFX_CFG.BLOOM_THRESHOLD },
      },
      vertexShader: VS,
      fragmentShader: `
        uniform sampler2D tDiffuse;
        uniform float     thresh;
        varying vec2      vUv;
        void main() {
          vec4  c   = texture2D(tDiffuse, vUv);
          float lum = dot(c.rgb, vec3(0.299, 0.587, 0.114));
          // Smooth step avoids hard aliasing at threshold boundary
          float w   = smoothstep(thresh, thresh + 0.05, lum);
          gl_FragColor = vec4(c.rgb * w, 1.0);
        }
      `,
    });

    // ── Passes [2/4] — Horizontal 7-tap Gaussian blur ─────────────────────────
    // Weights: 0.064+0.122+0.174+0.280+0.174+0.122+0.064 = 1.000 (normalised)
    const hBlurMat = new T.ShaderMaterial({
      depthTest: false, depthWrite: false,
      uniforms: {
        tDiffuse: { value: null },
        res:      { value: new T.Vector2(bW, bH) }, // updated on resize
      },
      vertexShader: VS,
      fragmentShader: `
        uniform sampler2D tDiffuse;
        uniform vec2      res;
        varying vec2      vUv;
        void main() {
          float h = 1.5 / res.x;  // step size: 1.5 texels → wider glow
          gl_FragColor =
            texture2D(tDiffuse, vUv + vec2(-3.0*h, 0.0)) * 0.064 +
            texture2D(tDiffuse, vUv + vec2(-2.0*h, 0.0)) * 0.122 +
            texture2D(tDiffuse, vUv + vec2(-1.0*h, 0.0)) * 0.174 +
            texture2D(tDiffuse, vUv)                      * 0.280 +
            texture2D(tDiffuse, vUv + vec2( 1.0*h, 0.0)) * 0.174 +
            texture2D(tDiffuse, vUv + vec2( 2.0*h, 0.0)) * 0.122 +
            texture2D(tDiffuse, vUv + vec2( 3.0*h, 0.0)) * 0.064;
        }
      `,
    });

    // ── Passes [3/5] — Vertical 7-tap Gaussian blur ───────────────────────────
    // Same kernel, transposed axis (h→v, res.x→res.y)
    const vBlurMat = new T.ShaderMaterial({
      depthTest: false, depthWrite: false,
      uniforms: {
        tDiffuse: { value: null },
        res:      { value: new T.Vector2(bW, bH) },
      },
      vertexShader: VS,
      fragmentShader: `
        uniform sampler2D tDiffuse;
        uniform vec2      res;
        varying vec2      vUv;
        void main() {
          float v = 1.5 / res.y;
          gl_FragColor =
            texture2D(tDiffuse, vUv + vec2(0.0, -3.0*v)) * 0.064 +
            texture2D(tDiffuse, vUv + vec2(0.0, -2.0*v)) * 0.122 +
            texture2D(tDiffuse, vUv + vec2(0.0, -1.0*v)) * 0.174 +
            texture2D(tDiffuse, vUv)                      * 0.280 +
            texture2D(tDiffuse, vUv + vec2(0.0,  1.0*v)) * 0.174 +
            texture2D(tDiffuse, vUv + vec2(0.0,  2.0*v)) * 0.122 +
            texture2D(tDiffuse, vUv + vec2(0.0,  3.0*v)) * 0.064;
        }
      `,
    });

    // ── Pass [6] — Additive composite (scene + bloom) ─────────────────────────
    // Additive blend: bloom only brightens, never darkens the scene.
    const compMat = new T.ShaderMaterial({
      depthTest: false, depthWrite: false,
      uniforms: {
        tScene:   { value: null },
        tBloom:   { value: null },
        strength: { value: VFX_CFG.BLOOM_STRENGTH },
      },
      vertexShader: VS,
      fragmentShader: `
        uniform sampler2D tScene;
        uniform sampler2D tBloom;
        uniform float     strength;
        varying vec2      vUv;
        void main() {
          vec4 base = texture2D(tScene, vUv);
          vec4 glow = texture2D(tBloom, vUv) * strength;
          // Additive: glow ≥ 0 always → never darkens
          gl_FragColor = vec4(base.rgb + glow.rgb, base.a);
        }
      `,
    });

    // Helper — set material, render to target, clear first
    function pass(mat, target) {
      quad.material = mat;
      renderer.setRenderTarget(target);
      renderer.clear();
      renderer.render(orthoScene, orthoCamera);
    }

    return {
      /**
       * Execute the full bloom pipeline for one frame.
       * Pass order is fixed — see VFX_CFG comment at top of file.
       */
      render(scene, camera) {
        // [0] Scene → sceneRT (full resolution, main render)
        renderer.setRenderTarget(sceneRT);
        renderer.clear();
        renderer.render(scene, camera);

        // [1] Threshold → extractRT (½ res, bright areas only)
        threshMat.uniforms.tDiffuse.value = sceneRT.texture;
        pass(threshMat, extractRT);

        // [2] H-blur pass 1 → blurRTA
        hBlurMat.uniforms.tDiffuse.value = extractRT.texture;
        pass(hBlurMat, blurRTA);

        // [3] V-blur pass 1 → blurRTB
        vBlurMat.uniforms.tDiffuse.value = blurRTA.texture;
        pass(vBlurMat, blurRTB);

        // [4] H-blur pass 2 (second iteration: wider glow radius) → blurRTA
        hBlurMat.uniforms.tDiffuse.value = blurRTB.texture;
        pass(hBlurMat, blurRTA);

        // [5] V-blur pass 2 → blurRTB (final bloom texture)
        vBlurMat.uniforms.tDiffuse.value = blurRTA.texture;
        pass(vBlurMat, blurRTB);

        // [6] Composite → canvas (additive: scene + bloom)
        compMat.uniforms.tScene.value = sceneRT.texture;
        compMat.uniforms.tBloom.value = blurRTB.texture;
        renderer.setRenderTarget(null);
        quad.material = compMat;
        renderer.render(orthoScene, orthoCamera);
      },

      setSize(w, h) {
        sceneRT.setSize(w, h);
        const bw = w >> 1, bh = h >> 1;
        extractRT.setSize(bw, bh);
        blurRTA.setSize(bw, bh);
        blurRTB.setSize(bw, bh);
        hBlurMat.uniforms.res.value.set(bw, bh);
        vBlurMat.uniforms.res.value.set(bw, bh);
      },
    };
  }
}

// ═══════════════════════════════════════════════════════════════════════════════
// INTEGRATION GUIDE — 7 changes needed in public/index.html
// ═══════════════════════════════════════════════════════════════════════════════
//
// 1. Add import at top of <script>:
//      import VFXSystem from '/vfx.js';
//
// 2. After `const _muzzleWP = new THREE.Vector3();`, add:
//      const vfxSys = new VFXSystem({ THREE, scene, renderer, camera });
//
// 3. In doShoot() — after vfx.spawnMuzzleFlash(...), add:
//      vfxSys.onShoot();
//
// 4. In onMsg 'hit_confirm' — inside `if (msg.targetId === myId)`, after AudioManager.play('damage'), add:
//      vfxSys.triggerShake('hit');
//
// 5. In onMsg 'kill' — inside `if (iDied)`, after triggerKillVignette(false), add:
//      vfxSys.triggerShake('death');
//
//    Also replace both `triggerKillVignette(...)` calls with `vfxSys.triggerKillVignette(...)`:
//      if (iKilled) { vfxSys.triggerKillVignette(true); }
//      if (iDied)   { ... vfxSys.triggerKillVignette(false); ... }
//    Then remove the standalone `triggerKillVignette` function (lines 815–834).
//
// 6. In render loop — replace:
//      renderer.render(scene, camera);
//    with:
//      vfxSys.renderWithBloom(scene, camera, dt);
//
// 7. In resize block — after camera.updateProjectionMatrix(), add:
//      vfxSys.setSize(cw, ch);
//
// ═══════════════════════════════════════════════════════════════════════════════
