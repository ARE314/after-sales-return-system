"""照片证据的存取 —— 原图 + 缩略图，按售后单分目录。

**存储约定**
- 文件落 `data/photos/<售后单号>/<明细键>-NN.<ext>`
- 同时生成 `<明细键>-NN.thumb.jpg`（长边 320px）供列表快速加载，原图完整保留
- 字段 `photo_evidence` 里只存**文件名**，不存路径 ——
  目录可由售后单号推出，将来整体搬迁 `data/` 不会失效。
  值形态：只有照片 → `["a.jpg"]`；照片与历史文本并存 →
  `{"photos": ["a.jpg"], "legacy": "…"}`。

**旧值兼容**
字段值若解析不出 JSON（如金山导入的内嵌图片公式 `=DISPIMG("ID_…")`），
一律按「历史文本」原样保留。**后续再上传照片时该文本也不会被覆盖** ——
两者并存写在同一个字段里，不丢数据、也不需要迁移脚本。

**回收路径**
照片是记录派生的附属文件，删除明细时必须一并清理（`delete_all`），
否则会留下永远访问不到的死图 —— 与字典 / 型号字典遵循同一条原则。
"""
import json
import re
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from config import (PHOTO_DIR, PHOTO_EXTS, PHOTO_MAX_MB, PHOTO_THUMB_QUALITY,
                    PHOTO_THUMB_SIZE, PHOTO_TRASH_DIR)

THUMB_SUFFIX = ".thumb.jpg"

# 只允许安全字符 —— 文件名来自系统生成，但删除接口的入参来自请求，必须过滤
_UNSAFE = re.compile(r"[^0-9A-Za-z._\u4e00-\u9fff-]")


class PhotoError(ValueError):
    """上传/删除照片时的可预期错误（由接口层转成 400/404）。"""


# ---------------------------------------------------------------------------
# 路径与命名
# ---------------------------------------------------------------------------

def order_no_of(detail_key: str) -> str:
    """从明细唯一键推出售后单号：`20260057-002` → `20260057`。"""
    key = str(detail_key or "").strip()
    return key.rsplit("-", 1)[0] if "-" in key else key


def _clean(text: str) -> str:
    return _UNSAFE.sub("_", str(text or "").strip()) or "_"


def dir_of(order_no: str, create: bool = True) -> Path:
    """按售后单号取照片目录。

    create=True（默认）用于写入路径，目录不存在就建；
    create=False 用于**只读探测**（例如删除后检查文件是否真的没了）——
    只读场景绝不能顺手建目录，否则「删干净了」也会留下一个空目录。
    """
    path = PHOTO_DIR / _clean(order_no)
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def dir_for(detail_key: str, order_no: str = "") -> Path:
    """该明细所属的照片目录（不存在则创建）。"""
    return dir_of(order_no or order_no_of(detail_key))


def thumb_name(name: str) -> str:
    """`a.jpg` → `a.thumb.jpg`。"""
    stem = name.rsplit(".", 1)[0] if "." in name else name
    return f"{stem}{THUMB_SUFFIX}"


def public_url(order_no: str, name: str) -> str:
    """静态访问路径 —— 与 app.py 里 `/photos` 挂载点对应。"""
    return f"/photos/{_clean(order_no)}/{name}"


# ---------------------------------------------------------------------------
# 字段值编解码
# ---------------------------------------------------------------------------

def parse_value(value) -> tuple:
    """拆解字段值 → (文件名列表, 历史文本)。

    支持的三种形态：
    - 空值            → `([], "")`
    - 文件名数组       → `(["a.jpg"], "")`（只有照片时的紧凑写法）
    - 对象             → `({"photos": [...], "legacy": "…"})`（照片 + 历史文本并存）
    - 其他文本         → `([], 原文)`（金山内嵌图片公式等历史值，原样保留）
    """
    text = str(value or "").strip()
    if not text:
        return [], ""
    if text.startswith("["):
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            return [], text
        if isinstance(data, list):
            names = [str(x).strip() for x in data if str(x or "").strip()]
            return names, ""
        return [], text
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            return [], text
        if isinstance(data, dict) and ("photos" in data or "legacy" in data):
            raw_names = data.get("photos") or []
            names = [str(x).strip() for x in raw_names
                     if isinstance(raw_names, list) and str(x or "").strip()]
            return names, str(data.get("legacy") or "").strip()
    return [], text


def dump_value(names, legacy: str = "") -> str:
    """文件名列表（+ 可选历史文本）→ 字段值。

    全空时存空串，便于「未填」判定；有照片且无历史文本时用紧凑的数组写法。
    """
    clean = [str(n).strip() for n in (names or []) if str(n or "").strip()]
    old = str(legacy or "").strip()
    if not clean and not old:
        return ""
    if not old:
        return json.dumps(clean, ensure_ascii=False)
    return json.dumps({"photos": clean, "legacy": old}, ensure_ascii=False)


