// Usage: node reference/provider-1.5-probe.mjs /path/to/node_modules/@earendil-works/pi-ai
// Capture request fields only. No account, SDK traffic or generated expected literals.
import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
const pkg = resolve(process.argv[2]);
const {name, version} = JSON.parse(readFileSync(resolve(pkg, 'package.json')));
if (name !== '@earendil-works/pi-ai' || version !== '1.5.2') throw Error('Unexpected reference package');
async function payload(api, model, context, options) {
  const {streamSimple} = await import(pathToFileURL(resolve(pkg, `dist/api/${api}.js`)));
  let body;
  await streamSimple(model, context, {apiKey:'fixture', ...options, maxRetries:0,
    fetch:async()=>{throw Error('Unexpected fetch')}, onPayload(params){body=params; throw Error('Captured');}
  }).result();
  if (!body) throw Error(`No ${api} payload`);
  return body;
}
const effort = {}, schemas = {}, chatTools = {};
for (const fixture of ['session-anthropic-native-tools.json', 'session-anthropic-oauth-effort.json']) {
  const f = JSON.parse(readFileSync(new URL(`../compat/provider-fixtures/${fixture}`, import.meta.url)));
  const model = {...f.model, provider:'anthropic', baseUrl:'https://fixture.invalid'};
  const opts = {...f.options, apiKey:f.oauth?'sk-ant-oat-fixture':'fixture'};
  const body = await payload(model.api, model, f.context, opts);
  effort[fixture] = body.output_config.effort;
  if (f.oauth) continue;
  for (const k of ['$defs', 'definitions']) {
    const ctx = structuredClone(f.context);
    ctx.messages[0].toolsAdded[0].parameters = {type:'object', properties:{size:{$ref:`#/${k}/Size`}}, [k]:{Size:{type:'integer'}}, allOf:[{type:'object'}]};
    schemas[k] = (await payload(model.api, model, ctx, opts)).tools[0].input_schema;
  }
  const chat = {...model, id:'test-model', api:'openai-completions', provider:'openai-compatible'};
  const messages = f.context.messages.filter(m => ['user','assistant','toolResult'].includes(m.role));
  for (const flag of [undefined, false, true]) {
    const body = await payload(chat.api, {...chat, compat:{requiresToolsWithToolHistory:flag}}, {messages}, {});
    chatTools[String(flag)] = Object.hasOwn(body,'tools') ? body.tools : null;
  }
}
console.log(JSON.stringify({source:`${name}@${version}`, effort, schemas, chatTools}, null, 2));
