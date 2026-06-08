const test = require('node:test');
const assert = require('node:assert/strict');

const {
    createSearchInputController,
} = require('../search-input-controller.js');

test('search input controller collapses rapid typing into one search', async () => {
    const calls = [];
    const controller = createSearchInputController({
        delayMs: 40,
        onRun(value) {
            calls.push(value);
        },
    });

    controller.schedule('m');
    controller.schedule('ma');
    controller.schedule('mai');
    controller.schedule('main');

    await new Promise(resolve => setTimeout(resolve, 70));

    assert.deepEqual(calls, ['main']);
});

test('search input controller skips duplicate issued values', async () => {
    const calls = [];
    const controller = createSearchInputController({
        delayMs: 20,
        onRun(value) {
            calls.push(value);
        },
    });

    controller.schedule('main');
    await new Promise(resolve => setTimeout(resolve, 40));
    controller.schedule('main');
    await new Promise(resolve => setTimeout(resolve, 40));

    assert.deepEqual(calls, ['main']);
});
