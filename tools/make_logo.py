"""从公司商标图生成应用图标资源（主图标 / favicon）。

**只取图形部分**，丢掉下方 "BEILIANG" 与 "贝良" 文字 ——
侧边栏那个位置只有 26px，带文字上去必然糊成一团。

为什么把商标源图放进仓库（`brand/logo-source.png`）：
图标是**派生资源**，没有源图就没法重建。原图 941×1097 / 38KB，
缩到 470 宽（够生成 256px 图标）后约十几 KB，值得随仓库带走。
换新版商标时：把新图覆盖 `brand/logo-source.png`，重跑本脚本即可。

输出（都保留透明背景，深浅底色都适用）：
    static/assets/logo.png        256×256  侧边栏 / 登录页主图标
    static/assets/favicon.png     64×64    浏览器页签
    favicon.ico                   16/32/48 老浏览器与快捷方式（放**项目根**）

用法：
    python tools/make_logo.py
    python tools/make_logo.py <其它源图>          # 临时换图试效果

⚠️ 图标也在页面里被引用（common.js 的侧边栏、login.html 的品牌区）。
   换图后**不必**改 HTML，但要递增静态资源版本号 `?v=`，
   否则浏览器会沿用旧图标（表现为「换了没生效」）。
   跑 tools/check_deploy.py 与 tests/check_frontend.js 可确认没漏。
"""
import os
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SRC = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "brand" / "logo-source.png"
ASSETS = ROOT / "static" / "assets"
# favicon.ico 放项目根：URL 就是 /favicon.ico，根下同名文件与 URL 一一对应
# （测试脚本与后来的人都不用再想「这个 URL 映射到哪」）。
ROOT_ICO = ROOT / "favicon.ico"

# 图形部分的纵向边界（原图 941×1097：图形占 y 0→668，
# 再往下 726→892 是 BEILIANG、932→1096 是贝良）。
# 换新版商标时这个值要重量 —— 见 tools/measure_logo.py 或本文件末尾的说明。
MARK_BOTTOM_RATIO = 668 / 1097
# 四周留白比例：图标贴边会显得很挤
PAD_RATIO = 0.06


def content_bbox(im: Image.Image, alpha_min: int = 40,
                 white_max: int = 245) -> tuple:
    """有内容的包围盒（忽略透明与近白像素）。"""
    px = im.load()
    w, h = im.size
    xs, ys = [], []
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if a < alpha_min:
                continue
            if r > white_max and g > white_max and b > white_max:
                continue
            xs.append(x)
            ys.append(y)
    if not xs:
        raise SystemExit("源图里找不到任何有内容的像素（全透明或全白？）")
    return min(xs), min(ys), max(xs), max(ys)


def main() -> int:
    if not SRC.exists():
        print(f"找不到源图：{SRC}")
        print("把公司商标图放到这里，或用参数指定：python tools/make_logo.py <图>")
        return 1

    im = Image.open(SRC).convert("RGBA")
    # 按比例取图形部分（原图与放大版的构图一致，比例比绝对像素稳）
    bottom = int(im.height * MARK_BOTTOM_RATIO)
    mark = im.crop((0, 0, im.width, bottom))
    box = content_bbox(mark)
    mark = mark.crop((box[0], box[1], box[2] + 1, box[3] + 1))
    print(f"源图      : {im.width}×{im.height}")
    print(f"图形包围盒: {box}  →  裁出 {mark.width}×{mark.height}")

    # 放进正方形画布居中（图标必须是方的，否则会被拉伸/裁切）
    side = int(max(mark.width, mark.height) * (1 + PAD_RATIO * 2))
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(mark, ((side - mark.width) // 2, (side - mark.height) // 2),
                 mark)

    ASSETS.mkdir(parents=True, exist_ok=True)

    logo = canvas.resize((256, 256), Image.LANCZOS)
    logo.save(ASSETS / "logo.png", optimize=True)
    print(f"写出      : {ASSETS / 'logo.png'}  256×256")

    fav = canvas.resize((64, 64), Image.LANCZOS)
    fav.save(ASSETS / "favicon.png", optimize=True)
    print(f"写出      : {ASSETS / 'favicon.png'}  64×64")

    canvas.resize((256, 256), Image.LANCZOS).save(
        ROOT_ICO, format="ICO", sizes=[(16, 16), (32, 32), (48, 48)])
    print(f"写出      : {ROOT_ICO}  16/32/48")

    print("\n校验：")
    ok = True
    for p in (ASSETS / "logo.png", ASSETS / "favicon.png", ROOT_ICO):
        with Image.open(p) as chk:
            good = chk.mode in ("RGBA", "P") and chk.width == chk.height
            ok = ok and good
            print(f"  [{'OK ' if good else 'BAD'}] {p.name:12s} "
                  f"{chk.size} {chk.mode}")
    print("\n提醒：换了图标请递增页面里的 ?v= 版本号，再跑一次")
    print("      node tests/check_frontend.js 与 python tools/check_deploy.py")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
