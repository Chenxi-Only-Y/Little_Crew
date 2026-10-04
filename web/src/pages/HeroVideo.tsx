/**
 * 首屏 MP4 背景。
 *
 * ## 素材放哪
 *
 * 把视频放到 `web/public/hero-bg.mp4`。Vite 会把 `public/` 下的文件
 * 原样拷到产物根目录，所以代码里用绝对路径 `/hero-bg.mp4` 引用即可，
 * **不需要 import**（`import` 会让 Vite 把它当构建产物做哈希命名、
 * 参与分包分析，而视频应当原样静态托管）。
 *
 * ## 没有素材时会怎样
 *
 * `onError` 时整个视频层淡出，露出下面的 `glow-stage` 光晕，
 * 所以这个组件是**可选增强**，不是必需依赖。
 *
 * ## 自动播放：踩了三个坑，全部记在这里
 *
 * 1. **`muted` 必须是 HTML 属性，不只是 React property。**
 *    React 把 `muted` 当 property 处理、不输出成属性，而浏览器的自动播放
 *    策略读的是**属性**。所以挂载时手动 `setAttribute('muted', '')`。
 *
 * 2. **不要用"减少动态效果"去关掉自动播放。** ← 这是用户报"MP4 不播放"的真凶
 *    我先前写了 `autoPlay={!reduced}`，后来又加了 `if (reduced) v.pause()`。
 *    结果：只要系统开着"减少动态效果"，`reduced` 就是 `true`，
 *    视频**永远停在第一帧**——正是用户看到的现象。
 *
 *    实测证据（同一台无头浏览器、同一份 mp4）：
 *      · 裸 HTML `<video autoplay muted loop playsinline>` → currentTime 3.94 → 6.94 ✅
 *      · 我的组件（带 reduced 分支）                      → 一直 0、paused ✅
 *    说明文件、浏览器策略、`muted` 修复都没问题，**就是这段 pause 的锅**。
 *
 *    结论：**背景视频无条件播放**。用户明确要它播放，我不该用一条
 *    自己加的"无障碍考虑"把明确需求挡掉。文字可读性由压暗层保证，
 *    不靠"停掉视频"。
 *
 * 3. **`play()` 被拒 ≠ 加载失败。** 不能 `play().catch(() => setFailed(true))`
 *    —— 那会在自动播放被拦时把整层隐藏，文件明明好好的却什么都不显示
 *    （实测过：资源 200、readyState=4，而 data-failed 已是 true）。
 *    播放被拒就保持暂停，画面停在首帧。
 */

import { useEffect, useRef, useState } from 'react';

interface Props {
  /** 视频文件名（放在 `web/public/` 下），默认 `hero-bg.mp4` */
  src?: string;
}

export default function HeroVideo({ src = '/hero-bg.mp4' }: Props) {
  const ref = useRef<HTMLVideoElement>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    const v = ref.current;
    if (!v) return;

    // 坑 1：属性必须手动补，否则自动播放被拒
    v.muted = true;
    v.setAttribute('muted', '');
    v.defaultMuted = true;
    v.setAttribute('playsinline', '');

    // 坑 2 的收尾：元素带 autoPlay 时浏览器会自己播；这里再显式 play()
    // 以覆盖"属性被某些扩展/环境剥掉"的情况。被拒不算失败（坑 3）。
    void v.play().catch(() => {
      /* 自动播放被拦：保持暂停即可，画面停在首帧，不隐藏 */
    });
  }, []);

  return (
    <div className="hero-video" aria-hidden="true" data-failed={failed ? '1' : undefined}>
      <video
        ref={ref}
        className="hero-video-el"
        src={src}
        autoPlay
        muted
        loop
        playsInline
        preload="auto"
        onError={() => setFailed(true)}
      />
      {/* 压暗层：视频亮度不可控，不给这层标题会跟画面打架、读不出来 */}
      <div className="hero-video-scrim" />
    </div>
  );
}