def describe(order_no: str, names) -> list:
    """给接口返回一份可直接用的清单（含原图与缩略图地址）。"""
    out = []
    for n in (names or []):
        out.append({
            "name": n,
            "url": public_url(order_no, n),
            "thumb": public_url(order_no, thumb_name(n)),
        })
    return out


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------

def _ext_of(filename: str) -> str:
    name = str(filename or "")
    return ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""


def _validate(raw: bytes, filename: str) -> str:
    """校验单个上传文件，返回规范化的扩展名。不通过则抛 PhotoError。"""
    if not raw:
        raise PhotoError(f"「{filename or '未命名文件'}」是空文件")
    limit = PHOTO_MAX_MB * 1024 * 1024
    if len(raw) > limit:
        raise PhotoError(
            f"「{filename}」{len(raw) / 1048576:.1f} MB，"
            f"超过单张上限 {PHOTO_MAX_MB} MB")
    ext = _ext_of(filename)
    if ext not in PHOTO_EXTS:
        raise PhotoError(
            f"「{filename}」格式不支持（可用：{'、'.join(PHOTO_EXTS)}）")
    try:
        with Image.open(BytesIO(raw)) as im:
            im.verify()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise PhotoError(f"「{filename}」不是可识别的图片（{exc}）") from exc
    return ext


def _write_thumb(raw: bytes, dest: Path) -> None:
    """生成缩略图，并按 EXIF 方向转正（手机照片常带旋转标记）。"""
    with Image.open(BytesIO(raw)) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
            rgba = im.convert("RGBA")
            canvas = Image.new("RGB", rgba.size, (255, 255, 255))
            canvas.paste(rgba, mask=rgba.split()[-1])
            im = canvas
        elif im.mode != "RGB":
            im = im.convert("RGB")
        im.thumbnail(PHOTO_THUMB_SIZE, Image.LANCZOS)
        im.save(dest, "JPEG", quality=PHOTO_THUMB_QUALITY, optimize=True)


def _next_index(directory: Path, detail_key: str) -> int:
    """该明细已有序号的最大值 + 1（文件名形如 `<明细键>-NN.ext`）。"""
    prefix = f"{_clean(detail_key)}-"
    max_idx = 0
    for p in directory.iterdir():
        if not p.is_file() or not p.name.startswith(prefix):
            continue
        tail = p.name[len(prefix):]
        num = tail.split(".")[0]
        if num.isdigit():
            max_idx = max(max_idx, int(num))
    return max_idx + 1


def save_uploads(detail_key: str, order_no: str, files: list,
                 existing: list, max_count: int = 0) -> list:
    """保存一批上传文件，返回**更新后的完整文件名列表**。

    files —— [(原始文件名, 字节内容), …]
    先整批校验再落盘：任一张不合格就整批拒绝，不会出现「传了一半」。
    """
    from config import PHOTO_MAX_PER_RECORD

    limit = max_count or PHOTO_MAX_PER_RECORD
    existing = [str(n).strip() for n in (existing or []) if str(n or "").strip()]
    if len(existing) + len(files) > limit:
        raise PhotoError(
            f"每条明细最多 {limit} 张，当前已有 {len(existing)} 张，"
            f"本次要传 {len(files)} 张（超出 {len(existing) + len(files) - limit} 张）")

    exts = [_validate(raw, fname) for fname, raw in files]

    directory = dir_for(detail_key, order_no)
    idx = _next_index(directory, detail_key)
    saved = []
    for (fname, raw), ext in zip(files, exts):
        name = f"{_clean(detail_key)}-{idx:02d}{ext}"
        (directory / name).write_bytes(raw)
        try:
            _write_thumb(raw, directory / thumb_name(name))
        except Exception:  # noqa: BLE001 —— 缩略图失败不该让整张图作废
            pass
        saved.append(name)
        idx += 1
    return existing + saved


# ---------------------------------------------------------------------------
# 删除
# ---------------------------------------------------------------------------

def _unlink(path: Path) -> bool:
    """删除单个文件，返回是否真的删掉了。

    清理是「尽力而为」的派生数据回收，**任何失败都不该冒泡出去**：
    - `OSError`：文件被占用 / 权限不足 / 已被别处删掉（Windows 上很常见）
    - `SystemExit`：某些受管环境（沙箱、CI）会在批量删除文件时中断进程，
      若不加拦截会顺着 ASGI 冒泡成 500，甚至把整个服务带崩
    """
    try:
        if path.is_file():
            path.unlink()
            return True
    except OSError:
        pass
    except SystemExit:
        # 环境级的删除策略拦截 —— 记作「没删掉」，由调用方汇总后提示，不中断服务
        pass
    return False


def delete_one(detail_key: str, name: str) -> int:
    """删除单张照片（原图 + 缩略图）。文件名不合法直接拒绝。"""
    safe = str(name or "").strip()
    if not safe or safe != _clean(safe) or "/" in safe or "\\" in safe:
        raise PhotoError(f"文件名不合法：{name!r}")
    directory = dir_for(detail_key)
    removed = int(_unlink(directory / safe))
    _unlink(directory / thumb_name(safe))
    return removed


