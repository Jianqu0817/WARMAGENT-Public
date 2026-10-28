/** Durable tool images for the Python-owned main model; never runs an LLM. */
import { createHash } from 'node:crypto';
import { mkdir, writeFile } from 'node:fs/promises';
import { join } from 'node:path';

export async function mountVision(ctx, load, route) {
  if (!route?.enabled || route.model !== 'deepseek-flash' ||
      !['https://api.deepseek.com', 'https://api.deepseek.com/v1'].includes(route.endpoint?.replace(/\/$/, '')))
    return undefined;
  const { LlmAdapter } = await load('dsh-llm');
  class MainModelMetadata extends LlmAdapter {
    async resolveModel(provider, model) {
      if (model !== route.model) throw Error('vision_route_mismatch');
      return { id: model, provider, name: model, inputModalities: ['text', 'image'] };
    }
    async *stream() { throw Error('model_dispatch_owned_by_warmagent'); }
  }
  ctx.llm.registerAdapter(['warmagent-main'], new MainModelMetadata());
  const attachment = await load('dsh-attachment-local');
  await ctx.plugin(attachment.default, { dshHome: route.root });
  return { provider: 'warmagent-main', model: route.model };
}

export async function projectToolImages(ctx, result, root) {
  const images = (result.content || []).filter(part => part.type === 'image' && part.attachment);
  if (!images.length) return result;
  const records = images.map(({ attachment: ref }) => ({
    file: ctx.attachments.imageHostPath(ref), sha256: String(ref.attachmentId).slice(7),
    media_type: ref.mediaType, bytes: ref.bytes, width: ref.width, height: ref.height,
  }));
  const body = JSON.stringify(records);
  const sha256 = createHash('sha256').update(body).digest('hex');
  const directory = join(root, 'observations');
  await mkdir(directory, { recursive: true });
  const file = join(directory, sha256 + '.json');
  await writeFile(file, body);
  // MCP keeps its raw wire payload in value. The durable attachment replaces
  // inline image bytes in receipts; text and other structured fields survive.
  const value = result.value?.content ? { ...result.value, content: result.value.content.map(part =>
    part.type === 'image' ? { type: 'image', stored: true } : part) } : result.value;
  return { ...result, value, image_manifest: { file, sha256, image_count: records.length } };
}
