const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');

test('exported messages include the same timestamp formatter as visible messages', () => {
    assert.match(html, /function formatTimestamp\(timestamp\) \{\s*return timestamp \? new Date\(timestamp\)\.toLocaleString\(\) : '';\s*\}/);
    assert.match(html, /const timestamp = formatTimestamp\(last\.msg\.timestamp\);/);
    assert.match(html, /const timestamp = formatTimestamp\(msg\.timestamp\);/);
    assert.match(html, /\*\*Timestamp:\*\* \$\{timestamp\}\\n\\n/);
});
