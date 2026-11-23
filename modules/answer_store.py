# -*- coding: utf-8 -*-
"""统一 SQLite 题库缓存.

替代分散的 JSON 题库:

* ``zhidao_questions``: 知到作业/考试题目, 支持 ``eid`` 与 ``question_id`` 双键桥接,
  记录正确选项组合与历史错题组合.
* ``ai_questions``: AI 智慧课接口考试/掌握度缓存, 保留 ``cache_key`` + ``loose_key``
  两层精确键与 ``difflib`` 模糊兜底, 并允许按课程跨 ``exam_id`` 检索.

每次操作使用独立短连接 (WAL + busy_timeout), 避免 Playwright 事件循环线程与
``asyncio.to_thread`` 工作线程交叉访问时互相锁死. 旧 JSON 题库首次使用时会自动
导入并标记, 原文件保留作只读备份.
"""
from __future__ import annotations

import difflib
import json
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
BUSY_TIMEOUT_MS = 5000
FUZZY_MIN_LENGTH = 12
FUZZY_RATIO = 0.85
WRONG_COMBO_LIMIT = 20

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS zhidao_questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id TEXT NOT NULL,
    course_name TEXT DEFAULT '',
    exam_id TEXT DEFAULT '',
    eid TEXT,
    question_id TEXT,
    question_type TEXT DEFAULT '',
    content TEXT DEFAULT '',
    options TEXT DEFAULT '[]',
    option_ids_hash TEXT DEFAULT '',
    correct_options TEXT DEFAULT '[]',
    wrong_options TEXT DEFAULT '[]',
    ai_analysis TEXT DEFAULT '',
    last_updated REAL DEFAULT 0
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_zhidao_eid
    ON zhidao_questions(course_id, exam_id, eid) WHERE eid IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_zhidao_qid
    ON zhidao_questions(course_id, exam_id, question_id) WHERE question_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_zhidao_hash
    ON zhidao_questions(course_id, option_ids_hash);
CREATE INDEX IF NOT EXISTS idx_zhidao_content
    ON zhidao_questions(course_id, content);

CREATE TABLE IF NOT EXISTS ai_questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id TEXT NOT NULL DEFAULT '',
    course_name TEXT DEFAULT '',
    exam_id TEXT NOT NULL DEFAULT '',
    question_id TEXT NOT NULL,
    version INTEGER DEFAULT 1,
    question_plain TEXT DEFAULT '',
    question_raw TEXT DEFAULT '',
    answer TEXT DEFAULT '',
    answer_content TEXT DEFAULT '',
    question_dict TEXT DEFAULT '{}',
    cache_key TEXT DEFAULT '',
    loose_key TEXT DEFAULT '',
    last_updated REAL DEFAULT 0,
    UNIQUE(course_id, exam_id, question_id, version)
);

CREATE INDEX IF NOT EXISTS idx_ai_plain
    ON ai_questions(course_id, question_plain) WHERE question_plain != '';
