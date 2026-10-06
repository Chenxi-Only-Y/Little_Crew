/**
 * 设置页的「提供者按云端/本地分组与过滤」逻辑。
 *
 * ## 为什么单独成模块
 *
 * 这段逻辑出过好几次问题，而它**很难靠肉眼看界面验证**：
 * 设置是异步加载的、下拉是原生控件、过滤结果又依赖"模式"与"字段级覆盖"
 * 两套优先级。我为此在浏览器里反复读 DOM 折腾了十几轮，仍被异步时序误导。
 *
 * 抽出来之后可以用 `node --test` 直接测纯函数（项目已有同样的做法：
 * `scripts/check-validate-parity.mjs` 就是用 `node --experimental-strip-types`
 * 跑 TS）。**逻辑的可验证性比它放在组件里方便，更重要。**
 *
 * ## 两套优先级（这是全部复杂度的来源）
 *
 * 后端 `resolve_provider` 的顺序是：
 *
 *     字段级覆盖（`image_provider_character_designer`）
 *       > 全局字段（`image_provider`）
 *         > 模式（`image_provider_mode`）对应的默认
 *
 * 所以"这个字段现在属于本地还是云端"**不能只看模式** —— 字段可能存了
 * 一个压过模式的显式值。用户报的"啥情况"就是这两者不一致造成的：
 * 模式是 cloud、字段却是 local，按模式过滤就**把当前值过滤掉了**。
 */

/** 一个提供者的最小信息（对应 `/api/providers` 下发的子集）。 */
export interface ProviderInfo {
  key: string;
  label: string;
  capability: string;
  /** 是否"本地 / 零成本"类（后端 `category` 为 local/free） */
  is_local: boolean;
  /** 协议风格；非 `openai` 的客户端调不了，不该列出来 */
  api_style: string;
  /**
   * 产出的视频里**有没有声音**（仅 video_gen 有意义）。
   *
   * ⚠️ 与 `conditioning` 里的 `reference_audio` 不是一回事：
   * 那个是"能不能**喂**音频当参考"（输入），这个是"产出里有没有声音"（输出）。
   */
  has_native_audio?: boolean;
  /**
   * **这台机器上现在能不能用**（后端本机能力探测的结果）。
   *
   * ⚠️ 与 `available` 完全不同：`available` 是"**代码里接没接**"
   * （未接入的提供者它是 False），这个才是"**本机跑不跑得起来**"。
   *
   * `undefined` 当作**可用**（老后端没下发这个字段时不该把云端也滤掉；
   * 而云端本来就恒为 true）。
   */
  runtime_ready?: boolean;
}

/** 值 → 该值属于哪一档。`unset` 表示"没有限制"（列全部）。 */
export type Restriction = 'local' | 'cloud' | 'unset';

/**
 * 表示"没有覆盖、跟随模式"的占位值。
 *
 * 注册表里用过两种写法，**含义相同**：
 *   · `(自动)`   —— LLM / 图像 / TTS / LoRA 的提供者字段
 *   · `(按模式)` —— 视频的 `video_provider`（它只有这一个静态选项，
 *                   真正的候选由后端 `_field_options` 动态补上）
 *
 * 必须都当占位符：否则 `(按模式)` 会被当成一个"真提供者"，在 providers 里
 * 找不到 → 归属判定走偏 → 不过滤。（这个差异让我多查了一轮。）
 */
const PLACEHOLDERS = new Set(['', '(自动)', '(按模式)', 'unset']);

/**
 * 把前端用的短前缀规范成 `/api/providers` 的能力名。
 *
 * ⚠️ 这个映射**必须完整**，漏一个能力名就会让那组下拉失去过滤。
 * 我第一版就漏了 `video → video_gen`（只写了 image 与 lora），
 * 而**恰好视频那组就是用户截图里混档的那组** —— 单元测试当场抓到了它。
 *
 * 对照后端 `ProviderSpec.capability` 的取值：
 *   llm / image_gen / image_edit / video_gen / lora_train / tts / music / render_2d
 */
const CAPABILITY_ALIAS: Record<string, string> = {
  image: 'image_gen',
  image_gen: 'image_gen',
  image_edit: 'image_gen',
  video: 'video_gen',
  video_gen: 'video_gen',
  lora: 'lora_train',
  lora_train: 'lora_train',
};

