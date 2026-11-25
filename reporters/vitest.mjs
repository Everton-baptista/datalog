import summarize from './summary.mjs';

export default class DatalogVitestReporter {
  constructor(options = {}) { this.options = options; }
  onTestRunEnd(modules, errors) {
    const cases = modules.flatMap(module => [...module.children.allTests()].map(test => {
      const state = test.result().state;
      return {
        name: test.fullName,
        status: ['passed', 'failed'].includes(state) ? state : 'skipped',
        durationMs: test.diagnostic()?.duration ?? null,
      };
    }));
    summarize('vitest', this.options, cases, errors.length);
  }
}
