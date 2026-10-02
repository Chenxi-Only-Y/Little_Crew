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
import { sampleCamera, activeTrack as activeTrackAt, migrateShot, type PrevisScene, type Vec3 } from './types';
import { POSE_PRESETS, JOINT_GROUPS, JOINT_LABELS, AXIS_LABELS } from './humanoid';
import { ENV_PRESETS } from './env';
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

  const shot = scene.shots[shotIndex] ? migrateShot(scene.shots[shotIndex]) : undefined;
  const selActor = selected?.kind === 'actor' ? scene.actors.find((a) => a.id === selected.id) : null;
  const selProp = selected?.kind === 'prop' ? scene.props.find((p) => p.id === selected.id) : null;
  const activeTrackId = usePrevis((s) => s.activeTrackId);
  const drawing = usePrevis((s) => s.drawing);
  const drawHeight = usePrevis((s) => s.drawHeight);
  /** 选中的相机关键帧 + 它所属的机位（多机位下必须知道是哪条轨道） */
  const selKeyHit = (() => {
    if (selected?.kind !== 'camera' || !shot) return null;
    for (const t of shot.cameraTracks) {
      const k = t.keys.find((x) => x.id === selected.id);
      if (k) return { track: t, key: k };
    }
    return null;
  })();
  const activeTrack =
    shot?.cameraTracks.find((t) => t.id === activeTrackId) ?? shot?.cameraTracks[0];
  /** 当前生效的机位（按播放头分派）——用于提示"现在切到哪个机位了" */
  const liveTrack = shot ? activeTrackAt(shot, playhead) : null;

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
      const shotNow = migrateShot(scene.shots[shotIndex]);
      const pose = sampleCamera(scene, shotNow, 0);
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

        <div className="card-title" style={{ marginTop: 14 }}>环境</div>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, marginBottom: 8 }}>
          {ENV_PRESETS.map((p) => (
            <button
              key={p.key}
              className="btn ghost"
              style={{ fontSize: 11, padding: '3px 7px' }}
              title={p.hint}
              onClick={() => usePrevis.getState().applyEnv(p.key)}
            >
              {p.label}
            </button>
          ))}
        </div>

        <div className="card-title" style={{ marginTop: 10 }}>道具</div>
        <button className="btn ghost" style={{ width: '100%', marginBottom: 6 }} onClick={() => usePrevis.getState().addProp()}>
          + 道具
        </button>
        {scene.props.filter((p) => !p.env).map((p) => (
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

        {/* 环境块：只在 UI 上与道具分开列，渲染/导出完全一视同仁 */}
        {scene.props.some((p) => p.env) && (
          <>
            <div className="card-title" style={{ marginTop: 10 }}>
              环境块（{scene.props.filter((p) => p.env).length}）
            </div>
            {scene.props.filter((p) => p.env).map((p) => (
              <div
                key={p.id}
                onClick={() => usePrevis.getState().select({ kind: 'prop', id: p.id })}
                style={{
                  fontSize: 11,
                  padding: '3px 6px',
                  borderRadius: 5,
                  cursor: 'pointer',
                  display: 'flex',
                  alignItems: 'center',
                  gap: 6,
                  opacity: 0.8,
                  background:
                    selected?.kind === 'prop' && selected.id === p.id
                      ? 'rgba(200,240,74,0.14)'
                      : 'transparent',
                }}
              >
                <span style={{ fontSize: 9 }}>👁</span>
                {p.name}
              </div>
            ))}
          </>
        )}

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
              {s.duration}s · {migrateShot(s).cameraTracks.length} 机位 ·{' '}
              {migrateShot(s).cameraTracks.reduce((n, t) => n + t.keys.length, 0)} 关键帧
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

          {/* 绘制轨道：在视口里点一下就落一个关键帧 */}
          <label
            className="pill"
            style={{ gap: 4, cursor: 'pointer', color: drawing ? 'var(--s-lime)' : undefined }}
            title="打开后在视口里点击即可放置相机关键帧（落在当前机位上）"
          >
            <input
              type="checkbox"
              checked={drawing}
              onChange={(e) => usePrevis.getState().setDrawing(e.target.checked)}
            />
            绘制轨道
          </label>
          {drawing && (
            <>
              <span className="pill">高度</span>
              <input
                type="number"
                step={0.1}
                value={drawHeight}
                onChange={(e) => usePrevis.getState().setDrawHeight(Number(e.target.value))}
                style={{ width: 62 }}
              />
              <span className="pill" style={{ color: 'var(--s-violet-mid)' }}>
                点视口放帧 · 落在「{activeTrack?.name ?? '—'}」
              </span>
            </>
          )}
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

            {/* 逐关节微调：叠加在预设之上，所以"行走"不会因调角而定格 */}
            <details style={{ marginTop: 10 }}>
              <summary style={{ cursor: 'pointer', fontSize: 11, color: 'var(--s-white)' }}>
                逐关节微调
                <span style={{ color: 'var(--s-violet-mid)', marginLeft: 6 }}>
                  {Object.keys(selActor.poseOverride ?? {}).length} 处已改
                </span>
              </summary>
              <div className="hint" style={{ marginTop: 4 }}>
                角度是**叠加偏移**，加在「{selActor.pose}」预设之上——
                所以微调不会让循环动作定格。单位：度。
              </div>
              {Object.keys(selActor.poseOverride ?? {}).length > 0 && (
                <button
                  className="btn ghost"
                  style={{ width: '100%', margin: '6px 0', fontSize: 11 }}
                  onClick={() => usePrevis.getState().clearAllJoints(selActor.id)}
                >
                  全部还原
                </button>
              )}
              {JOINT_GROUPS.map((g) => {
                const changed = g.joints.filter(
                  (j) => selActor.poseOverride?.[j]?.some((v) => Math.abs(v) > 1e-6),
                ).length;
                return (
                  <details key={g.label} style={{ marginTop: 4, paddingLeft: 6 }}>
                    <summary style={{ cursor: 'pointer', fontSize: 11 }}>
                      {g.label}
                      {changed > 0 && (
                        <span style={{ color: 'var(--s-lime)', marginLeft: 5 }}>•{changed}</span>
                      )}
                    </summary>
                    {g.joints.map((joint) => {
                      const off = selActor.poseOverride?.[joint] ?? [0, 0, 0];
                      const any = off.some((v) => Math.abs(v) > 1e-6);
                      return (
                        <div key={joint} style={{ margin: '5px 0 5px 6px' }}>
                          <div
                            style={{
                              display: 'flex',
                              alignItems: 'center',
                              gap: 6,
                              fontSize: 10,
                              color: any ? 'var(--s-lime)' : 'var(--s-muted)',
                            }}
                          >
                            <span>{JOINT_LABELS[joint] ?? joint}</span>
                            {any && (
                              <button
                                className="btn ghost"
                                style={{ fontSize: 9, padding: '1px 5px', marginLeft: 'auto' }}
                                onClick={() => usePrevis.getState().clearJoint(selActor.id, joint)}
                              >
                                还原
                              </button>
                            )}
                          </div>
                          {[0, 1, 2].map((ax) => (
                            <div key={ax} style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                              <span
                                style={{
                                  fontSize: 9,
                                  color: 'var(--s-violet-mid)',
                                  width: 46,
                                  flex: '0 0 auto',
                                }}
                                title={AXIS_LABELS[ax]}
                              >
                                {AXIS_LABELS[ax]}
                              </span>
                              <input
                                type="range"
                                min={-180}
                                max={180}
                                step={5}
                                value={Math.round(((off[ax] ?? 0) * 180) / Math.PI)}
                                style={{ flex: '1 1 auto', minWidth: 0 }}
                                onChange={(e) =>
                                  usePrevis
                                    .getState()
                                    .setJointOffset(
                                      selActor.id,
                                      joint,
                                      ax as 0 | 1 | 2,
                                      (Number(e.target.value) * Math.PI) / 180,
                                    )
                                }
                              />
                              <span style={{ fontSize: 9, width: 26, textAlign: 'right' }}>
                                {Math.round(((off[ax] ?? 0) * 180) / Math.PI)}
                              </span>
                            </div>
                          ))}
                        </div>
                      );
                    })}
                  </details>
                );
              })}
            </details>
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

        {selKeyHit && shot && (
          <Section title={`相机关键帧 · ${selKeyHit.track.name}`}>
            <NumField
              label="时间s"
              value={selKeyHit.key.time}
              step={0.5}
              onChange={(v) =>
                usePrevis
                  .getState()
                  .updateCameraKey(selKeyHit.track.id, selKeyHit.key.id, { time: v })
              }
            />
            <div style={{ height: 6 }} />
            <Vec3Field
              label="机位"
              value={selKeyHit.key.position}
              onChange={(v) =>
                usePrevis
                  .getState()
                  .updateCameraKey(selKeyHit.track.id, selKeyHit.key.id, { position: v })
              }
            />

            {/* 看向方式：固定点 / 锁定跟随某个角色 */}
            <label style={{ fontSize: 11, color: 'var(--s-violet-mid)', display: 'block', marginTop: 6 }}>
              看向方式
            </label>
            <select
              value={selKeyHit.key.aim?.kind === 'actor' ? `actor:${selKeyHit.key.aim.actorId}` : 'fixed'}
              onChange={(e) => {
                const v = e.target.value;
                if (v === 'fixed') {
                  usePrevis
                    .getState()
                    .setKeyAim(selKeyHit.track.id, selKeyHit.key.id, undefined);
                } else {
                  const actorId = v.slice('actor:'.length);
                  usePrevis.getState().setKeyAim(selKeyHit.track.id, selKeyHit.key.id, {
                    kind: 'actor',
                    actorId,
                    height: 1.1,
                  });
                }
              }}
              style={{ width: '100%', marginBottom: 8 }}
            >
              <option value="fixed">固定目标（看向某个点）</option>
              {scene.actors.map((a) => (
                <option key={a.id} value={`actor:${a.id}`}>
                  锁定跟随 · {a.name}
                </option>
              ))}
            </select>

            {selKeyHit.key.aim?.kind === 'actor' ? (
              <>
                <NumField
                  label="视线高"
                  value={selKeyHit.key.aim.height}
                  step={0.1}
                  onChange={(v) =>
                    usePrevis.getState().setKeyAim(selKeyHit.track.id, selKeyHit.key.id, {
                      kind: 'actor',
                      actorId: (selKeyHit.key.aim as { actorId: string }).actorId,
                      height: v,
                    })
                  }
                />
                <div className="hint">
                  锁定目标取「角色位置 + 视线高度」，不是角色原点（脚底）——
                  否则机位一靠近，看向点就落到脚下、画面变成仰拍地板。
                </div>
              </>
            ) : (
              <Vec3Field
                label="看向"
                value={selKeyHit.key.target}
                onChange={(v) =>
                  usePrevis
                    .getState()
                    .updateCameraKey(selKeyHit.track.id, selKeyHit.key.id, { target: v })
                }
              />
            )}

            <NumField
              label="视场°"
              value={selKeyHit.key.fov}
              step={2}
              onChange={(v) =>
                usePrevis.getState().updateCameraKey(selKeyHit.track.id, selKeyHit.key.id, { fov: v })
              }
            />
            <button
              className="btn ghost"
              style={{ width: '100%', marginTop: 6 }}
              disabled={selKeyHit.track.keys.length <= 1}
              onClick={() =>
                usePrevis.getState().removeCameraKey(selKeyHit.track.id, selKeyHit.key.id)
              }
            >
              删除该关键帧
            </button>
          </Section>
        )}

        {/* ---------------- 多机位 ---------------- */}
        {shot && (
          <Section title={`机位（${shot.cameraTracks.length}）`}>
            <div className="hint" style={{ marginBottom: 6 }}>
              多条机位会按数组顺序**均分片段时长**自动切换——
              这就是正反打/多机位调度的做法。也可以给每条填起止秒数精确排期。
            </div>
            {shot.cameraTracks.map((t, i) => {
              const isActive = t.id === (activeTrack?.id ?? '');
              const isLive = liveTrack?.id === t.id;
              return (
                <div
                  key={t.id}
                  style={{
                    padding: '5px 7px',
                    marginBottom: 4,
                    borderRadius: 6,
                    fontSize: 11,
                    cursor: 'pointer',
                    background: isActive ? 'rgba(200,240,74,0.12)' : 'transparent',
                    border: `1px solid ${isLive ? 'var(--s-lime)' : 'var(--s-hairline)'}`,
                  }}
                  onClick={() => usePrevis.getState().setActiveTrack(t.id)}
                >
                  <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                    <span style={{ color: isLive ? 'var(--s-lime)' : undefined }}>
                      {isLive ? '● ' : ''}
                      {t.name}
                    </span>
                    <span style={{ color: 'var(--s-violet-mid)', fontSize: 10 }}>
                      {t.keys.length} 帧
                      {t.start != null || t.end != null
                        ? ` · ${t.start ?? 0}~${t.end ?? shot.duration}s`
                        : ` · 均分第${i + 1}段`}
                    </span>
                    <button
                      className="btn ghost"
                      style={{ marginLeft: 'auto', fontSize: 9, padding: '1px 5px' }}
                      disabled={shot.cameraTracks.length <= 1}
                      onClick={(e) => {
                        e.stopPropagation();
                        usePrevis.getState().removeTrack(t.id);
                      }}
                    >
                      删
                    </button>
                  </div>
                  {isActive && (
                    <div style={{ marginTop: 4, display: 'flex', gap: 4 }}>
                      <input
                        value={t.name}
                        onChange={(e) =>
                          usePrevis.getState().updateTrack(t.id, { name: e.target.value })
                        }
                        style={{ flex: '1 1 auto', fontSize: 10, minWidth: 0 }}
                      />
                      <button
                        className="btn ghost"
                        style={{ fontSize: 9, padding: '1px 5px' }}
                        onClick={() => usePrevis.getState().addCameraKey(playhead, t.id)}
                      >
                        +帧
                      </button>
                    </div>
                  )}
                </div>
              );
            })}
            <button
              className="btn ghost"
              style={{ width: '100%', marginTop: 4 }}
              onClick={() => usePrevis.getState().addTrack()}
            >
              + 机位
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
              至少两个关键帧才能形成运镜。当前生效机位：{liveTrack?.name ?? '—'}
              {(() => {
                const p = sampleCamera(scene, shot, playhead);
                return ` · 机位(${p.position.map((v) => v.toFixed(1)).join(', ')})`;
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
