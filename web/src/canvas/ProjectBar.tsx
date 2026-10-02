/**
 * 工程栏（F5）：保存 / 打开 / 另存为 / 新建。
 *
 * 一个"工程" = 画布形状（放哪些节点、连了哪些线、各节点参数与备注）。
 * 运行结果不在这里（那是「运行历史」的事）。密钥也不在（后端写入前已掩码）。
 */

import { useEffect, useState } from 'react';
import {
  deleteProject,
  listProjects,
  loadProject,
  renameProject,
  saveProject,
  type ProjectSummary,
} from '../api/projects';
import { useCanvas } from '../store/canvas';
import { getToken } from '../api/auth';

export default function ProjectBar() {
  const nodes = useCanvas((s) => s.nodes);
  const toDoc = useCanvas((s) => s.toDoc);
  const loadDoc = useCanvas((s) => s.loadDoc);
  const projectId = useCanvas((s) => s.projectId);
  const projectName = useCanvas((s) => s.projectName);
  const dirty = useCanvas((s) => s.dirty);
  const setProject = useCanvas((s) => s.setProject);
  const layoutFromRegistry = useCanvas((s) => s.layoutFromRegistry);
  const registry = useCanvas((s) => s.registry);
  const setDirty = useCanvas((s) => s.setDirty);

  const [name, setName] = useState(projectName);
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState<ProjectSummary[]>([]);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  // 切换工程后同步名称输入框
  useEffect(() => setName(projectName), [projectName]);

  const refresh = async () => {
    const res = await listProjects(getToken());
    if (res.ok) setItems(res.projects);
    else setMsg({ ok: false, text: res.error ?? '读取工程列表失败' });
  };

  const onSave = async (asNew = false) => {
    if (busy) return;
    setBusy(true);
    setMsg(null);
    const res = await saveProject(
      name || '未命名工程',
      toDoc(),
      {},                                    // 设置快照由设置页负责；这里不写，避免覆盖
      getToken(),
      asNew ? null : projectId,               // 另存为时不带 id
    );
    setBusy(false);
    if (res.ok && res.projectId) {
      setProject(res.projectId, name || '未命名工程');
      setMsg({ ok: true, text: asNew ? '已另存为新工程' : '已保存' });
      if (open) void refresh();
    } else {
      setMsg({ ok: false, text: res.error ?? '保存失败' });
    }
  };

  const onOpen = async (id: string) => {
    if (busy) return;
    // 有未保存改动时先确认，避免直接覆盖用户的工作
    if (dirty && !window.confirm('当前画布有未保存改动，打开其它工程会丢弃它们。继续？')) {
      return;
    }
    setBusy(true);
    setMsg(null);
    const res = await loadProject(id, getToken());
    setBusy(false);
    if (res.ok && res.project) {
      const dropped = loadDoc(res.project.canvas);
      setProject(res.project.project_id, res.project.name);
      // 静默丢弃会让画布与保存时不一致，而用户以为"打开成功、内容完好"
      const lost = dropped.droppedNodes.length;
      if (lost > 0) {
        setMsg({
          ok: false,
          text:
            `已打开「${res.project.name}」，但丢弃了 ${lost} 个当前版本已不存在的节点` +
            (dropped.droppedEdges ? `（及 ${dropped.droppedEdges} 条连线）` : '') +
            `：${dropped.droppedNodes.slice(0, 3).join('、')}${lost > 3 ? '…' : ''}`,
        });
      } else {
        setMsg({ ok: true, text: `已打开「${res.project.name}」` });
      }
      setOpen(false);
    } else {
      setMsg({ ok: false, text: res.error ?? '打开失败' });
    }
  };

  const onDelete = async (id: string, pname: string) => {
    if (!window.confirm(`删除工程「${pname}」？此操作不可撤销。`)) return;
    const res = await deleteProject(id, getToken());
    if (res.ok) {
      if (id === projectId) setProject(null, '未命名工程');
      void refresh();
      setMsg({ ok: true, text: '已删除' });
    } else {
      setMsg({ ok: false, text: res.error ?? '删除失败' });
    }
  };

  const onRename = async (id: string) => {
    const next = window.prompt('新的工程名');
    if (!next) return;
    const res = await renameProject(id, next, getToken());
    if (res.ok) {
      if (id === projectId) setProject(id, next);
      void refresh();
    } else {
      setMsg({ ok: false, text: res.error ?? '重命名失败' });
    }
  };

  const onNew = () => {
    if (dirty && !window.confirm('新建会清空当前画布（未保存的改动将丢失）。继续？')) return;
    if (registry) layoutFromRegistry(registry);
    setProject(null, '未命名工程');
    setDirty(false);
    setMsg({ ok: true, text: '已新建（回到默认布局）' });
  };

  return (
    <>
      <div className="runbar" style={{ gap: 6 }}>
        <span className="pill" title="工程名可直接编辑">
          📁
        </span>
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          style={{ width: 150 }}
          placeholder="工程名"
        />
        <button className="btn" onClick={() => void onSave(false)} disabled={busy || nodes.length === 0}>
          保存
        </button>
        <button className="btn ghost" onClick={() => void onSave(true)} disabled={busy || nodes.length === 0}>
          另存为
        </button>
        <button
          className="btn ghost"
          onClick={() => {
            setOpen((v) => !v);
            if (!open) void refresh();
          }}
        >
          打开{items.length && open ? ` (${items.length})` : ''}
        </button>
        <button className="btn ghost" onClick={onNew}>
          新建
        </button>
        {dirty && (
          <span className="pill" style={{ color: 'var(--st-review)' }} title="有未保存的改动">
            ● 未保存
          </span>
        )}
        {msg && (
          <span
            className="pill"
            style={{ color: msg.ok ? 'var(--st-done)' : 'var(--st-failed)' }}
          >
            {msg.ok ? '✓' : '✕'} {msg.text}
          </span>
        )}
      </div>

      {open && (
        <div
          className="runbar"
          style={{ display: 'block', maxHeight: 220, overflowY: 'auto', padding: '8px 12px' }}
        >
          {items.length === 0 ? (
            <div style={{ fontSize: 11, color: 'var(--s-muted)' }}>
              还没有保存过工程。点「保存」把当前画布存下来。
            </div>
          ) : (
            items.map((p) => (
              <div
                key={p.project_id}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 8,
                  padding: '5px 0',
                  borderBottom: '1px solid var(--s-hairline)',
                  fontSize: 12,
                }}
              >
                <span style={{ flex: '1 1 auto' }}>
                  {p.name}
                  <span style={{ color: 'var(--s-violet-mid)', marginLeft: 6, fontSize: 10 }}>
                    {p.node_count} 节点 · {p.updated.slice(0, 16)}
                    {p.project_id === projectId ? ' · 当前' : ''}
                  </span>
                </span>
                <button className="btn ghost" onClick={() => void onOpen(p.project_id)} disabled={busy}>
                  打开
                </button>
                <button className="btn ghost" onClick={() => void onRename(p.project_id)}>
                  改名
                </button>
                <button className="btn ghost" onClick={() => void onDelete(p.project_id, p.name)}>
                  删除
                </button>
              </div>
            ))
          )}
        </div>
      )}
    </>
  );
}
