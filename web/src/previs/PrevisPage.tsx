/**
 * 3D 预演编辑器（白模）。
 *
 * 用途（按用户的路线一）：先把"谁在哪、相机怎么走"摆出来，
 * 导出白模帧，作为视频模型的结构输入。解决的是视频模型**自己编空间**
 * 导致的漂移、瞬移、比例失控。
 *
 * 交互分工：
 *   - 左栏：场景对象（人物/道具）与镜头列表、播放控制
 *   - 中间：3D 视口（相机由镜头关键帧驱动，不由鼠标控制）
 *   - 右栏：选中对象的数值编辑
 *
 * 导出用原生 Three.js API 逐帧渲染并读回像素（见 exporter.ts），
 * 不依赖 React 渲染循环。
 */

import { useEffect, useState } from 'react';
import Scene3D, { three } from './Scene3D';
import { usePrevis } from './store';
import { exportShot, exportDepth, dataUrlToBlob, type DepthPolarity } from './exporter';
import { sampleCamera, type PrevisScene, type Vec3 } from './types';
import { POSE_PRESETS } from './humanoid';
import { getToken } from '../api/auth';
import { loadProject, listProjects, saveProject, type ProjectSummary } from '../api/projects';

const AXES: Array<{ key: 0 | 1 | 2; label: string }> = [
  { key: 0, label: 'X 右' },
  { key: 1, label: 'Y 上' },
  { key: 2, label: 'Z 前' },
];

function NumField({
  label,
  value,
  step = 0.1,
  onChange,
}: {
  label: string;
  value: number;
  step?: number;
  onChange: (v: number) => void;
}) {
  return (
    <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 11 }}>
      <span style={{ width: 42, color: 'var(--s-violet-mid)' }}>{label}</span>
      <input
        type="number"
        step={step}
        value={Number.isFinite(value) ? Number(value.toFixed(3)) : 0}
        onChange={(e) => onChange(Number(e.target.value))}
        style={{
          flex: '1 1 auto',
          minWidth: 0,
          background: 'var(--s-white)',
          color: 'var(--s-ink-plum)',
          border: '1px solid var(--s-hairline-cool)',
          borderRadius: 5,
          padding: '3px 6px',
          fontSize: 11,
          fontFamily: 'inherit',
        }}
      />
    </label>
  );
}

function Vec3Field({
  label,
  value,
  onChange,
  step = 0.1,
}: {
  label: string;
  value: Vec3;
  onChange: (v: Vec3) => void;
  step?: number;
}) {
  return (
    <div style={{ marginBottom: 6 }}>
      <div style={{ fontSize: 11, color: 'var(--s-violet-mid)', marginBottom: 3 }}>{label}</div>
      <div style={{ display: 'grid', gap: 4 }}>
        {AXES.map(({ key, label: axisLabel }) => (
          <NumField
            key={key}
            label={axisLabel}
            step={step}
            value={value[key]}
            onChange={(v) => {
              const next = [...value] as Vec3;
              next[key] = v;
              onChange(next);
            }}
          />
        ))}
      </div>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="card" style={{ marginBottom: 10, padding: '10px 12px' }}>
      <div className="card-title" style={{ marginBottom: 8 }}>
        {title}
      </div>
      {children}
    </div>
  );
}

