/**
 * 设置页：**按能力分组**，每组把相关设置收在一张卡里。
 *
 * ## 为什么改成这样（用户反馈）
 *
 * 原来的结构是三张横跨全页的大卡：
 *
 *     ① 用云端还是本地？    5 个模式下拉（llm / image_gen / video_gen / lora_train / tts）
 *     ② 按职业指定模型      12 个职业，折叠着
 *     ③ 模型凭据            提供者级密钥
 *
 * 用户的原话是"为啥不能把 3 大项整合在一起，按职业分"。
 * 问题确实存在：**同一个能力的信息被劈在三张卡里**——
 * 想配"图像生成"，要在 ① 里找它的模式、在 ② 里翻到角色设计师/摄影师改覆盖、
 * 再到 ③ 里找对应提供者的 Key。三处来回跳，还容易漏。
 *
 * 现在按能力收成一张张卡：**这张卡里就是这个能力的全部设置**。
 *
 * ## ⚠️ 职业后缀可能是**旧键名**（返工过的地方）
 *
 * 注册表里这些字段的写法是：
 *
 *     llm_provider_writer         （showrunner 的旧名）
 *     llm_provider_critics        （script_editor 的旧名）
 *     llm_provider_director       （technical_director 的旧名）
 *     llm_provider_photographer   （这个才是当前 key）
 *
 * 我第一版只拿"当前职业 key"去匹配后缀，于是 `_writer` / `_critics` /
 * `_director` 这些**匹配不上**，被当成"能力级全局字段"渲染到卡顶——
 * 结果卡顶出现一排"文字模型来源 / 模型 / API Key"，而下面按职业展开里
 * **又是同一批字段**。用户看到的就是"同一行重复两遍、还分不清哪个是哪个"。
 *
 * 现在改成通用的**后缀剥离**：收集所有可能的角色标识（节点 key、
 * 职业 key、职业的旧键名），穷举匹配最长后缀。注册表再加旧名也不会漏。
 */

import { useEffect, useMemo, useState } from 'react';
import ConfigFieldEditor from '../canvas/ConfigFieldEditor';
import { fetchSettings, type SettingsMap } from '../api/settings';
import { AUTH_CHANGED_EVENT, getToken, requireAuth } from '../api/auth';
import { useCanvas } from '../store/canvas';
import type { ConfigField } from '../domain/registry';
import {
  allowedOptionsFor as computeAllowedOptions,
  allowedProviders as computeAllowedProviders,
  capabilityOf,
  modeOf as computeModeOf,
  optionLabelsFor as computeOptionLabels,
} from '../settings/providerFilter';

/** 费用审计返回的单项 */
interface CostItem {
  key?: string;
  label?: string;
  provider?: string;
  is_cloud?: boolean;
  note?: string;
  unit_cost?: number;
  currency?: string;
}

/** 后端下发的"提供者需要填什么凭据" */
interface CredField {
  key: string;
  label: string;
  kind: 'secret' | 'text';
  provider: string;
  hint?: string;
  models?: string[];
  verified?: boolean;
}

interface CredentialSpec {
  video?: { keys: CredField[]; endpoints: CredField[] };
}

/**
 * 把后端下发的凭据字段适配成 `ConfigField`，以便复用 `ConfigFieldEditor`。
 *
 * 为什么复用而不是另写输入框：密钥字段有"**不回显**、留空=沿用原值、
 * 置空=清除覆盖"三条约定，`ConfigFieldEditor` 已经实现并有测试。
 * 另写一份必然漂移——本项目已经因为重复实现吃过亏。
 */
function toConfigField(c: CredField): ConfigField {
  return {
    key: c.key,
    label: c.label,
    type: c.kind === 'secret' ? 'password' : 'text',
    scope: 'global',
    options: [],
    default: '',
    secret: c.kind === 'secret',
    help: c.hint ?? '',
  };
}

/**
 * 能力分组定义。
 *
 * `cap` 与后端 `/api/providers/modes` 的 `capability` 对齐
 * （`image_edit` 归入"图像生成"——对用户来说都是"出图"，不该分成两卡）。
 * `prefixes` 用来把注册表里的字段键归到本组
 * （字段形如 `llm_provider_writer` / `image_model_photographer`）。
 */