def delete_all(detail_key: str) -> tuple:
    """删除该明细的全部照片文件（删记录 / 批量删除时调用）。

    目录内可能混有同一售后单下其他明细的图，因此按 `<明细键>-` 前缀精确删除，
    清空后再尝试移除空目录。

    返回 `(已删除数, 未删除的文件名列表)` —— 未删除的不静默隐瞒，
    由调用方汇总提示，便于发现被占用或受策略拦截的文件。
    """
    prefix = f"{_clean(detail_key)}-"
    directory = PHOTO_DIR / _clean(order_no_of(detail_key))
    if not directory.is_dir():
        return 0, []
    removed, failed = 0, []
    for p in list(directory.iterdir()):
        if p.is_file() and p.name.startswith(prefix):
            if _unlink(p):
                removed += 1
            else:
                failed.append(p.name)
    try:
        if not any(directory.iterdir()):
            directory.rmdir()
    except OSError:
        pass
    return removed, failed


def count_files(detail_key: str) -> int:
    """该明细在磁盘上的实际文件数（含缩略图），用于校验清理是否彻底。"""
    prefix = f"{_clean(detail_key)}-"
    directory = PHOTO_DIR / _clean(order_no_of(detail_key))
    if not directory.is_dir():
        return 0
    return sum(1 for p in directory.iterdir()
               if p.is_file() and p.name.startswith(prefix))


# ---------------------------------------------------------------------------
# 回收站：搬走 / 搬回 / 真删
# ---------------------------------------------------------------------------
# 删除明细时**不能直接 unlink** —— 照片删除不可逆，而记录本身是能从回收站
# 还原的。所以照片改为「搬进 data/photos_trash/<回收站 id>/<售后单号>/」，
# 记录还原时搬回原位，彻底删除时才真删。同盘 rename，不做文件复制。

def _trash_dir(item_id) -> Path:
    return PHOTO_TRASH_DIR / str(int(item_id))


def _drop_empty(path: Path) -> None:
    """目录空了就删掉；删不掉（非空 / 被占用）不算错误。"""
    try:
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()
    except OSError:
        pass


def move_to_trash(detail_key: str, item_id) -> list:
    """把该明细的照片文件搬进回收站目录，返回搬走清单。

    清单形如 `[{"order_no": "20260057", "name": "20260057-002-1.jpg"}, …]`，
    写进回收站记录的 payload，还原时照着搬回来。
    搬不动的（被占用 / 受环境策略拦截）**留在原处**：文件还在，
    还原时本来就无需搬回，所以只跳过、不报错。
    """
    prefix = f"{_clean(detail_key)}-"
    order_no = _clean(order_no_of(detail_key))
    directory = PHOTO_DIR / order_no
    if not directory.is_dir():
        return []
    dest_dir = _trash_dir(item_id) / order_no
    moved = []
    for p in list(directory.iterdir()):
        if not (p.is_file() and p.name.startswith(prefix)):
            continue
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            target = dest_dir / p.name
            if target.exists():
                target.unlink()
            p.rename(target)
            moved.append({"order_no": order_no, "name": p.name})
        except OSError:
            continue
    _drop_empty(directory)
    return moved


def restore_from_trash(item_id, entries) -> int:
    """把回收站里该记录的照片搬回原位，返回搬回的文件数。

    `entries` 为空时（删除时搬运失败 / 老记录没写清单）退化为**按目录结构
    全部搬回** —— 暂存目录里的相对路径就是 `<售后单号>/<文件名>`，
    信息没丢，所以照样能还原。
    """
    base = _trash_dir(item_id)
    if not base.is_dir():
        return 0
    pairs = []
    if entries:
        for e in entries:
            if not isinstance(e, dict):
                continue
            order_no = _clean(e.get("order_no"))
            name = _clean(e.get("name"))
            if name:
                pairs.append((order_no, name))
    else:
        for p in sorted(base.rglob("*")):
            if p.is_file():
                rel = p.relative_to(base)
                parts = rel.parts
                pairs.append((parts[0] if len(parts) > 1 else "", rel.name))
    moved = 0
    for order_no, name in pairs:
        src = base / order_no / name
        if not src.is_file():
            continue
        try:
            dest = (PHOTO_DIR / order_no) if order_no else PHOTO_DIR
            dest.mkdir(parents=True, exist_ok=True)
            target = dest / name
            if target.exists():
                target.unlink()
            src.rename(target)
            moved += 1
        except OSError:
            continue
    _drop_empty(base)
    return moved


def purge_trash(item_id) -> int:
    """彻底删除该记录在回收站里的照片文件（不可恢复），返回删除的文件数。"""
    base = _trash_dir(item_id)
    if not base.is_dir():
        return 0
    removed = 0
    for p in sorted(base.rglob("*"), reverse=True):
        try:
            if p.is_file():
                p.unlink()
                removed += 1
            elif p.is_dir():
                p.rmdir()
        except OSError:
            continue
    _drop_empty(base)
    return removed