export function capabilityOf(capPrefix: string): string {
  return CAPABILITY_ALIAS[capPrefix] ?? capPrefix;
}

/**
 * 能力 → 它的"云端 / 本地"模式设置键。
 *
 * ## ⚠️ 这里有个容易搞反的地方（我错过两次）
 *
 * 模式键用的是**短前缀**，不是能力名：
 *
 *     image_provider_mode        ← 不是 image_gen_provider_mode
 *     video_provider_mode        ← 不是 video_gen_provider_mode
 *     lora_train_provider_mode   ← 这个是特例（带 train）
 *     llm_provider_mode  /  tts_provider_mode
 *
 * 所以**不能**先把入参规范化成能力名再拼键名 —— 那样 image/video 就拼错了，
 * 键不存在 → 永远读到 `unset` → 那两组下拉完全不过滤。
 * 只有 `lora` → `lora_train_provider_mode` 这一处需要改写。
 *
 * 反过来 `allowedProviders` 里比较 `p.capability` 时**需要**能力名
 * （`image_gen` / `video_gen`），那是另一件事，用 `capabilityOf`。
 * 两个映射混用正是我反复出错的根源，故此处显式区分。
 */
export function modeKeyOf(capPrefixOrCapability: string): string {
  if (capPrefixOrCapability === 'lora' || capPrefixOrCapability === 'lora_train') {
    return 'lora_train_provider_mode';
  }
  // image / image_gen / image_edit 共用同一个模式键
  if (capPrefixOrCapability === 'image' || capPrefixOrCapability === 'image_edit') {
    return 'image_provider_mode';
  }
  if (capPrefixOrCapability === 'image_gen') return 'image_provider_mode';
  // video / video_gen 共用
  if (capPrefixOrCapability === 'video_gen') return 'video_provider_mode';
  return `${capPrefixOrCapability}_provider_mode`;
}

/** 某能力的当前模式值（`cloud` / `local` / `unset`）。 */
export function modeOf(
  capPrefix: string,
  settings: Record<string, unknown> | null | undefined,
): string {
  return String(settings?.[modeKeyOf(capPrefix)] ?? 'unset');
}

/**
 * 一个提供者字段的**有效**云端/本地归属。
 *
 * 字段存了显式值 → 按**那个值**判定（它压过模式）；
 * 没存（占位符）→ 跟随模式。
 */
export function restrictionFor(
  capPrefix: string,
  fieldKey: string | undefined,
  settings: Record<string, unknown> | null | undefined,
  providers: readonly ProviderInfo[],
): Restriction {
  const own = fieldKey ? String(settings?.[fieldKey] ?? '') : '';
  if (!PLACEHOLDERS.has(own)) {
    const p = providers.find((x) => x.key === own && x.capability === capabilityOf(capPrefix));
    if (p) return p.is_local ? 'local' : 'cloud';
  }
  const m = modeOf(capPrefix, settings);
  return m === 'local' || m === 'cloud' ? m : 'unset';
}

/**
 * 某能力在当前限制下**可选**的提供者。
 *
 * 两件必须同时做的事：
 *   1. 按 `restriction` 只留该档（这就是用户要的"本地和云端分开"）；
 *   2. **把当前值本身放回列表** —— 字段级覆盖可能落在模式之外
 *      （模式 cloud、字段 local）。不放回去的话，原生下拉会退回显示
 *      第一个选项，用户看到一个**并非自己配置**的值，而且改不回来。
 */
