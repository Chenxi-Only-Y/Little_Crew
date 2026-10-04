/**
 * 首屏 MP4 背景（**固定不动**：页面滚动时画面不跟着移）。
 *
 * ## 素材放哪
 *
 * 把视频放到 `web/public/hero-bg.mp4`。Vite 会把 `public/` 下的文件
 * 原样拷到产物根目录，所以代码里用绝对路径 `/hero-bg.mp4` 引用即可，
 * **不需要 import**（`import` 会让 Vite 把它当构建产物做哈希命名、
 * 参与分包分析，而视频应当原样静态托管）。
 *
 * ## "固定不动"是怎么实现的
 *
 * 三层配合：
 *   1. `.hero-video` 是 `position: fixed; inset: 0` —— 铺满**视口**，
 *      滚动时天然不移动（`absolute` 会跟着内容走，所以不能用它）；
 *   2. 但它必须**只出现在首屏范围内**，不能盖住下面那些数据卡片 ——
 *      所以用 `clip-path: inset(...)` 把它裁到首屏当前的可视矩形；
 *   3. 裁剪值随滚动实时更新（`requestAnimationFrame` 里算，
 *      并用 `IntersectionObserver` 在首屏完全离开视口后停止计算，
 *      避免白跑）。
 *
 * ⚠️ 为什么不用"给祖先加 `overflow: hidden` 来裁 fixed 子元素"：
 * `overflow` **不会**裁 `position: fixed` 的后代（除非祖先带
 * transform/filter/will-change，而那又会把 fixed 变成"相对该祖先定位"，
 * 反而破坏"固定不动"）。`clip-path` 是这个需求下唯一干净的做法。
 */
import { useEffect, useRef } from 'react';

interface Props {
  /** 视频文件名（放在 `web/public/` 下），默认 `hero-bg.mp4` */
  src?: string;
}

export default function HeroVideo({ src = '/hero-bg.mp4' }: Props) {
  const hostRef = useRef<HTMLDivElement>(null);
  const videoRef = useRef<HTMLVideoElement>(null);

  /**
   * 把固定的视频层裁成"首屏当前在视口里的那一条"。
   *
   * 为什么用 `getBoundingClientRect()` 直接取视口坐标、而不去推算
   * `.page` 的 margin/padding：实测 `.page` 有 `margin-top: 56px` +
   * `padding-top: 64px`，`.hero` 的顶边在 y=120。任何"从父级往上顶多少"
   * 的写法都要同时算对这两个值，脆且容易错（我错过一次，结果是屏幕顶部
   * 留下一条 `.app-shell` 的不透明底色，看起来就像"导航栏不透明"）。
   *
   * `r.top` 就是首屏顶边在视口里的真实位置，直接拿它当裁剪上界，
   * 顺带把"上面那 56px 导航让位"也一起包含进来了。
   */
  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;

    let raf = 0;
    const apply = () => {
      raf = 0;
      const r = host.getBoundingClientRect();
      const vh = window.innerHeight;
      const top = Math.max(0, r.top);
      const bottom = Math.min(vh, r.bottom);
      if (bottom <= top) {
        host.style.visibility = 'hidden';   // 首屏已完全离开视口，不用再算
        return;
      }
      host.style.visibility = '';
      host.style.clipPath = `inset(${top}px 0 ${vh - bottom}px 0)`;
    };
    const schedule = () => {
      if (raf) return;
      raf = requestAnimationFrame(apply);
    };

    apply();

    // 滚动发生在内容容器 `.page` 上（它有 overflow-y），不是 window
    const scroller = document.querySelector('.page') ?? window;
    scroller.addEventListener('scroll', schedule, { passive: true });
    window.addEventListener('resize', schedule);

    // 首屏完全离开视口后就没必要继续算了
    const io = new IntersectionObserver(schedule, { threshold: [0, 0.01, 0.5, 1] });
    io.observe(host);

    return () => {
      scroller.removeEventListener('scroll', schedule);
      window.removeEventListener('resize', schedule);
      io.disconnect();
      if (raf) cancelAnimationFrame(raf);
    };
  }, []);

  /**
   * 自动播放：踩过三个坑，全部记在这里。
   *
   * 1. **`muted` 必须是 HTML 属性，不只是 React property。**
   *    React 把 `muted` 当 property 处理、不输出成属性，而浏览器的自动播放
   *    策略读的是**属性**。所以挂载时手动 `setAttribute('muted', '')`。
   *
   * 2. **不要用"减少动态效果"去关掉自动播放。** ← 用户报"MP4 不播放"的真凶
   *    我先前写了 `autoPlay={!reduced}`，后来又加了 `if (reduced) v.pause()`。
   *    结果：只要系统开着"减少动态效果"，视频**永远停在第一帧**。
   *    实测（同一浏览器、同一份 mp4）：
   *      裸 HTML `<video autoplay muted loop playsinline>` → currentTime 3.94 → 6.94 ✅
   *      我的组件（带 reduced 分支）                      → 一直 0、paused     ❌
   *    结论：**背景视频无条件播放**。可读性由压暗层保证，不靠停视频。
   *
   * 3. **`play()` 被拒 ≠ 加载失败。** 不能 `play().catch(() => setFailed(true))`
   *    —— 那会在自动播放被拦时把整层隐藏，文件明明好好的却什么都不显示。
   *    播放被拒就保持暂停，画面停在首帧。
   */
  useEffect(() => {
    const v = videoRef.current;
    if (!v) return;
    v.muted = true;
    v.setAttribute('muted', '');
    v.defaultMuted = true;
    v.setAttribute('playsinline', '');
    void v.play().catch(() => {
      /* 自动播放被拦：保持暂停即可，画面停在首帧，不隐藏 */
    });
  }, []);

  return (
    /* 外层只负责"锚定位置 + 裁剪"，自身没有视觉 */
    <div className="hero-video-clip" ref={hostRef} aria-hidden="true">
      <div className="hero-video">
        <video
          ref={videoRef}
          className="hero-video-el"
          src={src}
          autoPlay
          muted
          loop
          playsInline
          preload="auto"
        />
        {/* 压暗层：整块均匀 20% 黑（见 tokens.css 里的说明） */}
        <div className="hero-video-scrim" />
      </div>
    </div>
  );
}
