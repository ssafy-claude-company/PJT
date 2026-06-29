'use strict';

const express = require('express');
const http    = require('http');
const WebSocket = require('ws');
const path    = require('path');

// ─────────────────────────────────────────────
// Constants
// ─────────────────────────────────────────────
const PORT           = process.env.PORT || 3000;
const TICK_MS        = 50;   // 20 Hz state broadcast
const DAMAGE         = 25;   // HP per hit (4 shots = kill)
const MAX_KILLS      = 5;    // first to 5 kills wins
const RESULT_DELAY   = 5000; // ms before auto-rematch after win

const SPAWNS = {
  p1: { x: -8, y: 1, z: 0 },
  p2: { x:  8, y: 1, z: 0 }
};

// Player AABB half-extents (width 0.6, height 1.8, depth 0.6)
const HB = { hw: 0.3, hh: 0.9, hd: 0.3 };

// ─────────────────────────────────────────────
// HTTP + WebSocket setup
// ─────────────────────────────────────────────
const app    = express();
const server = http.createServer(app);
const wss    = new WebSocket.Server({ server });

app.use(express.static(path.join(__dirname, 'public')));
app.get('/health', (_req, res) => res.json({ status: 'ok', phase: room.phase }));

// ─────────────────────────────────────────────
// Room state  (single room, 1v1)
// ─────────────────────────────────────────────
function freshPlayer(id) {
  const s = SPAWNS[id];
  return { ws: null, name: id, x: s.x, y: s.y, z: s.z, yaw: 0, pitch: 0, hp: 100, kills: 0 };
}

let room = {
  phase: 'LOBBY',   // LOBBY | IN_GAME | RESULT
  players: { p1: null, p2: null },
  tickTimer: null,
  resultTimer: null
};

// ─────────────────────────────────────────────
// Helpers
// ─────────────────────────────────────────────
function oppOf(id) { return id === 'p1' ? 'p2' : 'p1'; }

function send(id, data) {
  const p = room.players[id];
  if (p && p.ws && p.ws.readyState === WebSocket.OPEN) {
    p.ws.send(JSON.stringify(data));
  }
}

function broadcast(data) {
  send('p1', data);
  send('p2', data);
}

function bothConnected() {
  return ['p1', 'p2'].every(id => {
    const p = room.players[id];
    return p && p.ws && p.ws.readyState === WebSocket.OPEN;
  });
}

// ─────────────────────────────────────────────
// Ray ↔ AABB intersection  (slab method)
// ray: origin (ox,oy,oz), unit-dir (dx,dy,dz)
// box center: (cx,cy,cz), half-extents from HB
// ─────────────────────────────────────────────
function rayHitsAABB(ox, oy, oz, dx, dy, dz, cx, cy, cz) {
  const axes = [
    { o: ox, d: dx, min: cx - HB.hw, max: cx + HB.hw },
    { o: oy, d: dy, min: cy - HB.hh, max: cy + HB.hh },
    { o: oz, d: dz, min: cz - HB.hd, max: cz + HB.hd }
  ];

  let tMin = -Infinity;
  let tMax =  Infinity;

  for (const { o, d, min, max } of axes) {
    if (Math.abs(d) < 1e-8) {
      if (o < min || o > max) return false;
    } else {
      const t1 = (min - o) / d;
      const t2 = (max - o) / d;
      tMin = Math.max(tMin, Math.min(t1, t2));
      tMax = Math.min(tMax, Math.max(t1, t2));
    }
  }

  return tMax >= tMin && tMax >= 0;
}

// ─────────────────────────────────────────────
// Game lifecycle
// ─────────────────────────────────────────────
function startGame() {
  clearTimeout(room.resultTimer);
  room.resultTimer = null;
  room.phase = 'IN_GAME';
  console.log('[GAME] Starting IN_GAME');

  for (const id of ['p1', 'p2']) {
    send(id, {
      type: 'init',
      yourId: id,
      opponentName: room.players[oppOf(id)].name,
      spawnPos: SPAWNS[id],
      maxKills: MAX_KILLS
    });
  }

  clearInterval(room.tickTimer);
  room.tickTimer = setInterval(() => {
    if (room.phase !== 'IN_GAME') return;
    const snap = { type: 'state', players: {}, phase: room.phase };
    for (const id of ['p1', 'p2']) {
      const p = room.players[id];
      if (p) {
        snap.players[id] = {
          x: p.x, y: p.y, z: p.z,
          yaw: p.yaw, pitch: p.pitch,
          hp: p.hp, kills: p.kills,
          pos: { x: p.x, y: p.y, z: p.z } // domain-contract alias
        };
      }
    }
    broadcast(snap);
  }, TICK_MS);
}

