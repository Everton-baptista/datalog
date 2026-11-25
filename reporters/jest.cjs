'use strict';

module.exports = class DatalogJestReporter {
  constructor(_config, options = {}) { this.options = options; }
  async onRunComplete(_contexts, result) {
    const { default: summarize } = await import('./summary.mjs');
    const cases = result.testResults.flatMap(file => file.testResults.map(test => ({
      name: test.fullName,
      status: ['passed', 'failed'].includes(test.status) ? test.status : 'skipped',
      durationMs: test.duration ?? null,
    })));
    summarize('jest', this.options, cases, result.numRuntimeErrorTestSuites ?? 0);
  }
};