export function allowedProviders(
  capPrefix: string,
  fieldKey: string | undefined,
  settings: Record<string, unknown> | null | undefined,
  providers: readonly ProviderInfo[],
): ProviderInfo[] {
  const cap = capabilityOf(capPrefix);
  const r = restrictionFor(capPrefix, fieldKey, settings, providers);
  const base = providers.filter((p) => {
    if (p.capability !== cap) return false;
    // 协议对不上的（如 unverified）不列 —— 选了必然失败
    if (p.api_style !== 'openai') return false;
    // **本机用不了的本地提供者直接不列**（用户："直接不列"）。
    //
    // 为什么必须滤掉而不是标灰：没有本地模型的用户照样能看到
    // 「本地 Ollama」，选了要跑到那一步才失败，报的还是技术性错误 ——
    // 他不知道"是你本机没装"。少列一个能用的只是让他手动找，
    // 列一个用不了的会让他配半天然后失败。
    //
    // ⚠️ `undefined` 当作可用：老后端没下发该字段时**不能**把一切都滤掉。
    if (p.runtime_ready === false) return false;
    if (r === 'local' && !p.is_local) return false;
    if (r === 'cloud' && p.is_local) return false;
    return true;
  });

  const own = fieldKey ? String(settings?.[fieldKey] ?? '') : '';
  if (own && !PLACEHOLDERS.has(own) && !base.some((p) => p.key === own)) {
    // ⚠️ 必须同时按 **key 与 capability** 找：`minimax` 同时在
    // image_gen / video_gen / tts 下登记，只按 key 找会拿到别的能力那条，
    // 于是把不相干的提供者塞进这个下拉。
    const p = providers.find((x) => x.key === own && x.capability === cap);
    if (p) return [p, ...base];
  }
  return base;
}

/**
 * 下拉显示名：值 → 显示文本。
 *
 * 只有**一档**可选时不加前缀（加了是噪音）；两档同时出现（模式未选）时
 * 用 `本地 · ` / `云端 · ` 标出各自属于哪一档。
 *
 * ⚠️ 加前缀前要**先看 label 里有没有** —— 后端的本地提供者名字本身就以
 * `本地 · ` 开头（如 `本地 · Z-Image`），无条件再加就变成
 * `本地 · 本地 · Z-Image`（实际发生过）。
 *
 * 为什么不用 `<optgroup>`：实测 Windows 原生下拉会把 optgroup 标题渲染成
 * **一条白色横条**（用户截图里那个白块），且各平台表现不一。
 * 文字前缀没有这个渲染差异。
 */
export function optionLabelsFor(
  capPrefix: string,
  fieldKey: string | undefined,
  settings: Record<string, unknown> | null | undefined,
  providers: readonly ProviderInfo[],
): Record<string, string> {
  const onlyOne = restrictionFor(capPrefix, fieldKey, settings, providers) !== 'unset';
  const out: Record<string, string> = {};
  for (const p of allowedProviders(capPrefix, fieldKey, settings, providers)) {
    // 后端 label 自带来源前缀时不再叠加
    const hasPrefix = p.label.startsWith('本地 · ') || p.label.startsWith('云端 · ');
    out[p.key] = onlyOne || hasPrefix
      ? p.label
      : p.is_local
        ? `本地 · ${p.label}`
        : `云端 · ${p.label}`;
  }
  return out;
}

/**
 * 「声音来源」的存储值 → 界面显示名。
 *
 * 存储值保持英文 key（后端据此判断），显示名说人话。
 *
 * ⚠️ `native` 用的是「**视频模型原生**」这个措辞（用户明确要求）：
 *    声音是**视频模型自己产出的**，不是我们另外配的。
 */
export const AUDIO_LABELS: Record<string, string> = {
  auto: '自动（按视频模型能力决定）',
  native: '视频模型原生',
  tts: 'TTS 配音',
  mix: '两者混音（配音为主，原生降为背景）',
  none: '不要声音',
};

/**
 * 哪些「声音来源」选项对当前视频提供者**有意义**。
 *
 * ## 为什么必须过滤（用户："配音不是某些视频模型自带吗"）
 *
 * 音频有**两条来源**，而它们会打架：
 *   · 阿里百炼的万相 wan3.0 官方写明「原生带音频」—— 产出里就有对白/环境音；
 *   · TTS 会另外生成一条配音轨。
 *
 * 于是：
 *   · 提供者**没有**原生音频（Seedance / 图片拼接 / 本机 H3）时，
 *     `视频模型原生` 与 `两者混音` **是无意义的选项** —— 选了也拿不到声音。
 *     让它可选比不给选更糟：用户以为配好了，产物却是无声的。
 *   · 提供者**有**原生音频时，`TTS 配音` 仍有意义（想用固定音色念台词），
 *     但要提醒它会盖掉模型生成的声音。
 *
 * `auto` 永远保留：表示"不指定，按提供者能力自动决定"，由后端解析。
 */