function stopTick() {
  clearInterval(room.tickTimer);
  room.tickTimer = null;
}

function respawn(id) {
  const p = room.players[id];
  if (!p) return;
  const s = SPAWNS[id];
  p.hp    = 100;
  p.x     = s.x;
  p.y     = s.y;
  p.z     = s.z;
  p.yaw   = 0;
  p.pitch = 0;
}

function declareWin(winnerId) {
  room.phase = 'RESULT';
  stopTick();
  const loserId = oppOf(winnerId);
  broadcast({ type: 'win', winner: winnerId });
  // domain-contract alias
  broadcast({ type: 'game_end', winnerId, loserId });
  console.log(`[GAME] Win → ${winnerId}`);

  // Auto-rematch if both still connected
  room.resultTimer = setTimeout(() => {
    room.resultTimer = null;
    if (bothConnected()) {
      for (const id of ['p1', 'p2']) {
        const p = room.players[id];
        if (p) { p.kills = 0; respawn(id); }
      }
      startGame();
    } else {
      room.phase = 'LOBBY';
    }
  }, RESULT_DELAY);
}

// ─────────────────────────────────────────────
// Shoot handler  (server-authoritative)
// ─────────────────────────────────────────────
function handleShoot(shooterId, origin, dir) {
  if (room.phase !== 'IN_GAME') return;

  const targetId = oppOf(shooterId);
  const target   = room.players[targetId];
  if (!target || target.hp <= 0) return;

  // Normalise direction
  const len = Math.sqrt(dir[0]*dir[0] + dir[1]*dir[1] + dir[2]*dir[2]);
  if (len < 1e-8) return;
  const dx = dir[0]/len, dy = dir[1]/len, dz = dir[2]/len;

  // AABB centre: feet pos + half-height
  const cx = target.x, cy = target.y + HB.hh, cz = target.z;

  if (!rayHitsAABB(origin[0], origin[1], origin[2], dx, dy, dz, cx, cy, cz)) return;

  // Hit confirmed
  target.hp = Math.max(0, target.hp - DAMAGE);

  broadcast({ type: 'hit', target: targetId, hp: target.hp, shooter: shooterId });
  // domain-contract alias
  broadcast({ type: 'hit_confirm', targetId, damage: DAMAGE, remainHp: target.hp, killerId: shooterId });

  if (target.hp > 0) return;

  // Kill
  const shooter = room.players[shooterId];
  shooter.kills++;

  broadcast({
    type: 'kill',
    killer: shooterId,
    victim: targetId,
    kills: {
      p1: room.players.p1 ? room.players.p1.kills : 0,
      p2: room.players.p2 ? room.players.p2.kills : 0
    }
  });
  console.log(`[KILL] ${shooterId} → ${targetId} (${shooter.kills}/${MAX_KILLS})`);

  if (shooter.kills >= MAX_KILLS) {
    declareWin(shooterId);
    return;
  }

  // Respawn victim
  respawn(targetId);
}

