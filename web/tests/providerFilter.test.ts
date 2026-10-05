/**
 * 「提供者按云端/本地分组过滤」的回归测试。
 *
 * 跑法（项目已有同样做法：脚本用 `node --experimental-strip-types` 跑 TS）：
 *
 *     node --experimental-strip-types --test web/tests/providerFilter.test.ts
 *
 * ## 为什么要有这些测试
 *
 * 这段逻辑我改错了**三次**，而且每次都很难靠肉眼看界面发现：
 *
 *   1. 模式键拼错（`image_gen_provider_mode`）→ 图像/视频**完全不过滤**，
 *      而 `llm` / `tts` 恰好是对的 —— "只改对一半"；
 *   2. 只过滤**显示名**没过过滤**选项** → 选项还在，用户照样能选错，
 *      而且被滤掉显示名的会退化成内部 key，比原来更难懂；
 *   3. 字段级覆盖落在模式之外时，当前值**从下拉里消失** → 下拉退回显示
 *      第一个选项，用户看到一个并非自己配置的值，也改不回来。
 *
 * 这些都不是"能不能跑起来"的问题，而是**语义正确性**，只有断言能守住。
 */

import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  allowedOptionsFor,
  allowedProviders,
  capabilityOf,
  modeKeyOf,
  modeOf,
  optionLabelsFor,
  restrictionFor,
  type ProviderInfo,
} from '../src/settings/providerFilter.ts';

/** 一份贴近真实下发的提供者清单（取自 /api/providers 的形状）。 */
const P = (key: string, label: string, capability: string, is_local: boolean): ProviderInfo => ({
  key,
  label,
  capability,
  is_local,
  api_style: 'openai',
});

const PROVIDERS: ProviderInfo[] = [
  P('local', '本地推理 local-gen（零成本）', 'image_gen', true),
  P('dashscope', '阿里百炼（通义万相 / 千问）', 'image_gen', false),
  P('minimax', 'MiniMax', 'image_gen', false),
  P('replicate', 'Replicate', 'image_gen', false),
  // 视频：注意 local 与 ffmpeg 都算"本地/零成本"
  P('local', '本地 MiniMax H3（零成本）', 'video_gen', true),
  P('ffmpeg', 'FFmpeg 本地合成（免费）', 'video_gen', true),
  P('minimax', 'MiniMax（Hailuo）', 'video_gen', false),
  P('seedance', 'Seedance（火山方舟）', 'video_gen', false),
  // LLM
  P('deepseek', 'DeepSeek', 'llm', false),
  P('openai', 'OpenAI', 'llm', false),
  P('ollama', '本地 Ollama', 'llm', true),
  P('vllm', '本地 vLLM', 'llm', true),
];

// ------------------------------------------------------------------
// 键名映射（错误 #1 的回归）
// ------------------------------------------------------------------

test('modeKeyOf 对每个能力都给出后端**真实存在**的键名', () => {
  // 这些就是后端 MODE_KEY_BY_CAPABILITY 的值
  assert.equal(modeKeyOf('llm'), 'llm_provider_mode');
  assert.equal(modeKeyOf('image'), 'image_provider_mode');
  assert.equal(modeKeyOf('video'), 'video_provider_mode');
  assert.equal(modeKeyOf('tts'), 'tts_provider_mode');
  assert.equal(modeKeyOf('lora'), 'lora_train_provider_mode');
  // 传能力名（CAP_GROUPS.cap）也必须落到同一个键 —— 第 1 次就是这么错的
  assert.equal(modeKeyOf('image_gen'), 'image_provider_mode');
  assert.equal(modeKeyOf('image_edit'), 'image_provider_mode');
  assert.equal(modeKeyOf('lora_train'), 'lora_train_provider_mode');
});

test('modeOf 能读到真实设置里的模式值', () => {
  // 用户那次的现象：后端存了 image_provider_mode=local，界面读成 unset
  const settings = { image_provider_mode: 'local', llm_provider_mode: 'cloud' };
  assert.equal(modeOf('image', settings), 'local');
  assert.equal(modeOf('image_gen', settings), 'local');
  assert.equal(modeOf('llm', settings), 'cloud');
  assert.equal(modeOf('video', settings), 'unset'); // 没有这个键 → unset
});

test('capabilityOf 把短前缀映射到接口的能力名', () => {
  assert.equal(capabilityOf('image'), 'image_gen');
  assert.equal(capabilityOf('lora'), 'lora_train');
  assert.equal(capabilityOf('llm'), 'llm');
  assert.equal(capabilityOf('video'), 'video_gen');
});

// ------------------------------------------------------------------
// 过滤（用户要的核心行为）
// ------------------------------------------------------------------