export function audioOptionsFor(
  providers: readonly ProviderInfo[],
  capPrefix: string,
  fieldKey: string | undefined,
  settings: Record<string, unknown> | null | undefined,
  all: readonly string[],
  modeDefaults?: Record<string, Record<string, string>> | null,
): string[] {
  const cap = capabilityOf(capPrefix);
  const mode = modeOf(capPrefix, settings);
  const own = fieldKey ? String(settings?.[fieldKey] ?? '') : '';

  /**
   * 解析"**实际会用哪个提供者**"，与后端 `resolve_provider` 的优先级一致：
   *
   *     字段覆盖 > 模式默认
   *
   * ⚠️ 我第一版漏了"模式默认"这一档，退而用"这一档里**全部**提供者都
   *    支持原生音频"来近似 —— 结果是**万相明明支持，界面却说没有**
   *    （因为云端档里还并列着 Seedance）。模式默认只有后端知道，
   *    所以由 `/api/providers` 下发 `mode_defaults`。
   */
  const explicitKey = !PLACEHOLDERS.has(own) && own ? own : '';
  const key = explicitKey || modeDefaults?.[cap]?.[mode] || '';
  const target = key
    ? providers.find((p) => p.key === key && p.capability === cap)
    : undefined;

  // 找不到目标（配置不全 / 还没选模式）时**保守地只给通用选项**：
  // 宁可少给一个可选，也不要在不确定时让用户配出一个无声的产物
  const hasNative = Boolean(target?.has_native_audio);

  return all.filter((o) => {
    if (o === 'auto') return true;
    if (o === 'native' || o === 'mix') return hasNative;
    return true; // tts / none 对任何提供者都适用
  });
}

/**
 * 这个下拉**为什么没有可选的真提供者**（有的话返回 `null`）。
 *
 * ## 为什么需要它
 *
 * "本机用不了的本地提供者直接不列"会带来一个副作用：如果用户把
 * 「云端 / 本地」模式设成了 `local`，而**这台机器上一个本地提供者都没有**
 * （比如本地推理服务没启动），那个提供者下拉里就**只剩一个占位符**
 * —— 用户看到一个空荡荡的框，不知道是自己配错了、还是坏了。
 *
 * 实测踩到过：
 *
 *     lora_train_provider_mode = 'local'  →  lora_train_provider 选项 = ["(按模式)"]
 *
 * 所以要给一句**能行动**的说明：不是"出错了"，而是"本机没检测到，
 * 你可以启动它，或者把模式切到云端"。
 */
export function emptyReasonFor(
  capPrefix: string,
  fieldKey: string | undefined,
  settings: Record<string, unknown> | null | undefined,
  providers: readonly ProviderInfo[],
): string | null {
  if (!fieldKey) return null;
  const cap = capabilityOf(capPrefix);
  const got = allowedProviders(capPrefix, fieldKey, settings, providers);
  if (got.length > 0) return null;

  const mode = modeOf(capPrefix, settings);

  // 这一档里注册表**本来有**提供者，只是本机都用不了 → 说清是哪一档、
  // 以及怎么让它出现
  const all = providers.filter((p) => p.capability === cap && p.api_style === 'openai');
  const blocked = all.filter((p) => p.runtime_ready === false);

  if (mode === 'local' && blocked.some((p) => p.is_local)) {
    return (
      '本机没有检测到可用的本地服务，暂无本地提供者可选。' +
      '先启动它（例如本地推理服务 / Ollama），或把上面的模式改成「云端 API」。'
    );
  }
  if (mode === 'cloud' && all.some((p) => !p.is_local)) {
    return '这一档暂时没有可用的提供者，请把上面的模式改成「本地推理」或检查配置。';
  }
  return '本机没有检测到可用的提供者。请检查上面的模式设置，或运行 scripts\\setup_tools.ps1 安装本地工具。';
}