// ─────────────────────────────────────────────
// WebSocket connections
// ─────────────────────────────────────────────
wss.on('connection', (ws) => {
  let myId = null;

  ws.on('message', (raw) => {
    let msg;
    try { msg = JSON.parse(raw.toString()); } catch { return; }

    // ── join ──────────────────────────────────
    if (msg.type === 'join' || msg.type === 'join_room') {
      // Find free slot
      for (const id of ['p1', 'p2']) {
        const p = room.players[id];
        if (!p || !p.ws || p.ws.readyState !== WebSocket.OPEN) {
          myId = id;
          break;
        }
      }

      // Stale socket recovery: room appears full but game not active =>
      // sockets may be zombie (TCP closed at OS level, readyState not yet
      // updated by Node.js event loop). Terminate them to free slots.
      if (!myId && room.phase !== 'IN_GAME') {
        console.log('[JOIN] Stale socket recovery: terminating zombie connections');
        for (const id of ['p1', 'p2']) {
          const p = room.players[id];
          if (p && p.ws) {
            try { p.ws.terminate(); } catch (e) {}
            room.players[id] = null;
          }
        }
        stopTick();
        room.phase = 'LOBBY';
        for (const id of ['p1', 'p2']) {
          if (!room.players[id]) { myId = id; break; }
        }
      }

      if (!myId) {
        ws.send(JSON.stringify({ type: 'error', message: 'Room is full' }));
        ws.close();
        return;
      }

      room.players[myId] = { ...freshPlayer(myId), ws, name: msg.name || myId };
      console.log(`[JOIN] ${myId} = "${room.players[myId].name}"`);

      if (bothConnected()) {
        // Reset kills/hp for clean start when re-using slots
        for (const id of ['p1', 'p2']) {
          if (room.players[id]) { room.players[id].kills = 0; respawn(id); }
        }
        broadcast({ type: 'room_ready' }); // domain-contract matching event
        startGame();
      } else {
        ws.send(JSON.stringify({ type: 'waiting', message: 'Waiting for opponent…' }));
      }
      return;
    }

    // All subsequent messages require an assigned id and active game
    if (!myId) return;

    // ── move ──────────────────────────────────
    if (msg.type === 'move') {
      if (room.phase !== 'IN_GAME') return;
      const p = room.players[myId];
      if (!p) return;
      p.x     = typeof msg.x     === 'number' ? msg.x     : p.x;
      p.y     = typeof msg.y     === 'number' ? msg.y     : p.y;
      p.z     = typeof msg.z     === 'number' ? msg.z     : p.z;
      // accept both 'yaw' and 'rotY' (domain-contract alias)
      p.yaw   = typeof msg.yaw   === 'number' ? msg.yaw   :
                typeof msg.rotY  === 'number' ? msg.rotY  : p.yaw;
      p.pitch = typeof msg.pitch === 'number' ? msg.pitch : p.pitch;
      return;
    }

    // ── shoot ─────────────────────────────────
    if (msg.type === 'shoot') {
      if (room.phase !== 'IN_GAME') return;

      // origin: accept array [x,y,z] or object {x,y,z}
      let origin, dir;
      if (Array.isArray(msg.origin)) {
        origin = msg.origin;
      } else if (msg.origin && typeof msg.origin === 'object') {
        origin = [msg.origin.x, msg.origin.y, msg.origin.z];
      } else return;

      // dir: accept 'dir' array, 'dir' object, or 'direction' object (domain-contract)
      if (Array.isArray(msg.dir)) {
        dir = msg.dir;
      } else if (msg.dir && typeof msg.dir === 'object') {
        dir = [msg.dir.x, msg.dir.y, msg.dir.z];
      } else if (msg.direction && typeof msg.direction === 'object') {
        dir = [msg.direction.x, msg.direction.y, msg.direction.z];
      } else return;

      handleShoot(myId, origin, dir);
      return;
    }
  });

  // ── disconnect ────────────────────────────
  ws.on('close', () => {
    if (!myId) return;

    // stale guard: 이 ws가 이미 다른 연결로 교체됐으면 슬롯 건드리지 않음
    const current = room.players[myId];
    if (!current || current.ws !== ws) {
      console.log(`[DISCONNECT] ${myId} (stale ws, ignored)`);
      return;
    }

    console.log(`[DISCONNECT] ${myId}`);

    const oppId = oppOf(myId);

    if (room.phase === 'IN_GAME' || room.phase === 'RESULT') {
      // Award win to opponent only if they're still connected
      const opp = room.players[oppId];
      if (opp && opp.ws && opp.ws.readyState === WebSocket.OPEN) {
        if (room.phase === 'IN_GAME') {
          stopTick();
          room.phase = 'RESULT';
          send(oppId, { type: 'win', winner: oppId });
          send(oppId, { type: 'game_end', winnerId: oppId, loserId: myId });
        }
        send(oppId, { type: 'opponent_left' });
      }
    }

    // Clean up slot
    room.players[myId] = null;
    stopTick();
    clearTimeout(room.resultTimer);
    room.resultTimer = null;
    room.phase = 'LOBBY';
    console.log('[ROOM] Returned to LOBBY');
  });

  ws.on('error', (err) => {
    console.error(`[WS ERROR] ${myId || 'unassigned'}:`, err.message);
  });
});

// ─────────────────────────────────────────────
// Start
// ─────────────────────────────────────────────
server.listen(PORT, () => {
  console.log(`[SERVER] FPS Game Server → http://localhost:${PORT}`);
  console.log(`[SERVER] WebSocket ready. Waiting for players…`);
});

server.on('error', (err) => {
  console.error('[SERVER ERROR]', err);
  process.exit(1);
});
