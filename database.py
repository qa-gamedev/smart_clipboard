"""SQLite-хранилище истории буфера обмена.

Структура папки:
    <root>/app_history.db
    <root>/Images/<ГГГГ-ММ>/img_<дата>_<время>.png   (+ Images/.thumbs/*.jpg — миниатюры)
    <root>/Text/txt_*.txt                              (тексты длиннее 5000 символов)

Пути к файлам в БД хранятся относительно <root>, поэтому папку можно переносить.
"""
from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
import threading
import time
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

LONG_TEXT_LIMIT = 5000      # длиннее — полный текст уходит в Text/*.txt
SEARCH_LIMIT = 20000        # сколько символов текста участвует в поиске
URL_RE = re.compile(r"^(?:https?://|ftp://|www\.)\S+$", re.IGNORECASE)

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    type       TEXT    NOT NULL,              -- text | link | image | file | note
    content    TEXT    NOT NULL DEFAULT '',   -- текст / URL / пути файлов (для длинного — начало)
    html       TEXT,                          -- форматированная версия (для режима не-Plain)
    text_path  TEXT,                          -- полный текст в Text/*.txt
    image_path TEXT,
    ocr_text   TEXT    NOT NULL DEFAULT '',   -- распознанный текст картинки
    search     TEXT    NOT NULL DEFAULT '',   -- lower(content + ocr_text) для поиска
    hash       TEXT    NOT NULL UNIQUE,       -- защита от дубликатов
    pinned     INTEGER NOT NULL DEFAULT 0,
    pinned_at  REAL    NOT NULL DEFAULT 0,    -- когда закреплено (для порядка среди закреплённых)
    color      TEXT    NOT NULL DEFAULT '',   -- цветовая метка карточки
    tags       TEXT    NOT NULL DEFAULT '',   -- хештеги через пробел: '#работа #идея'
    length     INTEGER NOT NULL DEFAULT 0,    -- длина текста в символах (для счётчика на карточке)
    created_at REAL    NOT NULL,
    updated_at REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_items_order ON items(pinned DESC, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_items_type  ON items(type);
"""

# Автоочистка и «очистить историю» не трогают закреплённое, заметки и помеченное (теги/цвет)
KEEP_FREE = "pinned=0 AND type!='note' AND color='' AND tags=''"

COLS = ("id, type, content, html, text_path, image_path, ocr_text, pinned, pinned_at, color, tags, length, "
        "created_at, updated_at")


def sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", "surrogatepass")).hexdigest()


def detect_kind(text: str) -> str:
    return "link" if URL_RE.match(text.strip()) else "text"


def clean_tags(raw: str) -> str:
    """'Работа, #идея  !!проект' -> '#работа #идея #проект' (без дублей)."""
    out: list[str] = []
    for tok in re.split(r"[\s,;]+", raw or ""):
        tok = re.sub(r"[^\w\-]", "", tok.lstrip("#"), flags=re.UNICODE).lower()
        if tok and f"#{tok}" not in out:
            out.append(f"#{tok}")
    return " ".join(out)


def _search_text(source: str, ocr: str, tags: str) -> str:
    return f"{source[:SEARCH_LIMIT]} {ocr} {tags}".lower().strip()


def _like(word: str) -> str:
    return "%" + word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


class Database:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        (self.root / "Images").mkdir(parents=True, exist_ok=True)
        (self.root / "Text").mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.root / "app_history.db", check_same_thread=False,
                                     isolation_level=None)  # autocommit
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)
        have = {r["name"] for r in self._conn.execute("PRAGMA table_info(items)")}
        for name, ddl in (("pinned_at", "REAL NOT NULL DEFAULT 0"), ("color", "TEXT NOT NULL DEFAULT ''"),
                          ("tags", "TEXT NOT NULL DEFAULT ''"), ("length", "INTEGER NOT NULL DEFAULT 0")):
            if name not in have:                                  # база от прошлой версии
                self._conn.execute(f"ALTER TABLE items ADD COLUMN {name} {ddl}")
                if name == "length":
                    self._conn.execute("UPDATE items SET length = LENGTH(content) WHERE type IN ('text','link','note')")

    # ---------- пути ----------
    def abs_path(self, rel: str) -> Path:
        return self.root / rel

    def thumb_path(self, image_rel: str) -> Path:
        return self.root / "Images" / ".thumbs" / (Path(image_rel).stem + ".jpg")

    # ---------- добавление ----------
    def add_text(self, text: str, html: str | None = None, ts: float | None = None,
                 bump: bool = True) -> tuple[int, bool]:
        """Возвращает (id, is_new). Дубликат только обновляет updated_at (bump=False — вообще не трогает).
        ts — своё время (для импорта старой истории)."""
        clean = text.strip()
        h = sha1("t:" + clean)
        now = ts or time.time()
        with self._lock:
            row = self._conn.execute("SELECT id FROM items WHERE hash=?", (h,)).fetchone()
            if row:
                if bump:
                    self._conn.execute("UPDATE items SET updated_at=?, html=COALESCE(?, html) WHERE id=?",
                                       (time.time(), html, row["id"]))
                return row["id"], False
            text_path, content = None, text
            if len(text) > LONG_TEXT_LIMIT:
                text_path = self._write_long_text(text, h)
                content = text[:LONG_TEXT_LIMIT]
            cur = self._conn.execute(
                "INSERT INTO items(type, content, html, text_path, search, hash, length, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (detect_kind(clean), content, html, text_path, _search_text(text, "", ""), h, len(text), now, now))
            return cur.lastrowid, True

    def add_files(self, paths: list[str]) -> tuple[int, bool]:
        content = "\n".join(paths)
        h = sha1("f:" + content)
        now = time.time()
        with self._lock:
            row = self._conn.execute("SELECT id FROM items WHERE hash=?", (h,)).fetchone()
            if row:
                self._conn.execute("UPDATE items SET updated_at=? WHERE id=?", (now, row["id"]))
                return row["id"], False
            cur = self._conn.execute(
                "INSERT INTO items(type, content, search, hash, created_at, updated_at)"
                " VALUES ('file',?,?,?,?,?)", (content, content.lower(), h, now, now))
            return cur.lastrowid, True

    def touch_by_hash(self, h: str, bump: bool = True) -> int | None:
        """Для картинок: если такая уже есть — обновить время (bump) и вернуть id."""
        with self._lock:
            row = self._conn.execute("SELECT id FROM items WHERE hash=?", (h,)).fetchone()
            if row:
                if bump:
                    self._conn.execute("UPDATE items SET updated_at=? WHERE id=?", (time.time(), row["id"]))
                return row["id"]
        return None

    def add_image(self, rel_path: str, h: str, ts: float | None = None) -> int:
        now = ts or time.time()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO items(type, image_path, hash, created_at, updated_at)"
                " VALUES ('image',?,?,?,?)", (rel_path, h, now, now))
            return cur.lastrowid

    def set_ocr_text(self, item_id: int, text: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE items SET ocr_text=? WHERE id=?", (text, item_id))
            self._refresh_search(item_id)

    def _refresh_search(self, item_id: int) -> None:
        row = self.get(item_id)
        if row:
            source = self.full_text(row) if row["type"] != "image" else ""
            self._conn.execute("UPDATE items SET search=? WHERE id=?",
                               (_search_text(source, row["ocr_text"], row["tags"]), item_id))

    def _write_long_text(self, text: str, h: str) -> str:
        rel = Path("Text") / f"txt_{datetime.now():%Y%m%d_%H%M%S}_{h[:8]}.txt"
        (self.root / rel).write_text(text, encoding="utf-8")
        return rel.as_posix()

    # ---------- чтение ----------
    def get(self, item_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(f"SELECT {COLS} FROM items WHERE id=?", (item_id,)).fetchone()
        return dict(row) if row else None

    def search(self, query: str = "", kind=None, favorites: bool = False, since: float | None = None,
               until: float | None = None, tag: str | None = None, pin_limit: int | None = 3,
               limit: int = 40, offset: int = 0) -> list[dict]:
        """kind — строка или кортеж типов. Вне «Избранного» наверху показывается не больше pin_limit закреплённых
        (остальные закреплённые идут в общем порядке, чтобы не занимать весь список)."""
        conds, args = ["1=1"], []
        if kind:
            kinds = list(kind) if isinstance(kind, (list, tuple)) else [kind]
            conds.append(f"type IN ({','.join('?' * len(kinds))})")
            args += kinds
        if favorites:
            conds.append("pinned=1")
        if since is not None:
            conds.append("updated_at>=?")
            args.append(since)
        if until is not None:
            conds.append("updated_at<?")
            args.append(until)
        if tag:
            conds.append("(' ' || tags || ' ') LIKE ? ESCAPE '\\'")
            args.append(_like(f" {tag.lower()} "))
        for word in query.lower().split():   # каждое слово должно встретиться (текст, OCR или теги)
            conds.append("search LIKE ? ESCAPE '\\'")
            args.append(_like(word))
        where = " AND ".join(conds)
        if favorites or pin_limit is None:
            sql = (f"SELECT {COLS} FROM items WHERE {where} "
                   "ORDER BY pinned DESC, pinned_at DESC, updated_at DESC LIMIT ? OFFSET ?")
            full_args = args + [limit, offset]
        else:
            sql = (f"SELECT {COLS}, (id IN (SELECT id FROM items WHERE {where} AND pinned=1 "
                   "ORDER BY pinned_at DESC, updated_at DESC LIMIT ?)) AS top "
                   f"FROM items WHERE {where} "
                   "ORDER BY top DESC, (CASE WHEN top=1 THEN pinned_at ELSE 0 END) DESC, updated_at DESC "
                   "LIMIT ? OFFSET ?")
            full_args = args + [max(0, pin_limit)] + args + [limit, offset]
        with self._lock:
            rows = self._conn.execute(sql, full_args).fetchall()
        return [dict(r) for r in rows]

    def full_text(self, row: dict) -> str:
        if row.get("text_path"):
            try:
                return self.abs_path(row["text_path"]).read_text(encoding="utf-8", errors="replace")
            except OSError:
                log.warning("Файл текста не найден: %s", row["text_path"])
        return row.get("content") or ""

    # ---------- изменение ----------
    def touch(self, item_id: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE items SET updated_at=? WHERE id=?", (time.time(), item_id))

    def toggle_pin(self, item_id: int) -> bool:
        with self._lock:
            self._conn.execute("UPDATE items SET pinned_at = CASE WHEN pinned=0 THEN ? ELSE 0 END, "
                               "pinned = 1 - pinned WHERE id=?", (time.time(), item_id))
            row = self._conn.execute("SELECT pinned FROM items WHERE id=?", (item_id,)).fetchone()
        return bool(row and row["pinned"])

    # ---------- заметки, правка, теги, цвета ----------
    def add_note(self, text: str, tags: str = "", color: str = "") -> int:
        now = time.time()
        tags = clean_tags(tags)
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO items(type, content, search, hash, tags, color, length, created_at, updated_at)"
                " VALUES ('note',?,?,?,?,?,?,?,?)",
                (text, _search_text(text, "", tags), "n:" + uuid.uuid4().hex, tags, color, len(text), now, now))
            return cur.lastrowid

    def update_text(self, item_id: int, text: str) -> int:
        """Сохраняет правку текста/заметки. Возвращает id итоговой записи
        (если после правки текст совпал с другой записью — они склеиваются)."""
        with self._lock:
            row = self.get(item_id)
            if not row:
                raise KeyError(item_id)
            now = time.time()
            if row["type"] == "note":
                self._conn.execute("UPDATE items SET content=?, length=?, updated_at=? WHERE id=?",
                                   (text, len(text), now, item_id))
                self._refresh_search(item_id)
                return item_id
            clean = text.strip()
            h = sha1("t:" + clean)
            other = self._conn.execute("SELECT id FROM items WHERE hash=? AND id!=?", (h, item_id)).fetchone()
            if other:                                              # такой текст уже есть — оставляем одну запись
                self.delete(item_id)
                self.touch(other["id"])
                return other["id"]
            if row.get("text_path"):
                try:
                    self.abs_path(row["text_path"]).unlink(missing_ok=True)
                except OSError:
                    pass
            text_path, content = None, text
            if len(text) > LONG_TEXT_LIMIT:
                text_path = self._write_long_text(text, h)
                content = text[:LONG_TEXT_LIMIT]
            self._conn.execute("UPDATE items SET type=?, content=?, html=NULL, text_path=?, hash=?, length=?, "
                               "updated_at=? WHERE id=?", (detect_kind(clean), content, text_path, h, len(text), now, item_id))
            self._refresh_search(item_id)
            return item_id

    def set_meta(self, item_id: int, tags: str | None = None, color: str | None = None) -> None:
        with self._lock:
            if tags is not None:
                self._conn.execute("UPDATE items SET tags=? WHERE id=?", (clean_tags(tags), item_id))
            if color is not None:
                self._conn.execute("UPDATE items SET color=? WHERE id=?", (color, item_id))
            self._refresh_search(item_id)

    def all_tags(self) -> list[tuple[str, int]]:
        with self._lock:
            rows = self._conn.execute("SELECT tags FROM items WHERE tags != ''").fetchall()
        counter = Counter(tag for r in rows for tag in r["tags"].split())
        return sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))

    def delete(self, item_id: int) -> None:
        with self._lock:
            row = self.get(item_id)
            if row:
                self._remove_files(row)
            self._conn.execute("DELETE FROM items WHERE id=?", (item_id,))

    def _remove_files(self, row: dict) -> None:
        paths = []
        if row.get("image_path"):
            paths += [self.abs_path(row["image_path"]), self.thumb_path(row["image_path"])]
        if row.get("text_path"):
            paths.append(self.abs_path(row["text_path"]))
        for p in paths:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                log.debug("Не удалось удалить %s", p)

    def cleanup(self, days: int) -> int:
        """Удаляет незакреплённые элементы старше N дней (закреплённые не трогает)."""
        if days <= 0:
            return 0
        cutoff = time.time() - days * 86400
        with self._lock:
            rows = [dict(r) for r in self._conn.execute(
                f"SELECT {COLS} FROM items WHERE {KEEP_FREE} AND updated_at<?", (cutoff,))]
            for r in rows:
                self._remove_files(r)
            self._conn.execute(f"DELETE FROM items WHERE {KEEP_FREE} AND updated_at<?", (cutoff,))
        return len(rows)

    def clear_all(self, keep_pinned: bool = True) -> int:
        with self._lock:
            where = f"WHERE {KEEP_FREE}" if keep_pinned else ""
            rows = [dict(r) for r in self._conn.execute(f"SELECT {COLS} FROM items {where}")]
            for r in rows:
                self._remove_files(r)
            self._conn.execute(f"DELETE FROM items {where}")
        return len(rows)

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