interface CapGroup {
  cap: string;
  title: string;
  desc: string;
  prefixes: string[];
  kinds: string;
}

const CAP_GROUPS: CapGroup[] = [
  {
    cap: 'llm',
    title: '文字模型',
    desc: '策划、剧本、分镜等所有"写字"的环节都走这里。',
    prefixes: ['llm'],
    kinds: '剧本 · 分镜 · 提示词',
  },
  {
    cap: 'image_gen',
    title: '图像生成',
    desc: '角色设定图、场景图、分镜图；也包含图像编辑（inpaint）。',
    prefixes: ['image'],
    kinds: '角色图 · 场景图 · 分镜图',
  },
  {
    cap: 'video_gen',
    title: '视频生成',
    desc: '分镜成片与白模参考视频。云端提供者的 Key 填在这一组里。',
    prefixes: ['video'],
    kinds: '镜头成片 · 白模参考视频',
  },
  {
    cap: 'tts',
    title: '配音',
    desc: '台词转语音。默认 Edge TTS 免费，不需要 Key。',
    prefixes: ['tts'],
    kinds: '对白配音',
  },
  {
    cap: 'lora_train',
    title: 'LoRA 训练',
    desc: '角色一致性训练。零成本走本地，云端按训练步数计费。',
    prefixes: ['lora'],
    kinds: '角色 LoRA 权重',
  },
];

/** 字段键 → 所属能力分组。匹配不到返回 null（不归任何组，不丢字段）。 */
function groupOfKey(key: string): CapGroup | null {
  for (const g of CAP_GROUPS) {
    if (g.prefixes.some((p) => key.startsWith(`${p}_`))) return g;
  }
  return null;
}

/**
 * 把一个字段键拆成「能力前缀 + 种类 + 角色后缀」。
 *
 * `roleIds` 是**所有可能的角色标识**（节点 key、职业 key、职业的旧键名），
 * 由调用方从注册表收集。用穷举最长后缀匹配而不是"查当前职业列表"，
 * 因为注册表里实际写的后缀是**旧键名**，只匹配当前 key 会漏掉它们，
 * 把角色覆盖项误当成能力级全局项（这正是"同一行重复两遍"的成因）。
 */
function splitKey(
  key: string,
  prefixes: string[],
  roleIds: Set<string>,
): { global: boolean; roleKey: string | null } {
  let rest = key;
  for (const p of prefixes) {
    if (key.startsWith(`${p}_`)) {
      rest = key.slice(p.length + 1);
      break;
    }
  }
  // 取**最长**匹配：避免 `director` 命中 `technical_director` 的尾段
  const sorted = [...roleIds].sort((a, b) => b.length - a.length);
  for (const rid of sorted) {
    if (rest.endsWith(`_${rid}`)) return { global: false, roleKey: rid };
  }
  return { global: true, roleKey: null };
}

