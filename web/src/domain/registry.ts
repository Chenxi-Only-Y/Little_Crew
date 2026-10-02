/**
 * 注册表类型 —— 与后端 `GET /api/registry` 的下发结构一一对应。
 *
 * 硬约束（见 docs/recon/04-frontend-rewrite.md §1）：
 *   前端**不得**自己定义职业、部门、阶段、产物、provider 清单——一律来自注册表。
 *   否则会重演 v1「同一概念散落 5 个文件、且已真实不一致」的问题。
 */

/** 产物形态：决定端口颜色与图标 */
export type ArtifactKind =
  | 'text'
  | 'json'
  | 'list'
  | 'image'
  | 'video'
  | 'audio'
  | 'score'
  | 'model'
  | 'timeline';

/** 能力类别：决定节点能用哪些 provider */
export type ProviderCapability =
  | 'llm'
  | 'image_gen'
  | 'image_edit'
  | 'video_gen'
  | 'lora_train'
  | 'tts'
  | 'music'
  | 'render_2d';

export type NodeKind = 'agent' | 'service';
export type PanelKind =
  | 'main'
  | 'report'
  | 'gallery'
  | 'timeline'
  | 'training'
  | 'console'
  | 'publish';
export type ConfigScope = 'global' | 'role' | 'node';
export type ConfigType = 'select' | 'text' | 'password' | 'number' | 'toggle' | 'slider';

export interface Department {
  key: string;
  label: string;
  order: number;
  desc: string;
}

export interface Artifact {
  key: string;
  label: string;
  kind: ArtifactKind;
  state_key: string | null;
}

export interface Panel {
  key: string;
  label: string;
  kind: PanelKind;
  node: string;
}

export interface Role {
  key: string;
  label: string;
  en: string;
  dept: string;
  duty: string;
  real_job: string;
  node: string;
  legacy_keys: string[];
  panels: Panel[];
}

export interface ConfigField {
  key: string;
  label: string;
  type: ConfigType;
  scope: ConfigScope;
  options: string[];
  default: unknown;
  secret: boolean;
  help: string;
}

export interface RegistryNode {
  key: string;
  label: string;
  dept: string;
  kind: NodeKind;
  produces: string[];
  consumes: string[];
  capability: ProviderCapability[];
  can_skip: boolean;
  iter_group: string | null;
  legacy_keys: string[];
  agent_class: string;
  config: ConfigField[];
}

export interface Checkpoint {
  key: string;
  label: string;
  node: string;
  after: string;
}

export interface Registry {
  schema_version: number;
  departments: Department[];
  artifacts: Artifact[];
  roles: Role[];
  nodes: RegistryNode[];
  checkpoints: Checkpoint[];
  execution_order: string[];
  artifact_state_map: Record<string, string>;
}

/* ---------------- 运行期状态（画布用，非注册表） ---------------- */

/** 阶段状态；注意 skipped 与 done 必须区分（v1 曾混淆，见 01-usage §6.3） */
export type StageStatus =
  | 'pending'
  | 'running'
  | 'done'
  | 'skipped'
  | 'stale'
  | 'failed'
  | 'review';

export type SessionStatus =
  | 'draft'
  | 'running'
  | 'waiting_review'
  | 'resuming'
  | 'done'
  | 'failed'
  | 'cancelled';

export interface StageState {
  stage: string;
  status: StageStatus;
}

export interface Session {
  id: string;
  userInput: string;
  status: SessionStatus;
  stages: StageState[];
}
