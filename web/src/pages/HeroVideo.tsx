/**
 * 首屏 MP4 背景。
 *
 * ## 素材放哪
 *
 * 把视频放到 `web/public/hero-bg.mp4`（或 `.webm`）。
 * Vite 会把 `public/` 下的文件原样拷到产物根目录，所以代码里用
 * 绝对路径 `/hero-bg.mp4` 引用即可，**不需要 import**。
 *
 * ⚠️ 用绝对路径而不是 `import` 的原因：`import` 会让 Vite 把视频
 * 当成构建产物去处理（哈希命名、并进 bundle 分析），而视频应当
 * **原样静态托管**——它不该被 inline 也不该参与代码分包。
 *
 * ## 没有素材时会怎样
 *
 * 组件**不报错、不留空白**：`onError` 时整个视频层淡出，
 * 露出下面的 `glow-stage` 光晕（那本来就是默认背景）。
 * 所以这个组件是**可选的增强**，不是必需依赖。
 *
 * ## 三条硬约束
 *
 * 1. **必须 `muted`**：浏览器只允许静音视频自动播放，非静音会被拦。
 * 2. **必须 `playsInline`**：iOS Safari 不加这个会强制全屏播放。
 * 3. **`prefers-reduced-motion` 下不自动播放**：连续视频对前庭敏感的
 *    用户最容易致晕，这比"好看"重要。此时显示 `poster` 静态帧。
 */

import { useEffect, useRef, useState } from 'react';

interface Props {
  /** 视频文件名（放在 `web/public/` 下），默认 `hero-bg.mp4` */
  src?: string;
  /** 视频加载失败/用户减少动效时显示的静态图（同样放 `public/`） */
  poster?: string;
}

export default function HeroVideo({ src = '/hero-bg.mp4', poster = '/hero-bg.jpg' }: Props) {
  const ref = useRef<HTMLVideoElement>(null);
  const [failed, setFailed] = useState(false);
  const [reduced, setReduced] = useState(false);

  useEffect(() => {
    const mq = window.matchMedia('(prefers-reduced-motion: reduce)');
    const apply = () => setReduced(mq.matches);
    apply();
    mq.addEventListener('change', apply);
    return () => mq.removeEventListener('change', apply);
  }, []);

  // 减少动效时主动暂停（仅靠 CSS 隐藏不够——视频还在解码、耗电）
  useEffect(() => {
    const v = ref.current;
    if (!v) return;
    if (reduced) {
      v.pause();
      return;
    }
    /**
     * ⚠️ `play()` 被拒**不等于加载失败**，两者必须分开。
     *
     * 我第一版写成 `play().catch(() => setFailed(true))`，
     * 结果自动播放被策略拦下时（无交互、省电模式、部分浏览器默认拦截），
     * 组件把自己整层隐藏了 —— 明明文件好好的却什么都不显示。
     * 实测：headless 下 404 只有一个（favicon），视频资源其实是 200，
     * 但 `data-failed` 已经是 true。这就是把"播放被拒"误判成"加载失败"。
     *
     * 正确行为：播放被拒就**保持暂停**，画面停在首帧/poster 上，
     * 不隐藏。只有真正的 `onError`（解码失败/文件缺失）才淡出。
     */
    void v.play().catch(() => {
      /* 自动播放被拦：保持暂停即可，不视为失败 */
    });
  }, [reduced]);

  return (
    <div className="hero-video" aria-hidden="true" data-failed={failed ? '1' : undefined}>
      <video
        ref={ref}
        className="hero-video-el"
        src={src}
        poster={reduced ? poster : undefined}
        muted
        loop
        playsInline
        autoPlay={!reduced}
        preload="metadata"
        onError={() => setFailed(true)}
      />
      {/* 压暗层：视频亮度不可控，不给这层的话标题会跟画面打架、读不出来 */}
      <div className="hero-video-scrim" />
    </div>
  );
}
