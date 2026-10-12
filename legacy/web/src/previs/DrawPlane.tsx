/**
 * 绘制轨道用的拾取面（在视口里点地面生成相机关键帧）。
 *
 * 为什么用一个专门的平面，而不是直接给地面绑点击事件：
 *   1. 绘制时要指定**高度**（离地 1.5 米的机位与贴地的走位线不是一回事），
 *      所以平面要能浮在指定高度上，而不是固定在地面。
 *   2. 地面本身带着 `helper` 标记、导出时会被隐藏；把交互逻辑挂在它上面
 *      会让"隐藏辅助线"顺带把交互也弄没。
 *   3. 只有处于绘制模式时才应该吃掉点击，否则会挡住拖拽人物与选中关键帧。
 *
 * 做法：把平面法线朝向相机的射线，用 Three.js 内置的 raycast（R3F 的
 * onPointerDown 就是它），拿到的交点即世界坐标。
 */

import { usePrevis } from './store';
import { PREVIS_COLOR } from './types';

export function DrawPlane() {
  const drawing = usePrevis((s) => s.drawing);
  const drawHeight = usePrevis((s) => s.drawHeight);
  const placeKeyAt = usePrevis((s) => s.placeKeyAt);

  if (!drawing) return null;

  return (
    <mesh
      rotation={[-Math.PI / 2, 0, 0]}
      position={[0, drawHeight, 0]}
      onPointerDown={(e) => {
        // 只在绘制模式下接管点击；平时这个平面根本不渲染
        e.stopPropagation();
        placeKeyAt([e.point.x, drawHeight, e.point.z]);
      }}
    >
      {/*
        尺寸取一个足够大的值：广场场景里用户可能点得很远。
        用 planeGeometry 而不是 gridHelper —— helper 会被导出隐藏，
        而这里只是编辑期的拾取面，不参与渲染结果。
      */}
      <planeGeometry args={[400, 400]} />
      <meshBasicMaterial color={PREVIS_COLOR.ACCENT} transparent opacity={0.06} depthWrite={false} />
    </mesh>
  );
}
