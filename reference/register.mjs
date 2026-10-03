import { registerHooks } from 'node:module';
registerHooks({resolve(specifier, context, nextResolve) {
 if (specifier === '@earendil-works/pi-ai') return {url:new URL('./pi/packages/ai/src/index.ts',import.meta.url).href,shortCircuit:true};
 if (specifier === '@earendil-works/pi-ai/compat') return {url:new URL('./pi/packages/ai/src/compat.ts',import.meta.url).href,shortCircuit:true};
 return nextResolve(specifier,context);
}});
