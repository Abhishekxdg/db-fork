/* Boot, state, routing and render. The phase is build configuration written into data.js; nothing in the URL can change it. */
const PROD = (typeof site === 'undefined' ? 'prod' : site.phase) !== 'local';
const DOCS = PROD ? 'https://github.com/elaineang/db-fork/blob/main/leaderboard/README.md' : 'README.md';
const allOn = values => Object.fromEntries(values.map(v => [v, true]));
// The page opens on the agent workflows whenever their data is built. Each suite keeps its own selections.
const state = {
    suite: PROD || MACRO.built ? 'macro' : 'micro', rank: Object.keys(SCENARIOS)[0], workflow: Object.keys(SCENARIOS)[0], campaign: new Set(),
    mode: 'first', sort: 'branch', ticked: new Set(suiteRows), op: allRows[0].op, hosted: allOn(Object.keys(HOSTING)),
    system: allOn(uniq(data.map(e => e.system))), machine: allOn(uniq(data.map(e => e.machine))), tag: allOn(uniq(data.flatMap(e => e.tags))),
};
const decoded = text => { try { return decodeURIComponent(text); } catch { return null; } };  // a mangled link is treated as absent, never as a crash
function parseHash() {
    const [path, query = ''] = window.location.hash.slice(1).split('?'), params = {};
    for (const pair of query.split('&')) {
        if (!pair) continue;
        const [k, v = ''] = pair.split('='), key = decoded(k), value = decoded(v);
        if (key !== null && value !== null) params[key] = value;
    }
    return {path, params};
}
// Routes: #leaderboard (suite unchanged), #agent-workflows (?workflow= ranks by that workflow, ?campaign= picks older campaigns),
// #workflows?workflow= (one workflow's definition), #macro-guide, and the microbench's #microbench, #dataset?operation= (one operation's
// definition; the bare link opens the dataset details) and #methodology.
function applyHash() {
    const {path, params} = parseHash();
    if (path === 'workflows') { state.suite = 'macro'; if (params.workflow in SCENARIOS) state.workflow = params.workflow; return; }
    if (['microbench', 'dataset', 'methodology'].includes(path)) state.suite = 'micro';
    if (path === 'dataset' && allRows.some(r => r.op === params.operation)) state.op = params.operation;
    if (path === 'macro-guide') state.suite = 'macro';
    if (path !== 'agent-workflows') return;
    state.suite = 'macro';
    state.rank = params.workflow in SCENARIOS ? params.workflow : Object.keys(SCENARIOS)[0];
    state.campaign = new Set((params.campaign || '').split(',').filter(Boolean));
}
const boardHash = () => state.suite === 'macro' ? '#agent-workflows' + [state.rank !== Object.keys(SCENARIOS)[0] ? `workflow=${state.rank}` : '', state.campaign.size ? `campaign=${[...state.campaign].join(',')}` : ''].filter(Boolean).map((p, i) => (i ? '&' : '?') + p).join('') : '#microbench';
const workflowsHash = () => `#workflows?workflow=${state.workflow}`;
const operationHash = () => `#dataset?operation=${state.op}`;
// The page's own hash writes never scroll; a link the viewer follows does.
let ownHash = null;
function writeHash(hash) { if (window.location.hash !== hash) { ownHash = hash; window.location.hash = hash; } }
// A link to a section opens it and brings it into view; a section already on screen does not jump.
const TARGETS = {workflows: 'workflows', dataset: 'dataset', methodology: 'methodology', 'macro-guide': 'macro-guide'};
function reveal() {
    const {path, params} = parseHash(), id = TARGETS[path]; if (!id) return;
    if (id === 'dataset') { if (!params.operation) el('dataset-details').open = true; } else if (id !== 'workflows') el(id).open = true;
    const box = el(id).getBoundingClientRect();
    if (box.top < 0 || box.top > window.innerHeight * .6) el(id).scrollIntoView({block: 'start'});
}
// The suites are tabs on the board card: arrow keys, Home and End move between them, and the active tab alone is in the tab order.
const SUITES = {macro: ['Macrobench', () => plural(Object.keys(SCENARIOS).length, 'agent workflow')], micro: ['Microbench', () => plural(suiteRows.length, 'operation')]};
function selectSuite(k) { state.suite = k; writeHash(boardHash()); render(); el(`tab-${k}`).focus(); }
function renderTabs() {
    el('selectors-suite').replaceChildren(...Object.entries(SUITES).map(([k, [name, sub]]) => {
        const tab = node('button', 'suite-tab'); tab.type = 'button'; tab.id = `tab-${k}`; tab.setAttribute('role', 'tab');
        tab.setAttribute('aria-selected', String(state.suite === k)); tab.setAttribute('aria-controls', `${k}-view`); tab.tabIndex = state.suite === k ? 0 : -1;
        tab.append(node('span', 'suite-name', name), node('span', 'suite-desc', sub()));
        tab.addEventListener('click', () => selectSuite(k));
        tab.addEventListener('keydown', event => { const to = {ArrowLeft: 'macro', ArrowRight: 'micro', Home: 'macro', End: 'micro'}[event.key]; if (to) { event.preventDefault(); selectSuite(to); } });
        return tab;
    }));
}
function render() {
    scrollers = [];
    el('nav-board').href = boardHash(); el('nav-workflows').href = workflowsHash(); el('nav-operations').href = operationHash();
    renderTabs();
    for (const k of Object.keys(SUITES)) el(`${k}-view`).hidden = el(`${k}-more`).hidden = state.suite !== k;
    if (state.suite === 'macro') renderMacro(); else renderMicro();
    syncScrollHints();
}

for (const id of ['doc-nav', 'doc-scoring', 'doc-macro', 'doc-footer']) el(id).href = el(id).getAttribute('href').replace(/^README\.md/, DOCS);
if (PROD) el('nav-run').remove();  // production has no run wizard
wireDetails(); wireMicro(); wireMacro();
window.addEventListener('resize', syncScrollHints);
renderDataset(); renderStatusRules();
const routeKey = () => `${state.suite}/${state.rank}/${state.workflow}/${state.op}/${[...state.campaign]}`;
window.addEventListener('hashchange', () => {
    const before = routeKey(), own = window.location.hash === ownHash; ownHash = null;
    applyHash(); if (before !== routeKey()) render(); if (!own) reveal();
});
applyHash();
render();
reveal();
