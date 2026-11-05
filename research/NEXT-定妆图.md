# 待办：生成 6 张真定妆图（2026-11-05 暂停处）

> **状态**：接线已完成（`alpha.164`），只差"真图"。
> 代码不再需要改 —— 把图放到 `assets/characters/<名字>/anchor.png`，链路就会自动用上。

---

## 一、已经做完的部分

| 件 | 状态 |
|---|---|
| `runtime/anchors.py`（发现 + 按角色匹配） | ✅ 9/9 自检 |
| `episode._bridge` 传 `anchor_sets=` | ✅ 真渲染验过（EA3 覆盖率 100%） |
| `delivery/anchors_used.json` | ✅ 每镜记录用了哪些锚点 |
| 占位图 | ✅ 已删除（不留假资产） |

---

## 二、要做的事（一步）

为 6 个角色各生成一张定妆图，存到：

```
assets/characters/少女/anchor.png
assets/characters/旅人/anchor.png
assets/characters/母亲/anchor.png
assets/characters/父亲/anchor.png
assets/characters/老奶奶/anchor.png
assets/characters/阿杏/anchor.png
```

**提示词直接取 `sheet.json` 里的 `promptFragment`**（已有，形如
「少年，结实挺拔身形，浅亚麻侧分长直发，琥珀眼睛，身穿素色长袍系腰带…」），
后面接定妆照风格后缀：

> ，正面半身定妆照，中性灰色背景，柔和均匀布光，清晰五官，电影质感

**源数据在**：`outputs/ep_white/assets/characters/*/sheet.json`

---

## 三、怎么跑（照抄）

```powershell
$LG = "C:\Users\OnlyY\.dsh\skills\local-gen\scripts\localgen.py"

# 1) 起服务（首次请求要 50~60 秒做 4bit 量化加载）
& "D:\Python\python.exe" $LG start --timeout 90

# 2) 逐张出（每张约 30~32 秒；768x1024 是 16 的倍数）
& "D:\Python\python.exe" $LG image "<promptFragment>，正面半身定妆照，中性灰色背景，柔和均匀布光，清晰五官，电影质感" `
    --out "D:\AI\ds.8\Little_Crew\assets\characters\少女" --size 768x1024 --seed 7

# 3) 出完把文件改名成 anchor.png（脚本输出名不是这个）
```

**注意**：
- 中文提示词**必须**通过 `localgen.py` 传（PowerShell 自己拼请求会把汉字变成 `?`）
- 一次只加载一个模型；出完图再出视频要重新加载
- 显存 < 4.5GB 空闲时先 `unload` 或关浏览器

---

## 四、跑完之后怎么验

```powershell
# 应该有 6 行 anchor=True
Get-ChildItem D:\AI\ds.8\Little_Crew\assets\characters -Directory |
  ForEach-Object { "$($_.Name)  anchor=$(Test-Path (Join-Path $_.FullName 'anchor.png'))" }

# 再跑一集，看 anchors_used.json 里的 table 是不是 6 个名字
```

**预期**：`anchors_used.json` 的 `table` 出现 6 个角色名，
`perShot` 里每个镜头对应到它出场的角色。
