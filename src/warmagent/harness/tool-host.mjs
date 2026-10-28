/** Direct DSH tool host. It creates Session Agents for native tools and never runs a model. */
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import { isAbsolute, join, resolve } from 'node:path';
import { readFile, readdir } from 'node:fs/promises';
import { createInterface } from 'node:readline';
import { mountNativeServices, mountNativeTools } from './native-tools.mjs';
import { mountVision, projectToolImages } from './vision.mjs';

const root = process.argv[2];
const nativeConfigPath = process.argv[3];
const defaultDirectory = process.argv[4];
const vision = JSON.parse(process.argv[5] || '{}');
const out = value => process.stdout.write(JSON.stringify(value) + '\n');
const require = createRequire(join(root, 'package.json'));
let packageRequire, packageAnchors;
const load = async name => {
  const packageName = '@deepseek-ai/' + name;
  if (!packageRequire) {
    const dirs = await readdir(join(root, 'node_modules', '.pnpm'));
    const entry = dirs.find(dir => dir.startsWith('@deepseek-ai+dsh-agent-loop'));
    if (!entry) throw Error('harness_package_unavailable');
    packageRequire = createRequire(join(root, 'node_modules', '.pnpm', entry,
      'node_modules', '@deepseek-ai', 'dsh-agent-loop', 'package.json'));
  }
  let anchor;
  try { anchor = packageRequire.resolve(packageName + '/package.json'); }
  catch {
    if (!packageAnchors) {
      packageAnchors = new Map();
      const dirs = await readdir(join(root, 'node_modules', '.pnpm'));
      for (const dir of dirs.filter(value => value.startsWith('@deepseek-ai+'))) {
        const base = join(root, 'node_modules', '.pnpm', dir, 'node_modules', '@deepseek-ai');
        for (const item of await readdir(base))
          if (!packageAnchors.has(item)) packageAnchors.set(item, join(base, item, 'package.json'));
      }
    }
    anchor = packageAnchors.get(name);
  }
  if (!anchor) throw Error('harness_package_unavailable');
  const packageLocalRequire = createRequire(anchor);
  const version = packageLocalRequire(anchor).version;
  const expectedVersion = name === 'cordis' ? '4.0.5' : '0.2.1-rc.2';
  if (version !== expectedVersion) throw Error(`harness_version_mismatch:${name}:${version}`);
  return import(pathToFileURL(packageLocalRequire.resolve(packageName)).href);
};

let ctx;
let makeSessionId;
let modelRoute;
const agents = new Map();
const backgroundJobs = new Map();
const earlyJobEvents = new Map();
try {
  const [{ Context }, { default: Session, SessionId }, { default: SessionProjection },
    { default: SystemPrompt }, { default: ToolRuntime }, { default: AgentLoop },
    { default: AgentRegistry }, { default: LlmRuntime }, { scopeOf }] = await Promise.all([
    load('cordis'), load('dsh-session'), load('dsh-session-projection'),
    load('dsh-system-prompt'), load('dsh-tools'), load('dsh-agent-loop'),
    load('dsh-agent'), load('dsh-llm'), load('dsh-scope'),
  ]);
  makeSessionId = SessionId;
  const version = require('@deepseek-ai/dsh/package.json').version;
  if (version !== '0.2.1-rc.2') throw Error('harness_version_mismatch');
  const nativeConfig = nativeConfigPath ? JSON.parse(await readFile(nativeConfigPath, 'utf8')) : {};
  if (!defaultDirectory || !isAbsolute(defaultDirectory)) throw Error('harness_default_directory_invalid');
  ctx = new Context();
  await ctx.plugin(Session);
  await ctx.plugin(SessionProjection);
  await ctx.plugin(SystemPrompt, { includeRuntimeContext: false });
  await ctx.plugin(ToolRuntime);
  await ctx.plugin(AgentRegistry);
  await ctx.plugin(LlmRuntime);
  await ctx.plugin(AgentLoop);
  await mountNativeServices(ctx, load, nativeConfig, defaultDirectory);
  modelRoute = await mountVision(ctx, load, vision);
  await mountNativeTools(ctx, load, nativeConfig, Boolean(modelRoute));
  // Native DSH job events are separate from tool execution replies.
  ctx.jobs.events.subscribe({ owners: 'scope' }, event => {
    if (event.type !== 'settled' || event.job.owner === undefined ||
        event.job.kind !== 'pwsh' || event.cause === 'teardown') return;
    const metadata = backgroundJobs.get(event.job.id);
    if (!metadata) {
      earlyJobEvents.set(event.job.id, event);
      return;
    }
    publishJobEvent(event, metadata);
  });
  if (nativeConfig.biliHumanism?.enabled === true) {
    const { enabled, ...options } = nativeConfig.biliHumanism;
    const plugin = await import(pathToFileURL(require.resolve('dsh-bili-humanism')).href);
    await ctx.plugin(plugin, {
      ...options,
      dataDir: resolve(defaultDirectory, options.dataDir || 'var/bili-humanism'),
      ...(options.credentialPath ? { credentialPath: resolve(defaultDirectory, options.credentialPath) } : {}),
    });
  }
  const tools = ctx.tools.schemas(scopeOf(ctx));
  out({ kind: 'ready', version, tools });
} catch {
  out({ kind: 'startup_error', error: 'harness_unavailable' });
  process.exitCode = 1;
}

