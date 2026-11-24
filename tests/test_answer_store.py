# -*- coding: utf-8 -*-
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

from modules.ai_smart_mastery import AnswerCache
from modules.answer_store import AiQuestionStore, option_hash
from modules.exam_ctx import AnswerBank

OPTIONS = [
    {"id": 11, "content": "选项A"},
    {"id": 12, "content": "选项B"},
]


class StoreTestCase(unittest.TestCase):
    def make_store(self, *, migrate=True):
        directory = tempfile.mkdtemp()
        self.addCleanup(lambda: _remove_tree(directory))
        root = Path(directory)
        store = AiQuestionStore(
            root / "questions_bank.db", data_root=root, migrate=migrate
        )
        return store, root


def _remove_tree(directory):
    for name in os.listdir(directory):
        path = os.path.join(directory, name)
        try:
            if os.path.isdir(path):
                _remove_tree(path)
            else:
                os.remove(path)
        except OSError:
            pass
    try:
        os.rmdir(directory)
    except OSError:
        pass


class ZhidaoBridgeTests(StoreTestCase):
    def test_save_by_eid_then_by_id_bridges_single_row(self):
        store, _ = self.make_store()
        eid_row = store.save_options_by_eid(
            "course-1",
            "exam-1",
            "E-1",
            OPTIONS,
            question_type="1",
            content="题干一",
        )
        row = store.get_by_eid("course-1", "E-1", exam_id="exam-1")
        self.assertIsNotNone(row)
        self.assertIsNone(row["question_id"])
        self.assertEqual(row["option_ids_hash"], option_hash(OPTIONS))

        id_row = store.save_options_by_id(
            "course-1",
            "exam-1",
            "Q-1",
            OPTIONS,
            question_type="1",
            content="题干一",
        )
        self.assertEqual(eid_row, id_row)

        bridged = store.get_by_id("course-1", "Q-1", exam_id="exam-1")
        self.assertEqual(bridged["eid"], "E-1")
        self.assertEqual(bridged["option_ids_hash"], "11,12")
        self.assertEqual(store.stats("course-1")["zhidao_questions"], 1)

    def test_blank_question_bridges_by_content(self):
        store, _ = self.make_store()
        store.save_options_by_eid("course-1", "exam-1", "E-2", [], content="填空题干")
        store.save_options_by_id("course-1", "exam-1", "Q-2", [], content="填空题干")
        row = store.get_by_id("course-1", "Q-2", exam_id="exam-1")
        self.assertEqual(row["eid"], "E-2")
        self.assertEqual(store.stats("course-1")["zhidao_questions"], 1)

    def test_wrong_combinations_accumulate_then_clear_on_correct(self):
        store, _ = self.make_store()
        store.save_options_by_eid("course-1", "exam-1", "E-3", OPTIONS, content="题干三")

        store.record_wrong(
            "course-1", "exam-1", eid="E-3", wrong_options=[12, 11]
        )
        store.record_wrong("course-1", "exam-1", eid="E-3", wrong_options=[11])
        row = store.get_by_eid("course-1", "E-3", exam_id="exam-1")
        self.assertEqual(row["wrong_options"], [["11", "12"], ["11"]])

        store.mark_correct(
            "course-1",
            "exam-1",
            eid="E-3",
            correct_options=[11, 12],
            ai_analysis="官方解析",
            auto_clear_wrong=True,
        )
        row = store.get_by_eid("course-1", "E-3", exam_id="exam-1")
        self.assertEqual(row["correct_options"], [11, 12])
        self.assertEqual(row["wrong_options"], [])
        self.assertEqual(row["ai_analysis"], "官方解析")

    def test_mark_correct_can_keep_wrong_history(self):
        store, _ = self.make_store()
        store.save_options_by_eid("course-1", "exam-1", "E-4", OPTIONS)
        store.record_wrong("course-1", "exam-1", eid="E-4", wrong_options=[12])
        store.mark_correct(
            "course-1",
            "exam-1",
            eid="E-4",
            correct_options=[11],
            auto_clear_wrong=False,
        )
        row = store.get_by_eid("course-1", "E-4", exam_id="exam-1")
        self.assertEqual(row["correct_options"], [11])
        self.assertEqual(row["wrong_options"], [["12"]])

    def test_record_wrong_preserve_order_keeps_order_and_duplicates(self):
        store, _ = self.make_store()
        store.save_options_by_eid("course-1", "exam-1", "F-1", [], content="多空填空")
        store.record_wrong(
            "course-1",
            "exam-1",
            eid="F-1",
            wrong_options=["乙", "甲"],
            preserve_order=True,
        )
        store.record_wrong(
            "course-1",
            "exam-1",
            eid="F-1",
            wrong_options=["1", "1"],
            preserve_order=True,
        )
        store.record_wrong(
            "course-1",
            "exam-1",
            eid="F-1",
            wrong_options=["甲", "乙"],
            preserve_order=True,
        )
        row = store.get_by_eid("course-1", "F-1", exam_id="exam-1")
        self.assertEqual(
            row["wrong_options"],
            [["乙", "甲"], ["1", "1"], ["甲", "乙"]],
        )

    def test_save_by_eid_bridges_existing_qid_row_and_reencrypted_eid(self):
        store, _ = self.make_store()
        id_row = store.save_options_by_id(
            "course-1",
            "exam-1",
            "Q-10",
            OPTIONS,
            question_type="1",
            content="已缓存题",
        )
        store.mark_correct(
            "course-1", "exam-1", question_id="Q-10", correct_options=["11"]
        )

        # 首次传入 eid 时回填到已有 question_id 行
        bridged_row = store.save_options_by_eid(
            "course-1",
            "exam-1",
            "E-OLD",
            OPTIONS,
            question_type="1",
            content="已缓存题",
        )
        self.assertEqual(bridged_row, id_row)
        row = store.get_by_eid("course-1", "E-OLD", exam_id="exam-1")
        self.assertEqual(row["question_id"], "Q-10")
        self.assertEqual(row["correct_options"], ["11"])

        # 重做时服务端重新加密 eid，仍复用原行并保留已掌握答案
        reused_row = store.save_options_by_eid(
            "course-1",
            "exam-1",
            "E-NEW",
            OPTIONS,
            question_type="1",
            content="已缓存题",
        )
        self.assertEqual(reused_row, id_row)
        row_new = store.get_by_eid("course-1", "E-NEW", exam_id="exam-1")
        self.assertEqual(row_new["question_id"], "Q-10")
        self.assertEqual(row_new["correct_options"], ["11"])
        self.assertEqual(store.stats("course-1")["zhidao_questions"], 1)


