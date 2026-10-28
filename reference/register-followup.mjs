// Provider/plugin differential reference only; the core runner still uses v1.0.0.
import { registerHooks } from 'node:module';
const oldRoot = new URL('./pi/', import.meta.url).href;
const root = new URL('./pi-followup/', import.meta.url).href;
registerHooks({resolve(specifier, context, nextResolve) {
 if (specifier === '@earendil-works/pi-ai') return {url:root+'packages/ai/src/index.ts',shortCircuit:true};
 if (specifier === '@earendil-works/pi-ai/compat') return {url:root+'packages/ai/src/compat.ts',shortCircuit:true};
 const resolved = nextResolve(specifier, context);
 if (resolved.url.startsWith(oldRoot)) return {...resolved,url:root+resolved.url.slice(oldRoot.length)};
 return resolved;
}});
