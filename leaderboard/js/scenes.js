/* Branch-history scenes drawn on a 2D Canvas: the workflow definitions and the operation definitions share one
   drawing language. x is a fraction of the drawable width, a lane is one branch row, times are seconds of one
   sequence that plays once when the scene is seen and then holds. Lines and curves grow from an existing commit,
   beads are commits, open circles are new branch heads, diamonds are reads, squares are writes, and a ref chip moves on
   its own and never adds a commit. Nothing is measured: spacing and counts are illustrative. Lane colors, ink and
   type come from the stylesheet's tokens. */
const SCENE_SECONDS = 4.5;
const L = (lane, x1, x2, t0, t1, fade) => ({kind: 'line', lane, x1, x2, t0, t1, fade});
const C = (from, x1, to, x2, t0, t1, fade) => ({kind: 'curve', from, x1, to, x2, t0, t1, fade});
const D = (lane, x, t, fade) => ({kind: 'dot', lane, x, t, fade});
const R = (name, lane, at, end, below) => ({kind: 'ref', name, lane, at, end, below});  // at: [[t, x, lane?], ...]; the chip glides between keyframes
const T = (lane, x, t, text, above, align = 'center') => ({kind: 'text', lane, x, t, text, above, align});
const H = (lane, x, t, fade) => ({kind: 'head', lane, x, t, fade});  // a new branch head with no commit of its own
const Q = (lane, x, t, n = 1) => ({kind: 'read', lane, x, t, n});  // rows read at a head: n diamonds for a range
const W = (lane, x, t, n = 1) => ({kind: 'write', lane, x, t, n});  // rows written: n squares for a range
const run = (lane, points, fade) => points.flatMap(([x, t, dot = true], i) => [...(i ? [L(lane, points[i - 1][0], x, points[i - 1][1], t, fade)] : []), ...(dot ? [D(lane, x, t, fade)] : [])]);
const easeInOut = p => p < .5 ? 4 * p ** 3 : 1 - (-2 * p + 2) ** 3 / 2;
const easeOut = p => 1 - (1 - p) ** 3;
const portion = (t, t0, t1) => Math.min(1, Math.max(0, t1 > t0 ? (t - t0) / (t1 - t0) : t >= t0 ? 1 : 0));
const soft = (item, t) => item.fade === undefined ? 1 : 1 - .65 * portion(t, item.fade, item.fade + .4);
function compileScene(raw) {
    const lanes = raw.lanes.map(([id, name, sub, color]) => ({id, name, sub, color: token(`--lane-${color}`), start: Infinity}));
    const byId = Object.fromEntries(lanes.map(l => [l.id, l]));
    for (const item of raw.items) {  // a lane's name appears when its first commit or trace does
        const lane = byId[item.kind === 'curve' ? item.to : item.lane], at = ['dot', 'head', 'read', 'write'].includes(item.kind) ? item.t : item.kind === 'ref' || item.kind === 'text' ? Infinity : item.t0;
        lane.start = Math.min(lane.start, at);
    }
    return {lanes, items: raw.items};
}
// Every mark of the finished scene as a box, so a ref chip or a label can be placed where nothing else is drawn.
function sceneBoxes(spec, X, Y, r, stroke) {
    const boxes = [], pad = stroke / 2 + 2, box = (x0, y0, x1, y1) => boxes.push([Math.min(x0, x1), Math.min(y0, y1), Math.max(x0, x1), Math.max(y0, y1)]);
    for (const item of spec.items) {
        if (item.kind === 'line') box(X(item.x1), Y(item.lane) - pad, X(item.x2), Y(item.lane) + pad);
        else if (item.kind === 'curve') {
            const x1 = X(item.x1), y1 = Y(item.from), x2 = X(item.x2), y2 = Y(item.to), at = u => [x1 + (x2 - x1) * u, y1 + (y2 - y1) * u * u * (3 - 2 * u)];
            for (let i = 0; i < 24; i++) { const [ax, ay] = at(i / 24), [bx, by] = at((i + 1) / 24); box(ax - pad, ay - pad, bx + pad, by + pad); }
        } else if (['dot', 'head'].includes(item.kind)) { const x = X(item.x), y = Y(item.lane), e = r + 3; box(x - e, y - e, x + e, y + e); }
        else if (item.kind === 'read' || item.kind === 'write') { const x = X(item.x), y = Y(item.lane) + 20, half = (item.n - 1) * 5.5 + 7; box(x - half, y - 7, x + half, y + 7); }
    }
    return boxes;
}
const meets = (a, b) => a[0] < b[2] && b[0] < a[2] && a[1] < b[3] && b[1] < a[3];
// A ref chip sits on its side of the lane at each of its keyframes. One that would cover a line, a mark or another chip
// slides along the lane to the nearest clear spot, then tries the other side, so a narrow scene never draws a name
// across the history.
function placeChips(ctx, spec, boxes, X, Y, w, h, font) {
    const offsets = new Map(), taken = [];
    ctx.font = font;
    for (const item of spec.items) {
        if (item.kind !== 'ref') continue;
        const width = ctx.measureText(item.name).width + 14, own = [], side = item.below ? 20 : -20;
        const spots = [side, -side].flatMap(dy => [0, 8, -8, 16, -16, 24, -24, 32, -32, 48, -48].map(dx => ({dx, dy})));
        offsets.set(item, item.at.map(([, x, lane = item.lane]) => {
            const box = ({dx, dy}) => { const cx = Math.min(Math.max(X(x) + dx, width / 2 + 2), w - width / 2 - 2), cy = Y(lane) + dy; return [cx - width / 2 - 2, cy - 12, cx + width / 2 + 2, cy + 12]; };
            const spot = spots.find(at => { const b = box(at); return b[1] >= 0 && b[3] <= h && ![...boxes, ...taken].some(o => meets(b, o)); }) || spots[0];
            own.push(box(spot)); return spot;
        }));
        taken.push(...own);
    }
    return {offsets, boxes: taken};
}
// A label goes to the free spot closest to where the scene puts it: its own side of the lane first, then a little further
// out, then the other side, sliding along the lane as needed. A label with no free spot is left out, never drawn over the
// history; the caption and the text version carry every step.
function placeLabels(ctx, spec, boxes, X, Y, w, h, gutter) {
    const placed = new Map(), free = b => b[0] >= gutter && b[2] <= w - 2 && b[1] >= 2 && b[3] <= h - 2 && !boxes.some(o => meets(b, o));
    ctx.font = `12px ${token('--sans')}`;
    for (const item of spec.items) {
        if (item.kind !== 'text') continue;
        const width = ctx.measureText(item.text).width, side = item.above ? -1 : 1, spots = [];
        for (const [flip, out] of [[1, 16], [1, 24], [1, 32], [1, 44], [-1, 16], [-1, 24], [-1, 32], [-1, 44]]) for (const dx of [0, 12, -12, 24, -24, 40, -40, 64, -64, 96, -96, 140, -140, 200, -200, 280, -280]) spots.push({dy: side * flip * out, dx, cost: Math.abs(dx) + (out - 16) * 2 + (flip < 0 ? 60 : 0)});
        spots.sort((a, b) => a.cost - b.cost);
        const at = ({dx, dy}) => { const x = X(item.x) + dx, y = Y(item.lane) + dy, left = item.align === 'center' ? x - width / 2 : item.align === 'right' ? x - width : x; return {x, y, b: [left - 3, y - 9, left + width + 3, y + 9]}; };
        const spot = spots.map(at).find(s => free(s.b));
        if (spot) { boxes.push(spot.b); placed.set(item, spot); }
    }
    return placed;
}
function drawScene(ctx, spec, w, h, t) {
    const ink = token('--ink'), muted = token('--muted'), paper = token('--surface'), font = token('--sans');
    const index = Object.fromEntries(spec.lanes.map((l, i) => [l.id, i])), laneOf = id => spec.lanes[index[id]];
    ctx.clearRect(0, 0, w, h);
    ctx.font = `600 13px ${font}`;
    const gutter = Math.ceil(Math.max(...spec.lanes.flatMap(l => [ctx.measureText(l.name).width, l.sub ? ctx.measureText(l.sub).width * 11 / 13 : 0]))) + 16;
    const pad = 16, gap = (h - 68) / Math.max(1, spec.lanes.length - 1);
    const X = f => gutter + pad + f * (w - gutter - 2 * pad), Y = id => spec.lanes.length === 1 ? h / 2 : 34 + index[id] * gap;
    const r = 4.5, stroke = 2, key = `${w}x${h}`;
    if (spec.layoutKey !== key) {  // computed once per size from the finished scene, so a chip or label never jumps while the scene plays
        const marks = sceneBoxes(spec, X, Y, r, stroke), chips = placeChips(ctx, spec, marks, X, Y, w, h, `600 11.5px ${font}`), lanes = spec.lanes.map(l => [0, Y(l.id) - 16, gutter, Y(l.id) + 16]);
        spec.offsets = chips.offsets; spec.layout = placeLabels(ctx, spec, [...marks, ...chips.boxes, ...lanes], X, Y, w, h, gutter); spec.layoutKey = key;
    }
    ctx.lineCap = 'round'; ctx.lineJoin = 'round'; ctx.textBaseline = 'middle';
    for (const item of spec.items) {
        if (item.kind === 'line' || item.kind === 'curve') {
            const p = easeInOut(portion(t, item.t0, item.t1)); if (!p) continue;
            ctx.globalAlpha = soft(item, t); ctx.strokeStyle = laneOf(item.kind === 'line' ? item.lane : item.to).color; ctx.lineWidth = stroke; ctx.beginPath();
            if (item.kind === 'line') { const y = Y(item.lane), x1 = X(item.x1); ctx.moveTo(x1, y); ctx.lineTo(x1 + (X(item.x2) - x1) * p, y); }
            else {
                const x1 = X(item.x1), y1 = Y(item.from), x2 = X(item.x2), y2 = Y(item.to), n = 24;
                for (let i = 0; i <= n; i++) { const u = i / n * p, m = u * u * (3 - 2 * u); const x = x1 + (x2 - x1) * u, y = y1 + (y2 - y1) * m; i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); }  // horizontal tangents at both ends
            }
            ctx.stroke();
        } else if (item.kind === 'dot') {
            const p = easeOut(portion(t, item.t, item.t + .3)); if (!p) continue;
            ctx.globalAlpha = soft(item, t); ctx.fillStyle = laneOf(item.lane).color; ctx.beginPath(); ctx.arc(X(item.x), Y(item.lane), r * p, 0, Math.PI * 2); ctx.fill();
        } else if (item.kind === 'head') {  // a branch head with no commit of its own: an open circle on the lane
            const p = easeOut(portion(t, item.t, item.t + .3)); if (!p) continue;
            ctx.globalAlpha = soft(item, t); ctx.strokeStyle = laneOf(item.lane).color; ctx.fillStyle = paper; ctx.lineWidth = stroke;
            ctx.beginPath(); ctx.arc(X(item.x), Y(item.lane), (r + 1) * p, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
        } else if (item.kind === 'read' || item.kind === 'write') {  // rows read (open diamonds) or written (squares), drawn just under the head they touch
            const p = easeOut(portion(t, item.t, item.t + .35)); if (!p) continue;
            ctx.globalAlpha = p; ctx.strokeStyle = ctx.fillStyle = laneOf(item.lane).color; ctx.lineWidth = 1.5;
            for (let i = 0; i < item.n; i++) {
                const x = X(item.x) + (i - (item.n - 1) / 2) * 11, y = Y(item.lane) + 20;
                ctx.beginPath();
                if (item.kind === 'read') { ctx.moveTo(x, y - 4.5); ctx.lineTo(x + 4.5, y); ctx.lineTo(x, y + 4.5); ctx.lineTo(x - 4.5, y); ctx.closePath(); ctx.stroke(); } else ctx.fillRect(x - 4, y - 4, 8, 8);
            }
        }
    }
    for (const item of spec.items) {
        if (item.kind !== 'text') continue;
        const a = portion(t, item.t, item.t + .35); if (!a) continue;
        const spot = spec.layout.get(item); if (!spot) continue;
        ctx.globalAlpha = a; ctx.fillStyle = muted; ctx.font = `12px ${font}`; ctx.textAlign = item.align;
        ctx.fillText(item.text, spot.x, spot.y);
    }
    for (const item of spec.items) {
        if (item.kind !== 'ref') continue;
        let a = portion(t, item.at[0][0] - .25, item.at[0][0]); if (item.end !== undefined) a *= 1 - portion(t, item.end, item.end + .35); if (!a) continue;
        const offsets = spec.offsets.get(item), keys = item.at.map(([time, x, lane = item.lane], k) => ({time, lane, x: X(x) + offsets[k].dx, y: Y(lane) + offsets[k].dy})), next = keys.findIndex(k => k.time > t), i = next === -1 ? keys.length : next;
        const from = keys[Math.max(0, i - 1)], to = keys[Math.min(keys.length - 1, i)], p = i === 0 || i === keys.length ? 1 : easeInOut(portion(t, from.time, to.time));
        const x = from.x + (to.x - from.x) * p, cy = from.y + (to.y - from.y) * p;  // the chip's center: beside its commit, as placed for this size
        ctx.font = `600 11.5px ${font}`; const width = ctx.measureText(item.name).width + 14, height = 20;
        const cx = Math.min(Math.max(x, width / 2 + 2), w - width / 2 - 2);  // the chip, border included, stays inside the canvas; the commit it names does not move
        ctx.globalAlpha = a; ctx.fillStyle = paper; ctx.strokeStyle = laneOf(to.lane).color; ctx.lineWidth = 1.25;
        ctx.beginPath(); ctx.roundRect(cx - width / 2, cy - height / 2, width, height, 6); ctx.fill(); ctx.stroke();
        ctx.fillStyle = ink; ctx.textAlign = 'center'; ctx.fillText(item.name, cx, cy);
    }
    for (const lane of spec.lanes) {
        const a = portion(t, lane.start - .2, lane.start + .1); if (!a) continue;
        ctx.globalAlpha = a; ctx.textAlign = 'right'; ctx.fillStyle = ink; ctx.font = `600 13px ${font}`;
        ctx.fillText(lane.name, gutter - 8, Y(lane.id) - (lane.sub ? 7 : 0));
        if (lane.sub) { ctx.fillStyle = muted; ctx.font = `11px ${font}`; ctx.fillText(lane.sub, gutter - 8, Y(lane.id) + 8); }
    }
    ctx.globalAlpha = 1;
}
const scene = {key: null, raf: 0, started: 0, elapsed: 0, done: false, visible: false, io: null, ro: null, onVisibility: null, motion: null, onMotion: null};
const played = new Set();  // a scene plays once per page load; coming back to it shows the held final state
function unmountScene() {
    if (scene.raf) cancelAnimationFrame(scene.raf);
    if (scene.io) scene.io.disconnect();
    if (scene.ro) scene.ro.disconnect();
    if (scene.onVisibility) document.removeEventListener('visibilitychange', scene.onVisibility);
    if (scene.onMotion) scene.motion.removeEventListener('change', scene.onMotion);
    Object.assign(scene, {key: null, raf: 0, started: 0, elapsed: 0, done: false, visible: false, io: null, ro: null, onVisibility: null, motion: null, onMotion: null});
}
function mountScene(key, raw, alt, host, fallback) {
    unmountScene();
    const canvas = node('canvas', 'workflow-scene'); canvas.setAttribute('role', 'img'); canvas.setAttribute('aria-label', alt);
    scene.key = key;
    let ctx = null;
    try { ctx = canvas.getContext ? canvas.getContext('2d') : null; } catch { ctx = null; }
    if (!ctx) { host.replaceChildren(fallback()); return; }  // no 2D context: a stand-in with the full text, nothing is scheduled
    host.replaceChildren(canvas);
    const compiled = compileScene(raw);
    let w = 0, h = 0, d = 0;
    const size = () => {
        const width = host.clientWidth || 640, dpr = window.devicePixelRatio || 1;
        const height = 68 + Math.max(1, compiled.lanes.length - 1) * 76;
        if (width === w && height === h && dpr === d) return;
        w = width; h = height; d = dpr; canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr); canvas.style.width = `${w}px`; canvas.style.height = `${h}px`;
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    };
    const draw = () => { size(); drawScene(ctx, compiled, w, h, scene.done ? SCENE_SECONDS : scene.elapsed); };
    const motion = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
    scene.done = Boolean(motion && motion.matches) || played.has(key);  // the complete history at once: no frame loop, no viewport watching
    draw();
    scene.ro = new ResizeObserver(draw); scene.ro.observe(host);  // a static or finished scene still follows its host's width and the device pixel ratio
    if (scene.done) return;
    const frame = now => {
        scene.elapsed = (now - scene.started) / 1000; scene.raf = 0;
        if (scene.elapsed >= SCENE_SECONDS) { scene.done = true; played.add(key); } else scene.raf = requestAnimationFrame(frame);
        draw();
    };
    const play = () => { if (scene.done || scene.raf) return; scene.started = performance.now() - scene.elapsed * 1000; scene.raf = requestAnimationFrame(frame); };
    const pause = () => { if (scene.raf) cancelAnimationFrame(scene.raf); scene.raf = 0; };
    if (motion) {  // reduced motion requested mid-sequence: stop, show the complete history, hold
        scene.motion = motion; scene.onMotion = event => { if (!event.matches || scene.done) return; pause(); scene.done = true; played.add(key); draw(); };
        motion.addEventListener('change', scene.onMotion);
    }
    scene.onVisibility = () => { if (document.hidden) pause(); else if (scene.visible) play(); };
    document.addEventListener('visibilitychange', scene.onVisibility);
    scene.io = new IntersectionObserver(entries => { scene.visible = entries.some(entry => entry.isIntersecting); if (scene.visible && !document.hidden) play(); else pause(); }, {threshold: .2});
    scene.io.observe(canvas);
}
