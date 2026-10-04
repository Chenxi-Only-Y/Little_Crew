/**
 * 极简哈希路由。
 *
 * 为什么用哈希而不是 history API：
 * 这是纯静态构建（Vite 产物直接由任意静态服务托管），哈希路由**不需要服务端
 * 配 rewrite**，刷新任意页面都不会 404。四个页面用不上更复杂的方案。
 *
 * 为什么自己写而不引 react-router：
 * 需求只有"四个页面 + 前进后退"，引入一个路由库并让全部页面依赖它，
 * 收益不抵一次依赖升级的成本。这个实现只有几十行、行为完全可控。
 */

import { useEffect, useState } from 'react';

export type PageKey = 'home' | 'director' | 'previs' | 'user' | 'settings';

export interface PageMeta {
  key: PageKey;
  /** 导航栏标题 */
  label: string;
  /** 页面说明（首页入口卡片的 title） */
  desc: string;
  icon: string;
  /**
   * 不进悬浮导航（但路由仍可达）。
   *
   * 为什么需要：有些页是**开发/调试**用途（例如设计系统小样），
   * 让它们出现在用户导航里会把界面变吵，而删掉又会让它们难以访问。
   */
  hidden?: boolean;
}

/** 站点结构 —— 五页用户可见 + 隐藏页 */
export const PAGES: PageMeta[] = [
  { key: 'home', label: '首页', icon: '⌂', desc: '最近创作、常用入口' },
  { key: 'director', label: '导演台', icon: '⚏', desc: '在画布上编排流程并运行' },
  { key: 'previs', label: '预演', icon: '◲', desc: '搭白模、摆走位、录机位，导出结构帧' },
  { key: 'user', label: '用户', icon: '☺', desc: '昵称、头像与账号信息' },
  { key: 'settings', label: '设置', icon: '⚙', desc: '云端 / 本地、模型与密钥' },
];

const KEYS = new Set<string>(PAGES.map((p) => p.key));

/** 从当前 URL 解析页面；无法识别时回首页 */
export function currentPage(): PageKey {
  const raw = window.location.hash.replace(/^#\/?/, '').split('?')[0];
  return (KEYS.has(raw) ? raw : 'home') as PageKey;
}

export function navigate(page: PageKey) {
  // 用 pushState 语义（写 hash）保留浏览器前进/后退
  window.location.hash = `#/${page}`;
}

/** 订阅页面变化 */
export function usePage(): PageKey {
  const [page, setPage] = useState<PageKey>(currentPage);
  useEffect(() => {
    const onHash = () => setPage(currentPage());
    window.addEventListener('hashchange', onHash);
    // 首次进入若没有 hash，补一个，保证刷新后停在同一个页面
    if (!window.location.hash) window.location.replace('#/home');
    return () => window.removeEventListener('hashchange', onHash);
  }, []);
  return page;
}