test('模式=local 时只列本地/零成本提供者', () => {
  const s = { image_provider_mode: 'local' };
  const got = allowedProviders('image', 'image_provider_character_designer', s, PROVIDERS);
  assert.deepEqual(got.map((p) => p.key), ['local']);

  const v = allowedProviders('video', 'video_provider', { video_provider_mode: 'local' }, PROVIDERS);
  assert.deepEqual(
    v.map((p) => p.key).sort(),
    ['ffmpeg', 'local'],
    'ffmpeg 是本地合成，也算本地档',
  );
});

test('模式=cloud 时只列云端提供者', () => {
  const s = { image_provider_mode: 'cloud' };
  const got = allowedProviders('image', 'image_provider_character_designer', s, PROVIDERS);
  assert.deepEqual(got.map((p) => p.key), ['dashscope', 'minimax', 'replicate']);
});

test('模式=unset 时列全部（用户还没选，不该限制他先看什么）', () => {
  const got = allowedProviders('llm', 'llm_provider_writer', {}, PROVIDERS);
  assert.deepEqual(
    got.map((p) => p.key).sort(),
    ['deepseek', 'ollama', 'openai', 'vllm'],
  );
  // 两档同时出现 → 必须加前缀区分，否则用户看不出哪个是本地
  const labels = optionLabelsFor('llm', 'llm_provider_writer', {}, PROVIDERS);
  assert.equal(labels.ollama, '本地 · 本地 Ollama');
  assert.equal(labels.deepseek, '云端 · DeepSeek');
});

test('只有一档时不加前缀（加了是噪音）', () => {
  const labels = optionLabelsFor('image', 'image_provider_photographer', { image_provider_mode: 'local' }, PROVIDERS);
  assert.equal(labels.local, '本地推理 local-gen（零成本）');
});

// ------------------------------------------------------------------
// 字段级覆盖压过模式（错误 #3 的回归）
// ------------------------------------------------------------------

test('字段存了显式值时，按**那个值**判定归属（它压过模式）', () => {
  // 后端解析优先级：字段 > 全局 > 模式
  const s = { image_provider_mode: 'cloud', image_provider_character_designer: 'local' };
  assert.equal(
    restrictionFor('image', 'image_provider_character_designer', s, PROVIDERS),
    'local',
    '字段是 local，就不能按模式 cloud 判',
  );
  assert.equal(restrictionFor('image', 'image_provider_photographer', s, PROVIDERS), 'cloud');
});

test('字段的显式值**必须留在选项里**，即使与模式相反', () => {
  // 这是第 3 次错误：模式 cloud、字段 local → 按模式过滤把当前值滤掉了，
  // 原生下拉退回显示第一个选项，用户看到并非自己配置的值、也改不回来。
  const s = { video_provider_mode: 'cloud', video_provider: 'local' };
  const got = allowedProviders('video', 'video_provider', s, PROVIDERS);
  assert.ok(
    got.some((p) => p.key === 'local'),
    '当前值 local 必须在选项里',
  );
  // 且它排在最前，保证原生下拉默认显示的就是真实值
  assert.equal(got[0].key, 'local');
});

test('占位符 `(按模式)` 与 `(自动)` 都表示"跟随模式"', () => {
  const s = { video_provider_mode: 'local', video_provider: '(按模式)' };
  assert.equal(restrictionFor('video', 'video_provider', s, PROVIDERS), 'local');

  const s2 = { llm_provider_mode: 'cloud', llm_provider_writer: '(自动)' };
  assert.equal(restrictionFor('llm', 'llm_provider_writer', s2, PROVIDERS), 'cloud');
});

// ------------------------------------------------------------------
// 白名单（错误 #2 的回归）
// ------------------------------------------------------------------

test('白名单只收窄提供者字段，模式字段不受限', () => {
  const s = { image_provider_mode: 'local' };
  const opts = allowedOptionsFor(
    { type: 'select', key: 'image_provider_character_designer' },
    'image',
    s,
    PROVIDERS,
  );
  assert.deepEqual(opts, ['(自动)', 'local'], '选项本身必须被收窄，不只是显示名');

  // 模式字段套白名单会被清空 → 下拉塌成一条缝（实际发生过）
  assert.equal(
    allowedOptionsFor({ type: 'select', key: 'image_provider_mode' }, 'image', s, PROVIDERS),
    undefined,
  );
  // 非提供者的 select（如时长）不限制
  assert.equal(
    allowedOptionsFor({ type: 'select', key: 'duration' }, 'video', s, PROVIDERS),
    undefined,
  );
});