export default function PrevisPage() {
  const scene = usePrevis((s) => s.scene);
  const shotIndex = usePrevis((s) => s.shotIndex);
  const selected = usePrevis((s) => s.selected);
  const playhead = usePrevis((s) => s.playhead);
  const playing = usePrevis((s) => s.playing);
  const showHelpers = usePrevis((s) => s.showHelpers);

  const [exporting, setExporting] = useState(false);
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(null);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [outW, setOutW] = useState(832);
  const [outH, setOutH] = useState(480);
  const [polarity, setPolarity] = useState<DepthPolarity>('near_white');
  const [withGround, setWithGround] = useState(false);

  /* ---- 工程存档 ---- */
  const [projName, setProjName] = useState('未命名预演');
  const [projId, setProjId] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [showOpen, setShowOpen] = useState(false);

  // 场景一改就标记未保存（用订阅而非轮询，避免把 playhead 变化也算进去）
  useEffect(() => {
    const unsub = usePrevis.subscribe((s, prev) => {
      if (s.scene !== prev.scene) setDirty(true);
    });
    return unsub;
  }, []);

  const refreshProjects = async () => {
    const res = await listProjects(getToken());
    setProjects(res.projects);
  };

  const onSavePrevis = async (asNew = false) => {
    // 复用工程的存储层：把预演场景塞进 canvas 字段的扩展位，
    // 后端 `_norm_canvas` 只认 nodes/edges/viewport，未知字段会被丢弃——
    // 所以这里把场景整体序列化进 viewport 之外的一个专用键，由 _norm_canvas 放行。
    const canvas = {
      nodes: [],
      edges: [],
      viewport: {},
      previs: scene,
    } as unknown as Parameters<typeof saveProject>[1];
    const res = await saveProject(
      projName || '未命名预演',
      canvas,
      {},
      getToken(),
      asNew ? null : projId,
    );
    if (res.ok && res.projectId) {
      setProjId(res.projectId);
      setDirty(false);
      setMsg({ ok: true, text: asNew ? '已另存为' : '预演已保存' });
      void refreshProjects();
    } else {
      setMsg({ ok: false, text: res.error ?? '保存失败' });
    }
  };

  const onOpenPrevis = async (id: string) => {
    if (dirty && !window.confirm('当前预演有未保存改动，打开其它会丢弃它们。继续？')) return;
    const res = await loadProject(id, getToken());
    if (!res.ok || !res.project) {
      setMsg({ ok: false, text: res.error ?? '打开失败' });
      return;
    }
    const doc = res.project.canvas as unknown as { previs?: PrevisScene };
    if (!doc.previs || !Array.isArray(doc.previs.actors)) {
      setMsg({ ok: false, text: '这个工程不是预演工程（没有 3D 场景数据）' });
      return;
    }
    usePrevis.getState().setScene(doc.previs);
    setProjId(res.project.project_id);
    setProjName(res.project.name);
    setDirty(false);
    setShowOpen(false);
    setMsg({ ok: true, text: `已打开「${res.project.name}」` });
  };

  const shot = scene.shots[shotIndex];
  const selActor = selected?.kind === 'actor' ? scene.actors.find((a) => a.id === selected.id) : null;
  const selProp = selected?.kind === 'prop' ? scene.props.find((p) => p.id === selected.id) : null;
  const selKey =
    selected?.kind === 'camera' ? shot?.cameraKeys.find((k) => k.id === selected.id) : null;

  const doExport = async (only?: number[]) => {
    const { renderer, scene: tScene, camera } = three;
    if (!renderer || !tScene || !camera) {
      setMsg({ ok: false, text: '3D 视口还没准备好' });
      return;
    }
    setExporting(true);
    setMsg(null);
    setProgress({ done: 0, total: 0 });
    try {
      const res = await exportShot(renderer, tScene, camera, scene, shotIndex, {
        width: outW,
        height: outH,
        only,
        onProgress: (done, total) => setProgress({ done, total }),
      });
      // 打包下载：逐帧下载会触发浏览器"多文件下载"拦截，故拼成一个 JSON
      const payload = {
        shot: shot?.name,
        fps: res.fps,
        duration: res.duration,
        width: res.width,
        height: res.height,
        frames: res.frames.map((f) => ({ index: f.index, time: f.time, png: f.dataUrl })),
      };
      const blob = new Blob([JSON.stringify(payload)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `previs_${shot?.name ?? 'shot'}_${res.frames.length}f.json`;
      a.click();
      URL.revokeObjectURL(url);

      // 同时把首尾两帧单独存下来，方便直接喂给视频模型
      if (res.frames.length > 1 || only) {
        for (const f of res.frames) {
          const tag = f.index === 0 ? 'first' : 'last';
          if (f.index === 0 || f.index === res.frames[res.frames.length - 1].index) {
            const link = document.createElement('a');
            link.href = f.dataUrl;
            link.download = `previs_${tag}_${f.index.toString().padStart(4, '0')}.png`;
            link.click();
            void dataUrlToBlob(f.dataUrl); // 保持引用，避免被 tree-shake
          }
        }
      }
      setMsg({ ok: true, text: `已导出 ${res.frames.length} 帧（含首尾帧 PNG）` });
    } catch (err) {
      setMsg({ ok: false, text: `导出失败：${(err as Error).message}` });
    } finally {
      setExporting(false);
      setProgress(null);
    }
  };

  /** 只导一帧深度：直接算，不走序列（省时间） */
  const doExportDepthSingle = async () => {
    const { scene: tScene, camera } = three;
    if (!tScene || !camera) {
      setMsg({ ok: false, text: '3D 视口还没准备好' });
      return;
    }
    setExporting(true);
    setMsg(null);
    try {
      const { renderDepthFrame } = await import('./exporter');
      const shotNow = scene.shots[shotIndex];
      const pose = sampleCamera(shotNow, 0);
      camera.position.set(pose.position[0], pose.position[1], pose.position[2]);
      camera.lookAt(pose.target[0], pose.target[1], pose.target[2]);
      if ((camera as import('three').PerspectiveCamera).isPerspectiveCamera) {
        const pc = camera as import('three').PerspectiveCamera;
        pc.fov = pose.fov;
        pc.updateProjectionMatrix();
      }
      camera.updateMatrixWorld(true);
      const rgba = renderDepthFrame(tScene, camera, outW, outH, polarity, withGround);
      const cv = document.createElement('canvas');
      cv.width = outW;
      cv.height = outH;
      const cx = cv.getContext('2d');
      if (!cx) throw new Error('无法创建 2D 上下文');
      const id = cx.createImageData(outW, outH);
      id.data.set(rgba);
      cx.putImageData(id, 0, 0);
      const link = document.createElement('a');
      link.href = cv.toDataURL('image/png');
      link.download = `previs_depth_${projName || 'shot'}_0000.png`;
      link.click();
      setMsg({ ok: true, text: '已导出深度首帧' });
    } catch (err) {
      setMsg({ ok: false, text: `深度导出失败：${(err as Error).message}` });
    } finally {
      setExporting(false);
    }
  };

  /** 导出整段深度序列 */
  const doExportDepthAll = async () => {
    const { scene: tScene, camera } = three;
    if (!tScene || !camera) {
      setMsg({ ok: false, text: '3D 视口还没准备好' });
      return;
    }
    setExporting(true);
    setMsg(null);
    setProgress({ done: 0, total: 0 });
    try {
      const res = await exportDepth(tScene, camera, scene, shotIndex, {
        width: outW,
        height: outH,
        polarity,
        includeGround: withGround,
        onProgress: (done, total) => setProgress({ done, total }),
      });
      const payload = {
        kind: 'depth',
        polarity: res.polarity,
        includeGround: res.includeGround,
        fps: res.fps,
        width: res.width,
        height: res.height,
        frames: res.frames.map((f) => ({ index: f.index, time: f.time, png: f.dataUrl })),
      };
      const blob = new Blob([JSON.stringify(payload)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `previs_depth_${res.frames.length}f.json`;
      a.click();
      URL.revokeObjectURL(url);
      setMsg({ ok: true, text: `已导出 ${res.frames.length} 帧深度图` });
    } catch (err) {
      setMsg({ ok: false, text: `深度导出失败：${(err as Error).message}` });
    } finally {
      setExporting(false);
      setProgress(null);
    }
  };

  return (
    <div style={{ flex: '1 1 auto', minHeight: 0, display: 'flex' }}>
      {/* ---------------- 左栏：场景与镜头 ---------------- */}
      <aside
        style={{
          width: 226,
          flex: '0 0 auto',
          borderRight: '1px solid var(--s-hairline)',
          overflowY: 'auto',
          padding: '12px 10px',
        }}
      >
        <div className="card-title">场景对象</div>
        <button className="btn ghost" style={{ width: '100%', marginBottom: 6 }} onClick={() => usePrevis.getState().addActor()}>
          + 人物
        </button>
        {scene.actors.map((a) => (
          <div
            key={a.id}
            onClick={() => usePrevis.getState().select({ kind: 'actor', id: a.id })}
            style={{
              fontSize: 11,
              padding: '4px 6px',
              borderRadius: 5,
              cursor: 'pointer',
              display: 'flex',
              alignItems: 'center',
              gap: 6,
              background:
                selected?.kind === 'actor' && selected.id === a.id ? 'rgba(200,240,74,0.14)' : 'transparent',
            }}
          >
            <span style={{ width: 8, height: 8, borderRadius: 4, background: a.color }} />
            {a.name}
          </div>
        ))}

        <button className="btn ghost" style={{ width: '100%', margin: '10px 0 6px' }} onClick={() => usePrevis.getState().addProp()}>
          + 道具
        </button>
        {scene.props.map((p) => (
          <div
            key={p.id}
            onClick={() => usePrevis.getState().select({ kind: 'prop', id: p.id })}
            style={{
              fontSize: 11,
              padding: '4px 6px',
              borderRadius: 5,
              cursor: 'pointer',
              display: 'flex',
              alignItems: 'center',
              gap: 6,
              background:
                selected?.kind === 'prop' && selected.id === p.id ? 'rgba(200,240,74,0.14)' : 'transparent',
            }}
          >
            <span style={{ width: 8, height: 8, borderRadius: 2, background: p.color }} />
            {p.name}
          </div>
        ))}

        <div className="card-title" style={{ marginTop: 14 }}>
          镜头
        </div>
        {scene.shots.map((s, i) => (
          <div
            key={s.id}
            onClick={() => usePrevis.getState().setShotIndex(i)}
            style={{
              fontSize: 11,
              padding: '4px 6px',
              borderRadius: 5,
              cursor: 'pointer',
              background: i === shotIndex ? 'rgba(200,240,74,0.14)' : 'transparent',
            }}
          >
            {s.name}
            <span style={{ color: 'var(--s-violet-mid)', marginLeft: 6 }}>
              {s.duration}s · {s.cameraKeys.length} 关键帧
            </span>
          </div>
        ))}
        <button className="btn ghost" style={{ width: '100%', marginTop: 6 }} onClick={() => usePrevis.getState().addShot()}>
          + 镜头
        </button>

        <button
          className="btn ghost"
          style={{ width: '100%', marginTop: 14 }}
          onClick={() => usePrevis.getState().removeSelected()}
          disabled={!selected || selected.kind === 'camera'}
        >
          删除选中
        </button>
      </aside>

      {/* ---------------- 中间：3D 视口 ---------------- */}
      <div style={{ flex: '1 1 auto', minWidth: 0, display: 'flex', flexDirection: 'column' }}>
        <div className="app-statusbar" style={{ borderBottom: '1px solid var(--s-hairline)' }}>
          <button className="btn" onClick={() => usePrevis.getState().setPlaying(!playing)}>
            {playing ? '⏸ 暂停' : '▶ 播放'}
          </button>
          <input
            type="range"
            min={0}
            max={shot?.duration ?? 7}
            step={1 / (shot?.fps ?? 24)}
            value={playhead}
            onChange={(e) => usePrevis.getState().setPlayhead(Number(e.target.value))}
            style={{ flex: '1 1 auto', minWidth: 120 }}
          />
          <span className="pill" style={{ minWidth: 96 }}>
            {playhead.toFixed(2)}s / {shot?.duration ?? 0}s
          </span>
          <button className="btn ghost" onClick={() => usePrevis.getState().setPlayhead(0)}>
            ⏮ 首帧
          </button>
          <button
            className="btn ghost"
            onClick={() => usePrevis.getState().setPlayhead(shot?.duration ?? 0)}
          >
            尾帧 ⏭
          </button>
          <label className="pill" style={{ gap: 4, cursor: 'pointer' }}>
            <input
              type="checkbox"
              checked={showHelpers}
              onChange={(e) => usePrevis.getState().setShowHelpers(e.target.checked)}
            />
            辅助线
          </label>
        </div>

        <div style={{ flex: '1 1 auto', minHeight: 0, position: 'relative' }}>
          <Scene3D />
          <div
            style={{
              position: 'absolute',
              left: 10,
              bottom: 10,
              fontSize: 10,
              color: 'var(--s-muted)',
              background: 'rgba(21,15,35,0.75)',
              padding: '4px 8px',
              borderRadius: 6,
              maxWidth: 380,
              lineHeight: 1.6,
            }}
          >
            相机由关键帧驱动、不受鼠标控制。导出时辅助线会被隐藏，
            画面只剩白模本体（网格线若留在画面里会被视频模型当成真实结构）。
          </div>
        </div>

        <div className="app-statusbar" style={{ borderTop: '1px solid var(--s-hairline)' }}>
          <span className="pill">📁</span>
          <input
            value={projName}
            onChange={(e) => setProjName(e.target.value)}
            placeholder="预演名称"
            style={{ width: 130 }}
          />
          <button className="btn" onClick={() => void onSavePrevis(false)} disabled={exporting}>
            保存
          </button>
          <button className="btn ghost" onClick={() => void onSavePrevis(true)} disabled={exporting}>
            另存为
          </button>
          <button
            className="btn ghost"
            onClick={() => {
              setShowOpen((v) => !v);
              if (!showOpen) void refreshProjects();
            }}
          >
            打开
          </button>
          {dirty && (
            <span className="pill" style={{ color: 'var(--st-review)' }}>
              ● 未保存
            </span>
          )}
          <div className="app-spacer" />

          <span className="pill">深度极性</span>
          <select
            value={polarity}
            onChange={(e) => setPolarity(e.target.value as DepthPolarity)}
            style={{ fontSize: 11 }}
          >
            <option value="near_white">近处亮（depth 常用）</option>
            <option value="far_white">远处亮（反相）</option>
          </select>
          <label
            className="pill"
            style={{ gap: 4, cursor: 'pointer' }}
            title="无细节的光滑地面在深度图上只是一片渐变，ControlNet 会坚持它是光滑的、从而压制提示词要的纹理。默认不计入。"
          >
            <input
              type="checkbox"
              checked={withGround}
              onChange={(e) => setWithGround(e.target.checked)}
            />
            含地面
          </label>
          <button
            className="btn ghost"
            disabled={exporting}
            onClick={() => void doExportDepthSingle()}
          >
            深度·首帧
          </button>
          <button className="btn ghost" disabled={exporting} onClick={() => void doExportDepthAll()}>
            深度·全部帧
          </button>
        </div>

        {showOpen && (
          <div
            className="app-statusbar"
            style={{ display: 'block', maxHeight: 150, overflowY: 'auto', borderTop: '1px solid var(--s-hairline)' }}
          >
            {projects.length === 0 ? (
              <span style={{ fontSize: 11, color: 'var(--s-muted)' }}>还没有保存过预演。</span>
            ) : (
              projects.map((p) => (
                <div
                  key={p.project_id}
                  style={{ display: 'flex', gap: 8, alignItems: 'center', fontSize: 11, padding: '3px 0' }}
                >
                  <span style={{ flex: '1 1 auto' }}>{p.name}</span>
                  <span style={{ color: 'var(--s-violet-mid)' }}>{p.updated.slice(5, 16)}</span>
                  <button className="btn ghost" onClick={() => void onOpenPrevis(p.project_id)}>
                    打开
                  </button>
                </div>
              ))
            )}
          </div>
        )}

        <div className="app-statusbar" style={{ borderTop: '1px solid var(--s-hairline)' }}>
          <span className="pill">导出尺寸</span>
          <input
            type="number"
            value={outW}
            step={32}
            onChange={(e) => setOutW(Number(e.target.value))}
            style={{ width: 78 }}
          />
          <span className="pill">×</span>
          <input
            type="number"
            value={outH}
            step={32}
            onChange={(e) => setOutH(Number(e.target.value))}
            style={{ width: 78 }}
          />
          <span className="pill" style={{ color: 'var(--s-violet-mid)' }}>
            （H3 要求 32 的倍数；本地 8GB 建议 832×480）
          </span>
          <div className="app-spacer" />
          <button className="btn" disabled={exporting} onClick={() => void doExport()}>
            {exporting ? `导出中 ${progress ? `${progress.done}/${progress.total}` : ''}` : '导出全部帧'}
          </button>
          <button className="btn ghost" disabled={exporting} onClick={() => void doExport([0])}>
            仅首帧
          </button>
          <button
            className="btn ghost"
            disabled={exporting}
            onClick={() => {
              const total = Math.max(1, Math.round((shot?.duration ?? 7) * (shot?.fps ?? 24)));
              void doExport([0, total - 1]);
            }}
          >
            仅首尾帧
          </button>
        </div>
      </div>

      {/* ---------------- 右栏：属性 ---------------- */}
      <aside
        className="inspector"
        style={{ width: 250, flex: '0 0 auto', overflowY: 'auto' }}
      >
        <h3>属性</h3>
        {!selected && <p className="ins-sub">在左栏或视口里选一个对象。</p>}

        {selActor && (
          <Section title={`人物 · ${selActor.name}`}>
            <label style={{ fontSize: 11, color: 'var(--s-violet-mid)' }}>名称</label>
            <input
              value={selActor.name}
              onChange={(e) => usePrevis.getState().updateActor(selActor.id, { name: e.target.value })}
              style={{ width: '100%', marginBottom: 8 }}
            />
            <Vec3Field
              label="位置（脚底）"
              value={selActor.position}
              onChange={(v) => usePrevis.getState().updateActor(selActor.id, { position: v })}
            />
            <NumField
              label="朝向°"
              value={(selActor.yaw * 180) / Math.PI}
              step={5}
              onChange={(v) => usePrevis.getState().updateActor(selActor.id, { yaw: (v * Math.PI) / 180 })}
            />
            <NumField
              label="身高倍"
              value={selActor.scale}
              step={0.05}
              onChange={(v) => usePrevis.getState().updateActor(selActor.id, { scale: Math.max(0.3, v) })}
            />

            <label style={{ fontSize: 11, color: 'var(--s-violet-mid)', display: 'block', marginTop: 8 }}>
              肢体动作
            </label>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, marginBottom: 8 }}>
              {POSE_PRESETS.map((p) => (
                <button
                  key={p.key}
                  className="btn ghost"
                  style={{
                    fontSize: 11,
                    padding: '3px 7px',
                    borderColor: selActor.pose === p.key ? 'var(--s-lime)' : undefined,
                    color: selActor.pose === p.key ? 'var(--s-lime)' : undefined,
                  }}
                  onClick={() => usePrevis.getState().updateActor(selActor.id, { pose: p.key })}
                >
                  {p.label}
                </button>
              ))}
            </div>

            {(selActor.pose === 'walk' || selActor.pose === 'run') && (
              <>
                <NumField
                  label="速度"
                  value={selActor.poseSpeed}
                  step={0.1}
                  onChange={(v) =>
                    usePrevis.getState().updateActor(selActor.id, { poseSpeed: Math.max(0.1, v) })
                  }
                />
                <NumField
                  label="相位"
                  value={selActor.posePhase}
                  step={0.05}
                  onChange={(v) =>
                    usePrevis.getState().updateActor(selActor.id, { posePhase: v })
                  }
                />
                <div className="hint">
                  相位让多人不要完全同步走路（0~1）。动作随时间自动推进，
                  导出序列里会自然循环，不需要逐帧摆。
                </div>
              </>
            )}
          </Section>
        )}

        {selProp && (
          <Section title={`道具 · ${selProp.name}`}>
            <label style={{ fontSize: 11, color: 'var(--s-violet-mid)' }}>名称</label>
            <input
              value={selProp.name}
              onChange={(e) => usePrevis.getState().updateProp(selProp.id, { name: e.target.value })}
              style={{ width: '100%', marginBottom: 8 }}
            />
            <Vec3Field
              label="位置（底面）"
              value={selProp.position}
              onChange={(v) => usePrevis.getState().updateProp(selProp.id, { position: v })}
            />
            <NumField
              label="朝向°"
              value={(selProp.yaw * 180) / Math.PI}
              step={5}
              onChange={(v) => usePrevis.getState().updateProp(selProp.id, { yaw: (v * Math.PI) / 180 })}
            />
            <Vec3Field
              label="体量"
              value={selProp.size}
              step={0.05}
              onChange={(v) => usePrevis.getState().updateProp(selProp.id, { size: v })}
            />
          </Section>
        )}

        {selKey && shot && (
          <Section title="相机关键帧">
            <NumField
              label="时间s"
              value={selKey.time}
              step={0.5}
              onChange={(v) => usePrevis.getState().updateCameraKey(selKey.id, { time: v })}
            />
            <div style={{ height: 6 }} />
            <Vec3Field
              label="机位"
              value={selKey.position}
              onChange={(v) => usePrevis.getState().updateCameraKey(selKey.id, { position: v })}
            />
            <Vec3Field
              label="看向"
              value={selKey.target}
              onChange={(v) => usePrevis.getState().updateCameraKey(selKey.id, { target: v })}
            />
            <NumField
              label="视场°"
              value={selKey.fov}
              step={2}
              onChange={(v) => usePrevis.getState().updateCameraKey(selKey.id, { fov: v })}
            />
            <button
              className="btn ghost"
              style={{ width: '100%', marginTop: 6 }}
              disabled={shot.cameraKeys.length <= 1}
              onClick={() => usePrevis.getState().removeCameraKey(selKey.id)}
            >
              删除该关键帧
            </button>
          </Section>
        )}

        {shot && (
          <Section title={`镜头 · ${shot.name}`}>
            <label style={{ fontSize: 11, color: 'var(--s-violet-mid)' }}>名称</label>
            <input
              value={shot.name}
              onChange={(e) => usePrevis.getState().updateShot({ name: e.target.value })}
              style={{ width: '100%', marginBottom: 8 }}
            />
            <NumField
              label="时长s"
              value={shot.duration}
              step={1}
              onChange={(v) => usePrevis.getState().updateShot({ duration: Math.max(1, v) })}
            />
            <NumField
              label="帧率"
              value={shot.fps}
              step={1}
              onChange={(v) => usePrevis.getState().updateShot({ fps: Math.max(1, Math.round(v)) })}
            />
            <button
              className="btn ghost"
              style={{ width: '100%', marginTop: 8 }}
              onClick={() => usePrevis.getState().addCameraKey(playhead)}
            >
              + 在 {playhead.toFixed(2)}s 加关键帧
            </button>
            <div className="hint">
              至少两个关键帧才能形成运镜。当前时刻相机姿态：
              {(() => {
                const p = sampleCamera(shot, playhead);
                return ` 机位(${p.position.map((v) => v.toFixed(1)).join(', ')})`;
              })()}
            </div>
          </Section>
        )}

        {msg && (
          <div
            style={{
              fontSize: 11,
              color: msg.ok ? 'var(--st-done)' : 'var(--st-failed)',
              marginTop: 6,
            }}
          >
            {msg.ok ? '✓' : '✕'} {msg.text}
          </div>
        )}
      </aside>
    </div>
  );
}