/**
 * 「视频**实际会用哪一家**提供者」——与后端 `resolve_provider` 的优先级一致：
 *
 *     字段覆盖（`video_provider` 选了具体某家）
 *       > 模式默认（后端下发的 `mode_defaults.video_gen[模式]`）
 *       > 空（还没定）
 *
 * ## 为什么做成**模块级纯函数**，而不是组件里的 `useMemo`
 *
 * 我第一版写在 `SettingsPage` 里当 `useMemo`，连踩两个坑：
 *
 *   1. **Hook 数量在两次渲染间变化**：它写在 `if (settings === null) return`
 *      之后 —— 首次渲染走早返回（hook 不执行），设置到达后重渲染（hook 执行），
 *      React 抛 "Rendered more hooks than during the previous render."
 *      并**把整页渲染成黑屏**（用户报的"黑屏"就是这个）。
 *   2. 挪到早返回之前后，又变成
 *      **"Cannot access 'modeOf' before initialization"** —— `modeOf` 是组件里
 *      定义在更下方的 `const`，在 `useMemo` 的求值顺序下仍处于 TDZ。
 *
 * 两个坑的根源相同：**它就是个廉价计算，不配用 `useMemo`**。
 * 抽成模块级纯函数后，既没有 hook 顺序问题、也没有 TDZ 问题，还能单独测。
 */
export function effectiveVideoProvider(
  settings: Record<string, unknown> | null | undefined,
  modeDefaults?: Record<string, Record<string, string>> | null,
): string {
  const prov = String(settings?.['video_provider'] ?? '');
  const isPlaceholder = !prov || prov.startsWith('(') || prov === 'unset';
  if (!isPlaceholder) return prov;
  return String(modeDefaults?.['video_gen']?.[modeOf('video_gen', settings)] ?? '');
}

/**
 * 某个凭据字段是否属于"当前会用的那家提供者"。
 *
 * 都没定（还没选模式、也没有默认）时**全放行** —— 别让用户在还没选的时候
 * 就无从下手配密钥。
 */
export function credForProvider(
  c: { provider: string },
  settings: Record<string, unknown> | null | undefined,
  modeDefaults?: Record<string, Record<string, string>> | null,
): boolean {
  const prov = String(settings?.['video_provider'] ?? '');
  const isPlaceholder = !prov || prov.startsWith('(') || prov === 'unset';
  const eff = effectiveVideoProvider(settings, modeDefaults);
  if (isPlaceholder && !eff) return true;
  return c.provider === eff;
}

/**
 * 下拉的**白名单**（选项本身，不只是显示名）。
 *
 * ⚠️ 只过滤显示名是不够的：`field.options` 来自后端注册表的**静态**
 * 派生 —— 那是在"用户还没选模式"的前提下算出来的，两种模式的提供者都在
 * 里面。所以必须**把选项本身收窄**，否则用户照样能选到另一档。
 *
 * ⚠️ **必须保留该字段自己的"跟随模式"占位符**。我漏过这一次，后果是：
 *   · 用户**没法把覆盖清回"跟随模式"** —— 一旦显式选过某个提供者，
 *     就永远被钉在那一档，切模式时这个下拉当然不动（用户报的
 *     "为啥我切了，第二项却不变"）；
 *   · 而原生 select 在"当前值不在选项里"时会**退回显示第一个选项**，
 *     于是界面上显示的值与后端存的**完全不是一回事**。
 *
 * 占位符有两种写法（`(自动)` / `(按模式)`），取该字段自己的那个，
 * 不要写死 —— 注册表里两种都在用。
 *
 * 返回 `undefined` 表示"不限制"（模式字段、密钥字段、数值字段等一律不限制）。
 * 判据是"**是 select 且键里含 `provider` 且不以 `_provider_mode` 结尾**" ——
 * 不能写成 `includes('_provider_')`：`llm_provider_mode` 含它但它是模式字段
 * （套上白名单会被清空、下拉塌成一条缝），而 `tts_provider` / `video_provider`
 * **不含**它却正是提供者字段。
 */
export function allowedOptionsFor(
  field: { type: string; key: string; options?: readonly string[] },
  capPrefix: string,
  settings: Record<string, unknown> | null | undefined,
  providers: readonly ProviderInfo[],
): string[] | undefined {
  if (field.type !== 'select') return undefined;
  if (!field.key.includes('provider')) return undefined;
  if (field.key.endsWith('_provider_mode')) return undefined;

  // 该字段的"跟随模式"占位符（`(自动)` 或 `(按模式)`；两者都有则都留）
  const placeholders = (field.options ?? []).filter((o) => PLACEHOLDERS.has(o) && o !== '');
  const keep = placeholders.length ? placeholders : ['(自动)'];

  return [...keep, ...allowedProviders(capPrefix, field.key, settings, providers).map((p) => p.key)];
}