export default function SettingsPage() {
  const registry = useCanvas((s) => s.registry);
  const [settings, setSettings] = useState<SettingsMap | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [cost, setCost] = useState<CostItem[] | null>(null);
  const [creds, setCreds] = useState<CredentialSpec | null>(null);
  /**
   * 提供者清单（`/api/providers`）：用来把下拉里的**内部标识**换成友好名。
   *
   * 注册表给的 `options` 是 `deepseek` / `zhipu` / `vllm` 这种内部 key，
   * 直接显示对用户没有意义；后端清单里本来就有
   * `DeepSeek` / `智谱 GLM` / `本地 vLLM`。
   */
  const [providers, setProviders] = useState<
    Array<{
      key: string;
      label: string;
      capability: string;
      /** 是否"本地 / 零成本"类（后端 `category` 为 local/free） */
      is_local: boolean;
      /** 协议风格；非 `openai` 的客户端调不了，不列进下拉 */
      api_style: string;
      default_model?: string;
      known_models?: string[];
    }>
  >([]);
  /**
   * 后端已**真正接入**的能力集合（如 `llm` / `image_gen` / `video_gen`）。
   *
   * 为什么需要它：注册表里有 5 个"云端/本地"模式字段，但 `lora_train` 与
   * `tts` 目前**没有任何调用点**——只是声明。原先前端把它们一起标成
   * "（必选）"并警告"未选择会报错"，**那是假的**：不选也不会报错。
   * 这里按后端下发的 `implemented` 只对真会用到的提示必选。
   * 拉不到时**保守地全部视为必选**（宁可多提示，也不要漏提示真需要的）。
   */
  const [implementedCaps, setImplementedCaps] = useState<Set<string> | null>(null);

  useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        const token = getToken();
        const r = await fetch('/api/providers/modes', {
          headers: token ? { Authorization: `Bearer ${token}` } : undefined,
        });
        const body = await r.json().catch(() => ({}));
        const modes = (body?.modes ?? []) as Array<{ capability: string; implemented?: boolean }>;
        if (alive && r.ok && modes.length) {
          setImplementedCaps(new Set(modes.filter((m) => m.implemented).map((m) => m.capability)));
        }
      } catch {
        /* 拉不到 → implementedCaps 保持 null → 视为全部必选（保守） */
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  /**
   * 拉取"提供者凭据"清单。
   *
   * 为什么必须由后端下发、而不是前端硬编码：
   *   视频的密钥是**提供者级**的（`minimax_api_key` / `dashscope_api_key` /
   *   `ark_api_key`），与 LLM、图像的 `*_api_key_{角色}` 命名完全不同。
   *   前端原先只按 `llm_/image_/video_/tts_/lora_` 前缀渲染字段，
   *   结果就是这三个键在白名单里（能存）却**没有输入框**——
   *   用户根本没法配视频 API key。由后端声明就不会再有这种漂移。
   */
  useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        const token = getToken();
        const r = await fetch('/api/providers', {
          headers: token ? { Authorization: `Bearer ${token}` } : undefined,
        });
        const body = await r.json().catch(() => ({}));
        if (alive && r.ok) {
          if (body?.credential_spec) setCreds(body.credential_spec as CredentialSpec);
          if (Array.isArray(body?.providers)) {
            setProviders(
              body.providers as Array<{
                key: string;
                label: string;
                capability: string;
                is_local: boolean;
                api_style: string;
                default_model?: string;
                known_models?: string[];
              }>,
            );
          }
        }
      } catch {
        /* 凭据清单拉不到不影响本页其余部分 */
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  /**
   * 拉设置。
   *
   * ## ⚠️ 为什么要订阅凭据变更（用户报的"啥情况"）
   *
   * 原先是 `useEffect(..., [])` —— **只在挂载时拉一次**。若那一刻 token
   * 还没就绪（或刚被清掉），`GET /api/settings` 401，页面就停在
   * "没有任何设置"的状态：所有字段回落到默认值。
   *
   * 后果不只是显示：提供者下拉的过滤依赖"云端/本地"模式的值，
   * 模式读成 `unset` 就**不过滤** —— 本地和云端又混在一起。
   * 这类 bug 表现为**时灵时不灵**（取决于登录与挂载的先后），最难查。
   *
   * 现在登录/退出/凭据失效都会触发重拉，不必再猜"挂载时 token 在不在"。
   *
   * ⚠️ 但**光靠事件还不够**（实测踩到）：`AUTH_CHANGED_EVENT` 是"派发过去、
   * 错过了就没了"的广播。如果本组件的监听器在事件派发**之后**才挂上
   * （用户先登录、再导航到设置页），那一次就永远收不到 —— 页面照样停在
   * `settings === null`。所以这里再加一层**不依赖时序的兜底**：
   * 拿不到（没 token / 401）就隔一会儿重试几次，直到成功。
   */
  useEffect(() => {
    let alive = true;
    let timer: number | undefined;

    const load = async (attempt = 0) => {
      const token = getToken();
      const res = await fetchSettings(token);
      if (!alive) return;
      if (res.ok) {
        setSettings(res.settings);
        setError(null);
      } else {
        setError(res.error ?? '读取设置失败');
        // 未登录/凭据未就绪：稍后重试（最多 5 次，约 4 秒）
        if (attempt < 5) {
          timer = window.setTimeout(() => void load(attempt + 1), 800);
        }
      }

      try {
        const r = await fetch('/api/cost/audit', {
          headers: token ? { Authorization: `Bearer ${token}` } : undefined,
        });
        const body = await r.json().catch(() => ({}));
        if (alive && r.ok) setCost((body.items ?? body.audit ?? []) as CostItem[]);
      } catch {
        /* 费用审计失败不影响设置本身 */
      }
    };
    void load();
    // 凭据变化（登录成功 / 退出 / 失效清除）后立刻重拉
    const onAuth = () => void load();
    window.addEventListener(AUTH_CHANGED_EVENT, onAuth);
    return () => {
      alive = false;
      if (timer !== undefined) window.clearTimeout(timer);
      window.removeEventListener(AUTH_CHANGED_EVENT, onAuth);
    };
  }, []);

  /**
   * 把注册表里所有配置字段**按能力分组**，组内再分"模式 / 能力级 / 按职业"。
   *
   * 一个字段可能被多个节点声明（`image_provider_mode` 在角色设计师、
   * 场景设计师、摄影师三处都有）——按 key 去重，否则同一个下拉会渲染三遍。
   */
  const grouped = useMemo(() => {
    type Entry = { node: string; nodeLabel: string; field: ConfigField };
    type RoleBucket = { label: string; fields: ConfigField[] };
    const empty = () => ({ mode: null as Entry | null, global: [] as Entry[], byRole: new Map<string, RoleBucket>() });
    const map = new Map<string, ReturnType<typeof empty>>();
    for (const g of CAP_GROUPS) map.set(g.cap, empty());
    if (!registry) return { groups: map, ungrouped: [] as Entry[] };

    /**
     * 收集**所有可能的角色标识**。三类都要收，缺一类就会把角色覆盖项
     * 误判成能力级全局项：
     *   · 节点 key（`continuity_checker`）——注册表字段实际用的后缀；
     *   · 职业 key（`continuity`）；
     *   · 职业的旧键名（`writer` / `critics` / `director` …）。
     */
    const roleIds = new Set<string>();
    const roleLabels = new Map<string, string>();
    for (const r of registry.roles) {
      // `Role.legacy_keys` 前端类型里已声明，后端 `/api/registry` 也下发
      const legacy = r.legacy_keys ?? [];
      roleIds.add(r.key);
      roleLabels.set(r.key, r.label);
      for (const lk of legacy) {
        roleIds.add(lk);
        roleLabels.set(lk, r.label);
      }
    }
    for (const n of registry.nodes) roleIds.add(n.key);

    const seen = new Set<string>();
    const ungrouped: Entry[] = [];

    for (const n of registry.nodes) {
      for (const f of n.config) {
        if (seen.has(f.key)) continue;
        const g = groupOfKey(f.key);
        if (!g) {
          seen.add(f.key);
          // 与选模型无关的字段（阈值、时长上限等）单独收在页尾，不能丢
          ungrouped.push({ node: n.key, nodeLabel: n.label, field: f });
          continue;
        }
        seen.add(f.key);

        const bucket = map.get(g.cap)!;
        const entry: Entry = { node: n.key, nodeLabel: n.label, field: f };
        if (f.key.endsWith('_provider_mode')) {
          bucket.mode = entry;
          continue;
        }
        const sp = splitKey(f.key, g.prefixes, roleIds);
        if (sp.global || !sp.roleKey) {
          bucket.global.push(entry);
        } else {
          const cur = bucket.byRole.get(sp.roleKey) ?? {
            label: roleLabels.get(sp.roleKey) ?? n.label,
            fields: [],
          };
          cur.fields.push(f);
          bucket.byRole.set(sp.roleKey, cur);
        }
      }
    }

    /**
     * ⚠️ 卡顶**只放能力级全局项**。角色覆盖**只在按职业里出现**。
     *
     * 曾经把角色覆盖项也渲染一份在卡顶（因为没识别出旧键名后缀），
     * 于是在卡顶看到"文字模型来源 / 模型 / API Key"一排、下面按职业展开里
     * 又是同一批——同一行重复两遍且分不清哪个是哪个。
     */
    return { groups: map, ungrouped };
  }, [registry]);

  /**
   * 未选择的模式，分成两拨：
   *   · blocked —— 已接入的能力（不选真的会在跑到那一步时报错）；
   *   · optional —— 仅注册表声明、当前无调用点（不选不影响任何行为）。
   */
  const { blockedModes, optionalModes } = useMemo(() => {
    const blocked: CapGroup[] = [];
    const optional: CapGroup[] = [];
    if (settings == null) return { blockedModes: blocked, optionalModes: optional };
    for (const g of CAP_GROUPS) {
      const entry = grouped.groups.get(g.cap)?.mode;
      if (!entry) continue;
      const v = settings[entry.field.key];
      if (v && v !== 'unset') continue;
      const cap = entry.field.key.replace(/_provider_mode$/, '');
      (implementedCaps === null || implementedCaps.has(cap) ? blocked : optional).push(g);
    }
    return { blockedModes: blocked, optionalModes: optional };
  }, [grouped, settings, implementedCaps]);

  const cloudItems = (cost ?? []).filter((c) => c.is_cloud);
  const localItems = (cost ?? []).filter((c) => c.is_cloud === false);

  if (!registry) {
    return (
      <div className="page">
        <h2 className="page-title">设置</h2>
        <p className="page-sub">注册表尚未加载，无法列出可配置项。</p>
      </div>
    );
  }

  /**
   * ⚠️ **设置还没拉到就先不要渲染这些字段**。
   *
   * ## 为什么（用户报的"啥情况"+ 过滤时灵时不灵，根因在这里）
   *
   * 设置的读取是**异步**的，而注册表是同步就有的。原先两者都到齐才开始用，
   * 结果在 `settings === null` 的那段时间里：
   *
   *   · "云端 / 本地"模式一律读成 `unset`（因为 `settings?.[key]` 取不到）；
   *   · 提供者下拉的过滤条件于是退化成"**不过滤**" → 本地与云端混在一起；
   *   · 更糟的是**之后没有重渲染对齐**：`ConfigFieldEditor` 用 `useState`
   *     初始化 `value`，只认首次的 `current`，于是界面会停在"没有覆盖"这个
   *     **假状态**上（用户明明存了 `local`，界面显示"— 请选择 —"）。
   *
   * 所以宁可不渲染：显示"正在读取设置…"，等真实值到了再画。
   * **显示假状态比显示空白更误导** —— 用户会照着假状态去改配置。
   *
   * ⚠️ 条件里**不能带 `&& !error`**（我犯过）：设置拉取失败（如凭据失效
   * 401）时 `error` 会被设上，这个分支就被跳过，于是**又回到"用空默认值
   * 渲染"的误导状态** —— 用户看到"所有配置都没了"，比一句报错吓人得多。
   * 所以：只要 `settings` 为空就挡住，并把原因和可做的动作说清楚。
   */
  if (settings === null) {
    const needLogin = Boolean(error && /未登录|登录|401|Unauthorized/i.test(error));
    return (
      <div className="page">
        <h2 className="page-title">设置</h2>
        <p className="page-sub">
          {needLogin ? '登录已失效，无法读取你的设置。' : '正在读取你的设置…'}
        </p>
        {error && (
          <div className="card" style={{ borderColor: 'var(--st-review)' }}>
            <div style={{ fontSize: 12, color: 'var(--st-review)' }}>⚠ {error}</div>
            {needLogin && (
              <button
                type="button"
                className="btn"
                style={{ marginTop: 10 }}
                onClick={() => requireAuth()}
              >
                重新登录
              </button>
            )}
          </div>
        )}
      </div>
    );
  }

  /**
   * 过滤与分组逻辑**统一放在 `settings/providerFilter.ts`**，这里只做薄封装。
   *
   * 为什么不写在组件里：这段逻辑出过三次语义错误（模式键拼错、
   * 只过滤显示名没过过滤选项、字段覆盖落在模式之外导致当前值消失），
   * 而它**很难靠肉眼看界面验证** —— 设置异步加载、下拉是原生控件、
   * 过滤结果还依赖"模式"与"字段级覆盖"两套优先级。
   * 抽成纯函数后能用 `node --test` 断言守住（见同目录的 .test.ts）。
   */
  const modeOf = (capPrefix: string) => computeModeOf(capPrefix, settings);
  const allowedProviders = (capPrefix: string, fieldKey?: string) =>
    computeAllowedProviders(capPrefix, fieldKey, settings, providers);
  const optionLabelsFor = (capPrefix: string, fieldKey?: string) =>
    computeOptionLabels(capPrefix, fieldKey, settings, providers);
  const allowedOptionsFor = (f: ConfigField, capPrefix: string) =>
    computeAllowedOptions(f, capPrefix, settings, providers);

  /**
   * 下拉的**分组标签**（值 → 组名）。
   *
   * 用户："api 里会出现 本地 我希望分开"。
   *
   * 模式选定后下拉本来就只剩一类，分组看不出来；但模式是 `unset`
   * （"— 请选择 —"）时按设计要列全部，两档混在一起。这时用
   * `<optgroup>` 把"本地 / 零成本"和"云端 / 网关"分成两块，
   * 一眼就是两类 —— 且 optgroup 是纯标签、没有 value，
   * 不会像插入占位项那样被回显成假选项。
   */


  /** 某能力在某模式下可选的提供者名（用于模式字段下方的提示文案）。 */
  const providerNamesForMode = (capPrefix: string): string[] =>
    allowedProviders(capPrefix).map((p) => p.label);

  /**
   * 「模型」字段的"获取可用模型"能力。
   *
   * 只给**模型**字段（`*_model_*` / `*_model`）用，不给提供者字段 ——
   * 不然每一栏都多一个按钮，页面会很吵。
   *
   * ## 为什么需要（用户提问）
   *
   * 选了「本地 vLLM / Ollama」后，"模型"是空的：注册表里的 `known_models`
   * 是**云端模型名**，对本地没用；本地装了什么只有本地服务知道。
   * 用户补充要求："如果是 api 就检测厂商模型" ——
   * 所以云端也走同一按钮：优先问厂商 `/models`，问不到回退已核对清单。
   *
   * 职业从字段键尾部取（`llm_model_writer` → `writer`），
   * 于是接口按**该职业**的设置解析提供者与地址，与页面上显示的一致。
   */
  const modelSourceFor = (f: ConfigField, capPrefix: string) => {
    if (!/_model(_|$)/.test(f.key)) return undefined;
    // 取职业后缀：`llm_model_writer` → `writer`；`llm_model` → ''
    const role = f.key.replace(/^.*?_model_?/, '');
    const cap = capabilityOf(capPrefix);
    if (cap !== 'llm') return undefined; // 目前只有 llm 有统一的 /models
    return {
      fetch: async () => {
        const token = getToken();
        const qs = new URLSearchParams({ capability: cap, role });
        const r = await fetch(`/api/providers/models?${qs}`, {
          headers: token ? { Authorization: `Bearer ${token}` } : undefined,
        });
        const body = await r.json().catch(() => ({}));
        // 后端返回 [{id, kind}]；兼容旧的纯字符串数组，避免半新半旧时炸掉
        const raw = (body?.models ?? []) as Array<string | { id?: string; kind?: string }>;
        const models = raw
          .map((m) => (typeof m === 'string' ? { id: m, kind: '' } : { id: String(m?.id ?? ''), kind: String(m?.kind ?? '') }))
          .filter((m) => m.id);
        return {
          ok: Boolean(body?.ok),
          models,
          detail: String(body?.detail ?? (r.ok ? '没有返回模型清单' : `请求失败（HTTP ${r.status}）`)),
          source: body?.source as string | undefined,
        };
      },
    };
  };

  /** 渲染一个字段：设置值 + 保存回调，避免每处都写一遍 */
  const renderField = (f: ConfigField, node: string, capPrefix: string) => (
    <ConfigFieldEditor
      key={`${node}:${f.key}`}
      field={f}
      current={settings?.[f.key]}
      optionLabels={optionLabelsFor(capPrefix, f.key)}
      allowedOptions={allowedOptionsFor(f, capPrefix)}
      modelSource={modelSourceFor(f, capPrefix)}
      onSaved={(k, v) => setSettings((s) => ({ ...(s ?? {}), [k]: v }))}
    />
  );

  return (
    <div className="page">
      <h2 className="page-title">设置</h2>
      <p className="page-sub">
        按能力分组：每张卡里就是这个能力的<strong>全部</strong>设置——先选云端还是本地，
        再按需给个别职业单独指定，最后填提供者密钥。
      </p>

      {error && (
        <div className="card" style={{ borderColor: 'var(--st-review)', marginBottom: 14 }}>
          <div style={{ fontSize: 12, color: 'var(--st-review)' }}>⚠ {error}</div>
        </div>
      )}

      {blockedModes.length > 0 && (
        <div className="card" style={{ borderColor: 'var(--st-review)', marginBottom: 14 }}>
          <div style={{ fontSize: 13, color: 'var(--st-review)' }}>
            ⚠ 还有 {blockedModes.length} 项没选：
            {blockedModes.map((g) => g.title).join('、')}
          </div>
          <div style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 6, lineHeight: 1.7 }}>
            跑到对应环节时后端会<strong>明确报错</strong>，而不是替你猜——
            猜本地会失败、猜云端会产生你没预期的费用。
          </div>
        </div>
      )}
      {optionalModes.length > 0 && (
        <div className="card" style={{ marginBottom: 14 }}>
          <div style={{ fontSize: 12, color: 'var(--text-muted)', lineHeight: 1.7 }}>
            另有 {optionalModes.length} 项（{optionalModes.map((g) => g.title).join('、')}）
            当前<strong>未接入流程</strong>，不选不影响运行；将来接入后会自动变为必选。
          </div>
        </div>
      )}

      {/* ── 每个能力一张卡 ── */}
      {CAP_GROUPS.map((g) => {
        const bucket = grouped.groups.get(g.cap);
        if (!bucket) return null;
        const hasCreds = g.cap === 'video' && (creds?.video?.keys?.length ?? 0) > 0;
        if (!bucket.mode && !bucket.global.length && !bucket.byRole.size && !hasCreds) return null;

        const needsPick =
          bucket.mode != null &&
          settings != null &&
          (!settings[bucket.mode.field.key] || settings[bucket.mode.field.key] === 'unset');
        const implemented =
          bucket.mode == null ||
          implementedCaps === null ||
          implementedCaps.has(bucket.mode.field.key.replace(/_provider_mode$/, ''));

        return (
          <div
            className="card"
            key={g.cap}
            style={{
              marginBottom: 14,
              borderColor: needsPick && implemented ? 'var(--st-review)' : undefined,
            }}
          >
            <div className="card-title" style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
              {g.title}
              <span style={{ fontSize: 11, color: 'var(--text-faint)', fontWeight: 400 }}>
                {g.kinds}
              </span>
              {needsPick && implemented && (
                <span style={{ color: 'var(--st-review)', fontSize: 12, fontWeight: 400 }}>
                  （必选）
                </span>
              )}
              {!implemented && (
                <span style={{ color: 'var(--text-faint)', fontSize: 12, fontWeight: 400 }}>
                  未接入（可选）
                </span>
              )}
            </div>
            <div style={{ fontSize: 12, color: 'var(--text-muted)', lineHeight: 1.7, marginBottom: 12 }}>
              {g.desc}
            </div>

            {/* 能力级：模式 + 该能力的全局参数。标签对齐靠 .settings-grid */}
            {(bucket.mode || bucket.global.length > 0) && (
              <div className="settings-grid">
                {bucket.mode && (
                  <div className="settings-cell">
                    {renderField(bucket.mode.field, bucket.mode.node, g.cap)}
                    {/*
                      在模式字段下方列出**该模式下可选**的提供者。
                      用户要的是"本地和云端分开"——这样一眼能看出
                      选「本地」之后下面能挑什么，不用先点开下拉试探。
                    */}
                    <div
                      style={{
                        fontSize: 11,
                        color: 'var(--text-faint)',
                        marginTop: 6,
                        lineHeight: 1.6,
                      }}
                    >
                      {(() => {
                        const names = providerNamesForMode(g.cap);
                        const mode = modeOf(g.cap);
                        if (mode === 'unset') return `未选择时下面会列出全部 ${names.length} 家`;
                        const which = mode === 'local' ? '本地 / 零成本' : '云端';
                        return names.length
                          ? `「${which}」模式下可选：${names.join(' · ')}`
                          : `「${which}」模式下没有可用提供者`;
                      })()}
                    </div>
                  </div>
                )}
                {bucket.global.map((e) => (
                  <div className="settings-cell" key={e.field.key}>
                    {renderField(e.field, e.node, g.cap)}
                  </div>
                ))}
              </div>
            )}

            {/* 该能力的提供者凭据（目前只有视频有提供者级密钥） */}
            {hasCreds && creds?.video ? (
              <div style={{ marginTop: 14 }}>
                <div style={{ fontSize: 12, color: 'var(--text-muted)', marginBottom: 8, lineHeight: 1.7 }}>
                  视频的密钥是<strong>提供者级</strong>的，与按职业的密钥不是同一个键。
                  想用云端视频，必须在这里填对应提供者的 Key；只用本地 / FFmpeg 则不需要。
                </div>
                <div className="settings-grid">
                  {creds.video.keys.map((c) => (
                    <div className="settings-cell" key={c.key}>
                      {renderField(toConfigField(c), 'video-cred', g.cap)}
                      {c.verified === false && (
                        <div style={{ fontSize: 11, color: 'var(--st-review)', marginTop: 4 }}>
                          ⚠ 该提供者按公开文档实现，尚未用真实 key 跑通；首次使用请核对返回的报错
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            ) : null}

            {/*
              按职业覆盖：**默认展开**。
              收起时用户会以为"要配的只有卡顶那个模式"，而真正要配的是
              每个职业的来源/模型/Key（注册表里这些字段都是**职业级**的，
              没有能力级默认值）。展开并标清职业名才不会困惑。
            */}
            {bucket.byRole.size > 0 && (
              <div style={{ marginTop: 14 }}>
                <div style={{ fontSize: 12, color: 'var(--text-muted)', marginBottom: 8, lineHeight: 1.7 }}>
                  按职业单独指定（{bucket.byRole.size} 个）—— 不填就跟随上面的
                  「云端 / 本地」选择与后端默认模型。
                </div>
                {[...bucket.byRole.entries()].map(([roleKey, v]) => (
                  <div
                    key={roleKey}
                    style={{ borderTop: '1px solid var(--line)', paddingTop: 10, marginTop: 10 }}
                  >
                    <div
                      style={{
                        fontSize: 12.5,
                        color: 'var(--text)',
                        marginBottom: 8,
                        display: 'flex',
                        alignItems: 'baseline',
                        gap: 6,
                      }}
                    >
                      <strong>{v.label}</strong>
                      <span style={{ color: 'var(--text-faint)', fontSize: 11 }}>{roleKey}</span>
                    </div>
                    <div className="settings-grid">
                      {v.fields.map((f) => (
                        <div className="settings-cell" key={f.key}>
                          {renderField(f, roleKey, g.cap)}
                        </div>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        );
      })}

      {/* ── 与选模型无关的其它配置（阈值、时长上限…）——不能丢 ── */}
      {grouped.ungrouped.length > 0 && (
        <div className="card" style={{ marginBottom: 14 }}>
          <div className="card-title">其它配置</div>
          <div style={{ fontSize: 12, color: 'var(--text-muted)', lineHeight: 1.7, marginBottom: 12 }}>
            阈值、时长上限等与"用哪个模型"无关的参数。
          </div>
          <div className="settings-grid">
            {grouped.ungrouped.map((e) => (
              <div className="settings-cell" key={e.field.key}>
                {renderField(e.field, e.node, '')}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* ── 费用提醒（只提醒不拦截）── */}
      <div className="card">
        <div className="card-title">费用提醒</div>
        <div style={{ fontSize: 12, color: 'var(--text-muted)', lineHeight: 1.7, marginBottom: 10 }}>
          只提示，不拦截。云端项会在发起真实请求前标出单价与本次预估。
        </div>
        {!cost ? (
          <div style={{ fontSize: 12, color: 'var(--text-faint)' }}>费用审计不可用。</div>
        ) : (
          <>
            <div style={{ fontSize: 12, color: 'var(--text-muted)', marginBottom: 6 }}>
              云端 {cloudItems.length} 项 · 本地 {localItems.length} 项
            </div>
            <div className="settings-grid">
              {(cost ?? []).map((c, i) => (
                <div
                  key={c.key ?? i}
                  className="settings-cell"
                  style={{ fontSize: 12, color: 'var(--text-muted)', lineHeight: 1.6 }}
                >
                  <span style={{ color: c.is_cloud ? 'var(--glow-violet)' : 'var(--text)' }}>
                    {c.is_cloud ? '云' : '本地'}
                  </span>{' '}
                  {c.label ?? c.key}
                  {c.provider ? ` · ${c.provider}` : ''}
                  {c.unit_cost != null ? ` · ${c.unit_cost}${c.currency ?? ''}` : ''}
                </div>
              ))}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