test('`tts_provider` / `video_provider` 这类**不含** `_provider_` 的也要被收窄', () => {
  // 判据写成 includes('_provider_') 会漏掉它们（tts_provider 结尾没有下划线）
  const s = { tts_provider_mode: 'cloud' };
  const opts = allowedOptionsFor({ type: 'select', key: 'tts_provider' }, 'tts', s, [
    P('edge', 'Edge TTS（免费兜底）', 'tts', true),
    P('dashscope', '阿里 CosyVoice', 'tts', false),
  ]);
  assert.deepEqual(opts, ['(自动)', 'dashscope']);
});

test('`(按模式)` 占位符必须留在白名单里（用户报的"切了第二项不变"）', () => {
  // 我漏过这一次：白名单写成 ['(自动)', ...提供者]，于是 video/tts 的
  // "(按模式)" 被滤掉 → 用户**没法把覆盖清回跟随模式**，
  // 一旦显式选过就被钉死；而原生 select 在当前值不在选项里时会
  // 退回显示第一个选项，界面显示的值与后端存的完全不是一回事。
  const st = { video_provider_mode: 'cloud', video_provider: '(按模式)' };
  const opts = allowedOptionsFor(
    { type: 'select', key: 'video_provider', options: ['(按模式)', 'local', 'minimax'] },
    'video',
    st,
    PROVIDERS,
  );
  assert.ok(opts, '应当给出白名单');
  assert.ok(opts.includes('(按模式)'), `(按模式) 必须可选，实际 ${JSON.stringify(opts)}`);
  // 模式=cloud → 只列云端，且带占位符
  assert.deepEqual(opts, ['(按模式)', 'minimax', 'seedance'], JSON.stringify(opts));
});

test('`(自动)` 字段同样保留 `(自动)`', () => {
  const opts = allowedOptionsFor(
    { type: 'select', key: 'llm_provider_writer', options: ['(自动)', 'deepseek', 'ollama'] },
    'llm',
    { llm_provider_mode: 'local' },
    PROVIDERS,
  );
  assert.deepEqual(opts, ['(自动)', 'ollama', 'vllm'], JSON.stringify(opts));
});

test('label 自带来源前缀时不再叠加（避免 `本地 · 本地 · X`）', () => {
  // 后端的本地提供者名字本身就以 `本地 · ` 开头（如 `本地 · Z-Image`），
  // 而模式未选时前端还会加前缀 —— 无条件加就变成 `本地 · 本地 · Z-Image`。
  const list: ProviderInfo[] = [
    P('local', '本地 · Z-Image', 'image_gen', true),
    P('dashscope', '阿里百炼（通义万相 / 千问）', 'image_gen', false),
  ];
  const labels = optionLabelsFor('image', 'image_provider_photographer', {}, list);
  assert.equal(labels.local, '本地 · Z-Image', `不该重复加前缀: ${labels.local}`);
  assert.equal(labels.dashscope, '云端 · 阿里百炼（通义万相 / 千问）');
  // 只有一档时同样不叠加
  const one = optionLabelsFor('image', 'image_provider_photographer', { image_provider_mode: 'local' }, list);
  assert.equal(one.local, '本地 · Z-Image');
});

test('非 openai 协议的提供者永远不列（选了必然失败）', () => {
  const list: ProviderInfo[] = [
    { ...P('claude', 'Anthropic Claude', 'llm', false), api_style: 'unverified' },
    P('deepseek', 'DeepSeek', 'llm', false),
  ];
  const got = allowedProviders('llm', undefined, { llm_provider_mode: 'cloud' }, list);
  assert.deepEqual(got.map((p) => p.key), ['deepseek']);
});

test('本地与云端两档**永无交集**（用户的核心诉求）', () => {
  for (const cap of ['llm', 'image', 'video']) {
    const local = new Set(
      allowedProviders(cap, undefined, { [`${cap}_provider_mode`]: 'local' }, PROVIDERS).map((p) => p.key),
    );
    const cloud = new Set(
      allowedProviders(cap, undefined, { [`${cap}_provider_mode`]: 'cloud' }, PROVIDERS).map((p) => p.key),
    );
    // image 的字段前缀是 image_，模式键却是 image_provider_mode —— 用 modeKeyOf 统一
    const l2 = new Set(
      allowedProviders(cap, undefined, { [modeKeyOf(cap)]: 'local' }, PROVIDERS).map((p) => p.key),
    );
    const c2 = new Set(
      allowedProviders(cap, undefined, { [modeKeyOf(cap)]: 'cloud' }, PROVIDERS).map((p) => p.key),
    );
    for (const s of [local, cloud, l2, c2]) assert.ok(s.size > 0, `${cap} 某一档为空`);
    const inter = [...l2].filter((k) => c2.has(k));
    assert.deepEqual(inter, [], `${cap} 的本地与云端出现交集: ${inter}`);
  }
});
