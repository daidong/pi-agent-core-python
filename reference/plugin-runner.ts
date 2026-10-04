// Run the pinned upstream resource functions on the shared plugin cases.
import { readFileSync } from 'node:fs';
import { resolve, relative } from 'node:path';
import { expandPromptTemplate, loadPromptTemplates, parseCommandArgs, substituteArgs } from './pi/packages/coding-agent/src/core/prompt-templates.ts';
import { formatSkillsForPrompt, loadSkillsFromDir } from './pi/packages/coding-agent/src/core/skills.ts';
import { parseFrontmatter } from './pi/packages/coding-agent/src/utils/frontmatter.ts';
const cases = JSON.parse(readFileSync(process.argv[2], 'utf8'));
const root = process.cwd();
const rel = (p: string) => relative(root, p).split('\\').join('/');
const skills = loadSkillsFromDir({ dir: resolve(cases.skills_dir), source: 'path' });
const prompts = loadPromptTemplates({ cwd: root, agentDir: resolve('reference/.no-agent-dir'), promptPaths: [resolve(cases.prompts_dir)], includeDefaults: false });
console.log(JSON.stringify({
 substitute: cases.substitute.map(([content, args]: [string, string[]]) => substituteArgs(content, args)),
 parse_args: cases.parse_args.map((text: string) => parseCommandArgs(text)),
 expand: cases.expand.map((c: any) => expandPromptTemplate(c.text, c.templates)),
 frontmatter: cases.frontmatter.map((text: string) => { try { return parseFrontmatter(text); } catch { return { error: true }; } }),
 skills: {
  skills: skills.skills.map((s) => ({ name: s.name, description: s.description, path: rel(s.filePath), disable_model_invocation: s.disableModelInvocation })),
  diagnostics: skills.diagnostics.map((d) => ({ type: d.type, message: d.message, path: rel(d.path ?? '') })),
 },
 skills_prompt: formatSkillsForPrompt(skills.skills).split(root).join('<ROOT>'),
 prompts: prompts.templates.map((t) => ({ name: t.name, description: t.description, argument_hint: t.argumentHint ?? null, content: t.content, path: rel(t.filePath) })),
}, null, 1));
