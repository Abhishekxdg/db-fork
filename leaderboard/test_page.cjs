// Run from the repository root: node leaderboard/test_page.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

// Only the DOM operations used by the page; execute its real script unchanged.
class Element {
    constructor(tag) { this.tag = tag; this.children = []; this.style = {}; }
    append(...children) { this.children.push(...children); }
    replaceChildren(...children) { this.children = children; }
    set textContent(text) { this.children = [String(text)]; }
    get textContent() { return this.children.map(c => typeof c === 'string' ? c : c.textContent).join(''); }
    addEventListener() {}
    child(tag) { const node = new Element(tag); this.append(node); return node; }
    insertRow() { return this.child('tr'); }
    insertCell() { return this.child('td'); }
    createTHead() { return this.child('thead'); }
    createTBody() { return this.child('tbody'); }
}
const elements = new Map();
const document = {
    createElement: tag => new Element(tag),
    getElementById: id => {
        if (!elements.has(id)) elements.set(id, new Element('div'));
        return elements.get(id);
    },
};
const suite = JSON.parse(fs.readFileSync(`${__dirname}/suite.json`, 'utf8'));
const entry = (system, value, tags, mock = false) => ({
    system, machine: 'test', hosted: 'no', date: '2026-09-30', source: 'test.json', tags, mock,
    basis: 'Illustrative only', result: suite.rows.map(() => [value, value, value]),
    load_time: value, provision_time: value,
});
const data = [entry('Tagless', 0.03, []), entry('Measured', 0.06, ['sql']), entry('Mock', 0.001, ['sql'], true)];
const context = vm.createContext({document, suite, data, excluded: []});
const run = source => vm.runInContext(source, context);
const script = fs.readFileSync(`${__dirname}/index.html`, 'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];
run(script);
const descendants = node => [node, ...node.children.flatMap(c => typeof c === 'string' ? [] : descendants(c))];
const byClass = name => descendants(elements.get('groups')).filter(e => e.className === name);

assert.equal(run('FILTERS.tags(data[0])'), true, 'All includes tagless entries');
run('state.tag.sql = false; render()');
assert.equal(run('FILTERS.tags(data[0])'), false, 'Turning all tags off excludes tagless entries too');
run('state.tag.sql = true');
for (const view of ['branch', 'data', 'load']) {
    run(`state.view = '${view}'; render()`);
    const withMock = byClass('summary-number').map(e => e.textContent);
    assert.equal(withMock.length, 2, 'Mocks never earn a summary rank');
    assert.match(withMock[0], /×1\.00/, 'Fastest measurement, not mock, sets the baseline');
    assert.match(withMock[1], view === 'load' ? /×2\.00/ : /×1\.75/);
    const preview = byClass('details').at(-1);
    assert.match(preview.textContent, /Mock/);
    assert.match(preview.textContent, /0\.001s/);
    assert.doesNotMatch(preview.textContent, /\(×/, 'Mock values have no comparison ratios');
    run('state.kind.mock = false; render()');
    assert.deepEqual(byClass('summary-number').map(e => e.textContent), withMock,
        'Toggling mocks cannot change measured scores');
    run('state.kind.mock = true');
}

// A populated mock must not fill a missing measured row or crash null baselines.
data[0].result = data[1].result = suite.rows.map(() => [null, null, null]);
run("state.view = 'branch'; render()");
assert.equal(byClass('summary-number').length, 0);
assert.match(elements.get('groups').textContent, /Not ranked, because no result/);
run('state.kind.measured = false; render()');
assert.equal(byClass('summary').length, 0, 'Mock-only filters never produce a ranking');
assert.match(elements.get('groups').textContent, /Mock preview \(not ranked\)/);
assert.doesNotMatch(elements.get('groups').textContent, /Not ranked, because no result/);
console.log('Page regression checks passed');