class AiSearchTests(StoreTestCase):
    def test_exact_loose_and_fuzzy_lookup(self):
        store, _ = self.make_store()
        store.upsert_ai_question(
            "course-1",
            "exam-1",
            "q1",
            1,
            question_plain="光合作用的主要产物是什么？",
            answer="A",
            question_dict={"letters": ["A"]},
        )

        exact = store.search_in_course("course-1", "光合作用的主要产物是什么？")
        self.assertEqual(exact["question_id"], "q1")
        self.assertEqual(exact["match"], "cache_key")

        loose = store.search_in_course("course-1", "光合作用的主要产物是什么")
        self.assertEqual(loose["question_id"], "q1")
        self.assertEqual(loose["match"], "loose")

        fuzzy = store.search_in_course("course-1", "光合作用的主要产物是什么呀")
        self.assertEqual(fuzzy["question_id"], "q1")
        self.assertEqual(fuzzy["match"], "fuzzy")
        self.assertGreaterEqual(fuzzy["ratio"], 0.85)

    def test_search_across_exam_ids_and_exclusion(self):
        store, _ = self.make_store()
        store.upsert_ai_question(
            "course-1",
            "exam-1",
            "q1",
            1,
            question_plain="细胞呼吸的主要场所是线粒体",
            answer="B",
        )
        store.upsert_ai_question(
            "course-1",
            "exam-2",
            "q2",
            1,
            question_plain="有丝分裂发生在细胞核",
            answer="C",
        )

        row = store.search_in_course("course-1", "细胞呼吸的主要场所是线粒体")
        self.assertEqual(row["exam_id"], "exam-1")
        row = store.search_in_course("course-1", "有丝分裂发生在细胞核")
        self.assertEqual(row["exam_id"], "exam-2")

        excluded = store.search_in_course(
            "course-1", "细胞呼吸的主要场所是线粒体", exclude_exam_id="exam-1"
        )
        self.assertIsNone(excluded)

    def test_short_question_does_not_fuzzy_match(self):
        store, _ = self.make_store()
        store.upsert_ai_question(
            "course-1", "exam-1", "q1", 1, question_plain="短题干", answer="A"
        )
        self.assertIsNone(store.search_in_course("course-1", "短题"))


