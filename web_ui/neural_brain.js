/* ==========================================================================
   J.A.R.V.I.S. // TACTICAL SPATIAL OS - 3D NEURAL CORE (Three.js r149 + Custom Shader)
   Ebenen:
     1. GPU-Point-Cloud-Hologramm der Kortex-Kontur (bis ~140k Mikropunkte, Vertex-Shader)
     2. Transparente Hemisphären-Hülle (Wireframe)
     3. Synapsen-Netzwerk + Dendriten-Verästelungen
     4. 45°/90° Platinen-Leiterbahnen (Circuit Traces) + Löt-Pads
     5. Licht-Impulse (Datenpakete), Knoten-Glühen, Kern-Sprite
     6. Telemetrie-Ringe mit Grad-Einteilung + kreisförmiger Audio-Spektrum-Visualizer
     7. 3D-Scan-Laser (Dreiecks-Strahl) zu den Monitor-Widgets + Schockwelle (n8n / Surge)
   Zustände: idle | listening | thinking | n8n | screen_scan | speaking | error
   ========================================================================== */
(function (global) {
  "use strict";

  const CYAN = [0.0, 0.94, 1.0];      // #00F0FF
  const AMBER = [1.0, 0.69, 0.125];   // #FFB020
  const ORANGE = [1.0, 0.30, 0.0];    // #FF4D00
  const PLATIN = [0.97, 0.98, 0.99];  // #F8FAFC
  const SPEC_BINS = 96;

  function makeGlowTexture() {
    const c = document.createElement("canvas");
    c.width = 64;
    c.height = 64;
    const g = c.getContext("2d");
    if (!g || !global.THREE) return null;
    const grad = g.createRadialGradient(32, 32, 0, 32, 32, 32);
    grad.addColorStop(0.0, "rgba(255,255,255,1)");
    grad.addColorStop(0.22, "rgba(220,250,255,0.92)");
    grad.addColorStop(0.55, "rgba(0,240,255,0.26)");
    grad.addColorStop(1.0, "rgba(0,0,0,0)");
    g.fillStyle = grad;
    g.fillRect(0, 0, 64, 64);
    const tex = new global.THREE.CanvasTexture(c);
    tex.needsUpdate = true;
    return tex;
  }

  /* Organische zweilappige Gehirn-Verformung (Längsfissur, Frontal/Temporal/Okzipital, Kleinhirn) */
  function brainSurfaceOffset(nx, ny, nz) {
    const side = nx >= 0 ? 1 : -1;
    const fissure = Math.exp(-Math.abs(nx) * 8.5) * 0.14;
    let x = nx * 0.84 + side * 0.08 - side * fissure * 0.35;
    let y = ny * 0.86 - fissure * 0.25;
    let z = nz * 1.18;
    if (z > 0.2) x *= 0.88;
    if (y < 0.05 && Math.abs(z) < 0.45) x += side * 0.09;
    if (z < -0.35 && y < -0.15) {
      y -= 0.12;
      z -= 0.07;
    }
    return { x: x * 1.32, y: y * 1.32, z: z * 1.32 };
  }

  /* Point-Cloud auf der gefalteten Kortex-Oberfläche (Gyri/Sulci per Trig-Faltung) */
  function buildCortexCloud(count) {
    const pos = new Float32Array(count * 3);
    const seed = new Float32Array(count * 4);
    for (let i = 0; i < count; i++) {
      const u = Math.random() * 2 - 1;
      const th = Math.random() * Math.PI * 2;
      const s = Math.sqrt(1 - u * u);
      const dx = s * Math.cos(th);
      const dy = u;
      const dz = s * Math.sin(th);
      const fold =
        0.045 * Math.sin(dx * 13.0 + dy * 5.0) * Math.sin(dz * 11.0 - dy * 4.0) +
        0.030 * Math.sin(dy * 17.0 + dz * 7.0) +
        0.018 * Math.sin(dx * 29.0 + dz * 23.0);
      const shell = Math.random();
      const radius = 0.90 + fold + shell * 0.075;
      const p = brainSurfaceOffset(dx * radius, dy * radius, dz * radius);
      pos[i * 3] = p.x;
      pos[i * 3 + 1] = p.y;
      pos[i * 3 + 2] = p.z;
      seed[i * 4] = Math.random();
      seed[i * 4 + 1] = Math.random();
      seed[i * 4 + 2] = Math.random();
      seed[i * 4 + 3] = shell;
    }
    return { pos, seed };
  }

  function buildNeuralTopology(nodeCount) {
    const nodes = [];
    for (let i = 0; i < nodeCount; i++) {
      const side = i % 2 === 0 ? -1 : 1;
      const isCore = i < Math.floor(nodeCount * 0.18);
      const u = Math.random() * Math.PI * 2;
      const v = Math.acos(2 * Math.random() - 1);
      const rBase = isCore ? 0.16 + Math.random() * 0.28 : 0.58 + Math.pow(Math.random(), 0.5) * 0.42;
      const rawX = Math.sin(v) * Math.cos(u) * rBase;
      const rawY = Math.cos(v) * rBase;
      const rawZ = Math.sin(v) * Math.sin(u) * rBase;
      const p = brainSurfaceOffset(isCore ? rawX * 0.55 : Math.abs(rawX) * side, rawY, rawZ);
      nodes.push({
        x: p.x, y: p.y, z: p.z, side, isCore,
        dist: Math.sqrt(p.x * p.x + p.y * p.y + p.z * p.z),
        ang: Math.atan2(p.y, p.x),
        phase: Math.random() * Math.PI * 2,
      });
    }

    const edges = [];
    const seen = new Set();
    const maxDistSq = 0.60 * 0.60;
    for (let i = 0; i < nodes.length; i++) {
      const a = nodes[i];
      const cand = [];
      for (let j = i + 1; j < nodes.length; j++) {
        const b = nodes[j];
        const dx = a.x - b.x, dy = a.y - b.y, dz = a.z - b.z;
        const d2 = dx * dx + dy * dy + dz * dz;
        if (d2 < maxDistSq && (a.side === b.side || a.isCore || b.isCore || Math.abs(a.x) < 0.32)) cand.push({ j, d2 });
      }
      cand.sort((p, q) => p.d2 - q.d2);
      const take = Math.min(4, cand.length);
      for (let k = 0; k < take; k++) {
        const j = cand[k].j;
        const key = i < j ? i + ":" + j : j + ":" + i;
        if (!seen.has(key)) {
          seen.add(key);
          edges.push({ a: i, b: j, bridge: nodes[i].side !== nodes[j].side });
        }
      }
    }

    // Dendriten-Verästelungen: kurze, leicht gekrümmte 2-Segment-Zweige je Knoten
    const dendrites = [];
    for (let i = 0; i < nodes.length; i++) {
      const n = nodes[i];
      const branches = n.isCore ? 1 : 2;
      for (let b = 0; b < branches; b++) {
        const ang = Math.random() * Math.PI * 2;
        const el = (Math.random() - 0.5) * Math.PI;
        const l1 = 0.05 + Math.random() * 0.08;
        const mx = n.x + Math.cos(ang) * Math.cos(el) * l1;
        const my = n.y + Math.sin(el) * l1;
        const mz = n.z + Math.sin(ang) * Math.cos(el) * l1;
        const l2 = 0.04 + Math.random() * 0.07;
        const ang2 = ang + (Math.random() - 0.5) * 1.3;
        const ex = mx + Math.cos(ang2) * l2;
        const ey = my + (Math.random() - 0.5) * l2;
        const ez = mz + Math.sin(ang2) * l2;
        dendrites.push([n.x, n.y, n.z, mx, my, mz], [mx, my, mz, ex, ey, ez]);
      }
    }

    // Platinen-Leiterbahnen: 45°/90° geroutete Z-Pfade + Löt-Pads
    const circuits = [];
    const traceCount = Math.min(160, Math.floor(nodes.length * 0.38));
    for (let t = 0; t < traceCount; t++) {
      const na = nodes[(t * 7) % nodes.length];
      const nb = nodes[(t * 13 + 5) % nodes.length];
      if (Math.abs(na.x - nb.x) + Math.abs(na.y - nb.y) < 0.12) continue;
      const dx = nb.x - na.x, dy = nb.y - na.y;
      const diag = Math.min(Math.abs(dx), Math.abs(dy)) * 0.5; // 45°-Abschnitt
      const p1 = { x: na.x + Math.sign(dx) * (Math.abs(dx) - diag) * 0.5, y: na.y, z: (na.z + nb.z) * 0.5 };
      const p2 = { x: p1.x + Math.sign(dx) * diag, y: na.y + Math.sign(dy) * diag, z: p1.z };
      const p3 = { x: p2.x, y: nb.y, z: nb.z };
      const ph = t * 0.37;
      circuits.push({ x1: na.x, y1: na.y, z1: na.z, x2: p1.x, y2: p1.y, z2: p1.z, phase: ph });
      circuits.push({ x1: p1.x, y1: p1.y, z1: p1.z, x2: p2.x, y2: p2.y, z2: p2.z, phase: ph + 0.3 });
      circuits.push({ x1: p2.x, y1: p2.y, z1: p2.z, x2: p3.x, y2: p3.y, z2: p3.z, phase: ph + 0.6 });
      circuits.push({ x1: p3.x, y1: p3.y, z1: p3.z, x2: nb.x, y2: nb.y, z2: nb.z, phase: ph + 0.9 });
    }
    return { nodes, edges, dendrites, circuits };
  }

  const CLOUD_VERT = [
    "uniform float uTime; uniform float uAudio; uniform float uHeat; uniform float uScanY;",
    "uniform float uScanOn; uniform float uShock; uniform float uPx; uniform float uListen;",
    "uniform vec3 uColA; uniform vec3 uColB; uniform vec3 uColC;",
    "attribute vec4 aSeed;",
    "varying vec3 vCol; varying float vAlpha;",
    "void main() {",
    "  vec3 p = position;",
    "  float r = length(p);",
    "  float breath = 1.0 + 0.012 * sin(uTime * 1.35 + aSeed.x * 6.2831) + uAudio * 0.045 * sin(uTime * 17.0 + aSeed.y * 21.0);",
    "  p *= breath;",
    "  float wave = sin(p.y * 5.0 - uTime * (1.1 + uHeat * 4.2) + aSeed.z * 2.0);",
    "  float w = smoothstep(0.55, 1.0, wave);",
    "  float d = abs(r - uShock * 2.6);",
    "  float sh = exp(-d * d * 26.0) * step(0.001, uShock) * (1.0 - uShock);",
    "  p += normalize(p) * sh * 0.14;",
    "  float sc = exp(-pow((p.y - uScanY) * 5.2, 2.0)) * uScanOn;",
    "  float lis = uListen * uAudio * smoothstep(0.3, 1.0, aSeed.w) * (0.5 + 0.5 * sin(uTime * 9.0 + aSeed.x * 30.0));",
    "  vec4 mv = modelViewMatrix * vec4(p, 1.0);",
    "  gl_Position = projectionMatrix * mv;",
    "  float wsz = 0.016 + aSeed.y * 0.026;",
    "  wsz *= (1.0 + w * 0.9 + sc * 1.5 + sh * 2.2 + lis * 1.4);",
    "  gl_PointSize = max(1.0, wsz * uPx / max(0.1, -mv.z));",
    "  vec3 col = mix(uColA, vec3(0.97, 0.98, 0.99), 0.12 + 0.38 * w);",
    "  col = mix(col, uColB, clamp(uHeat * (0.30 + w * 0.70), 0.0, 1.0));",
    "  col = mix(col, uColC, clamp(sc, 0.0, 1.0));",
    "  col += sh * vec3(1.0, 0.75, 0.30) + lis * vec3(0.4, 0.7, 0.8);",
    "  vCol = col;",
    "  vAlpha = 0.20 + 0.50 * w + sc * 0.85 + sh + lis * 0.7;",
    "}",
  ].join("\n");

  const CLOUD_FRAG = [
    "varying vec3 vCol; varying float vAlpha;",
    "void main() {",
    "  vec2 c = gl_PointCoord - 0.5;",
    "  float d = dot(c, c);",
    "  if (d > 0.25) discard;",
    "  float a = smoothstep(0.25, 0.0, d);",
    "  gl_FragColor = vec4(vCol, a * vAlpha);",
    "}",
  ].join("\n");

  function isHotState(st) {
    return st === "thinking" || st === "n8n" || st === "screen_scan";
  }

  class JarvisNeuralBrain {
    constructor(canvas, opts) {
      this.canvas = canvas;
      this.opts = opts || {};
      this.supported = false;
      this.state = "idle";
      this.intensity = 0.3;
      this.audioLevel = 0.0;
      this.vadActive = false;
      this.speakingActive = false;
      this.activeMonitor = null;
      this.detail = "STANDBY";
      this.pointerX = 0;
      this.pointerY = 0;
      this.cinematic = false;
      this.scanTargets = [];
      this._t = 0;
      this._smoothAudio = 0;
      this._smoothSpeed = 0.4;
      this._heat = 0;
      this._listen = 0;
      this._camAngle = 0;
      this._scanY = 0;
      this._shock = 0;
      this._shockActive = false;
      this._scanBlend = 0;

      if (!global.THREE || !canvas) return;
      const THREE = global.THREE;
      try {
        this.renderer = new THREE.WebGLRenderer({ canvas, alpha: true, antialias: true, powerPreference: "high-performance" });
      } catch (_err) {
        return;
      }

      this.supported = true;
      const lite = /arm|aarch64/i.test(navigator.platform || "") || (navigator.hardwareConcurrency || 8) <= 4 ||
        document.body.classList.contains("perf-lite");
      this.lite = lite;
      this.dpr = Math.min(window.devicePixelRatio || 1, lite ? 1.25 : 2);
      this.renderer.setPixelRatio(this.dpr);
      this.renderer.setClearColor(0x000000, 0);

      this.scene = new THREE.Scene();
      this.camera = new THREE.PerspectiveCamera(40, 1, 0.1, 40);
      this.camera.position.set(0, 0.15, 4.3);

      this.root = new THREE.Group();
      this.scene.add(this.root);

      const hi = !lite && (navigator.hardwareConcurrency || 8) >= 8;
      const cloudCount = lite ? 28000 : (hi ? 140000 : 90000);
      const nodeCount = lite ? 260 : 400;
      const pulseCount = lite ? 110 : 210;

      const topo = buildNeuralTopology(nodeCount);
      this.nodes = topo.nodes;
      this.edges = topo.edges;
      this.circuits = topo.circuits;
      this.nodeEnergy = new Float32Array(this.nodes.length);
      this.spec = new Float32Array(SPEC_BINS);
      const glowTex = makeGlowTexture();

      /* 1. GPU Point-Cloud-Hologramm */
      const cloud = buildCortexCloud(cloudCount);
      const cloudGeo = new THREE.BufferGeometry();
      cloudGeo.setAttribute("position", new THREE.BufferAttribute(cloud.pos, 3));
      cloudGeo.setAttribute("aSeed", new THREE.BufferAttribute(cloud.seed, 4));
      this.cloudUniforms = {
        uTime: { value: 0 }, uAudio: { value: 0 }, uHeat: { value: 0 }, uScanY: { value: 0 },
        uScanOn: { value: 0 }, uShock: { value: 0 }, uPx: { value: 300 }, uListen: { value: 0 },
        uColA: { value: new THREE.Vector3(CYAN[0], CYAN[1], CYAN[2]) },
        uColB: { value: new THREE.Vector3(AMBER[0], AMBER[1], AMBER[2]) },
        uColC: { value: new THREE.Vector3(ORANGE[0], ORANGE[1], ORANGE[2]) },
      };
      this.cloudMat = new THREE.ShaderMaterial({
        uniforms: this.cloudUniforms,
        vertexShader: CLOUD_VERT,
        fragmentShader: CLOUD_FRAG,
        transparent: true,
        depthWrite: false,
        blending: THREE.AdditiveBlending,
      });
      this.cloudMesh = new THREE.Points(cloudGeo, this.cloudMat);
      this.cloudMesh.frustumCulled = false;
      this.root.add(this.cloudMesh);

      /* 2. Transparente Hemisphären-Hülle */
      this.hullGroup = new THREE.Group();
      [-1, 1].forEach((side) => {
        const geo = new THREE.IcosahedronGeometry(0.78, 2);
        const pos = geo.attributes.position;
        for (let i = 0; i < pos.count; i++) {
          const vx = pos.getX(i), vy = pos.getY(i), vz = pos.getZ(i);
          const sx = vx * side > -0.08 ? vx : vx * 0.15;
          const p = brainSurfaceOffset(sx + side * 0.24, vy, vz);
          pos.setXYZ(i, p.x * 0.94, p.y * 0.94, p.z * 0.94);
        }
        pos.needsUpdate = true;
        this.hullGroup.add(new THREE.Mesh(geo, new THREE.MeshBasicMaterial({
          color: 0x00f0ff, wireframe: true, transparent: true, opacity: 0.045, depthWrite: false,
        })));
      });
      this.root.add(this.hullGroup);

      /* 3a. Neuronen-Knoten */
      const nPos = new Float32Array(this.nodes.length * 3);
      this.nodeColors = new Float32Array(this.nodes.length * 3);
      for (let i = 0; i < this.nodes.length; i++) {
        nPos[i * 3] = this.nodes[i].x;
        nPos[i * 3 + 1] = this.nodes[i].y;
        nPos[i * 3 + 2] = this.nodes[i].z;
      }
      const nodeGeo = new THREE.BufferGeometry();
      nodeGeo.setAttribute("position", new THREE.BufferAttribute(nPos, 3));
      this.nodeColorAttr = new THREE.BufferAttribute(this.nodeColors, 3);
      nodeGeo.setAttribute("color", this.nodeColorAttr);
      this.nodeMat = new THREE.PointsMaterial({
        size: lite ? 0.088 : 0.08, map: glowTex, transparent: true, opacity: 0.96,
        vertexColors: true, depthWrite: false, blending: THREE.AdditiveBlending,
      });
      this.root.add(new THREE.Points(nodeGeo, this.nodeMat));

      /* 3b. Synapsen */
      const ePos = new Float32Array(this.edges.length * 6);
      this.edgeColors = new Float32Array(this.edges.length * 6);
      for (let i = 0; i < this.edges.length; i++) {
        const a = this.nodes[this.edges[i].a], b = this.nodes[this.edges[i].b];
        ePos.set([a.x, a.y, a.z, b.x, b.y, b.z], i * 6);
      }
      const edgeGeo = new THREE.BufferGeometry();
      edgeGeo.setAttribute("position", new THREE.BufferAttribute(ePos, 3));
      this.edgeColorAttr = new THREE.BufferAttribute(this.edgeColors, 3);
      edgeGeo.setAttribute("color", this.edgeColorAttr);
      this.edgeMat = new THREE.LineBasicMaterial({
        transparent: true, opacity: 0.42, vertexColors: true, depthWrite: false, blending: THREE.AdditiveBlending,
      });
      this.root.add(new THREE.LineSegments(edgeGeo, this.edgeMat));

      /* 3c. Dendriten (statisch, sehr fein) */
      const dFlat = new Float32Array(topo.dendrites.length * 6);
      topo.dendrites.forEach((seg, i) => dFlat.set(seg, i * 6));
      const dendGeo = new THREE.BufferGeometry();
      dendGeo.setAttribute("position", new THREE.BufferAttribute(dFlat, 3));
      this.dendMat = new THREE.LineBasicMaterial({
        color: 0x00f0ff, transparent: true, opacity: 0.16, depthWrite: false, blending: THREE.AdditiveBlending,
      });
      this.root.add(new THREE.LineSegments(dendGeo, this.dendMat));

      /* 4. Platinen-Leiterbahnen + Löt-Pads */
      const cPos = new Float32Array(this.circuits.length * 6);
      this.circuitColors = new Float32Array(this.circuits.length * 6);
      for (let i = 0; i < this.circuits.length; i++) {
        const s = this.circuits[i];
        cPos.set([s.x1, s.y1, s.z1, s.x2, s.y2, s.z2], i * 6);
      }
      const circuitGeo = new THREE.BufferGeometry();
      circuitGeo.setAttribute("position", new THREE.BufferAttribute(cPos, 3));
      this.circuitColorAttr = new THREE.BufferAttribute(this.circuitColors, 3);
      circuitGeo.setAttribute("color", this.circuitColorAttr);
      this.circuitMat = new THREE.LineBasicMaterial({
        transparent: true, opacity: 0.55, vertexColors: true, depthWrite: false, blending: THREE.AdditiveBlending,
      });
      this.root.add(new THREE.LineSegments(circuitGeo, this.circuitMat));

      const padCount = Math.floor(this.circuits.length / 4);
      const padPos = new Float32Array(padCount * 3);
      for (let i = 0; i < padCount; i++) {
        const s = this.circuits[i * 4 + 3];
        padPos.set([s.x2, s.y2, s.z2], i * 3);
      }
      const padGeo = new THREE.BufferGeometry();
      padGeo.setAttribute("position", new THREE.BufferAttribute(padPos, 3));
      this.padMat = new THREE.PointsMaterial({
        size: 0.045, map: glowTex, color: 0xffb020, transparent: true, opacity: 0.7,
        depthWrite: false, blending: THREE.AdditiveBlending,
      });
      this.root.add(new THREE.Points(padGeo, this.padMat));

      /* 5. Licht-Impulse */
      this.pulses = [];
      const pPos = new Float32Array(pulseCount * 3);
      this.pulseColors = new Float32Array(pulseCount * 3);
      for (let i = 0; i < pulseCount; i++) {
        const useCircuit = i % 3 === 0 && this.circuits.length > 0;
        this.pulses.push({
          useCircuit,
          idx: useCircuit ? Math.floor(Math.random() * this.circuits.length) : (this.edges.length ? Math.floor(Math.random() * this.edges.length) : 0),
          t: Math.random(), speed: 0.5 + Math.random() * 0.95, dir: Math.random() > 0.5 ? 1 : -1, amber: Math.random() > 0.6,
        });
      }
      const pulseGeo = new THREE.BufferGeometry();
      this.pulsePosAttr = new THREE.BufferAttribute(pPos, 3);
      this.pulseColorAttr = new THREE.BufferAttribute(this.pulseColors, 3);
      pulseGeo.setAttribute("position", this.pulsePosAttr);
      pulseGeo.setAttribute("color", this.pulseColorAttr);
      this.pulseMat = new THREE.PointsMaterial({
        size: 0.115, map: glowTex, transparent: true, opacity: 0.98, vertexColors: true,
        depthWrite: false, blending: THREE.AdditiveBlending,
      });
      const pulsePoints = new THREE.Points(pulseGeo, this.pulseMat);
      pulsePoints.frustumCulled = false;
      this.root.add(pulsePoints);

      /* Kern-Sprite (pulsiert; Schockwellen-Ursprung) */
      this.coreSprite = new THREE.Sprite(new THREE.SpriteMaterial({
        map: glowTex, color: 0x00f0ff, transparent: true, opacity: 0.5, depthWrite: false, blending: THREE.AdditiveBlending,
      }));
      this.coreSprite.scale.set(1.1, 1.1, 1);
      this.scene.add(this.coreSprite);

      /* 6a. Telemetrie-Ringe + Grad-Einteilung */
      this.ringsGroup = new THREE.Group();
      this.scene.add(this.ringsGroup);
      this.ringOuter = new THREE.Mesh(
        new THREE.TorusGeometry(1.76, 0.0045, 8, 140),
        new THREE.MeshBasicMaterial({ color: 0xf8fafc, transparent: true, opacity: 0.14, blending: THREE.AdditiveBlending }));
      this.ringOuter.rotation.x = Math.PI * 0.4;
      this.ringOuter.rotation.y = 0.22;
      this.ringsGroup.add(this.ringOuter);
      this.ringCyan = new THREE.Mesh(
        new THREE.TorusGeometry(1.56, 0.006, 8, 110, Math.PI * 1.35),
        new THREE.MeshBasicMaterial({ color: 0x00f0ff, transparent: true, opacity: 0.38, blending: THREE.AdditiveBlending }));
      this.ringCyan.rotation.x = -Math.PI * 0.32;
      this.ringCyan.rotation.y = -0.3;
      this.ringsGroup.add(this.ringCyan);
      this.ringAmber = new THREE.Mesh(
        new THREE.TorusGeometry(1.40, 0.0075, 8, 90, Math.PI * 0.85),
        new THREE.MeshBasicMaterial({ color: 0xffb020, transparent: true, opacity: 0.34, blending: THREE.AdditiveBlending }));
      this.ringAmber.rotation.x = Math.PI * 0.14;
      this.ringAmber.rotation.z = 0.8;
      this.ringsGroup.add(this.ringAmber);

      const tickPts = [];
      for (let deg = 0; deg < 360; deg += 5) {
        const a = (deg * Math.PI) / 180;
        const major = deg % 30 === 0;
        const r0 = 2.02, r1 = major ? 2.12 : 2.07;
        tickPts.push(Math.cos(a) * r0, Math.sin(a) * r0, 0, Math.cos(a) * r1, Math.sin(a) * r1, 0);
      }
      const tickGeo = new THREE.BufferGeometry();
      tickGeo.setAttribute("position", new THREE.BufferAttribute(new Float32Array(tickPts), 3));
      this.tickRing = new THREE.LineSegments(tickGeo, new THREE.LineBasicMaterial({
        color: 0xf8fafc, transparent: true, opacity: 0.30, blending: THREE.AdditiveBlending, depthWrite: false,
      }));
      this.ringsGroup.add(this.tickRing);

      /* 6b. Kreisförmiger Audio-Spektrum-Visualizer */
      this.specPos = new Float32Array(SPEC_BINS * 6);
      const specCol = new Float32Array(SPEC_BINS * 6);
      for (let i = 0; i < SPEC_BINS; i++) {
        specCol.set([CYAN[0], CYAN[1], CYAN[2], PLATIN[0], PLATIN[1], PLATIN[2]], i * 6);
      }
      const specGeo = new THREE.BufferGeometry();
      this.specPosAttr = new THREE.BufferAttribute(this.specPos, 3);
      specGeo.setAttribute("position", this.specPosAttr);
      specGeo.setAttribute("color", new THREE.BufferAttribute(specCol, 3));
      this.specMat = new THREE.LineBasicMaterial({
        vertexColors: true, transparent: true, opacity: 0.9, blending: THREE.AdditiveBlending, depthWrite: false,
      });
      this.specLines = new THREE.LineSegments(specGeo, this.specMat);
      this.specLines.frustumCulled = false;
      this.scene.add(this.specLines);

      /* 7a. Schockwelle */
      this.shockRing = new THREE.Mesh(
        new THREE.RingGeometry(0.97, 1.0, 120),
        new THREE.MeshBasicMaterial({ color: 0xffb020, transparent: true, opacity: 0, side: THREE.DoubleSide, depthWrite: false, blending: THREE.AdditiveBlending }));
      this.scene.add(this.shockRing);

      /* 7b. 3D-Scan-Ebene + Scan-Laser (Dreiecksstrahl Gehirn -> Monitor-Widget) */
      this.scanPlane = new THREE.Mesh(
        new THREE.RingGeometry(0.08, 1.54, 72),
        new THREE.MeshBasicMaterial({ color: 0xff4d00, side: THREE.DoubleSide, transparent: true, opacity: 0, depthWrite: false, blending: THREE.AdditiveBlending }));
      this.scanPlane.rotation.x = Math.PI / 2;
      this.root.add(this.scanPlane);

      this.laserPos = new Float32Array(9);
      const laserGeo = new THREE.BufferGeometry();
      this.laserPosAttr = new THREE.BufferAttribute(this.laserPos, 3);
      laserGeo.setAttribute("position", this.laserPosAttr);
      this.laserCone = new THREE.Mesh(laserGeo, new THREE.MeshBasicMaterial({
        color: 0xff4d00, transparent: true, opacity: 0, side: THREE.DoubleSide, depthWrite: false, blending: THREE.AdditiveBlending,
      }));
      this.laserCone.frustumCulled = false;
      this.scene.add(this.laserCone);

      this.laserCorePos = new Float32Array(6);
      const laserLineGeo = new THREE.BufferGeometry();
      this.laserCoreAttr = new THREE.BufferAttribute(this.laserCorePos, 3);
      laserLineGeo.setAttribute("position", this.laserCoreAttr);
      this.laserCore = new THREE.Line(laserLineGeo, new THREE.LineBasicMaterial({
        color: 0xfff3e0, transparent: true, opacity: 0, depthWrite: false, blending: THREE.AdditiveBlending,
      }));
      this.laserCore.frustumCulled = false;
      this.scene.add(this.laserCore);

      this.laserTip = new THREE.Sprite(new THREE.SpriteMaterial({
        map: glowTex, color: 0xff4d00, transparent: true, opacity: 0, depthWrite: false, blending: THREE.AdditiveBlending,
      }));
      this.laserTip.scale.set(0.4, 0.4, 1);
      this.scene.add(this.laserTip);

      this._paused = false;
      this.resize();
    }

    setPaused(paused) {
      this._paused = Boolean(paused);
    }

    resize() {
      if (!this.supported || !this.canvas) return;
      const parent = this.canvas.parentElement;
      const w = Math.max(220, parent ? parent.clientWidth : this.canvas.clientWidth || 420);
      const h = Math.max(220, parent ? parent.clientHeight : this.canvas.clientHeight || 420);
      this.renderer.setSize(w, h, false);
      this.camera.aspect = w / h;
      this.camera.updateProjectionMatrix();
      this.cloudUniforms.uPx.value = h * this.dpr * 0.5;
    }

    destroy() {
      this.supported = false;
      try {
        if (this.renderer && typeof this.renderer.dispose === "function") {
          this.renderer.dispose();
        }
      } catch (_e) {}
    }

    triggerShock() {
      this._shock = 0.0001;
      this._shockActive = true;
    }

    setActivity(opts) {
      if (!opts) return;
      const prev = this.state;
      if (opts.state !== undefined) this.state = String(opts.state || "idle").toLowerCase();
      if (opts.intensity !== undefined) this.intensity = Math.max(0, Math.min(1, Number(opts.intensity) || 0));
      if (opts.active_monitor !== undefined) this.activeMonitor = opts.active_monitor;
      if (opts.detail !== undefined) this.detail = String(opts.detail || "");
      if (opts.cinematic !== undefined) this.cinematic = Boolean(opts.cinematic);
      if (this.state !== prev && (this.state === "n8n" || this.state === "thinking" || this.state === "screen_scan")) {
        this.triggerShock();
      }
    }

    setAudio(level, vadActive, speakingActive) {
      this.audioLevel = Math.max(0, Math.min(1, Number(level) || 0));
      this.vadActive = Boolean(vadActive);
      this.speakingActive = Boolean(speakingActive);
    }

    setPointer(nx, ny) {
      this.pointerX = Math.max(-1, Math.min(1, Number(nx) || 0));
      this.pointerY = Math.max(-1, Math.min(1, Number(ny) || 0));
    }

    /* Ziele (NDC relativ zum Canvas, y nach oben) für den Scan-Laser; leer = Laser aus */
    setScanTargets(list) {
      this.scanTargets = Array.isArray(list) ? list.slice(0, 3) : [];
    }

    render(dt) {
      if (!this.supported || this._paused) return;
      const safeDt = Math.min(0.05, Math.max(0.004, dt || 0.016));
      this._t += safeDt;
      const t = this._t;
      const st = this.state;
      const hot = isHotState(st);

      this._smoothAudio += (this.audioLevel - this._smoothAudio) * (1 - Math.exp(-safeDt * 14));
      const lvl = Math.min(1, this._smoothAudio * 1.8);
      this._heat += ((hot ? 1 : 0) - this._heat) * (1 - Math.exp(-safeDt * 4));
      const listenTarget = st === "listening" || st === "speaking" ? 1 : 0;
      this._listen += (listenTarget - this._listen) * (1 - Math.exp(-safeDt * 6));

      const speedTarget =
        st === "thinking" ? 1.95 : st === "n8n" ? 1.82 : st === "screen_scan" ? 1.55 :
        st === "speaking" ? 1.15 + lvl * 0.95 : st === "listening" ? 0.78 + lvl * 0.85 : st === "error" ? 1.6 : 0.34;
      this._smoothSpeed += (speedTarget - this._smoothSpeed) * (1 - Math.exp(-safeDt * 3.5));

      /* Kamera-Dynamik */
      const activeCam = this.cinematic || hot || st === "speaking";
      if (activeCam) this._camAngle += safeDt * 0.42;
      const tx = this.pointerX * 0.32 + (activeCam ? Math.sin(this._camAngle) * 0.36 : Math.sin(t * 0.25) * 0.08);
      const ty = 0.14 - this.pointerY * 0.24 + (activeCam ? Math.cos(this._camAngle * 0.7) * 0.16 : 0);
      const tz = hot ? 3.82 : this.cinematic ? 3.74 : 4.28 - lvl * 0.22;
      this.camera.position.x += (tx - this.camera.position.x) * (1 - Math.exp(-safeDt * 3.2));
      this.camera.position.y += (ty - this.camera.position.y) * (1 - Math.exp(-safeDt * 3.2));
      this.camera.position.z += (tz - this.camera.position.z) * (1 - Math.exp(-safeDt * 2.8));
      this.camera.lookAt(0, 0, 0);

      this.root.rotation.y += safeDt * 0.22 * this._smoothSpeed;
      this.root.rotation.x = 0.12 + Math.sin(t * 0.5) * 0.05;
      this.root.scale.setScalar(1 + Math.sin(t * 2.0) * 0.015 + (st === "speaking" ? lvl * 0.075 : 0));

      /* Schockwelle */
      if (this._shockActive) {
        this._shock += safeDt * 0.85;
        if (this._shock >= 1) {
          this._shock = 0;
          this._shockActive = false;
        }
      }
      const shockP = this._shockActive ? this._shock : 0;
      this.shockRing.scale.setScalar(0.25 + shockP * 2.7);
      this.shockRing.material.opacity = this._shockActive ? (1 - shockP) * 0.85 : 0;
      this.shockRing.material.color.setRGB(st === "screen_scan" ? ORANGE[0] : AMBER[0], st === "screen_scan" ? ORANGE[1] : AMBER[1], st === "screen_scan" ? ORANGE[2] : AMBER[2]);

      /* Scan-Ebene & Scan-Fenster */
      const scanOn = st === "screen_scan";
      this._scanBlend += ((scanOn ? 1 : 0) - this._scanBlend) * (1 - Math.exp(-safeDt * 7));
      if (scanOn) this._scanY = Math.sin(t * 2.8) * 1.05;
      this.scanPlane.position.y = this._scanY;
      this.scanPlane.rotation.z += safeDt * 1.6;
      this.scanPlane.material.opacity = this._scanBlend * (0.5 + 0.22 * Math.sin(t * 6.5));

      /* Cloud-Uniforms */
      const U = this.cloudUniforms;
      U.uTime.value = t;
      U.uAudio.value = lvl;
      U.uHeat.value = this._heat * (st === "n8n" ? 1.0 : 0.85);
      U.uScanY.value = this._scanY;
      U.uScanOn.value = this._scanBlend;
      U.uShock.value = shockP;
      U.uListen.value = this._listen;

      /* Ringe */
      this.ringOuter.rotation.z += safeDt * 0.22 * this._smoothSpeed;
      this.ringCyan.rotation.z -= safeDt * 0.44 * this._smoothSpeed;
      this.ringAmber.rotation.y += safeDt * 0.62 * this._smoothSpeed;
      this.ringAmber.material.opacity = hot ? 0.82 : 0.24 + lvl * 0.25;
      this.tickRing.rotation.z -= safeDt * (0.05 + 0.12 * this._smoothSpeed);
      this.tickRing.material.opacity = 0.22 + 0.18 * this._heat + (st === "listening" ? 0.15 : 0);
      this.hullGroup.children.forEach((m) => {
        m.material.opacity = hot ? 0.085 : st === "listening" ? 0.06 + lvl * 0.05 : 0.045;
      });

      /* Kern-Sprite */
      const coreS = 1.0 + 0.18 * Math.sin(t * 2.1) + lvl * 0.5 + this._heat * 0.35 + (this._shockActive ? (1 - shockP) * 0.9 : 0);
      this.coreSprite.scale.set(coreS, coreS, 1);
      const cc = hot && st !== "screen_scan" ? AMBER : st === "screen_scan" ? ORANGE : st === "error" ? ORANGE : CYAN;
      this.coreSprite.material.color.setRGB(cc[0], cc[1], cc[2]);
      this.coreSprite.material.opacity = 0.32 + 0.35 * this._heat + lvl * 0.25;

      /* Audio-Spektrum-Ring */
      const specActive = st === "listening" || st === "speaking";
      const specR0 = 1.66;
      for (let i = 0; i < SPEC_BINS; i++) {
        const env = Math.exp(-i / 70) * 0.6 + 0.4;
        let v = lvl * env * (0.35 + 0.65 * Math.abs(Math.sin(t * (6.0 + i * 0.29) + i * 1.31))) * (0.8 + 0.2 * Math.sin(t * 3.0 + i));
        if (!specActive) v = 0.02 + 0.015 * Math.sin(t * 1.5 + i * 0.4);
        else v += 0.03;
        this.spec[i] += (v - this.spec[i]) * (1 - Math.exp(-safeDt * 22));
        const a = (i / SPEC_BINS) * Math.PI * 2 + t * 0.05;
        const len = 0.025 + this.spec[i] * 0.55;
        const ca = Math.cos(a), sa = Math.sin(a);
        this.specPos[i * 6] = ca * specR0;
        this.specPos[i * 6 + 1] = sa * specR0;
        this.specPos[i * 6 + 2] = 0;
        this.specPos[i * 6 + 3] = ca * (specR0 + len);
        this.specPos[i * 6 + 4] = sa * (specR0 + len);
        this.specPos[i * 6 + 5] = 0;
      }
      this.specPosAttr.needsUpdate = true;
      this.specMat.opacity = specActive ? 0.95 : 0.35;

      /* Scan-Laser: Dreieck vom Kern zum (rotierenden) Monitor-Widget-Ziel */
      const laserOn = scanOn && this.scanTargets.length > 0;
      let laserAlpha = this.laserCone.material.opacity;
      laserAlpha += ((laserOn ? 1 : 0) - laserAlpha) * (1 - Math.exp(-safeDt * 10));
      if (laserOn || laserAlpha > 0.01) {
        const list = this.scanTargets.length ? this.scanTargets : [{ x: 0, y: 0 }];
        const tgt = list[Math.floor(t * 2.4) % list.length];
        const halfH = Math.tan((this.camera.fov * Math.PI) / 360) * this.camera.position.z;
        const halfW = halfH * this.camera.aspect;
        const wx = Math.max(-2.6, Math.min(2.6, tgt.x * halfW));
        const wy = Math.max(-2.2, Math.min(2.2, tgt.y * halfH));
        const len = Math.max(0.001, Math.hypot(wx, wy));
        const px = (-wy / len) * 0.03, py = (wx / len) * 0.03;
        const lp = this.laserPos;
        lp[0] = px; lp[1] = py; lp[2] = 0.05;
        lp[3] = -px; lp[4] = -py; lp[5] = 0.05;
        lp[6] = wx; lp[7] = wy; lp[8] = 0.05;
        this.laserPosAttr.needsUpdate = true;
        const lc = this.laserCorePos;
        lc[0] = 0; lc[1] = 0; lc[2] = 0.05; lc[3] = wx; lc[4] = wy; lc[5] = 0.05;
        this.laserCoreAttr.needsUpdate = true;
        this.laserTip.position.set(wx, wy, 0.05);
        const flick = 0.75 + 0.25 * Math.sin(t * 40.0);
        this.laserCone.material.opacity = laserAlpha * 0.42 * flick;
        this.laserCore.material.opacity = laserAlpha * 0.95;
        this.laserTip.material.opacity = laserAlpha * 0.9;
        const ts = 0.25 + 0.2 * Math.sin(t * 18.0) + 0.12;
        this.laserTip.scale.set(ts, ts, 1);
      } else {
        this.laserCone.material.opacity = 0;
        this.laserCore.material.opacity = 0;
        this.laserTip.material.opacity = 0;
      }

      /* Knoten-Energie abklingen */
      const decay = Math.exp(-safeDt * 5.2);
      for (let i = 0; i < this.nodeEnergy.length; i++) this.nodeEnergy[i] *= decay;

      /* Licht-Impulse */
      const pp = this.pulsePosAttr.array;
      const pc = this.pulseColors;
      const pRate =
        st === "thinking" ? 3.5 : st === "n8n" ? 3.4 : st === "screen_scan" ? 2.4 :
        st === "speaking" ? 1.6 + lvl * 2.3 : st === "listening" ? 1.25 + lvl * 2.1 : 0.52;
      for (let i = 0; i < this.pulses.length; i++) {
        const p = this.pulses[i];
        let ax = 0, ay = 0, az = 0, bx = 0, by = 0, bz = 0;
        let dir = p.dir;
        if (p.useCircuit && this.circuits.length > 0) {
          const s = this.circuits[p.idx];
          ax = s.x1; ay = s.y1; az = s.z1; bx = s.x2; by = s.y2; bz = s.z2;
        } else if (this.edges.length > 0) {
          const e = this.edges[p.idx];
          const na = this.nodes[e.a], nb = this.nodes[e.b];
          ax = na.x; ay = na.y; az = na.z; bx = nb.x; by = nb.y; bz = nb.z;
          if (st === "listening") dir = nb.dist < na.dist ? 1 : -1;
          else if (st === "speaking") dir = nb.dist > na.dist ? 1 : -1;
        }
        p.t += safeDt * p.speed * pRate * dir;
        if (p.t > 1 || p.t < 0) {
          if (!p.useCircuit && this.edges.length > 0) {
            const hitNode = p.t > 1 ? this.edges[p.idx].b : this.edges[p.idx].a;
            this.nodeEnergy[hitNode] = Math.min(1.0, this.nodeEnergy[hitNode] + 0.85);
            p.idx = Math.floor(Math.random() * this.edges.length);
          } else if (this.circuits.length > 0) {
            p.idx = (p.idx + 1) % this.circuits.length;
          }
          p.t = dir > 0 ? 0.0 : 1.0;
        }
        const u = Math.max(0, Math.min(1, p.t));
        pp[i * 3] = ax + (bx - ax) * u;
        pp[i * 3 + 1] = ay + (by - ay) * u;
        pp[i * 3 + 2] = az + (bz - az) * u;
        const gold = st === "n8n" || (st === "thinking" && p.amber) || p.useCircuit;
        const orange = st === "error" || st === "screen_scan";
        const col = orange ? ORANGE : gold ? AMBER : CYAN;
        pc[i * 3] = col[0]; pc[i * 3 + 1] = col[1]; pc[i * 3 + 2] = col[2];
      }
      this.pulsePosAttr.needsUpdate = true;
      this.pulseColorAttr.needsUpdate = true;
      this.pulseMat.size = hot ? 0.14 : 0.11 + lvl * 0.04;

      /* Knoten-Farben */
      const nc = this.nodeColors;
      for (let i = 0; i < this.nodes.length; i++) {
        const n = this.nodes[i];
        const energy = this.nodeEnergy[i];
        const wave = 0.5 + 0.5 * Math.sin(n.dist * 5.0 - t * (st === "listening" ? -3.6 : 2.6) + n.phase * 0.4);
        let r = CYAN[0] + wave * 0.18, g = CYAN[1] - 0.12 + wave * 0.12, b = 1.0;
        if (st === "listening") {
          const bin = Math.max(0, Math.min(SPEC_BINS - 1, Math.floor(((n.ang + Math.PI) / (Math.PI * 2)) * SPEC_BINS)));
          const sp = this.spec[bin];
          const outer = Math.max(0, n.dist - 0.6);
          g = Math.min(1, 0.7 + sp * 1.4 * (0.4 + outer));
          r = 0.1 + sp * 1.1 * outer;
        } else if (st === "n8n") {
          const surge = Math.pow(0.5 + 0.5 * Math.sin(t * 8.0 + n.phase * 4), 2);
          r = AMBER[0]; g = 0.56 + surge * 0.3; b = 0.06 + surge * 0.14;
        } else if (st === "thinking") {
          if (n.isCore || energy > 0.35) { r = AMBER[0]; g = 0.62 + energy * 0.3; b = 0.08; }
          else { r = 0.0 + energy * 0.9; g = 0.8 + energy * 0.2; b = 1.0; }
        } else if (st === "screen_scan") {
          const hit = Math.exp(-Math.pow((n.y - this._scanY) * 4.2, 2));
          r = hit * ORANGE[0] + (1 - hit) * 0.0;
          g = hit * 0.55 + (1 - hit) * 0.82;
          b = hit * 0.08 + (1 - hit) * 1.0;
        } else if (st === "speaking") {
          const outW = 0.5 + 0.5 * Math.sin(n.dist * 6.0 - t * (5.0 + lvl * 7.0));
          r = n.isCore ? AMBER[0] : outW * 0.5 * lvl;
          g = 0.78 + outW * 0.2;
          b = n.isCore ? 0.2 : 1.0;
        } else if (st === "error") {
          r = ORANGE[0]; g = 0.26; b = 0.12;
        }
        nc[i * 3] = Math.min(1, r + energy * 0.45);
        nc[i * 3 + 1] = Math.min(1, g + energy * 0.35);
        nc[i * 3 + 2] = Math.min(1, b + energy * 0.25);
      }
      this.nodeColorAttr.needsUpdate = true;

      /* Synapsen-Farben */
      const ec = this.edgeColors;
      const edgeDim = hot ? 0.78 : st === "idle" ? 0.36 : 0.64;
      for (let i = 0; i < this.edges.length; i++) {
        const e = this.edges[i];
        const a3 = e.a * 3, b3 = e.b * 3;
        ec[i * 6] = nc[a3] * edgeDim; ec[i * 6 + 1] = nc[a3 + 1] * edgeDim; ec[i * 6 + 2] = nc[a3 + 2] * edgeDim;
        ec[i * 6 + 3] = nc[b3] * edgeDim; ec[i * 6 + 4] = nc[b3 + 1] * edgeDim; ec[i * 6 + 5] = nc[b3 + 2] * edgeDim;
      }
      this.edgeColorAttr.needsUpdate = true;

      /* Platinen-Leiterbahnen: laufende Pulse (Cyan ruhig, Gold bei Rechenlast) */
      const cc2 = this.circuitColors;
      const cSpeed = hot ? 7.8 : 2.8;
      for (let i = 0; i < this.circuits.length; i++) {
        const pulse = Math.pow(0.5 + 0.5 * Math.sin(t * cSpeed - this.circuits[i].phase * 2.2), 4);
        const base = hot ? 0.36 : 0.14;
        let r, g, b;
        if (st === "screen_scan") { r = base + pulse * 0.95; g = base * 0.45 + pulse * 0.4; b = base * 0.1; }
        else if (hot) { r = base + pulse * 0.9; g = base * 0.7 + pulse * 0.58; b = base * 0.15 + pulse * 0.12; }
        else { r = base * 0.2 + pulse * 0.1; g = base + pulse * 0.7; b = base + pulse * 0.9; }
        cc2[i * 6] = r; cc2[i * 6 + 1] = g; cc2[i * 6 + 2] = b;
        cc2[i * 6 + 3] = r * 0.85; cc2[i * 6 + 4] = g * 0.85; cc2[i * 6 + 5] = b * 0.85;
      }
      this.circuitColorAttr.needsUpdate = true;
      this.circuitMat.opacity = hot ? 0.9 : 0.45 + lvl * 0.3;
      this.edgeMat.opacity = hot ? 0.76 : st === "speaking" ? 0.56 + lvl * 0.3 : st === "listening" ? 0.52 + lvl * 0.28 : 0.34;
      this.dendMat.opacity = 0.12 + 0.12 * this._heat + (st === "listening" ? lvl * 0.2 : 0);
      this.padMat.opacity = 0.45 + 0.4 * this._heat;

      this.renderer.render(this.scene, this.camera);
    }
  }

  global.JarvisNeuralBrain = JarvisNeuralBrain;
})(window);
