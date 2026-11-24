import { writeFileSync } from 'node:fs';

export default function summarize(runner, options, cases, errors) {
  const counts = { passed: 0, failed: 0, skipped: 0 };
  for (const item of cases) counts[item.status] += 1;
  const evaluated = counts.passed + counts.failed;
  const result = {
    version: 1, runner, evaluation: options.name ?? 'Datalog', url: options.url ?? null,
    ...counts, runnerErrors: errors, passRate: evaluated ? counts.passed / evaluated : null, cases,
  };
  if (options.outputFile) writeFileSync(options.outputFile, JSON.stringify(result, null, 2) + '\n', { mode: 0o600 });
  process.stdout.write(`\n${result.evaluation} · ${runner}: ${counts.passed}/${evaluated} passed, ${counts.skipped} skipped, ${errors} runner errors\n`);
}