class StoreMigrationTests(StoreTestCase):
    def test_legacy_exam_and_mastery_json_are_imported(self):
        store, root = self.make_store()
        course_dir = root / "exam_answers" / "course-1"
        course_dir.mkdir(parents=True)
        (course_dir / "data.json").write_text(
            json.dumps(
                {
                    "100": {
                        "version": 1,
                        "question": "旧题干",
                        "answer": "1#@#2",
                        "answer_content": "甲\n乙",
                        "questionDict": {"id": 100},
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (course_dir / "exam-9.json").write_text(
            json.dumps(
                {
                    "100_2": {
                        "version": 2,
                        "question": "旧题干二版",
                        "answer": "3",
                        "questionDict": {"id": 100},
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (root / "mastery_answers.json").write_text(
            json.dumps(
                {"细胞呼吸的场所是线粒体": {"letters": ["B"]}},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        migrated, db_path = self.make_store_from(root)
        self.assertEqual(
            migrated.get_ai_question("course-1", "", "100", 1)["answer"], "1#@#2"
        )
        self.assertEqual(
            migrated.get_ai_question("course-1", "exam-9", "100", 2)["answer"], "3"
        )
        mastery = migrated.search_in_course("", "细胞呼吸的场所是线粒体")
        self.assertIsNotNone(mastery)
        self.assertEqual(mastery["answer"], "B")
        self.assertEqual(mastery["question_dict"], {"letters": ["B"]})

        # 原 JSON 保留作只读备份; 再次初始化不会重复导入
        self.assertTrue((course_dir / "data.json").is_file())
        self.assertTrue((root / "mastery_answers.json").is_file())
        before = migrated.stats()["ai_questions"]
        again = AiQuestionStore(db_path, data_root=root)
        self.assertEqual(again.stats()["ai_questions"], before)

    def make_store_from(self, root):
        db = root / "migrated.db"
        return (
            AiQuestionStore(db, data_root=root),
            db,
        )


class ExportImportTests(StoreTestCase):
    def _populate(self, store):
        store.save_options_by_eid(
            "course-1", "exam-1", "E-1", OPTIONS, content="题干一"
        )
        store.save_options_by_id(
            "course-1", "exam-1", "Q-1", OPTIONS, content="题干一"
        )
        store.record_wrong("course-1", "exam-1", eid="E-1", wrong_options=[12])
        store.mark_correct(
            "course-1",
            "exam-1",
            eid="E-1",
            correct_options=[11],
            ai_analysis="解析一",
        )
        store.upsert_ai_question(
            "course-1",
            "exam-1",
            "q-ai",
            1,
            question_plain="AI 题干",
            answer="A",
            question_dict={"letters": ["A"]},
        )

    @staticmethod
    def _normalize(payload):
        payload = json.loads(json.dumps(payload, ensure_ascii=False))
        payload.pop("exported_at", None)
        for group in ("zhidao_questions", "ai_questions"):
            for item in payload[group]:
                item.pop("last_updated", None)
            payload[group].sort(
                key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True)
            )
        return payload

    def test_export_clear_import_roundtrip(self):
        store, _ = self.make_store()
        self._populate(store)
        exported = store.export_data("course-1")
        self.assertEqual(exported["schema_version"], 1)
        self.assertEqual(len(exported["zhidao_questions"]), 1)
        self.assertEqual(len(exported["ai_questions"]), 1)

        counts = store.clear("course-1")
        self.assertEqual(
            counts,
            {"zhidao_questions": 1, "ai_questions": 1, "json_files": 0},
        )
        stats = store.stats("course-1")
        self.assertEqual(stats["zhidao_questions"], 0)
        self.assertEqual(stats["ai_questions"], 0)

        counts = store.import_data(exported)
        self.assertEqual(counts, {"zhidao_questions": 1, "ai_questions": 1})
        reexported = store.export_data("course-1")
        self.assertEqual(self._normalize(reexported), self._normalize(exported))

    def test_import_does_not_erase_existing_correct_answer_with_empty_data(self):
        store, _ = self.make_store()
        self._populate(store)
        payload = {
            "zhidao_questions": [
                {
                    "course_id": "course-1",
                    "exam_id": "exam-1",
                    "eid": "E-1",
                    "question_id": "Q-1",
                    "options": OPTIONS,
                    "correct_options": [],
                    "wrong_options": [],
                    "ai_analysis": "",
                }
            ]
        }
        store.import_data(payload)
        row = store.get_by_eid("course-1", "E-1", exam_id="exam-1")
        self.assertEqual(row["correct_options"], [11])
        self.assertEqual(row["ai_analysis"], "解析一")


    def test_import_with_both_keys_merges_conflicting_rows(self):
        store, _ = self.make_store()
        store.save_options_by_eid(
            "course-1", "exam-1", "E-9", OPTIONS, content="按 eid 的题干"
        )
        store.save_options_by_id(
            "course-1",
            "exam-1",
            "Q-9",
            [{"id": 21, "content": "另一组选项"}],
            content="按数字 id 的题干",
        )
        payload = {
            "zhidao_questions": [
                {
                    "course_id": "course-1",
                    "exam_id": "exam-1",
                    "eid": "E-9",
                    "question_id": "Q-9",
                    "content": "合并后的题干",
                    "options": OPTIONS,
                    "correct_options": [11],
                }
            ]
        }

        counts = store.import_data(payload)

        self.assertEqual(counts["zhidao_questions"], 1)
        row = store.get_by_eid("course-1", "E-9", exam_id="exam-1")
        self.assertEqual(row["question_id"], "Q-9")
        self.assertEqual(row["content"], "合并后的题干")
        self.assertEqual(row["correct_options"], [11])
        self.assertEqual(store.stats("course-1")["zhidao_questions"], 1)

    def test_import_conflict_merge_preserves_dropped_row_learning_records(self):
        store, _ = self.make_store()
        store.save_options_by_eid(
            "course-1", "exam-1", "E-10", [], content=""
        )
        store.save_options_by_id(
            "course-1", "exam-1", "Q-10", OPTIONS, content="原题干"
        )
        store.record_wrong("course-1", "exam-1", question_id="Q-10", wrong_options=["12"])
        store.mark_correct(
            "course-1",
            "exam-1",
            question_id="Q-10",
            correct_options=["11"],
            ai_analysis="保留的旧解析",
            auto_clear_wrong=False,
        )

        payload = {
            "zhidao_questions": [
                {
                    "course_id": "course-1",
                    "exam_id": "exam-1",
                    "eid": "E-10",
                    "question_id": "Q-10",
                    "content": "",
                    "options": [],
                    "correct_options": [],
                    "wrong_options": [],
                    "ai_analysis": "",
                }
            ]
        }
        store.import_data(payload)

        row = store.get_by_eid("course-1", "E-10", exam_id="exam-1")
        self.assertEqual(row["question_id"], "Q-10")
        self.assertEqual(row["correct_options"], ["11"])
        self.assertEqual(row["wrong_options"], [["12"]])
        self.assertEqual(row["ai_analysis"], "保留的旧解析")
        self.assertEqual(row["content"], "原题干")
        self.assertEqual(row["options"], OPTIONS)
        self.assertEqual(store.stats("course-1")["zhidao_questions"], 1)


class MarkCorrectTests(StoreTestCase):
    def test_mark_correct_clears_stale_ai_analysis(self):
        store, _ = self.make_store()
        store.save_options_by_eid(
            "course-1", "exam-1", "E-1", OPTIONS, content="题干一"
        )
        store.mark_correct(
            "course-1",
            "exam-1",
            eid="E-1",
            correct_options=[11],
            ai_analysis="旧解析",
        )
        row = store.get_by_eid("course-1", "E-1", exam_id="exam-1")
        self.assertEqual(row["ai_analysis"], "旧解析")

        store.mark_correct("course-1", "exam-1", eid="E-1", correct_options=[12])

        row = store.get_by_eid("course-1", "E-1", exam_id="exam-1")
        self.assertEqual(row["ai_analysis"], "")

    def test_mark_correct_keeps_explicit_analysis(self):
        store, _ = self.make_store()
        store.save_options_by_eid(
            "course-1", "exam-1", "E-2", OPTIONS, content="题干二"
        )
        store.mark_correct(
            "course-1",
            "exam-1",
            eid="E-2",
            correct_options=[11],
            ai_analysis="新解析",
        )
        row = store.get_by_eid("course-1", "E-2", exam_id="exam-1")
        self.assertEqual(row["ai_analysis"], "新解析")


class ConcurrencyTests(StoreTestCase):
    def test_threaded_writers_do_not_lose_rows(self):
        store, root = self.make_store()
        errors = []

        def worker(index):
            try:
                local = AiQuestionStore(
                    store.db_path, data_root=root, migrate=False
                )
                for item in range(25):
                    local.upsert_ai_question(
                        "course-1",
                        "exam-1",
                        f"q-{index}-{item}",
                        1,
                        question_plain=f"并发题干 {index}-{item}",
                        answer="A",
                    )
            except Exception as exc:  # pragma: no cover - 失败时用于定位
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(errors, [])
        self.assertEqual(store.stats("course-1")["ai_questions"], 200)


class AnswerBankStoreTests(StoreTestCase):
    def test_answer_bank_roundtrip_and_cross_exam_search(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(lambda: _remove_tree(directory))
        bank = AnswerBank("course-x", base_dir=directory)
        bank.load("exam-1")
        bank.set(
            501,
            1,
            {"question": "银行题干", "answer": "2#@#3", "questionDict": {"id": 501}},
        )
        bank.save("exam-1")

        loaded = AnswerBank("course-x", base_dir=directory)
        loaded.load("exam-1")
        self.assertEqual(loaded.get(501, 1)["answer"], "2#@#3")

        row = loaded.search_in_course("银行题干")
        self.assertIsNotNone(row)
        self.assertEqual(row["answer"], "2#@#3")


class AnswerCacheStoreTests(StoreTestCase):
    def test_answer_cache_syncs_to_store_and_falls_back(self):
        store, root = self.make_store()
        cache_path = root / "mastery_answers.json"
        cache = AnswerCache(cache_path, store=store)
        cache.put("光合作用的主要产物是什么？", {"letters": ["A"]})

        fresh = AnswerCache(root / "empty.json", store=store)
        self.assertEqual(
            fresh.get("光合作用的主要产物是什么"),
            {"letters": ["A"]},
        )

    def test_clear_removes_sqlite_rows_and_json_snapshots(self):
        store, root = self.make_store()
        store.save_options_by_eid("course-1", "exam-1", "E-1", OPTIONS, content="题干1")
        store.upsert_ai_question("course-1", "exam-1", "q-1", 1, question_plain="AI题1", answer="A")
        store.upsert_ai_question("course-2", "exam-2", "q-2", 1, question_plain="AI题2", answer="B")

        c1_dir = root / "exam_answers" / "course-1"
        c2_dir = root / "exam_answers" / "course-2"
        c1_dir.mkdir(parents=True)
        c2_dir.mkdir(parents=True)
        (c1_dir / "data.json").write_text("{}", encoding="utf-8")
        (c2_dir / "data.json").write_text("{}", encoding="utf-8")
        mastery_file = root / "mastery_answers.json"
        mastery_file.write_text("{}", encoding="utf-8")
        unrelated = root / "keep.json"
        unrelated.write_text("{}", encoding="utf-8")

        # 路径穿越 course_id 不会误删 exam_answers 外的文件
        escape_counts = store.clear("..")
        self.assertEqual(escape_counts["json_files"], 0)
        self.assertTrue(unrelated.exists())

        # 按课程清理: 只清 course-1 的记录与快照
        part = store.clear("course-1")
        self.assertEqual(part, {"zhidao_questions": 1, "ai_questions": 1, "json_files": 1})
        self.assertFalse((c1_dir / "data.json").exists())
        self.assertTrue((c2_dir / "data.json").exists())
        self.assertTrue(mastery_file.exists())
        self.assertEqual(store.stats()["ai_questions"], 1)

        # 全量清理: 清空剩余记录与所有 JSON 快照, 不影响无关文件
        rest = store.clear()
        self.assertEqual(rest, {"zhidao_questions": 0, "ai_questions": 1, "json_files": 2})
        self.assertFalse((c2_dir / "data.json").exists())
        self.assertFalse(mastery_file.exists())
        self.assertTrue(unrelated.exists())
        self.assertEqual(store.stats()["zhidao_questions"], 0)
        self.assertEqual(store.stats()["ai_questions"], 0)

        # 全量清理并执行 WAL checkpoint + VACUUM 后，DB 仍可正常读写无锁异常
        store.save_options_by_eid("course-after", "exam-after", "E-after", OPTIONS, content="清理后新题")
        row = store.get_by_eid("course-after", "E-after", exam_id="exam-after")
        self.assertIsNotNone(row)
        self.assertEqual(row["content"], "清理后新题")


class T15ConcurrencyAndMergeTests(StoreTestCase):
    def test_save_options_by_id_bridge_preserves_eid_answers_and_analysis(self):
        store, _ = self.make_store()
        # 1. Existing eid row already has correct_options, wrong_options, and ai_analysis
        store.save_options_by_eid(
            "course-m", "exam-m", "E-99", OPTIONS, question_type="1", content="桥接保留题"
        )
        store.record_wrong("course-m", "exam-m", eid="E-99", wrong_options=[12])
        store.mark_correct(
            "course-m",
            "exam-m",
            eid="E-99",
            correct_options=[11],
            ai_analysis="权威解析内容",
            auto_clear_wrong=False,
        )
        # 2. Separate question_id row created first without options/content so it doesn't bridge yet
        store.save_options_by_id("course-m", "exam-m", "Q-99", [], question_type="1", content="")
        self.assertEqual(store.stats("course-m")["zhidao_questions"], 2)

        # 3. Now save_options_by_id supplies matching options -> triggers bridge + delete of eid row
        merged_id = store.save_options_by_id(
            "course-m", "exam-m", "Q-99", OPTIONS, question_type="1", content="桥接保留题"
        )
        self.assertEqual(store.stats("course-m")["zhidao_questions"], 1)
        row = store.get_by_id("course-m", "Q-99", exam_id="exam-m")
        self.assertEqual(row["id"], merged_id)
        self.assertEqual(row["eid"], "E-99")
        self.assertEqual(row["correct_options"], [11])
        self.assertEqual(row["wrong_options"], [["12"]])
        self.assertEqual(row["ai_analysis"], "权威解析内容")

    def test_multithreaded_concurrent_writes_succeed_without_lock_or_integrity_errors(self):
        store, _ = self.make_store()
        errors: list[Exception] = []

        def worker(tid: int):
            try:
                for i in range(15):
                    eid = f"E-{tid}-{i}"
                    opts = [
                        {"id": tid * 1000 + i * 2 + 1, "content": "A"},
                        {"id": tid * 1000 + i * 2 + 2, "content": "B"},
                    ]
                    store.save_options_by_eid(
                        "course-c", "exam-c", eid, opts, content=f"并发题-{tid}-{i}"
                    )
                    store.record_wrong(
                        "course-c", "exam-c", eid=eid, wrong_options=[opts[1]["id"]]
                    )
                    store.upsert_ai_question(
                        "course-c",
                        "exam-c",
                        f"q-{tid}-{i}",
                        1,
                        question_plain=f"AI并发题-{tid}-{i}",
                        answer="A",
                    )
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(errors, [])
        stats = store.stats("course-c")
        self.assertEqual(stats["zhidao_questions"], 90)
        self.assertEqual(stats["ai_questions"], 90)

    def test_import_caps_oversized_wrong_options_to_limit(self):
        from modules.answer_store import WRONG_COMBO_LIMIT

        store, _ = self.make_store()
        oversized = [[str(i)] for i in range(WRONG_COMBO_LIMIT + 15)]
        payload = {
            "zhidao_questions": [
                {
                    "course_id": "course-lim",
                    "exam_id": "exam-lim",
                    "eid": "E-lim",
                    "content": "超长错题记录",
                    "options": OPTIONS,
                    "wrong_options": oversized,
                }
            ],
            "ai_questions": [],
        }
        store.import_data(payload)
        row = store.get_by_eid("course-lim", "E-lim", exam_id="exam-lim")
        self.assertEqual(len(row["wrong_options"]), WRONG_COMBO_LIMIT)
        self.assertEqual(row["wrong_options"], oversized[-WRONG_COMBO_LIMIT:])

        # Re-importing more wrong options also respects the limit
        more = [[f"new-{i}"] for i in range(10)]
        payload["zhidao_questions"][0]["wrong_options"] = more
        store.import_data(payload)
        row2 = store.get_by_eid("course-lim", "E-lim", exam_id="exam-lim")
        self.assertEqual(len(row2["wrong_options"]), WRONG_COMBO_LIMIT)
        self.assertEqual(row2["wrong_options"][-10:], more)


if __name__ == "__main__":
    unittest.main()