CREATE INDEX IF NOT EXISTS idx_ai_cache_key ON ai_questions(course_id, cache_key);
CREATE INDEX IF NOT EXISTS idx_ai_loose_key ON ai_questions(course_id, loose_key);
"""


def cache_key(value: str | None) -> str:
    """题干精确归一化键: 去空白, 统一全角括号, 去掉尾部空括号."""
    text = re.sub(r"[\s\u3000]+", "", value or "")
    text = text.replace("（", "(").replace("）", ")")
    while text.endswith("()"):
        text = text[:-2]
    return text


def loose_key(value: str | None) -> str:
    """宽松键: 只保留中英文与数字并转小写, 供模糊匹配."""
    text = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", value or "")
    return text.lower()


def _json_dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _json_load(value, default):
    if value is None or value == "":
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _as_text(value) -> str:
    return "" if value is None else str(value)


def _option_ids(options) -> list[str]:
    ids: list[str] = []
    for option in options or []:
        if isinstance(option, dict):
            raw = option.get("id", option.get("optionId", option.get("option_id")))
            if raw is None:
                continue
            ids.append(str(raw))
        else:
            ids.append(str(option))
    return ids


def option_hash(options) -> str:
    return ",".join(sorted(identifier for identifier in _option_ids(options) if identifier))


def _split_legacy_key(key: str) -> tuple[str, int]:
    base, _, suffix = key.rpartition("_")
    if base and suffix.isdigit():
        return base, int(suffix)
    return key, 1


def _flatten_answer(record: dict) -> str:
    letters = record.get("letters") or []
    if letters:
        return "".join(str(item) for item in letters)
    values = record.get("values") or []
    if values:
        return "；".join(str(item) for item in values)
    return _as_text(record.get("text"))


class AiQuestionStore:
    """Sqlite-backed question bank shared by exam / homework / mastery modules."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        data_root: str | Path | None = None,
        migrate: bool = True,
    ):
        if db_path is None:
            from modules.utils import get_runtime_path

            db_path = get_runtime_path("data", "questions_bank.db")
        if data_root is None:
            from modules.utils import get_runtime_path

            data_root = get_runtime_path("data")
        self.db_path = Path(db_path)
        self.data_root = Path(data_root)
        self._lock = threading.RLock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        if migrate:
            self.migrate_legacy_json()

    # ------------------------------------------------------------------ sqlite
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=BUSY_TIMEOUT_MS / 1000)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        return conn

    def _init_schema(self) -> None:
        with self._lock:
            conn = sqlite3.connect(str(self.db_path), timeout=BUSY_TIMEOUT_MS / 1000)
            try:
                conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.executescript(_SCHEMA)
                conn.commit()
            finally:
                conn.close()

    @contextmanager
    def _cursor(self):
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _write(self, action):
        last_error = None
        for attempt in range(4):
            try:
                with self._cursor() as conn:
                    return action(conn)
            except sqlite3.OperationalError as exc:
                message = str(exc).lower()
                if "locked" not in message and "busy" not in message:
                    raise
                last_error = exc
                time.sleep(0.05 * (attempt + 1))
        raise last_error

    def _read(self, action):
        with self._cursor() as conn:
            return action(conn)

    # -------------------------------------------------------------------- meta
    def get_meta(self, key: str) -> str | None:
        try:
            row = self._read(
                lambda conn: conn.execute(
                    "SELECT value FROM meta WHERE key=?", (key,)
                ).fetchone()
            )
        except sqlite3.Error:
            return None
        return None if row is None else row["value"]

    def set_meta(self, key: str, value: str) -> None:
        def action(conn):
            conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )

        self._write(action)

    # ------------------------------------------------------------------- rows
    @staticmethod
    def _zhidao_row(row: sqlite3.Row) -> dict:
        data = dict(row)
        data["options"] = _json_load(data.get("options"), [])
        data["correct_options"] = _json_load(data.get("correct_options"), [])
        data["wrong_options"] = _json_load(data.get("wrong_options"), [])
        data["ai_analysis"] = data.get("ai_analysis") or ""
        return data

    @staticmethod
    def _ai_row(row: sqlite3.Row) -> dict:
        data = dict(row)
        data["question_dict"] = _json_load(data.get("question_dict"), {})
        return data

    # -------------------------------------------------------- zhidao: queries
    def _find_zhidao(
        self,
        conn,
        course_id: str,
        exam_id: str | None = None,
        *,
        eid: str | None = None,
        question_id: str | None = None,
    ) -> sqlite3.Row | None:
        sql = "SELECT * FROM zhidao_questions WHERE course_id=?"
        args: list = [str(course_id)]
        if exam_id is not None:
            sql += " AND exam_id=?"
            args.append(str(exam_id))
        if eid is not None:
            sql += " AND eid=?"
            args.append(str(eid))
        elif question_id is not None:
            sql += " AND question_id=?"
            args.append(str(question_id))
        else:
            return None
        sql += " ORDER BY id LIMIT 1"
        return conn.execute(sql, args).fetchone()

    def _bridge_zhidao(
        self,
        conn,
        course_id: str,
        exam_id: str,
        *,
        question_id: str,
        hashed: str,
        content: str,
        exclude_id: int | None = None,
    ) -> sqlite3.Row | None:
        clauses = []
        args: list = [str(course_id), str(exam_id)]
        if hashed:
            clauses.append("option_ids_hash=?")
            args.append(hashed)
        if content:
            clauses.append("content=?")
            args.append(content)
        if not clauses:
            return None
        sql = (
            "SELECT * FROM zhidao_questions WHERE course_id=? AND exam_id=? "
            "AND question_id IS NULL AND eid IS NOT NULL AND ("
            + " OR ".join(clauses)
            + ")"
        )
        if exclude_id is not None:
            sql += " AND id<>?"
            args.append(exclude_id)
        sql += " ORDER BY id LIMIT 1"
        return conn.execute(sql, args).fetchone()

    def get_by_eid(self, course_id, eid, exam_id=None) -> dict | None:
        row = self._read(
            lambda conn: self._find_zhidao(
                conn, course_id, exam_id, eid=str(eid)
            )
        )
        return None if row is None else self._zhidao_row(row)

    def get_by_id(self, course_id, question_id, exam_id=None) -> dict | None:
        row = self._read(
            lambda conn: self._find_zhidao(
                conn, course_id, exam_id, question_id=str(question_id)
            )
        )
        return None if row is None else self._zhidao_row(row)

    def find_by_hash(self, course_id, hashed, exam_id=None) -> dict | None:
        if not hashed:
            return None

        def action(conn):
            sql = "SELECT * FROM zhidao_questions WHERE course_id=? AND option_ids_hash=?"
            args: list = [str(course_id), hashed]
            if exam_id is not None:
                sql += " AND exam_id=?"
                args.append(str(exam_id))
            sql += " ORDER BY id LIMIT 1"
            return conn.execute(sql, args).fetchone()

        row = self._read(action)
        return None if row is None else self._zhidao_row(row)

    def find_by_content(self, course_id, content, exam_id=None) -> dict | None:
        if not content:
            return None

        def action(conn):
            sql = "SELECT * FROM zhidao_questions WHERE course_id=? AND content=?"
            args: list = [str(course_id), content]
            if exam_id is not None:
                sql += " AND exam_id=?"
                args.append(str(exam_id))
            sql += " ORDER BY id LIMIT 1"
            return conn.execute(sql, args).fetchone()

        row = self._read(action)
        return None if row is None else self._zhidao_row(row)

    # -------------------------------------------------- zhidao: write / bridge
    def save_options_by_eid(
        self,
        course_id,
        exam_id,
        eid,
        options,
        *,
        question_type: str = "",
        content: str = "",
        course_name: str = "",
    ) -> int:
        course_id = str(course_id)
        exam_id = _as_text(exam_id)
        eid = _as_text(eid)
        hashed = option_hash(options)
        now = time.time()

        def action(conn):
            row = self._find_zhidao(conn, course_id, exam_id, eid=eid)
            if row is not None:
                conn.execute(
                    "UPDATE zhidao_questions SET options=?, option_ids_hash=?, "
                    "content=COALESCE(NULLIF(?, ''), content), "
                    "question_type=COALESCE(NULLIF(?, ''), question_type), "
                    "course_name=COALESCE(NULLIF(?, ''), course_name), "
                    "last_updated=? WHERE id=?",
                    (
                        _json_dumps(list(options or [])),
                        hashed,
                        _as_text(content),
                        _as_text(question_type),
                        _as_text(course_name),
                        now,
                        row["id"],
                    ),
                )
                return int(row["id"])
            cursor = conn.execute(
                "INSERT INTO zhidao_questions(course_id, course_name, exam_id, eid, "
                "question_id, question_type, content, options, option_ids_hash, "
                "correct_options, wrong_options, ai_analysis, last_updated) "
                "VALUES(?, ?, ?, ?, NULL, ?, ?, ?, ?, '[]', '[]', '', ?)",
                (
                    course_id,
                    _as_text(course_name),
                    exam_id,
                    eid,
                    _as_text(question_type),
                    _as_text(content),
                    _json_dumps(list(options or [])),
                    hashed,
                    now,
                ),
            )
            return int(cursor.lastrowid)

        return self._write(action)

    def save_options_by_id(
        self,
        course_id,
        exam_id,
        question_id,
        options,
        *,
        question_type: str = "",
        content: str = "",
        course_name: str = "",
    ) -> int:
        course_id = str(course_id)
        exam_id = _as_text(exam_id)
        question_id = _as_text(question_id)
        hashed = option_hash(options)
        now = time.time()

        def action(conn):
            row = self._find_zhidao(
                conn, course_id, exam_id, question_id=question_id
            )
            if row is not None:
                eid = row["eid"]
                if not eid:
                    bridge = self._bridge_zhidao(
                        conn,
                        course_id,
                        exam_id,
                        question_id=question_id,
                        hashed=hashed,
                        content=_as_text(content),
                        exclude_id=row["id"],
                    )
                    if bridge is not None:
                        eid = bridge["eid"]
                        conn.execute(
                            "DELETE FROM zhidao_questions WHERE id=?", (bridge["id"],)
                        )
                conn.execute(
                    "UPDATE zhidao_questions SET eid=COALESCE(eid, ?), options=?, "
                    "option_ids_hash=?, content=COALESCE(NULLIF(?, ''), content), "
                    "question_type=COALESCE(NULLIF(?, ''), question_type), "
                    "course_name=COALESCE(NULLIF(?, ''), course_name), "
                    "last_updated=? WHERE id=?",
                    (
                        eid,
                        _json_dumps(list(options or [])),
                        hashed,
                        _as_text(content),
                        _as_text(question_type),
                        _as_text(course_name),
                        now,
                        row["id"],
                    ),
                )
                return int(row["id"])
            bridge = self._bridge_zhidao(
                conn,
                course_id,
                exam_id,
                question_id=question_id,
                hashed=hashed,
                content=_as_text(content),
            )
            if bridge is not None:
                conn.execute(
                    "UPDATE zhidao_questions SET question_id=?, options=?, "
                    "option_ids_hash=?, content=COALESCE(NULLIF(?, ''), content), "
                    "question_type=COALESCE(NULLIF(?, ''), question_type), "
                    "course_name=COALESCE(NULLIF(?, ''), course_name), "
                    "last_updated=? WHERE id=?",
                    (
                        question_id,
                        _json_dumps(list(options or [])),
                        hashed,
                        _as_text(content),
                        _as_text(question_type),
                        _as_text(course_name),
                        now,
                        bridge["id"],
                    ),
                )
                return int(bridge["id"])
            cursor = conn.execute(
                "INSERT INTO zhidao_questions(course_id, course_name, exam_id, eid, "
                "question_id, question_type, content, options, option_ids_hash, "
                "correct_options, wrong_options, ai_analysis, last_updated) "
                "VALUES(?, ?, ?, NULL, ?, ?, ?, ?, ?, '[]', '[]', '', ?)",
                (
                    course_id,
                    _as_text(course_name),
                    exam_id,
                    question_id,
                    _as_text(question_type),
                    _as_text(content),
                    _json_dumps(list(options or [])),
                    hashed,
                    now,
                ),
            )
            return int(cursor.lastrowid)

        return self._write(action)

    def record_wrong(
        self,
        course_id,
        exam_id="",
        *,
        eid=None,
        question_id=None,
        wrong_options=None,
    ) -> int:
        combo = sorted({_as_text(item) for item in (wrong_options or []) if _as_text(item)})
        if not combo:
            return 0

        def action(conn):
            row = self._find_zhidao(
                conn,
                course_id,
                exam_id,
                eid=None if eid is None else str(eid),
                question_id=None if question_id is None else str(question_id),
            )
            if row is None:
                return 0
            history = _json_load(row["wrong_options"], [])
            if not isinstance(history, list):
                history = []
            normalized = [
                sorted({_as_text(part) for part in item if _as_text(part)})
                for item in history
                if isinstance(item, list)
            ]
            normalized = [item for item in normalized if item]
            if combo not in normalized:
                normalized.append(combo)
                normalized = normalized[-WRONG_COMBO_LIMIT:]
            conn.execute(
                "UPDATE zhidao_questions SET wrong_options=?, last_updated=? WHERE id=?",
                (_json_dumps(normalized), time.time(), row["id"]),
            )
            return int(row["id"])

        return self._write(action)

    def mark_correct(
        self,
        course_id,
        exam_id="",
        *,
        eid=None,
        question_id=None,
        correct_options=None,
        ai_analysis=None,
        auto_clear_wrong: bool = True,
    ) -> int:
        key_id = None if eid is None else str(eid)
        qid = None if question_id is None else str(question_id)

        def action(conn):
            row = self._find_zhidao(
                conn, course_id, exam_id, eid=key_id, question_id=qid
            )
            if row is None:
                return 0
            assignments = ["correct_options=?", "last_updated=?"]
            args: list = [
                _json_dumps(list(correct_options or [])),
                time.time(),
            ]
            if auto_clear_wrong:
                assignments.append("wrong_options='[]'")
                if ai_analysis is None:
                    assignments.append("ai_analysis=''")
            if ai_analysis is not None:
                assignments.append("ai_analysis=?")
                args.append(_as_text(ai_analysis))
            args.append(row["id"])
            conn.execute(
                "UPDATE zhidao_questions SET " + ", ".join(assignments) + " WHERE id=?",
                args,
            )
            return int(row["id"])

        return self._write(action)

    def list_zhidao(self, course_id, exam_id=None) -> list[dict]:
        def action(conn):
            sql = "SELECT * FROM zhidao_questions WHERE course_id=?"
            args: list = [str(course_id)]
            if exam_id is not None:
                sql += " AND exam_id=?"
                args.append(str(exam_id))
            sql += " ORDER BY id"
            return conn.execute(sql, args).fetchall()

        return [self._zhidao_row(row) for row in self._read(action)]

    # ------------------------------------------------------------- ai: write
    def upsert_ai_question(
        self,
        course_id,
        exam_id,
        question_id,
        version: int = 1,
        *,
        question_plain: str = "",
        question_raw: str = "",
        answer: str = "",
        answer_content: str = "",
        question_dict=None,
        course_name: str = "",
    ) -> int:
        course_id = _as_text(course_id)
        exam_id = _as_text(exam_id)
        question_id = _as_text(question_id)
        try:
            version = int(version or 1)
        except (TypeError, ValueError):
            version = 1
        plain = _as_text(question_plain)
        now = time.time()

        def action(conn):
            row = conn.execute(
                "SELECT * FROM ai_questions WHERE course_id=? AND exam_id=? "
                "AND question_id=? AND version=?",
                (course_id, exam_id, question_id, version),
            ).fetchone()
            if row is not None:
                current_plain = row["question_plain"] or ""
                merged_plain = plain or current_plain
                merged_raw = _as_text(question_raw) or (row["question_raw"] or "")
                merged_answer = _as_text(answer) or (row["answer"] or "")
                merged_content = _as_text(answer_content) or (row["answer_content"] or "")
                existing_dict = _json_load(row["question_dict"], {})
                if question_dict is None or question_dict == {}:
                    merged_dict = existing_dict
                else:
                    merged_dict = question_dict
                conn.execute(
                    "UPDATE ai_questions SET course_name=COALESCE(NULLIF(?, ''), course_name), "
                    "question_plain=?, question_raw=?, answer=?, answer_content=?, "
                    "question_dict=?, cache_key=?, loose_key=?, last_updated=? WHERE id=?",
                    (
                        _as_text(course_name),
                        merged_plain,
                        merged_raw,
                        merged_answer,
                        merged_content,
                        _json_dumps(merged_dict),
                        cache_key(merged_plain),
                        loose_key(merged_plain),
                        now,
                        row["id"],
                    ),
                )
                return int(row["id"])
            cursor = conn.execute(
                "INSERT INTO ai_questions(course_id, course_name, exam_id, question_id, "
                "version, question_plain, question_raw, answer, answer_content, "
                "question_dict, cache_key, loose_key, last_updated) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    course_id,
                    _as_text(course_name),
                    exam_id,
                    question_id,
                    version,
                    plain,
                    _as_text(question_raw),
                    _as_text(answer),
                    _as_text(answer_content),
                    _json_dumps(question_dict or {}),
                    cache_key(plain),
                    loose_key(plain),
                    now,
                ),
            )
            return int(cursor.lastrowid)

        return self._write(action)

    # -------------------------------------------------------------- ai: query
    def get_ai_question(
        self, course_id, exam_id, question_id, version: int = 1
    ) -> dict | None:
        def action(conn):
            return conn.execute(
                "SELECT * FROM ai_questions WHERE course_id=? AND exam_id=? "
                "AND question_id=? AND version=?",
                (str(course_id), _as_text(exam_id), str(question_id), int(version or 1)),
            ).fetchone()

        row = self._read(action)
        return None if row is None else self._ai_row(row)

    def load_course_questions(self, course_id) -> list[dict]:
        def action(conn):
            return conn.execute(
                "SELECT * FROM ai_questions WHERE course_id=? ORDER BY id",
                (str(course_id),),
            ).fetchall()

        return [self._ai_row(row) for row in self._read(action)]

    def search_in_course(
        self,
        course_id,
        question: str,
        *,
        exclude_exam_id: str | None = None,
    ) -> dict | None:
        """精确 cache_key -> 精确 loose_key -> 同课程 difflib 模糊匹配."""
        course_id = _as_text(course_id)
        target_cache = cache_key(question)
        target_loose = loose_key(question)
        if not target_cache and not target_loose:
            return None

        base = "SELECT * FROM ai_questions WHERE course_id=? AND COALESCE(answer,'')<>''"
        args: list = [course_id]
        if exclude_exam_id is not None:
            base += " AND exam_id<>?"
            args.append(str(exclude_exam_id))

        def fetch(extra: str, extra_args: list):
            sql = base + extra + " ORDER BY last_updated DESC, id DESC LIMIT 1"
            row = self._read(lambda conn: conn.execute(sql, args + extra_args).fetchone())
            return None if row is None else self._ai_row(row)

        row = fetch(" AND cache_key=?", [target_cache]) if target_cache else None
        if row is not None:
            row["match"] = "cache_key"
            return row
        row = fetch(" AND loose_key=?", [target_loose]) if target_loose else None
        if row is not None:
            row["match"] = "loose"
            return row
        if len(target_loose) < FUZZY_MIN_LENGTH:
            return None

        window = max(20, int(len(target_loose) * 0.3))
        low, high = len(target_loose) - window, len(target_loose) + window

        def candidates(conn):
            sql = (
                base
                + " AND loose_key<>'' AND length(loose_key) BETWEEN ? AND ?"
            )
            return conn.execute(sql, args + [low, high]).fetchall()

        best, best_ratio = None, 0.0
        for row in self._read(candidates):
            ratio = difflib.SequenceMatcher(
                None, target_loose, row["loose_key"] or ""
            ).ratio()
            if ratio > best_ratio:
                best, best_ratio = row, ratio
        if best is None or best_ratio < FUZZY_RATIO:
            return None
        data = self._ai_row(best)
        data["match"] = "fuzzy"
        data["ratio"] = best_ratio
        return data

    # ------------------------------------------------------------- migration
    def migrate_legacy_json(self, data_root=None, *, force: bool = False) -> int:
        root = Path(data_root) if data_root is not None else self.data_root
        if not force and self.get_meta("legacy_json_migrated") == "1":
            return 0
        count = 0
        exam_root = root / "exam_answers"
        if exam_root.is_dir():
            for course_dir in sorted(exam_root.iterdir()):
                if course_dir.is_dir():
                    count += self._migrate_course_dir(course_dir.name, course_dir)
        mastery_path = root / "mastery_answers.json"
        if mastery_path.is_file() and (
            force or self.get_meta("legacy_mastery_migrated") != "1"
        ):
            count += self._migrate_mastery(mastery_path)
            self.set_meta("legacy_mastery_migrated", "1")
        self.set_meta("legacy_json_migrated", "1")
        return count

    def migrate_course_json_dir(
        self, course_id, directory, *, force: bool = False
    ) -> int:
        directory = Path(directory)
        marker = f"legacy_course_migrated:{course_id}"
        if not force and self.get_meta(marker) == "1":
            return 0
        count = self._migrate_course_dir(course_id, directory) if directory.is_dir() else 0
        self.set_meta(marker, "1")
        return count

    def _migrate_course_dir(self, course_id, directory: Path) -> int:
        count = 0
        for file in sorted(directory.glob("*.json")):
            try:
                payload = json.loads(file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            exam_id = "" if file.name == "data.json" else file.stem
            for raw_key, record in payload.items():
                if not isinstance(record, dict):
                    continue
                question_id, version = _split_legacy_key(str(raw_key))
                self.upsert_ai_question(
                    course_id,
                    exam_id,
                    question_id,
                    record.get("version", version),
                    question_plain=record.get("question", ""),
                    question_raw=record.get("question", ""),
                    answer=_as_text(record.get("answer")),
                    answer_content=_as_text(record.get("answer_content")),
                    question_dict=record.get("questionDict") or {},
                )
                count += 1
        return count

    def _migrate_mastery(self, path: Path) -> int:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0
        if not isinstance(payload, dict):
            return 0
        count = 0
        for key, record in payload.items():
            if not isinstance(record, dict):
                continue
            self.upsert_ai_question(
                "",
                "",
                cache_key(key) or loose_key(key) or str(key),
                1,
                question_plain=str(key),
                question_raw=str(key),
                answer=_flatten_answer(record),
                answer_content="",
                question_dict=record,
            )
            count += 1
        return count

    # ---------------------------------------------------------- export/import
    def export_data(self, course_id=None) -> dict:
        def action(conn):
            if course_id is None:
                zhidao = conn.execute("SELECT * FROM zhidao_questions ORDER BY id").fetchall()
                ai = conn.execute("SELECT * FROM ai_questions ORDER BY id").fetchall()
            else:
                zhidao = conn.execute(
                    "SELECT * FROM zhidao_questions WHERE course_id=? ORDER BY id",
                    (str(course_id),),
                ).fetchall()
                ai = conn.execute(
                    "SELECT * FROM ai_questions WHERE course_id=? ORDER BY id",
                    (str(course_id),),
                ).fetchall()
            return zhidao, ai

        zhidao_rows, ai_rows = self._read(action)
        zhidao = []
        for row in zhidao_rows:
            item = self._zhidao_row(row)
            item.pop("id", None)
            zhidao.append(item)
        ai = []
        for row in ai_rows:
            item = self._ai_row(row)
            item.pop("id", None)
            ai.append(item)
        return {
            "schema_version": SCHEMA_VERSION,
            "exported_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "zhidao_questions": zhidao,
            "ai_questions": ai,
        }

    def import_data(self, payload) -> dict:
        if isinstance(payload, (str, bytes)):
            payload = json.loads(payload)
        if not isinstance(payload, dict):
            raise ValueError("导入内容必须是 JSON 对象")
        zhidao_items = payload.get("zhidao_questions") or []
        ai_items = payload.get("ai_questions") or []
        if not isinstance(zhidao_items, list) or not isinstance(ai_items, list):
            raise ValueError("zhidao_questions / ai_questions 必须是数组")

        counts = {"zhidao_questions": 0, "ai_questions": 0}
        for item in zhidao_items:
            if isinstance(item, dict) and self._import_zhidao_item(item):
                counts["zhidao_questions"] += 1
        for item in ai_items:
            if not isinstance(item, dict):
                continue
            question_id = item.get("question_id")
            if question_id is None:
                continue
            self.upsert_ai_question(
                item.get("course_id", ""),
                item.get("exam_id", ""),
                question_id,
                item.get("version", 1),
                question_plain=item.get("question_plain", ""),
                question_raw=item.get("question_raw", ""),
                answer=item.get("answer", ""),
                answer_content=item.get("answer_content", ""),
                question_dict=item.get("question_dict") or {},
                course_name=item.get("course_name", ""),
            )
            counts["ai_questions"] += 1
        return counts

    def _import_zhidao_item(self, item: dict) -> bool:
        course_id = _as_text(item.get("course_id"))
        exam_id = _as_text(item.get("exam_id"))
        eid = item.get("eid")
        question_id = item.get("question_id")
        if not course_id or (eid is None and question_id is None):
            return False
        options = _json_load(item.get("options"), [])
        correct_options = _json_load(item.get("correct_options"), [])
        wrong_options = _json_load(item.get("wrong_options"), [])
        ai_analysis = _as_text(item.get("ai_analysis"))
        hashed = item.get("option_ids_hash") or option_hash(options)
        content = _as_text(item.get("content"))
        now = time.time()

        def action(conn):
            eid_row = None
            qid_row = None
            if eid is not None:
                eid_row = self._find_zhidao(
                    conn, course_id, exam_id, eid=str(eid)
                )
            if question_id is not None:
                qid_row = self._find_zhidao(
                    conn, course_id, exam_id, question_id=str(question_id)
                )
            row = eid_row or qid_row
            if (
                eid_row is not None
                and qid_row is not None
                and eid_row["id"] != qid_row["id"]
            ):
                keep, drop = (
                    (eid_row, qid_row) if eid_row["id"] < qid_row["id"] else (qid_row, eid_row)
                )
                conn.execute("DELETE FROM zhidao_questions WHERE id=?", (drop["id"],))
                row = conn.execute(
                    "SELECT * FROM zhidao_questions WHERE id=?", (keep["id"],)
                ).fetchone()
            if row is None and hashed:
                sql = (
                    "SELECT * FROM zhidao_questions WHERE course_id=? AND exam_id=? "
                    "AND option_ids_hash=? ORDER BY id LIMIT 1"
                )
                row = conn.execute(sql, (course_id, exam_id, hashed)).fetchone()
            if row is None:
                cursor = conn.execute(
                    "INSERT INTO zhidao_questions(course_id, course_name, exam_id, eid, "
                    "question_id, question_type, content, options, option_ids_hash, "
                    "correct_options, wrong_options, ai_analysis, last_updated) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        course_id,
                        _as_text(item.get("course_name")),
                        exam_id,
                        None if eid is None else str(eid),
                        None if question_id is None else str(question_id),
                        _as_text(item.get("question_type")),
                        content,
                        _json_dumps(options),
                        hashed,
                        _json_dumps(correct_options),
                        _json_dumps(wrong_options),
                        ai_analysis,
                        now,
                    ),
                )
                return int(cursor.lastrowid)
            existing_correct = _json_load(row["correct_options"], [])
            existing_history = _json_load(row["wrong_options"], [])
            if not isinstance(existing_history, list):
                existing_history = []
            incoming_history = wrong_options if isinstance(wrong_options, list) else []
            merged_history = list(existing_history)
            for combo in incoming_history:
                if combo not in merged_history:
                    merged_history.append(combo)
            conn.execute(
                "UPDATE zhidao_questions SET eid=COALESCE(eid, ?), "
                "question_id=COALESCE(question_id, ?), "
                "course_name=COALESCE(NULLIF(?, ''), course_name), "
                "question_type=COALESCE(NULLIF(?, ''), question_type), "
                "content=COALESCE(NULLIF(?, ''), content), "
                "options=CASE WHEN ? <> '' THEN ? ELSE options END, "
                "option_ids_hash=COALESCE(NULLIF(?, ''), option_ids_hash), "
                "correct_options=CASE WHEN ? <> '[]' THEN ? ELSE correct_options END, "
                "wrong_options=?, "
                "ai_analysis=COALESCE(NULLIF(?, ''), ai_analysis), "
                "last_updated=? WHERE id=?",
                (
                    None if eid is None else str(eid),
                    None if question_id is None else str(question_id),
                    _as_text(item.get("course_name")),
                    _as_text(item.get("question_type")),
                    content,
                    _json_dumps(options) if options else "",
                    _json_dumps(options),
                    hashed,
                    _json_dumps(correct_options) if correct_options else "[]",
                    _json_dumps(correct_options),
                    _json_dumps(merged_history),
                    ai_analysis,
                    now,
                    row["id"],
                ),
            )
            return int(row["id"])

        self._write(action)
        return True

    # ------------------------------------------------------------------- misc
    def clear(self, course_id=None) -> None:
        def action(conn):
            if course_id is None:
                conn.execute("DELETE FROM zhidao_questions")
                conn.execute("DELETE FROM ai_questions")
            else:
                conn.execute(
                    "DELETE FROM zhidao_questions WHERE course_id=?", (str(course_id),)
                )
                conn.execute(
                    "DELETE FROM ai_questions WHERE course_id=?", (str(course_id),)
                )

        self._write(action)

    def stats(self, course_id=None) -> dict:
        def action(conn):
            if course_id is None:
                zhidao = conn.execute(
                    "SELECT COUNT(*) AS n FROM zhidao_questions"
                ).fetchone()["n"]
                ai = conn.execute("SELECT COUNT(*) AS n FROM ai_questions").fetchone()["n"]
            else:
                zhidao = conn.execute(
                    "SELECT COUNT(*) AS n FROM zhidao_questions WHERE course_id=?",
                    (str(course_id),),
                ).fetchone()["n"]
                ai = conn.execute(
                    "SELECT COUNT(*) AS n FROM ai_questions WHERE course_id=?",
                    (str(course_id),),
                ).fetchone()["n"]
            return int(zhidao), int(ai)

        zhidao, ai = self._read(action)
        return {
            "db_path": str(self.db_path),
            "schema_version": SCHEMA_VERSION,
            "zhidao_questions": zhidao,
            "ai_questions": ai,
            "legacy_migrated": self.get_meta("legacy_json_migrated") == "1",
        }