function publishJobEvent(event, metadata) {
  if (event.job.owner !== metadata.session_id) return;
  let read = { chunks: [], lossy: true };
  try { read = ctx.jobs.readAt(event.job.id, 0, metadata.session_id); } catch {}
  const text = read.chunks.map(chunk => chunk.text).join('');
  const bytes = Buffer.from(text, 'utf8');
  const preview = bytes.length > 4000 ? bytes.subarray(0, 4000).toString('utf8') : text;
  out({ kind: 'job_event', event_id: event.job.id, job_id: event.job.id,
    session_id: metadata.session_id, scope: metadata.scope, task: metadata.task,
    turn: metadata.turn, status: event.job.status, cause: event.cause,
    ...(event.cause === 'kill' ? { effect_note: '停止请求不撤销已发生的副作用。' } : {}),
    detail: event.job.detail, output_preview: preview,
    output_complete: !read.lossy && !event.job.output?.spillPaths?.length,
    output_ref: { spill_paths: event.job.output?.spillPaths || [],
      total_bytes: event.job.output?.total ?? read.next ?? 0,
      earliest_byte: event.job.output?.earliest ?? 0, lossy: read.lossy } });
  backgroundJobs.delete(event.job.id);
}

if (ctx) {
  const lines = createInterface({ input: process.stdin, crlfDelay: Infinity });
  lines.on('line', line => {
    let request;
    try { request = JSON.parse(line); } catch { out({ kind: 'error', error: 'invalid_ipc' }); return; }
    if (request.kind === 'close') {
      void Promise.all([...agents.values()].map(entry => entry.handle.dispose()))
        .then(() => ctx.fiber.dispose()).then(() => process.exit(0));
      return;
    }
    if (request.kind !== 'execute' || typeof request.id !== 'number' ||
        typeof request.name !== 'string' || typeof request.call_id !== 'string' ||
        !request.args || typeof request.args !== 'object' || Array.isArray(request.args) ||
        typeof request.session_id !== 'string' || !request.session_id ||
        typeof request.session_slot !== 'string' || !request.session_slot ||
        typeof request.scope !== 'string' || typeof request.task !== 'string' ||
        typeof request.turn !== 'string') {
      out({ kind: 'error', id: request.id, error: 'invalid_ipc' });
      return;
    }
    void (async () => {
      let entry = agents.get(request.session_slot);
      if (entry?.id !== request.session_id) {
        if (entry) await entry.handle.dispose();
        const handle = await ctx.agents.create({
          sessionId: makeSessionId(request.session_id), meta: { cwd: defaultDirectory },
          ...(modelRoute ? { agentOptions: modelRoute } : {}),
        });
        entry = { id: request.session_id, handle };
        agents.set(request.session_slot, entry);
      }
      const result = await ctx.tools.execute({ agent: entry.handle.agent, callId: request.call_id, name: request.name,
        arguments: request.args, signal: AbortSignal.timeout(request.timeout_ms || 600000) });
      const projected = await projectToolImages(ctx, result, vision.root);
      if (request.name === 'pwsh' && request.args.run_in_background === true &&
          result.value?.kind === 'background' && typeof result.value.jobId === 'string') {
        const metadata = { session_id: request.session_id, scope: request.scope,
          task: request.task, turn: request.turn };
        backgroundJobs.set(result.value.jobId, metadata);
        const early = earlyJobEvents.get(result.value.jobId);
        if (early) {
          earlyJobEvents.delete(result.value.jobId);
          publishJobEvent(early, metadata);
        }
      }
      return projected;
    })()
      .then(result => out({ kind: 'result', id: request.id, result }))
      .catch(() => out({ kind: 'result', id: request.id, error: 'tool_execution_failed' }));
  });
}
