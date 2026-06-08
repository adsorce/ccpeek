(function(global) {
    function createSearchInputController(options) {
        const delayMs = options.delayMs;
        const onRun = options.onRun;
        const setTimer = options.setTimer || global.setTimeout.bind(global);
        const clearTimer = options.clearTimer || global.clearTimeout.bind(global);

        let timerId = null;
        let pendingValue = '';
        let lastIssuedValue = null;

        function flush() {
            timerId = null;
            if (pendingValue === lastIssuedValue) return;
            lastIssuedValue = pendingValue;
            onRun(pendingValue);
        }

        return {
            schedule(value) {
                pendingValue = value;
                if (timerId !== null) clearTimer(timerId);
                timerId = setTimer(flush, delayMs);
            },
            cancel() {
                if (timerId !== null) {
                    clearTimer(timerId);
                    timerId = null;
                }
            },
            resetIssuedValue() {
                lastIssuedValue = null;
            },
        };
    }

    if (typeof module !== 'undefined' && module.exports) {
        module.exports = { createSearchInputController };
        return;
    }

    global.createSearchInputController = createSearchInputController;
})(typeof window !== 'undefined' ? window : globalThis);
