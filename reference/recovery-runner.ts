// Classify shared failed responses with the pinned upstream pi-ai functions.
import { readFileSync } from 'node:fs';
import { isContextOverflow, isRecoverableLength } from './pi/packages/ai/src/utils/overflow.ts';
import { isRetryableAssistantError } from './pi/packages/ai/src/utils/retry.ts';
const cases = JSON.parse(readFileSync(process.argv[2], 'utf8'));
const results = cases.map((c: any) => {
 const usage = { input: c.usage?.input ?? 0, output: c.usage?.output ?? 0, cacheRead: c.usage?.cache_read ?? 0, cacheWrite: 0, totalTokens: 0, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } };
 const message: any = { role: 'assistant', content: [], api: 'mock', provider: c.provider, model: 'mock', usage, stopReason: c.stop_reason, ...(c.error ? { errorMessage: c.error } : {}), timestamp: 0 };
 return {
  overflow: isContextOverflow(message, c.context_window),
  retryable: isRetryableAssistantError(message),
  recoverable_length: c.desired_max_output === undefined ? null : isRecoverableLength(message, c.desired_max_output),
 };
});
console.log(JSON.stringify(results, null, 2));
