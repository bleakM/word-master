import json
import lzma
import os
import random
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set, Tuple

from PyQt5 import QtCore, QtGui, QtWidgets


DATA_FILE = "word_data.wmz"
LEGACY_DATA_FILE = "word_data.json"
DATA_MAGIC = b"WMZ2"
DEFAULT_REMAIN = 3
APP_DIR = os.path.dirname(os.path.abspath(__file__))
SPLASH_IMAGE = os.path.join(APP_DIR, "word_master.png")
DEFAULT_ALGORITHM = "normal"
ALGORITHM_CODES = {"normal": 0, "ebbinghaus": 1, "scientific": 2}
CODE_TO_ALGORITHM = {code: name for name, code in ALGORITHM_CODES.items()}
FULL_SESSION_ALGORITHMS = ("normal", "scientific")
ALGORITHM_FALLBACK_ORDER = {
    "normal": ("ebbinghaus",),
    "scientific": ("ebbinghaus",),
    "ebbinghaus": ("scientific", "normal"),
}
RESULT_CODES = {"": 0, "correct": 1, "wrong": 2, "unknown": 3, "slash": 4}
CODE_TO_RESULT = {code: name for name, code in RESULT_CODES.items()}


def current_timestamp() -> int:
    return int(time.time())


@dataclass
class WordEntry:
    word: str
    meaning: str
    errors: int = 0

    def as_dict(self) -> dict:
        return {"word": self.word, "meaning": self.meaning, "errors": self.errors}


@dataclass
class WordBook:
    id: str
    name: str
    book_type: str
    tags: List[str]
    entries: List[WordEntry] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "book_type": self.book_type,
            "tags": self.tags,
            "entries": [e.as_dict() for e in self.entries],
        }


class DataStore:
    def __init__(self, path: str):
        self.path = path
        self.books: List[WordBook] = []
        self.global_errors: Dict[str, int] = {}
        self.global_words: Dict[str, str] = {}
        self.memory_states: Dict[str, dict] = {}
        self.study_states: Dict[str, dict] = {}
        self.window_geo: dict = {}
        self.settings: dict = {"algorithm": DEFAULT_ALGORITHM}
        self.history: List[dict] = []
        self.future: List[dict] = []
        self.load()

    def load(self) -> None:
        data: Optional[dict] = None
        should_save = False
        if os.path.exists(self.path):
            try:
                data = self._read_storage(self.path)
            except Exception:
                data = None
        if data is None:
            legacy_path = self._legacy_path()
            if os.path.exists(legacy_path):
                try:
                    data = self._read_legacy_json(legacy_path)
                except Exception:
                    data = self._blank_payload()
                should_save = True
            else:
                data = self._blank_payload()
                should_save = True
        self._apply_payload(data)
        self.history.clear()
        self.future.clear()
        init_payload = self._build_payload()
        if should_save or not os.path.exists(self.path):
            self._write_payload(init_payload, record_history=False, label="初始化")
        self._record_snapshot(init_payload, label="初始状态", clear_future=True)

    def save(self, push_history: bool = True, label: str = "更改") -> None:
        payload = self._build_payload()
        self._write_payload(payload, record_history=push_history, label=label or "更改")

    def _save_blank(self) -> None:
        self._write_payload(self._blank_payload(), record_history=False, label="初始化")

    def _build_payload(self) -> dict:
        return {
            "books": [b.as_dict() for b in self.books],
            "global_errors": self.global_errors,
            "global_words": self.global_words,
            "memory_states": self.memory_states,
            "study_states": self.study_states,
            "window": self.window_geo,
            "settings": self.settings,
        }

    def _apply_payload(self, data: dict) -> None:
        data = self._normalize_payload(data)
        self.books = [
            WordBook(
                id=b.get("id", str(uuid.uuid4())),
                name=b.get("name", "未命名"),
                book_type=b.get("book_type", "英-汉"),
                tags=b.get("tags", ["默认"]),
                entries=[WordEntry(e["word"], e["meaning"], e.get("errors", 0)) for e in b.get("entries", [])],
            )
            for b in data.get("books", [])
        ]
        self.global_errors = {str(k).lower(): int(v) for k, v in data.get("global_errors", {}).items()}
        self.global_words = {str(k).lower(): str(v) for k, v in data.get("global_words", {}).items()}
        self.memory_states = {
            str(k).lower(): self._normalize_memory_state(v) for k, v in data.get("memory_states", {}).items()
        }
        self.study_states = self._normalize_study_states(data.get("study_states", {}))
        self.window_geo = dict(data.get("window", {}))
        self.settings = {"algorithm": DEFAULT_ALGORITHM}
        self.settings.update(data.get("settings", {}))
        if self.settings.get("algorithm") not in ALGORITHM_CODES:
            self.settings["algorithm"] = DEFAULT_ALGORITHM
        for b in self.books:
            for e in b.entries:
                key = e.word.lower()
                self.global_errors.setdefault(key, e.errors)
                if e.meaning:
                    self.global_words.setdefault(key, e.meaning)
        for key in list(self.global_errors.keys()):
            self.global_words.setdefault(key, "")
        for key in list(self.global_words.keys()):
            self.global_errors.setdefault(key, 0)
        for key in list(self.memory_states.keys()):
            self.global_errors.setdefault(key, 0)
            self.global_words.setdefault(key, "")

    def _write_payload(self, payload: dict, record_history: bool, label: str = "更改") -> None:
        encoded = self._encode_payload(payload)
        with open(self.path, "wb") as f:
            f.write(encoded)
        if record_history:
            self._record_snapshot(payload, label=label, clear_future=True)

    def _record_snapshot(self, payload: dict, label: str = "更改", clear_future: bool = False) -> None:
        snapshot = {"payload": json.loads(json.dumps(payload)), "label": label or "更改"}
        if self.history and self.history[-1]["payload"] == snapshot["payload"]:
            return
        self.history.append(snapshot)
        if len(self.history) > 20:
            self.history = self.history[-20:]
        if clear_future:
            self.future.clear()

    def can_undo(self) -> bool:
        return len(self.history) > 1

    def can_redo(self) -> bool:
        return len(self.future) > 0

    def undo(self) -> Optional[str]:
        if not self.can_undo():
            return None
        current = self.history.pop()
        self.future.append(current)
        payload_entry = self.history[-1]
        self._apply_payload(payload_entry["payload"])
        self._write_payload(payload_entry["payload"], record_history=False, label=payload_entry["label"])
        return current["label"]

    def redo(self) -> Optional[str]:
        if not self.can_redo():
            return None
        entry = self.future.pop()
        self._apply_payload(entry["payload"])
        self._write_payload(entry["payload"], record_history=False, label=entry["label"])
        self._record_snapshot(entry["payload"], label=entry["label"], clear_future=False)
        return entry["label"]

    def unique_name(self, name: str, exclude_id: Optional[str] = None) -> str:
        base = name or "未命名"
        existing = [b.name for b in self.books if b.id != exclude_id]
        candidate = base
        idx = 1
        while candidate in existing:
            candidate = f"{base}{idx}"
            idx += 1
        return candidate

    def ensure_global_word(self, word: str, meaning: str, errors: int = 0) -> None:
        key = word.lower()
        self.global_errors[key] = max(errors, self.global_errors.get(key, errors))
        if meaning:
            self.global_words.setdefault(key, meaning)

    def rename_or_update_word(self, old_word: str, new_word: str, meaning: str) -> None:
        old_key = old_word.lower()
        new_key = new_word.lower()
        err_val = self.global_errors.pop(old_key, 0)
        err_val = max(err_val, self.global_errors.get(new_key, err_val))
        self.global_errors[new_key] = err_val
        memory_state = self.memory_states.pop(old_key, None)
        if memory_state:
            existing_memory = self.memory_states.get(new_key)
            if existing_memory:
                merged = self._normalize_memory_state(existing_memory)
                incoming = self._normalize_memory_state(memory_state)
                merged["stability"] = max(merged["stability"], incoming["stability"])
                merged["difficulty"] = max(merged["difficulty"], incoming["difficulty"])
                merged["due_at"] = max(merged["due_at"], incoming["due_at"])
                merged["last_reviewed_at"] = max(merged["last_reviewed_at"], incoming["last_reviewed_at"])
                merged["last_wrong_at"] = max(merged["last_wrong_at"], incoming["last_wrong_at"])
                merged["reps"] = max(merged["reps"], incoming["reps"])
                merged["lapses"] = max(merged["lapses"], incoming["lapses"])
                merged["streak"] = max(merged["streak"], incoming["streak"])
                merged["seen_count"] = max(merged["seen_count"], incoming["seen_count"])
                merged["last_result"] = incoming["last_result"] or merged["last_result"]
                self.memory_states[new_key] = merged
            else:
                self.memory_states[new_key] = self._normalize_memory_state(memory_state)
        if meaning:
            self.global_words.pop(old_key, None)
            self.global_words[new_key] = meaning
        for book in self.books:
            for entry in book.entries:
                if entry.word.lower() == old_key:
                    entry.word = new_word
                    entry.meaning = meaning
                    entry.errors = err_val
        for state in self.study_states.values():
            words_state = state.get("words")
            if words_state and old_key in words_state:
                words_state[new_key] = words_state.pop(old_key)

    def delete_words(self, words: List[str], label: str = "删除单词") -> None:
        keys = {w.lower() for w in words}
        if not keys:
            return
        for key in keys:
            self.global_errors.pop(key, None)
            self.global_words.pop(key, None)
            self.memory_states.pop(key, None)
        for book in self.books:
            book.entries = [e for e in book.entries if e.word.lower() not in keys]
        for state in self.study_states.values():
            words_state = state.get("words")
            if words_state:
                for key in list(keys):
                    words_state.pop(key, None)
                cap = 5 * len(words_state)
                if "initial" in state:
                    state["initial"] = min(state.get("initial", cap), cap)
        self.save(label=label)

    def _study_state_key(self, book_id: str, algorithm: Optional[str] = None) -> str:
        algo = algorithm if algorithm in ALGORITHM_CODES else DEFAULT_ALGORITHM
        return f"{book_id}|{algo}"

    def _state_copy_for_algorithm(self, book_id: str, state: dict, algorithm: str) -> dict:
        state_copy = json.loads(json.dumps(state))
        state_copy["algorithm"] = algorithm
        state_copy["book_id"] = book_id
        normalized = self._normalize_study_states({self._study_state_key(book_id, algorithm): state_copy})
        return normalized[self._study_state_key(book_id, algorithm)]

    def _full_session_score(self, state: dict) -> Tuple[int, int, int, int, int]:
        words = state.get("words", {})
        step = int(state.get("step", 0))
        attempts_total = 0
        remaining_total = 0
        seen_total = 0
        has_scientific_fields = 0
        for info in words.values():
            if not isinstance(info, dict):
                continue
            attempts_total += int(info.get("correct", 0)) + int(info.get("wrong", 0))
            remaining_total += int(info.get("remaining", 0))
            seen_total += int(info.get("seen", 0))
            if any(key in info for key in ("seen", "last_result", "last_step")):
                has_scientific_fields = 1
        return step, attempts_total, seen_total, -remaining_total, has_scientific_fields

    def _sync_full_session_states(self, book_id: str) -> Optional[dict]:
        states = []
        for algorithm in FULL_SESSION_ALGORITHMS:
            state = self.study_states.get(self._study_state_key(book_id, algorithm))
            if state:
                states.append(state)
        if not states:
            return None
        canonical = max(states, key=self._full_session_score)
        for algorithm in FULL_SESSION_ALGORITHMS:
            self.study_states[self._study_state_key(book_id, algorithm)] = self._state_copy_for_algorithm(
                book_id, canonical, algorithm
            )
        return canonical

    def get_study_state(self, book_id: str, algorithm: Optional[str] = None) -> Optional[dict]:
        algo = algorithm or self.settings.get("algorithm", DEFAULT_ALGORITHM)
        if algo in FULL_SESSION_ALGORITHMS:
            synced = self._sync_full_session_states(book_id)
            if not synced:
                return None
        return self.study_states.get(self._study_state_key(book_id, algo))

    def get_alternate_study_state(self, book_id: str, algorithm: Optional[str] = None) -> Optional[dict]:
        algo = algorithm or self.settings.get("algorithm", DEFAULT_ALGORITHM)
        for candidate in ALGORITHM_FALLBACK_ORDER.get(
            algo, tuple(name for name in ALGORITHM_CODES.keys() if name != algo)
        ):
            state = self.study_states.get(self._study_state_key(book_id, candidate))
            if state:
                return state
        return None

    def set_study_state(self, book_id: str, state: dict, algorithm: Optional[str] = None) -> None:
        algo = algorithm or state.get("algorithm", DEFAULT_ALGORITHM)
        targets = FULL_SESSION_ALGORITHMS if algo in FULL_SESSION_ALGORITHMS else (algo,)
        for target in targets:
            self.study_states[self._study_state_key(book_id, target)] = self._state_copy_for_algorithm(
                book_id, state, target
            )

    def clear_book_study_states(self, book_id: str, algorithm: Optional[str] = None) -> None:
        if algorithm:
            targets = FULL_SESSION_ALGORITHMS if algorithm in FULL_SESSION_ALGORITHMS else (algorithm,)
            for target in targets:
                self.study_states.pop(self._study_state_key(book_id, target), None)
            return
        prefix = f"{book_id}|"
        for key in [key for key in self.study_states.keys() if key == book_id or key.startswith(prefix)]:
            self.study_states.pop(key, None)

    def set_algorithm(self, algorithm: str) -> None:
        if algorithm not in ALGORITHM_CODES:
            algorithm = DEFAULT_ALGORITHM
        if self.settings.get("algorithm") == algorithm:
            return
        self.settings["algorithm"] = algorithm
        self.save(push_history=False, label="切换记忆框架")

    def ensure_memory_state(self, word: str) -> dict:
        key = word.lower()
        state = self.memory_states.get(key)
        if not state:
            state = self._default_memory_state()
            self.memory_states[key] = state
        else:
            state = self._normalize_memory_state(state)
            self.memory_states[key] = state
        return state

    def review_word(self, word: str, result: str) -> None:
        key = word.lower()
        state = self.ensure_memory_state(key)
        now = current_timestamp()
        previous_seen = state["seen_count"]
        difficulty = state["difficulty"]
        stability = max(state["stability"], 30 * 60)
        state["last_reviewed_at"] = now
        state["seen_count"] += 1
        state["last_result"] = result

        if result in ("wrong", "unknown"):
            state["lapses"] += 1
            state["streak"] = 0
            difficulty = min(850, difficulty + (70 if result == "unknown" else 45))
            stability = max(20 * 60, stability * (35 if result == "unknown" else 50) // 100)
            state["last_wrong_at"] = now
            retry_minutes = random.randint(8, 14) if result == "unknown" else random.randint(12, 20)
            due_at = now + retry_minutes * 60
        else:
            state["reps"] += 1
            state["streak"] += 1
            if result == "slash":
                difficulty = max(130, difficulty - 22)
                if previous_seen == 0:
                    stability = 18 * 60 * 60
                else:
                    factor_bp = 235 + min(state["streak"], 6) * 22 + max(0, 600 - difficulty) // 25
                    stability = max(30 * 60, stability * factor_bp // 100)
            else:
                difficulty = max(130, difficulty - 14)
                if previous_seen == 0:
                    stability = 12 * 60 * 60
                else:
                    factor_bp = 185 + min(state["streak"], 6) * 18 + max(0, 600 - difficulty) // 30
                    stability = max(30 * 60, stability * factor_bp // 100)
            jitter_bp = random.randint(92, 108)
            due_at = now + stability * jitter_bp // 100

        state["difficulty"] = int(difficulty)
        state["stability"] = int(stability)
        state["due_at"] = int(due_at)
        self.memory_states[key] = state

    @classmethod
    def convert_legacy_json(cls, legacy_path: str, target_path: str) -> str:
        helper = cls.__new__(cls)
        helper.path = target_path
        payload = helper._normalize_payload(helper._read_legacy_json(legacy_path))
        helper._write_payload(payload, record_history=False, label="迁移数据")
        return target_path

    def _legacy_path(self) -> str:
        return os.path.join(os.path.dirname(self.path), LEGACY_DATA_FILE)

    def _blank_payload(self) -> dict:
        return {
            "books": [],
            "global_errors": {},
            "global_words": {},
            "memory_states": {},
            "study_states": {},
            "window": {},
            "settings": {"algorithm": DEFAULT_ALGORITHM},
        }

    def _default_memory_state(self) -> dict:
        return {
            "stability": 12 * 60 * 60,
            "difficulty": 500,
            "due_at": 0,
            "last_reviewed_at": 0,
            "last_wrong_at": 0,
            "reps": 0,
            "lapses": 0,
            "streak": 0,
            "seen_count": 0,
            "last_result": "",
        }

    def _normalize_memory_state(self, state: Optional[dict]) -> dict:
        normalized = self._default_memory_state()
        if not isinstance(state, dict):
            return normalized
        for key in ("stability", "difficulty", "due_at", "last_reviewed_at", "last_wrong_at", "reps", "lapses", "streak", "seen_count"):
            if key in state:
                normalized[key] = int(state.get(key, normalized[key]))
        result = str(state.get("last_result", normalized["last_result"]))
        normalized["last_result"] = result if result in RESULT_CODES else ""
        normalized["difficulty"] = max(130, min(850, normalized["difficulty"]))
        normalized["stability"] = max(20 * 60, normalized["stability"])
        return normalized

    def _normalize_study_states(self, states: dict) -> Dict[str, dict]:
        normalized: Dict[str, dict] = {}
        if not isinstance(states, dict):
            return normalized
        for bid, state in states.items():
            if not isinstance(state, dict):
                continue
            raw_key = str(bid)
            parsed_book_id = raw_key
            parsed_algorithm: Optional[str] = None
            if "|" in raw_key:
                maybe_book_id, maybe_algorithm = raw_key.rsplit("|", 1)
                if maybe_algorithm in ALGORITHM_CODES:
                    parsed_book_id = maybe_book_id
                    parsed_algorithm = maybe_algorithm
            algorithm = str(state.get("algorithm", parsed_algorithm or DEFAULT_ALGORITHM))
            if algorithm not in ALGORITHM_CODES:
                algorithm = parsed_algorithm or DEFAULT_ALGORITHM
            book_id = str(state.get("book_id", parsed_book_id))
            words_state: Dict[str, dict] = {}
            for key, info in state.get("words", {}).items():
                if not isinstance(info, dict):
                    continue
                item = {
                    "remaining": int(info.get("remaining", 0)),
                    "correct": int(info.get("correct", 0)),
                    "wrong": int(info.get("wrong", 0)),
                }
                if "next_step" in info:
                    item["next_step"] = int(info.get("next_step", 0))
                if "seen" in info:
                    item["seen"] = int(info.get("seen", 0))
                if "last_step" in info:
                    item["last_step"] = int(info.get("last_step", -1))
                last_result = str(info.get("last_result", ""))
                if last_result in RESULT_CODES and last_result:
                    item["last_result"] = last_result
                words_state[str(key).lower()] = item
            normalized[self._study_state_key(book_id, algorithm)] = {
                "book_id": book_id,
                "words": words_state,
                "initial": int(state.get("initial", 0)),
                "algorithm": algorithm,
                "step": int(state.get("step", 0)),
            }
        return normalized

    def _normalize_payload(self, data: Optional[dict]) -> dict:
        payload = self._blank_payload()
        if not isinstance(data, dict):
            return payload
        payload["books"] = data.get("books", [])
        payload["global_errors"] = data.get("global_errors", {})
        payload["global_words"] = data.get("global_words", {})
        payload["memory_states"] = data.get("memory_states", {})
        payload["study_states"] = data.get("study_states", {})
        payload["window"] = data.get("window", {})
        payload["settings"] = data.get("settings", {"algorithm": DEFAULT_ALGORITHM})
        return payload

    def _read_legacy_json(self, path: str) -> dict:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        payload = self._normalize_payload(data)
        payload.setdefault("memory_states", {})
        payload.setdefault("settings", {"algorithm": DEFAULT_ALGORITHM})
        return payload

    def _encode_memory_state(self, state: dict) -> List[int]:
        normalized = self._normalize_memory_state(state)
        return [
            normalized["stability"],
            normalized["difficulty"],
            normalized["due_at"],
            normalized["last_reviewed_at"],
            normalized["last_wrong_at"],
            normalized["reps"],
            normalized["lapses"],
            normalized["streak"],
            normalized["seen_count"],
            RESULT_CODES.get(normalized["last_result"], 0),
        ]

    def _decode_memory_state(self, data: list) -> dict:
        if not isinstance(data, list) or not data:
            return self._default_memory_state()
        normalized = self._default_memory_state()
        fields = [
            "stability",
            "difficulty",
            "due_at",
            "last_reviewed_at",
            "last_wrong_at",
            "reps",
            "lapses",
            "streak",
            "seen_count",
        ]
        for idx, field in enumerate(fields):
            if idx < len(data):
                normalized[field] = int(data[idx])
        result_code = int(data[9]) if len(data) > 9 else 0
        normalized["last_result"] = CODE_TO_RESULT.get(result_code, "")
        return self._normalize_memory_state(normalized)

    def _encode_payload(self, payload: dict) -> bytes:
        payload = self._normalize_payload(payload)
        payload["study_states"] = self._normalize_study_states(payload.get("study_states", {}))
        words_meta: Dict[str, dict] = {}
        for key, meaning in payload.get("global_words", {}).items():
            lower_key = str(key).lower()
            words_meta.setdefault(lower_key, {"word": lower_key, "meaning": "", "errors": 0, "memory": None})
            words_meta[lower_key]["meaning"] = str(meaning)
        for key, errors in payload.get("global_errors", {}).items():
            lower_key = str(key).lower()
            words_meta.setdefault(lower_key, {"word": lower_key, "meaning": "", "errors": 0, "memory": None})
            words_meta[lower_key]["errors"] = max(words_meta[lower_key]["errors"], int(errors))
        for key, memory_state in payload.get("memory_states", {}).items():
            lower_key = str(key).lower()
            words_meta.setdefault(lower_key, {"word": lower_key, "meaning": "", "errors": 0, "memory": None})
            words_meta[lower_key]["memory"] = self._normalize_memory_state(memory_state)
        for book in payload.get("books", []):
            for entry in book.get("entries", []):
                lower_key = str(entry.get("word", "")).lower()
                words_meta.setdefault(lower_key, {"word": lower_key, "meaning": "", "errors": 0, "memory": None})
                words_meta[lower_key]["word"] = str(entry.get("word", words_meta[lower_key]["word"]))
                if entry.get("meaning"):
                    words_meta[lower_key]["meaning"] = str(entry.get("meaning"))
                words_meta[lower_key]["errors"] = max(words_meta[lower_key]["errors"], int(entry.get("errors", 0)))
        for state in payload.get("study_states", {}).values():
            for key in state.get("words", {}).keys():
                lower_key = str(key).lower()
                words_meta.setdefault(lower_key, {"word": lower_key, "meaning": "", "errors": 0, "memory": None})

        sorted_keys = sorted(words_meta.keys())
        word_index = {key: idx for idx, key in enumerate(sorted_keys)}
        word_records = []
        for key in sorted_keys:
            meta = words_meta[key]
            word_records.append(
                [
                    meta["word"],
                    meta["meaning"],
                    int(meta["errors"]),
                    self._encode_memory_state(meta["memory"]) if meta["memory"] else [],
                ]
            )

        book_records = []
        for book in payload.get("books", []):
            book_records.append(
                [
                    book.get("id", str(uuid.uuid4())),
                    book.get("name", "未命名"),
                    book.get("book_type", "英-汉"),
                    book.get("tags", ["默认"]),
                    [word_index[str(entry.get("word", "")).lower()] for entry in book.get("entries", []) if str(entry.get("word", "")).lower() in word_index],
                ]
            )

        state_records = []
        for state_key, state in payload.get("study_states", {}).items():
            book_id = str(state.get("book_id", str(state_key).split("|", 1)[0]))
            algorithm = str(state.get("algorithm", DEFAULT_ALGORITHM))
            words_state = []
            for key, info in state.get("words", {}).items():
                lower_key = str(key).lower()
                if lower_key not in word_index:
                    continue
                words_state.append(
                    [
                        word_index[lower_key],
                        int(info.get("remaining", 0)),
                        int(info.get("correct", 0)),
                        int(info.get("wrong", 0)),
                        int(info.get("next_step", 0)),
                        int(info.get("seen", 0)),
                        RESULT_CODES.get(str(info.get("last_result", "")), 0),
                        int(info.get("last_step", -1)),
                    ]
                )
            state_records.append(
                [
                    book_id,
                    int(state.get("initial", 0)),
                    ALGORITHM_CODES.get(algorithm, 0),
                    int(state.get("step", 0)),
                    words_state,
                ]
            )

        compact_payload = [
            2,
            ALGORITHM_CODES.get(payload.get("settings", {}).get("algorithm", DEFAULT_ALGORITHM), 0),
            [payload.get("window", {}).get("geometry", ""), payload.get("window", {}).get("state", "")],
            word_records,
            book_records,
            state_records,
        ]
        raw = json.dumps(compact_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return DATA_MAGIC + lzma.compress(raw, preset=9 | lzma.PRESET_EXTREME)

    def _read_storage(self, path: str) -> dict:
        with open(path, "rb") as f:
            data = f.read()
        if not data.startswith(DATA_MAGIC):
            raise ValueError("unsupported data file")
        payload = json.loads(lzma.decompress(data[len(DATA_MAGIC) :]).decode("utf-8"))
        if not isinstance(payload, list) or len(payload) < 6 or int(payload[0]) != 2:
            raise ValueError("invalid payload")
        _, algorithm_code, window_record, word_records, book_records, state_records = payload[:6]
        decoded = self._blank_payload()
        decoded["settings"]["algorithm"] = CODE_TO_ALGORITHM.get(int(algorithm_code), DEFAULT_ALGORITHM)
        if isinstance(window_record, list):
            decoded["window"] = {
                "geometry": str(window_record[0]) if len(window_record) > 0 else "",
                "state": str(window_record[1]) if len(window_record) > 1 else "",
            }

        words_lookup: List[Tuple[str, str, int]] = []
        for record in word_records:
            if not isinstance(record, list) or len(record) < 3:
                continue
            word = str(record[0])
            meaning = str(record[1]) if len(record) > 1 else ""
            errors = int(record[2]) if len(record) > 2 else 0
            key = word.lower()
            decoded["global_words"][key] = meaning
            decoded["global_errors"][key] = errors
            if len(record) > 3 and record[3]:
                decoded["memory_states"][key] = self._decode_memory_state(record[3])
            words_lookup.append((word, meaning, errors))

        for record in book_records:
            if not isinstance(record, list) or len(record) < 5:
                continue
            entries = []
            for idx in record[4]:
                idx = int(idx)
                if idx < 0 or idx >= len(words_lookup):
                    continue
                word, meaning, errors = words_lookup[idx]
                entries.append({"word": word, "meaning": meaning, "errors": errors})
            decoded["books"].append(
                {
                    "id": str(record[0]),
                    "name": str(record[1]),
                    "book_type": str(record[2]),
                    "tags": [str(tag) for tag in record[3]],
                    "entries": entries,
                }
            )

        for record in state_records:
            if not isinstance(record, list) or len(record) < 5:
                continue
            words_state: Dict[str, dict] = {}
            for item in record[4]:
                if not isinstance(item, list) or len(item) < 4:
                    continue
                idx = int(item[0])
                if idx < 0 or idx >= len(words_lookup):
                    continue
                key = words_lookup[idx][0].lower()
                state_item = {
                    "remaining": int(item[1]),
                    "correct": int(item[2]),
                    "wrong": int(item[3]),
                }
                if len(item) > 4 and int(item[4]):
                    state_item["next_step"] = int(item[4])
                if len(item) > 5 and int(item[5]):
                    state_item["seen"] = int(item[5])
                if len(item) > 6 and int(item[6]):
                    state_item["last_result"] = CODE_TO_RESULT.get(int(item[6]), "")
                if len(item) > 7:
                    state_item["last_step"] = int(item[7])
                words_state[key] = state_item
            algorithm = CODE_TO_ALGORITHM.get(int(record[2]), DEFAULT_ALGORITHM)
            book_id = str(record[0])
            decoded["study_states"][self._study_state_key(book_id, algorithm)] = {
                "book_id": book_id,
                "words": words_state,
                "initial": int(record[1]),
                "algorithm": algorithm,
                "step": int(record[3]),
            }
        return decoded


def themed_palette() -> QtGui.QPalette:
    palette = QtGui.QPalette()
    bg = QtGui.QColor("#f5f7fb")
    card = QtGui.QColor("#ffffff")
    text = QtGui.QColor("#1f2937")
    accent = QtGui.QColor("#2f8bfd")
    danger = QtGui.QColor("#e15554")
    palette.setColor(QtGui.QPalette.Window, bg)
    palette.setColor(QtGui.QPalette.Base, card)
    palette.setColor(QtGui.QPalette.AlternateBase, card)
    palette.setColor(QtGui.QPalette.WindowText, text)
    palette.setColor(QtGui.QPalette.Text, text)
    palette.setColor(QtGui.QPalette.Button, card)
    palette.setColor(QtGui.QPalette.ButtonText, text)
    palette.setColor(QtGui.QPalette.Highlight, accent)
    palette.setColor(QtGui.QPalette.BrightText, danger)
    return palette


def accent_button(text: str, color: str, parent=None) -> QtWidgets.QPushButton:
    btn = QtWidgets.QPushButton(text, parent)
    btn.setCursor(QtCore.Qt.PointingHandCursor)
    base = QtGui.QColor(color)
    normal = base.lighter(110).name()
    hover = base.lighter(118).name()
    pressed = base.darker(105).name()
    btn.setStyleSheet(
        f"""
        QPushButton {{
            background-color: {normal};
            border: none;
            border-radius: 12px;
            padding: 12px 16px;
            font-weight: 600;
            color: #0b1224;
        }}
        QPushButton:hover {{ background-color: {hover}; }}
        QPushButton:pressed {{ background-color: {pressed}; }}
        """
    )
    return btn


def show_toast(parent: QtWidgets.QWidget, text: str, duration: int = 2000) -> None:
    toast = QtWidgets.QDialog(parent)
    toast.setWindowFlags(
        QtCore.Qt.FramelessWindowHint
        | QtCore.Qt.ToolTip
        | QtCore.Qt.WindowStaysOnTopHint
        | QtCore.Qt.X11BypassWindowManagerHint
    )
    toast.setAttribute(QtCore.Qt.WA_TranslucentBackground)
    layout = QtWidgets.QVBoxLayout(toast)
    layout.setContentsMargins(0, 0, 0, 0)
    card = QtWidgets.QFrame()
    card.setStyleSheet(
        "background-color: rgba(38, 40, 44, 0.96); color: #ffffff; border: 1px solid #4b4e55; "
        "border-radius: 10px; padding: 10px 12px; font-size: 11pt;"
    )
    lab = QtWidgets.QLabel(text, card)
    lab.setAlignment(QtCore.Qt.AlignCenter)
    cl = QtWidgets.QVBoxLayout(card)
    cl.setContentsMargins(0, 0, 0, 0)
    cl.addWidget(lab)
    layout.addWidget(card)
    toast.adjustSize()
    geo = parent.geometry()
    x = geo.center().x() - toast.width() // 2
    y = geo.center().y() - toast.height() // 2
    toast.move(x, y)
    toast.show()
    def start_fade():
        anim = QtCore.QPropertyAnimation(toast, b"windowOpacity")
        anim.setDuration(260)
        anim.setStartValue(1.0)
        anim.setEndValue(0.0)
        anim.finished.connect(toast.close)
        anim.start()
        toast._anim = anim  # type: ignore
    QtCore.QTimer.singleShot(duration, start_fade)


def fade_widget(widget: QtWidgets.QWidget, duration: int = 200) -> None:
    if not widget:
        return
    effect = QtWidgets.QGraphicsOpacityEffect(widget)
    widget.setGraphicsEffect(effect)
    anim = QtCore.QPropertyAnimation(effect, b"opacity", widget)
    anim.setDuration(duration)
    anim.setStartValue(0.0)
    anim.setEndValue(1.0)
    anim.setEasingCurve(QtCore.QEasingCurve.InOutCubic)
    anim.finished.connect(lambda: widget.setGraphicsEffect(None))
    anim.start()
    widget._fade_anim = anim  # type: ignore


def style_popup(dlg: QtWidgets.QDialog) -> None:
    # Keep for compatibility; no custom window styling to use system window decorations.
    return


class SplashScreen(QtWidgets.QWidget):
    def __init__(self, image_path: str, parent: Optional[QtWidgets.QWidget] = None):
        super().__init__(parent)
        self.image_path = image_path
        self.pixmap = QtGui.QPixmap(self.image_path) if os.path.exists(self.image_path) else None
        if parent is None:
            self.setWindowFlags(
                QtCore.Qt.FramelessWindowHint | QtCore.Qt.WindowStaysOnTopHint | QtCore.Qt.SplashScreen
            )
        self.setAttribute(QtCore.Qt.WA_StyledBackground, True)
        self.setStyleSheet("background: white;")
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setAlignment(QtCore.Qt.AlignCenter)
        self.image_label = QtWidgets.QLabel()
        self.image_label.setAlignment(QtCore.Qt.AlignCenter)
        layout.addWidget(self.image_label)
        self.update_pixmap()

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:  # type: ignore
        super().resizeEvent(event)
        self.update_pixmap()

    def update_pixmap(self) -> None:
        if not self.pixmap or self.pixmap.isNull():
            self.image_label.clear()
            return
        side = max(1, int(min(self.width(), self.height()) / 3))
        scaled = self.pixmap.scaled(side, side, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation)
        self.image_label.setPixmap(scaled)

    def _animate_opacity(self, start: float, end: float, duration: int, on_finished: Optional[Callable[[], None]]) -> None:
        effect = self.graphicsEffect()
        if not isinstance(effect, QtWidgets.QGraphicsOpacityEffect):
            effect = QtWidgets.QGraphicsOpacityEffect(self)
            self.setGraphicsEffect(effect)
        effect.setOpacity(start)
        anim = QtCore.QPropertyAnimation(effect, b"opacity", self)
        anim.setDuration(duration)
        anim.setStartValue(start)
        anim.setEndValue(end)
        anim.setEasingCurve(QtCore.QEasingCurve.InOutCubic)
        if on_finished:
            anim.finished.connect(on_finished)
        anim.start()
        self._fade_anim = anim  # type: ignore

    def fade_out(self, duration: int = 100, on_finished: Optional[Callable[[], None]] = None) -> None:
        self._animate_opacity(1.0, 0.0, duration, lambda: self._finish(on_finished))

    def fade_in(self, duration: int = 120, on_finished: Optional[Callable[[], None]] = None) -> None:
        effect = self.graphicsEffect()
        current = effect.opacity() if isinstance(effect, QtWidgets.QGraphicsOpacityEffect) else 0.0
        self._animate_opacity(current, 1.0, duration, on_finished)

    def start_sequence(
        self,
        fade_in_ms: int = 120,
        hold_ms: int = 280,
        fade_out_ms: int = 400,
        on_finished: Optional[Callable[[], None]] = None,
        cover_immediately: bool = True,
    ) -> None:
        self.setVisible(True)
        self.raise_()

        if cover_immediately:
            effect = self.graphicsEffect()
            if not isinstance(effect, QtWidgets.QGraphicsOpacityEffect):
                effect = QtWidgets.QGraphicsOpacityEffect(self)
                self.setGraphicsEffect(effect)
            effect.setOpacity(1.0)

        def after_in():
            QtCore.QTimer.singleShot(hold_ms, lambda: self.fade_out(fade_out_ms, on_finished))

        if fade_in_ms > 0:
            self.fade_in(fade_in_ms, after_in)
        else:
            after_in()

    def _finish(self, on_finished: Optional[Callable[[], None]]) -> None:
        self.hide()
        self.deleteLater()
        if on_finished:
            on_finished()


def confirm_dialog(parent: QtWidgets.QWidget, text: str) -> bool:
    dlg = QtWidgets.QDialog(parent)
    layout = QtWidgets.QVBoxLayout(dlg)
    card = QtWidgets.QFrame()
    card.setStyleSheet(
        "background-color: #4a4f59; color: #ffffff; border: 1px solid #6b7280; "
        "border-radius: 14px; padding: 18px; font-size: 11pt;"
    )
    v = QtWidgets.QVBoxLayout(card)
    v.addWidget(QtWidgets.QLabel(text))
    btns = QtWidgets.QHBoxLayout()
    btns.addStretch()
    btn_ok = accent_button("确认", "#7cf29c")
    btn_cancel = accent_button("取消", "#c7cdda")
    btn_ok.clicked.connect(lambda: dlg.done(1))
    btn_cancel.clicked.connect(lambda: dlg.done(0))
    btns.addWidget(btn_cancel)
    btns.addWidget(btn_ok)
    v.addLayout(btns)
    layout.addWidget(card)
    shadow = QtWidgets.QGraphicsDropShadowEffect(card)
    shadow.setBlurRadius(22)
    shadow.setOffset(0, 8)
    shadow.setColor(QtGui.QColor(0, 0, 0, 55))
    card.setGraphicsEffect(shadow)
    dlg.adjustSize()
    dlg.move(parent.geometry().center() - dlg.rect().center())
    return dlg.exec_() == 1


class BookEditorDialog(QtWidgets.QDialog):
    def __init__(self, datastore: DataStore, book: Optional[WordBook] = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("编辑单词本" if book else "添加单词本")
        self.datastore = datastore
        self.book = book
        self.resize(820, 640)
        self.setModal(True)
        self.init_ui()
        style_popup(self)

    def init_ui(self) -> None:
        layout = QtWidgets.QVBoxLayout(self)
        form = QtWidgets.QFormLayout()
        self.name_edit = QtWidgets.QLineEdit(self.book.name if self.book else "")
        self.type_combo = QtWidgets.QComboBox()
        self.type_combo.addItems(["英-汉", "汉-英", "日-汉", "德-汉", "法-汉", "自定义"])
        self.tag_edit = QtWidgets.QLineEdit(",".join(self.book.tags) if self.book else "默认")
        if self.book:
            idx = self.type_combo.findText(self.book.book_type)
            if idx >= 0:
                self.type_combo.setCurrentIndex(idx)
        form.addRow("名称", self.name_edit)
        form.addRow("类型", self.type_combo)
        form.addRow("标签（逗号分隔）", self.tag_edit)
        layout.addLayout(form)

        tip = QtWidgets.QLabel("按照“外文:释义”输入，每行一条；支持英文或中文冒号。")
        layout.addWidget(tip)
        self.text_edit = QtWidgets.QPlainTextEdit()
        self.text_edit.setPlaceholderText("hello:你好\nworld:世界")
        if self.book:
            lines = [f"{e.word}:{e.meaning}" for e in self.book.entries]
            self.text_edit.setPlainText("\n".join(lines))
        layout.addWidget(self.text_edit, 1)

        self.error_label = QtWidgets.QLabel("")
        self.error_label.setStyleSheet("color: #e15554;")
        layout.addWidget(self.error_label)

        btn_row = QtWidgets.QHBoxLayout()
        btn_row.addStretch()
        save_btn = accent_button("保存", "#7ad7f0")
        save_btn.clicked.connect(self.on_save)
        cancel = accent_button("取消", "#c7cdda")
        cancel.clicked.connect(self.reject)
        btn_row.addWidget(cancel)
        btn_row.addWidget(save_btn)
        layout.addLayout(btn_row)

    def on_save(self) -> None:
        name = self.datastore.unique_name(self.name_edit.text().strip() or "未命名", exclude_id=self.book.id if self.book else None)
        book_type = self.type_combo.currentText()
        tags = [t.strip() for t in self.tag_edit.text().split(",") if t.strip()] or ["默认"]
        content = self.text_edit.toPlainText()
        entries, errors = self.parse_entries(content, self.book.entries if self.book else None)
        if errors:
            lines = "\n".join([f"第 {ln} 行: {msg}" for ln, msg in errors[:6]])
            self.error_label.setText("格式错误，请修正：\n" + lines)
            return
        self.error_label.setText("")
        if self.book:
            self.book.name = name
            self.book.book_type = book_type
            self.book.tags = tags
            self.book.entries = entries
            self.datastore.clear_book_study_states(self.book.id)
        else:
            new_book = WordBook(id=str(uuid.uuid4()), name=name, book_type=book_type, tags=tags, entries=entries)
            self.datastore.books.append(new_book)
        # refresh global errors to include new words
        for e in entries:
            self.datastore.ensure_global_word(e.word, e.meaning, e.errors)
        self.datastore.save(label="编辑单词本" if self.book else "添加单词本")
        self.accept()

    def parse_entries(
        self, content: str, existing: Optional[List[WordEntry]]
    ) -> Tuple[List[WordEntry], List[Tuple[int, str]]]:
        lines = content.splitlines()
        entries: List[WordEntry] = []
        errors: List[Tuple[int, str]] = []
        existing_map = {e.word.lower(): e for e in existing} if existing else {}
        for idx, raw in enumerate(lines, start=1):
            line = raw.strip()
            if not line:
                continue
            sep = ":" if ":" in line else "：" if "：" in line else None
            if not sep:
                errors.append((idx, "缺少冒号分隔"))
                continue
            left, right = line.split(sep, 1)
            word = left.strip()
            meaning = right.strip()
            if not word or not meaning:
                errors.append((idx, "外文或释义为空"))
                continue
            key = word.lower()
            base_err = self.datastore.global_errors.get(key, 0)
            if key in existing_map:
                base_err = max(base_err, existing_map[key].errors)
            entries.append(WordEntry(word, meaning, base_err))
        return entries, errors


class BookPreviewDialog(QtWidgets.QDialog):
    def __init__(self, datastore: DataStore, book: WordBook, parent=None):
        super().__init__(parent)
        self.datastore = datastore
        self.book = book
        self.setWindowTitle(f"单词本 - {book.name}")
        self.resize(780, 560)
        self.init_ui()
        style_popup(self)

    def init_ui(self) -> None:
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(QtWidgets.QLabel(f"{self.book.name} | {self.book.book_type} | 标签: {','.join(self.book.tags)}"))
        self.table = QtWidgets.QTableWidget()
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(["外文", "释义", "错误次数"])
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QtWidgets.QHeaderView.Stretch)
        header.setSectionsMovable(False)
        header.setSectionsClickable(False)
        vheader = self.table.verticalHeader()
        vheader.setSectionResizeMode(QtWidgets.QHeaderView.Fixed)
        vheader.setDefaultSectionSize(30)
        vheader.setVisible(False)
        layout.addWidget(self.table, 1)

        btns = QtWidgets.QHBoxLayout()
        add_btn = accent_button("添加单词", "#7cf29c")
        del_btn = accent_button("删除选中", "#ff6b6b")
        close_btn = accent_button("关闭", "#c7cdda")
        add_btn.clicked.connect(self.add_word)
        del_btn.clicked.connect(self.delete_word)
        close_btn.clicked.connect(self.accept)
        btns.addWidget(add_btn)
        btns.addWidget(del_btn)
        btns.addStretch()
        btns.addWidget(close_btn)
        layout.addLayout(btns)
        self.refresh()

    def refresh(self) -> None:
        entries = sorted(self.book.entries, key=lambda e: e.errors, reverse=True)
        self.table.setRowCount(len(entries))
        for i, e in enumerate(entries):
            self.table.setItem(i, 0, QtWidgets.QTableWidgetItem(e.word))
            self.table.setItem(i, 1, QtWidgets.QTableWidgetItem(e.meaning))
            self.table.setItem(i, 2, QtWidgets.QTableWidgetItem(str(e.errors)))
            self.table.setRowHeight(i, 30)

    def add_word(self) -> None:
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("添加单词")
        container = QtWidgets.QVBoxLayout(dlg)
        form = QtWidgets.QFormLayout()
        container.addLayout(form)
        word_edit = QtWidgets.QLineEdit()
        meaning_edit = QtWidgets.QLineEdit()
        form.addRow("单词", word_edit)
        form.addRow("释义", meaning_edit)
        btns = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        form.addRow(btns)

        def do_add():
            w = word_edit.text().strip()
            m = meaning_edit.text().strip()
            if not w or not m:
                show_toast(self, "单词与释义不能为空")
                return
            key = w.lower()
            err_val = self.datastore.global_errors.get(key, 0)
            self.book.entries.append(WordEntry(w, m, err_val))
            self.datastore.ensure_global_word(w, m, err_val)
            self.datastore.clear_book_study_states(self.book.id)
            self.datastore.save(label="添加单词")
            self.refresh()
            dlg.accept()

        btns.accepted.connect(do_add)
        btns.rejected.connect(dlg.reject)
        style_popup(dlg)
        dlg.exec_()

    def delete_word(self) -> None:
        rows = {idx.row() for idx in self.table.selectionModel().selectedRows()}
        if not rows:
            show_toast(self, "请选择要删除的单词")
            return
        words = [self.table.item(r, 0).text() for r in rows]
        self.book.entries = [e for e in self.book.entries if e.word not in words]
        self.datastore.clear_book_study_states(self.book.id)
        self.datastore.save(label="删除单词")
        self.refresh()


class GlobalLibraryDialog(QtWidgets.QDialog):
    def __init__(self, datastore: DataStore, parent=None):
        super().__init__(parent)
        self.setWindowTitle("全局词库")
        self.datastore = datastore
        self.resize(760, 620)
        self.filtered: List[str] = []
        self.word_lookup = self._build_word_lookup()
        self.init_ui()
        style_popup(self)
        self.refresh()

    def _build_word_lookup(self) -> Dict[str, WordEntry]:
        lookup = {}
        for b in self.datastore.books:
            for e in b.entries:
                key = e.word.lower()
                if key not in lookup:
                    lookup[key] = e
        for key, meaning in self.datastore.global_words.items():
            if key not in lookup:
                lookup[key] = WordEntry(key, meaning, self.datastore.global_errors.get(key, 0))
        return lookup

    def init_ui(self) -> None:
        layout = QtWidgets.QVBoxLayout(self)
        search_row = QtWidgets.QHBoxLayout()
        self.search_edit = QtWidgets.QLineEdit()
        self.search_edit.setPlaceholderText("输入单词搜索")
        self.search_edit.textChanged.connect(self.refresh)
        search_row.addWidget(self.search_edit)
        self.tag_filter = QtWidgets.QComboBox()
        self.type_filter = QtWidgets.QComboBox()
        self.tag_filter.currentIndexChanged.connect(self.refresh)
        self.type_filter.currentIndexChanged.connect(self.refresh)
        search_row.addWidget(QtWidgets.QLabel("标签"))
        search_row.addWidget(self.tag_filter)
        search_row.addWidget(QtWidgets.QLabel("类型"))
        search_row.addWidget(self.type_filter)
        self.chk_all = QtWidgets.QCheckBox("")
        self.chk_all.stateChanged.connect(self.toggle_all)
        search_row.addWidget(self.chk_all)
        layout.addLayout(search_row)

        self.table = QtWidgets.QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(["", "单词", "释义", "错误次数", "所在单词本"])
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.cellDoubleClicked.connect(self.edit_cell)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QtWidgets.QHeaderView.Stretch)
        header.setSectionsMovable(False)
        header.setSectionsClickable(False)
        vheader = self.table.verticalHeader()
        vheader.setSectionResizeMode(QtWidgets.QHeaderView.Fixed)
        vheader.setDefaultSectionSize(32)
        vheader.setVisible(False)
        layout.addWidget(self.table, 1)

        btn_row = QtWidgets.QHBoxLayout()
        add_btn = accent_button("添加到单词本/新建", "#7cf29c")
        add_btn.clicked.connect(self.add_to_book)
        close_btn = accent_button("关闭", "#c7cdda")
        close_btn.clicked.connect(self.reject)
        btn_row.addStretch()
        btn_row.addWidget(add_btn)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

    def toggle_all(self, state: int) -> None:
        for r in range(self.table.rowCount()):
            item = self.table.item(r, 0)
            if item:
                item.setCheckState(QtCore.Qt.Checked if state == QtCore.Qt.Checked else QtCore.Qt.Unchecked)

    def refresh(self) -> None:
        self.word_lookup = self._build_word_lookup()
        # rebuild filters
        tags = {"全部标签"}
        types = {"全部类型"}
        for b in self.datastore.books:
            types.add(b.book_type)
            for t in b.tags:
                tags.add(t)
        current_tag = self.tag_filter.currentText() or "全部标签"
        current_type = self.type_filter.currentText() or "全部类型"
        self.tag_filter.blockSignals(True)
        self.type_filter.blockSignals(True)
        self.tag_filter.clear()
        self.type_filter.clear()
        for t in sorted(tags):
            self.tag_filter.addItem(t)
        for t in sorted(types):
            self.type_filter.addItem(t)
        if current_tag in tags:
            self.tag_filter.setCurrentText(current_tag)
        if current_type in types:
            self.type_filter.setCurrentText(current_type)
        self.tag_filter.blockSignals(False)
        self.type_filter.blockSignals(False)

        tag_sel = self.tag_filter.currentText() if hasattr(self, "tag_filter") else "全部标签"
        type_sel = self.type_filter.currentText() if hasattr(self, "type_filter") else "全部类型"
        keyword = self.search_edit.text().strip().lower()
        words = list(self.datastore.global_errors.keys())
        if keyword:
            words = [w for w in words if keyword in w]
        filtered_words = []
        for word in words:
            if tag_sel != "全部标签" or type_sel != "全部类型":
                keep = False
                for b in self.datastore.books:
                    if any(e.word.lower() == word for e in b.entries):
                        if (tag_sel == "全部标签" or tag_sel in b.tags) and (
                            type_sel == "全部类型" or b.book_type == type_sel
                        ):
                            keep = True
                            break
                if not keep:
                    continue
            filtered_words.append(word)
        self.filtered = filtered_words
        self.table.setRowCount(len(filtered_words))
        for row, word in enumerate(filtered_words):
            books = [b.name for b in self.datastore.books if any(e.word.lower() == word for e in b.entries)]
            err = self.datastore.global_errors.get(word, 0)
            meaning = self.word_lookup.get(word, WordEntry(word, "")).meaning
            chk = QtWidgets.QTableWidgetItem()
            chk.setFlags(QtCore.Qt.ItemIsEnabled | QtCore.Qt.ItemIsUserCheckable)
            chk.setCheckState(QtCore.Qt.Unchecked)
            self.table.setItem(row, 0, chk)
            self.table.setItem(row, 1, QtWidgets.QTableWidgetItem(word))
            self.table.setItem(row, 2, QtWidgets.QTableWidgetItem(meaning))
            err_item = QtWidgets.QTableWidgetItem(str(err))
            err_item.setData(QtCore.Qt.UserRole, word)
            self.table.setItem(row, 3, err_item)
            self.table.setItem(row, 4, QtWidgets.QTableWidgetItem(", ".join(books)))
            self.table.setRowHeight(row, 32)

    def edit_cell(self, row: int, column: int) -> None:
        # adjust for checkbox column
        if row < 0 or column == 0:
            return
        word_item = self.table.item(row, 1)
        meaning_item = self.table.item(row, 2)
        if not word_item:
            return
        word_text = word_item.text()
        word = word_text.lower()
        if column == 4:
            books = [b for b in self.datastore.books if any(e.word.lower() == word for e in b.entries)]
            dlg = QtWidgets.QDialog(self)
            dlg.setWindowTitle("所在单词本")
            lay = QtWidgets.QVBoxLayout(dlg)
            list_widget = QtWidgets.QListWidget()
            for b in books:
                item = QtWidgets.QListWidgetItem(f"{b.name} ({b.book_type})")
                item.setData(QtCore.Qt.UserRole, b.id)
                list_widget.addItem(item)
            lay.addWidget(list_widget)
            style_popup(dlg)

            def open_selected(item: QtWidgets.QListWidgetItem) -> None:
                bid = item.data(QtCore.Qt.UserRole)
                book = next((bk for bk in self.datastore.books if bk.id == bid), None)
                if book:
                    dlg.accept()
                    prev = BookPreviewDialog(self.datastore, book, self)
                    prev.exec_()

            list_widget.itemDoubleClicked.connect(open_selected)
            dlg.exec_()
            return
        if column == 3:
            old = self.datastore.global_errors.get(word, 0)
            spin = QtWidgets.QSpinBox(self.table)
            spin.setRange(0, 999)
            spin.setValue(old)
            spin.setFrame(False)
            self.table.setCellWidget(row, 3, spin)
            spin.setFocus()

            def finish_err():
                self.datastore.global_errors[word] = spin.value()
                for b in self.datastore.books:
                    for e in b.entries:
                        if e.word.lower() == word:
                            e.errors = self.datastore.global_errors[word]
                self.datastore.save(label="修改错误次数")
                self.table.removeCellWidget(row, 3)
                self.refresh()

            spin.editingFinished.connect(finish_err)
            return
        elif column in (1, 2):
            old_word = word_text
            old_meaning = meaning_item.text() if meaning_item else ""
            edit = QtWidgets.QDialog(self)
            edit.setWindowTitle("编辑单词/释义")
            container = QtWidgets.QVBoxLayout(edit)
            form = QtWidgets.QFormLayout()
            container.addLayout(form)
            word_edit = QtWidgets.QLineEdit(old_word)
            meaning_edit = QtWidgets.QLineEdit(old_meaning)
            form.addRow("单词", word_edit)
            form.addRow("释义", meaning_edit)
            btns = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
            form.addRow(btns)
            style_popup(edit)

            def apply_changes():
                new_word = word_edit.text().strip()
                new_meaning = meaning_edit.text().strip()
                if not new_word or not new_meaning:
                    show_toast(self, "单词与释义不能为空")
                    return
                self.datastore.rename_or_update_word(old_word, new_word, new_meaning)
                self.datastore.save(label="编辑单词/释义")
                edit.accept()
                self.word_lookup = self._build_word_lookup()
                self.refresh()

            btns.accepted.connect(apply_changes)
            btns.rejected.connect(edit.reject)
            edit.exec_()

    def add_to_book(self) -> None:
        selected_words = []
        for r in range(self.table.rowCount()):
            item = self.table.item(r, 0)
            if item and item.checkState() == QtCore.Qt.Checked:
                selected_words.append(self.table.item(r, 1).text())
        if not selected_words:
            show_toast(self, "请选择要添加的单词（勾选复选框）。")
            return
        lookup = self._build_word_lookup()
        for r in range(self.table.rowCount()):
            w = self.table.item(r, 1).text()
            m = self.table.item(r, 2).text()
            k = w.lower()
            if k not in lookup:
                lookup[k] = WordEntry(w, m, self.datastore.global_errors.get(k, 0))
        dialog = AddToBookDialog(self.datastore, selected_words, lookup, self)
        dialog.exec_()
        self.refresh()


class AddToBookDialog(QtWidgets.QDialog):
    def __init__(self, datastore: DataStore, words: List[str], word_lookup: Dict[str, WordEntry], parent=None):
        super().__init__(parent)
        self.datastore = datastore
        self.words = words
        self.word_lookup = word_lookup
        self.setWindowTitle("添加到单词本 / 新建")
        self.resize(420, 280)
        self.init_ui()
        style_popup(self)

    def init_ui(self) -> None:
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(QtWidgets.QLabel(f"选中 {len(self.words)} 个单词"))
        self.book_combo = QtWidgets.QComboBox()
        self.book_combo.addItem("新建单词本...")
        for b in self.datastore.books:
            self.book_combo.addItem(b.name, b.id)
        layout.addWidget(self.book_combo)

        form = QtWidgets.QFormLayout()
        self.new_name = QtWidgets.QLineEdit()
        self.new_type = QtWidgets.QComboBox()
        self.new_type.addItems(["英-汉", "汉-英", "日-汉", "德-汉", "法-汉", "自定义"])
        form.addRow("新建名称", self.new_name)
        form.addRow("新建类型", self.new_type)
        layout.addLayout(form)

        btn_row = QtWidgets.QHBoxLayout()
        btn_row.addStretch()
        ok = accent_button("确定", "#7cf29c")
        ok.clicked.connect(self.on_confirm)
        cancel = accent_button("取消", "#c7cdda")
        cancel.clicked.connect(self.reject)
        btn_row.addWidget(cancel)
        btn_row.addWidget(ok)
        layout.addLayout(btn_row)

    def on_confirm(self) -> None:
        target_idx = self.book_combo.currentIndex()
        if target_idx == 0:
            base_name = self.new_name.text().strip() or "未命名"
            name = self.datastore.unique_name(base_name)
            btype = self.new_type.currentText()
            new_book = WordBook(id=str(uuid.uuid4()), name=name, book_type=btype, tags=["默认"], entries=[])
            self.datastore.books.append(new_book)
            target_book = new_book
        else:
            book_id = self.book_combo.currentData()
            target_book = next((b for b in self.datastore.books if b.id == book_id), None)
        if not target_book:
            show_toast(self, "未找到目标单词本")
            return
        existing = {e.word.lower(): e for e in target_book.entries}
        missing = []
        for w in self.words:
            key = w.lower()
            if key in existing:
                continue
            entry_src = self.word_lookup.get(key)
            if not entry_src:
                missing.append(w)
                continue
            err_val = self.datastore.global_errors.get(key, entry_src.errors)
            target_book.entries.append(WordEntry(entry_src.word, entry_src.meaning, err_val))
            self.datastore.ensure_global_word(entry_src.word, entry_src.meaning, err_val)
        self.datastore.save(label="添加到单词本")
        if missing:
            show_toast(self, f"以下单词未找到释义，已跳过: {', '.join(missing)}")
        self.accept()


class StudyWindow(QtWidgets.QDialog):
    def __init__(
        self,
        datastore: DataStore,
        book: WordBook,
        resume_state: Optional[dict],
        fallback_state: Optional[dict],
        parent=None,
    ):
        super().__init__(parent)
        self.datastore = datastore
        self.book = book
        self.algorithm = self.datastore.settings.get("algorithm", DEFAULT_ALGORITHM)
        if resume_state and resume_state.get("algorithm", DEFAULT_ALGORITHM) != self.algorithm:
            resume_state = None
        if fallback_state and fallback_state.get("algorithm", DEFAULT_ALGORITHM) == self.algorithm:
            fallback_state = None
        self.saved_state = resume_state
        self.fallback_state = fallback_state
        self.reverse = False
        self.state: dict = {}
        self.last_word: Optional[str] = None
        self.current_word: Optional[str] = None
        self.splash_overlay: Optional[SplashScreen] = None
        self._splash_shown = False
        self.splash_image = SPLASH_IMAGE
        self.setWindowTitle(f"考察 - {book.name}")
        self.setWindowFlags(QtCore.Qt.Window)
        self.setWindowModality(QtCore.Qt.ApplicationModal)
        self.resize(520, 960)
        self.init_ui()
        self.adjust_fonts()

    def _init_state(self, resume_state: Optional[dict], allow_fallback: bool = False) -> dict:
        per_cap = 4 if self.algorithm == "ebbinghaus" else 5
        if resume_state:
            state = json.loads(json.dumps(resume_state))
            state["algorithm"] = self.algorithm
            state["step"] = int(state.get("step", 0))
            words_state = state.get("words", {})
            for _, value in words_state.items():
                value["remaining"] = max(0, min(per_cap, int(value.get("remaining", per_cap))))
                value["correct"] = int(value.get("correct", 0))
                value["wrong"] = int(value.get("wrong", 0))
                if self.algorithm in ("ebbinghaus", "scientific"):
                    value["next_step"] = int(value.get("next_step", 0))
                    value["seen"] = int(value.get("seen", 0))
                    last_result = str(value.get("last_result", ""))
                    if last_result in RESULT_CODES and last_result:
                        value["last_result"] = last_result
                    else:
                        value.pop("last_result", None)
                if self.algorithm == "scientific":
                    value["last_step"] = int(value.get("last_step", -1))
            total_cap = per_cap * len(words_state) if words_state else 0
            state["initial"] = min(int(state.get("initial", total_cap)), total_cap) if total_cap else 0
            if self.algorithm in ("normal", "scientific") and self.fallback_state and total_cap:
                remaining_total = sum(v["remaining"] for v in words_state.values())
                fallback_initial = int(self.fallback_state.get("initial", 0))
                fallback_initial = min(max(fallback_initial, remaining_total), total_cap) if fallback_initial else 0
                if state["initial"] <= remaining_total and fallback_initial > state["initial"]:
                    state["initial"] = fallback_initial
            return state
        if allow_fallback and self.fallback_state:
            if self.algorithm == "ebbinghaus":
                return self._build_ebbinghaus_state(self.fallback_state)
            if self.algorithm == "scientific":
                return self._build_scientific_state(self.fallback_state)
            return self._build_normal_state(self.fallback_state)
        if self.algorithm == "ebbinghaus":
            return self._build_ebbinghaus_state()
        if self.algorithm == "scientific":
            return self._build_scientific_state()
        return self._build_normal_state()

    def _shared_memory_mastery(self, key: str, now_ts: int) -> float:
        memory_state = self.datastore.ensure_memory_state(key)
        if memory_state["seen_count"] == 0:
            return 0.0
        score = 0.22
        total_reviews = max(1, memory_state["reps"] + memory_state["lapses"])
        score += min(0.36, memory_state["reps"] / total_reviews * 0.36)
        score += min(0.14, memory_state["seen_count"] * 0.02)
        stability = max(memory_state["stability"], 30 * 60)
        due_at = memory_state["due_at"]
        if due_at > now_ts:
            score += min(0.18, (due_at - now_ts) / stability * 0.12)
        elif due_at > 0:
            score -= min(0.26, max(0, now_ts - due_at) / stability * 0.26)
        if memory_state["last_result"] in ("wrong", "unknown"):
            score -= 0.18
        return max(0.0, min(0.92, score))

    def _estimate_mastery(self, key: str, source_state: Optional[dict], now_ts: int) -> float:
        memory_mastery = self._shared_memory_mastery(key, now_ts)
        if not source_state:
            return memory_mastery
        info = source_state.get("words", {}).get(key)
        if not isinstance(info, dict):
            return memory_mastery
        attempts = int(info.get("correct", 0)) + int(info.get("wrong", 0))
        remaining = max(0, int(info.get("remaining", 0)))
        denom = attempts + remaining
        state_mastery = attempts / denom if denom else 1.0
        if source_state.get("algorithm") == "ebbinghaus" and info.get("last_result") in ("wrong", "unknown"):
            state_mastery = max(0.0, state_mastery - 0.18)
        if source_state.get("algorithm") in ("normal", "scientific") and attempts == 0 and remaining > 0:
            state_mastery = 0.0
        if source_state.get("algorithm") == "scientific" and info.get("last_result") in ("wrong", "unknown"):
            state_mastery = max(0.0, state_mastery - 0.15)
        if self.datastore.ensure_memory_state(key)["seen_count"] == 0:
            return max(0.0, min(0.9, state_mastery))
        return max(0.0, min(0.92, state_mastery * 0.65 + memory_mastery * 0.35))

    def _build_normal_entry(self, key: str, source_state: Optional[dict], now_ts: int) -> dict:
        base_remaining = min(DEFAULT_REMAIN, 5)
        if not source_state:
            return {"remaining": base_remaining, "correct": 0, "wrong": 0, "seen": 0, "last_result": "", "last_step": -1}
        source_info = source_state.get("words", {}).get(key, {}) if source_state else {}
        if source_state and source_state.get("algorithm") in ("normal", "scientific") and source_info:
            last_result = str(source_info.get("last_result", ""))
            if last_result not in RESULT_CODES:
                last_result = ""
            return {
                "remaining": max(0, min(5, int(source_info.get("remaining", base_remaining)))),
                "correct": int(source_info.get("correct", 0)),
                "wrong": int(source_info.get("wrong", 0)),
                "seen": int(source_info.get("seen", int(source_info.get("correct", 0)) + int(source_info.get("wrong", 0)))),
                "last_result": last_result,
                "last_step": int(source_info.get("last_step", -1)),
            }
        memory_state = self.datastore.ensure_memory_state(key)
        if not source_info and memory_state["seen_count"] == 0:
            return {"remaining": base_remaining, "correct": 0, "wrong": 0, "seen": 0, "last_result": "", "last_step": -1}

        mastery = self._estimate_mastery(key, source_state, now_ts)
        attempts = max(0, min(3, int(round(min(mastery, 0.75) * 4))))
        remaining = max(1, min(5, 4 - attempts))
        recent_failure = str(source_info.get("last_result", memory_state["last_result"])) in ("wrong", "unknown")
        if recent_failure:
            remaining = min(5, remaining + 1)
        wrong = 1 if recent_failure and attempts > 0 else 0
        correct = max(0, attempts - wrong)
        return {
            "remaining": remaining,
            "correct": correct,
            "wrong": wrong,
            "seen": correct + wrong,
            "last_result": "wrong" if recent_failure else "",
            "last_step": -1,
        }

    def _build_normal_state(self, source_state: Optional[dict] = None) -> dict:
        now_ts = current_timestamp()
        words = {
            e.word.lower(): self._build_normal_entry(e.word.lower(), source_state, now_ts) for e in self.book.entries
        }
        initial = self._mapped_full_session_initial(words, source_state)
        return {
            "algorithm": "normal",
            "words": words,
            "initial": initial,
            "step": 0,
        }

    def _mapped_full_session_initial(self, words: Dict[str, dict], source_state: Optional[dict]) -> int:
        total_cap = 5 * len(words)
        remaining_total = sum(value["remaining"] for value in words.values())
        if not source_state or source_state.get("algorithm") not in ("normal", "scientific"):
            return remaining_total
        source_initial = int(source_state.get("initial", remaining_total))
        return min(max(source_initial, remaining_total), total_cap) if total_cap else 0

    def _build_scientific_entry(self, key: str, source_state: Optional[dict], now_ts: int) -> dict:
        source_info = source_state.get("words", {}).get(key, {}) if source_state else {}
        if source_state and source_state.get("algorithm") in ("normal", "scientific") and source_info:
            last_result = str(source_info.get("last_result", self.datastore.ensure_memory_state(key)["last_result"]))
            if last_result not in RESULT_CODES:
                last_result = ""
            return {
                "remaining": max(0, min(5, int(source_info.get("remaining", DEFAULT_REMAIN)))),
                "correct": int(source_info.get("correct", 0)),
                "wrong": int(source_info.get("wrong", 0)),
                "seen": int(source_info.get("seen", int(source_info.get("correct", 0)) + int(source_info.get("wrong", 0)))),
                "last_result": last_result,
                "last_step": int(source_info.get("last_step", -1)),
            }

        memory_state = self.datastore.ensure_memory_state(key)
        if not source_state:
            return {
                "remaining": min(DEFAULT_REMAIN, 5),
                "correct": 0,
                "wrong": 0,
                "seen": 0,
                "last_result": "",
                "last_step": -1,
            }

        mastery = self._estimate_mastery(key, source_state, now_ts)
        attempts = max(0, min(3, int(round(min(mastery, 0.75) * 4))))
        remaining = max(1, min(5, 4 - attempts))
        last_result = str(source_info.get("last_result", memory_state["last_result"]))
        if last_result not in RESULT_CODES:
            last_result = ""
        if last_result in ("wrong", "unknown"):
            remaining = min(5, remaining + 1)
        wrong = 1 if last_result in ("wrong", "unknown") and attempts > 0 else 0
        correct = max(0, attempts - wrong)
        seen = int(source_info.get("seen", correct + wrong))
        if seen == 0 and memory_state["seen_count"] > 0:
            seen = 1
        return {
            "remaining": remaining,
            "correct": correct,
            "wrong": wrong,
            "seen": seen,
            "last_result": last_result,
            "last_step": -1,
        }

    def _build_scientific_state(self, source_state: Optional[dict] = None) -> dict:
        now_ts = current_timestamp()
        words = {
            entry.word.lower(): self._build_scientific_entry(entry.word.lower(), source_state, now_ts)
            for entry in self.book.entries
        }
        initial = self._mapped_full_session_initial(words, source_state)
        return {
            "algorithm": "scientific",
            "words": words,
            "initial": initial,
            "step": 0,
        }

    def _ebbinghaus_due_priority(self, key: str, now_ts: int) -> float:
        memory_state = self.datastore.ensure_memory_state(key)
        stability = max(memory_state["stability"], 30 * 60)
        overdue = max(0, now_ts - memory_state["due_at"])
        return (overdue / stability) + min(memory_state["lapses"] * 0.12, 0.6) + max(0, memory_state["difficulty"] - 450) / 400.0

    def _ebbinghaus_initial_remaining(self, key: str, now_ts: int) -> int:
        memory_state = self.datastore.ensure_memory_state(key)
        if memory_state["seen_count"] == 0:
            return 2
        if memory_state["due_at"] <= now_ts:
            stability = max(memory_state["stability"], 30 * 60)
            overdue = max(0, now_ts - memory_state["due_at"])
            return 2 if overdue >= stability // 2 else 1
        return 1

    def _build_ebbinghaus_entry(self, key: str, source_state: Optional[dict], now_ts: int) -> dict:
        source_info = source_state.get("words", {}).get(key, {}) if source_state else {}
        memory_state = self.datastore.ensure_memory_state(key)
        if not source_info and memory_state["seen_count"] == 0:
            return {
                "remaining": 2,
                "correct": 0,
                "wrong": 0,
                "next_step": 0,
                "seen": 0,
                "last_result": "",
            }

        mastery = self._estimate_mastery(key, source_state, now_ts)
        remaining = 2 if mastery < 0.45 else 1
        last_result = str(source_info.get("last_result", memory_state["last_result"]))
        if last_result not in RESULT_CODES:
            last_result = ""
        if last_result in ("wrong", "unknown"):
            remaining = min(4, remaining + 1)
        elif memory_state["due_at"] > 0 and memory_state["due_at"] <= now_ts:
            remaining = min(4, remaining + 1)
        correct = int(source_info.get("correct", 0))
        wrong = int(source_info.get("wrong", 0))
        seen = int(source_info.get("seen", correct + wrong))
        if seen == 0 and memory_state["seen_count"] > 0:
            seen = 1
        return {
            "remaining": remaining,
            "correct": correct,
            "wrong": wrong,
            "next_step": 0,
            "seen": seen,
            "last_result": last_result,
        }

    def _build_ebbinghaus_state(self, source_state: Optional[dict] = None) -> dict:
        now_ts = current_timestamp()
        book_keys = {entry.word.lower() for entry in self.book.entries}
        due_words: List[str] = []
        near_due_words: List[str] = []
        future_words: List[str] = []
        unseen_words: List[str] = []
        carryover_words: List[str] = []

        for entry in self.book.entries:
            key = entry.word.lower()
            memory_state = self.datastore.ensure_memory_state(key)
            if memory_state["seen_count"] == 0:
                unseen_words.append(key)
                continue
            if memory_state["due_at"] <= now_ts:
                due_words.append(key)
                continue
            near_window = max(2 * 60 * 60, min(memory_state["stability"] // 5, 24 * 60 * 60))
            if memory_state["due_at"] - now_ts <= near_window:
                near_due_words.append(key)
            else:
                future_words.append(key)

        if source_state:
            carryover_words = [
                key
                for key, info in source_state.get("words", {}).items()
                if key in book_keys and int(info.get("remaining", 0)) > 0
            ]
            carryover_words = sorted(
                carryover_words,
                key=lambda key: (
                    self._estimate_mastery(key, source_state, now_ts),
                    -int(source_state.get("words", {}).get(key, {}).get("remaining", 0)),
                    key,
                ),
            )
        due_words = sorted(due_words, key=lambda key: self._ebbinghaus_due_priority(key, now_ts), reverse=True)
        near_due_words = sorted(near_due_words, key=lambda key: self.datastore.ensure_memory_state(key)["due_at"])
        if unseen_words:
            random.shuffle(unseen_words)

        session_cap = min(max(12, len(due_words) + 6), 40)
        selected: List[str] = carryover_words[:session_cap]
        for key in due_words:
            if len(selected) >= session_cap:
                break
            if key not in selected:
                selected.append(key)
        remaining_slots = session_cap - len(selected)

        if remaining_slots > 0:
            new_target = 0
            if len(selected) < 12:
                new_target = min(12 - len(selected), len(unseen_words), remaining_slots)
            elif len(selected) < 24:
                new_target = min(4, len(unseen_words), remaining_slots)
            if new_target > 0:
                selected.extend(unseen_words[:new_target])
                remaining_slots = session_cap - len(selected)

        if remaining_slots > 0 and len(selected) < 24:
            extra_due = [key for key in near_due_words if key not in selected]
            take = min(24 - len(selected), len(extra_due), remaining_slots)
            selected.extend(extra_due[:take])

        if not selected:
            fallback = near_due_words or future_words or unseen_words
            selected.extend(fallback[: min(5, len(fallback))])

        words_state: Dict[str, dict] = {}
        for key in selected:
            if source_state:
                words_state[key] = self._build_ebbinghaus_entry(key, source_state, now_ts)
            else:
                words_state[key] = {
                    "remaining": self._ebbinghaus_initial_remaining(key, now_ts),
                    "correct": 0,
                    "wrong": 0,
                    "next_step": 0,
                    "seen": 0,
                    "last_result": "",
                }

        initial = sum(value["remaining"] for value in words_state.values())
        return {
            "algorithm": "ebbinghaus",
            "words": words_state,
            "initial": initial,
            "step": 0,
        }

    def init_ui(self) -> None:
        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(12)
        top_bar = QtWidgets.QHBoxLayout()
        top_bar.addStretch()
        self.full_btn = QtWidgets.QPushButton("□")
        self.full_btn.setFixedSize(28, 24)
        self.full_btn.setStyleSheet(
            "QPushButton {border: 1px solid #cbd3e1; border-radius: 4px; background: #eef1f7;}"
            "QPushButton:hover {background: #dfe5f2;}"
            "QPushButton:pressed {background: #d0d8e9;}"
        )
        self.full_btn.clicked.connect(self.toggle_fullscreen)
        top_bar.addWidget(self.full_btn)
        layout.addLayout(top_bar)

        self.controls_container = QtWidgets.QFrame()
        ctrl_layout = QtWidgets.QVBoxLayout(self.controls_container)
        ctrl_layout.setAlignment(QtCore.Qt.AlignCenter)
        ctrl_layout.setSpacing(10)
        has_resume_source = self.saved_state is not None or self.fallback_state is not None
        self.chk_resume = QtWidgets.QCheckBox("读取上次进度")
        self.chk_resume.setEnabled(has_resume_source)
        self.chk_resume.setChecked(has_resume_source)
        self.chk_reverse = QtWidgets.QCheckBox("反向考察（释义→外文）")
        self.chk_reverse.setChecked(False)
        self.chk_reverse.toggled.connect(lambda v: setattr(self, "reverse", v))
        self.btn_start = accent_button("开始考察", "#7ad7f0")
        self.btn_start.clicked.connect(self.begin_session)
        ctrl_layout.addWidget(self.chk_resume, alignment=QtCore.Qt.AlignCenter)
        ctrl_layout.addWidget(self.chk_reverse, alignment=QtCore.Qt.AlignCenter)
        ctrl_layout.addWidget(self.btn_start, alignment=QtCore.Qt.AlignCenter)
        layout.addWidget(self.controls_container, alignment=QtCore.Qt.AlignCenter)

        self.word_label = QtWidgets.QLabel("开始考察")
        self.word_label.setAlignment(QtCore.Qt.AlignCenter)
        self.word_label.setStyleSheet("font-size: 32px; font-weight: 800; padding: 20px;")
        layout.addWidget(self.word_label)

        self.meaning_label = QtWidgets.QLabel("")
        self.meaning_label.setAlignment(QtCore.Qt.AlignCenter)
        self.meaning_label.setWordWrap(True)
        self.meaning_label.setStyleSheet("font-size: 22px; padding: 14px;")
        layout.addWidget(self.meaning_label, 1)
        layout.addStretch(1)

        button_frame = QtWidgets.QFrame()
        self.button_area = QtWidgets.QHBoxLayout(button_frame)
        self.button_area.setAlignment(QtCore.Qt.AlignCenter)
        self.button_area.setSpacing(16)
        self.btn_known = accent_button("会", "#7cf29c")
        self.btn_unknown = accent_button("不会", "#ff6b6b")
        self.btn_continue = accent_button("继续", "#7ad7f0")
        self.btn_correct = accent_button("答对了", "#7cf29c")
        self.btn_wrong = accent_button("答错了", "#ff6b6b")
        self.btn_slash = accent_button("斩", "#ffb347")
        for b in (
            self.btn_known,
            self.btn_unknown,
            self.btn_continue,
            self.btn_correct,
            self.btn_wrong,
            self.btn_slash,
        ):
            b.setMinimumWidth(120)
        layout.addWidget(button_frame)

        progress_frame = QtWidgets.QFrame()
        bottom = QtWidgets.QVBoxLayout(progress_frame)
        bottom.setSpacing(8)
        self.word_bar = QtWidgets.QProgressBar()
        self.global_bar = QtWidgets.QProgressBar()
        for bar in (self.word_bar, self.global_bar):
            bar.setTextVisible(True)
            bar.setRange(0, 100)
            bar.setStyleSheet(
                "QProgressBar {border-radius: 14px; height: 20px;}"
                "QProgressBar::chunk {border-radius: 14px; background-color: #7ad7f0;}"
            )
        bottom.addWidget(self.word_bar)
        bottom.addWidget(self.global_bar)
        layout.addWidget(progress_frame)
        layout.setStretchFactor(self.word_label, 2)
        layout.setStretchFactor(self.meaning_label, 2)
        layout.setStretchFactor(button_frame, 1)
        layout.setStretchFactor(progress_frame, 0)

        self.btn_known.clicked.connect(self.on_known)
        self.btn_unknown.clicked.connect(self.on_unknown)
        self.btn_continue.clicked.connect(self.choose_next)
        self.btn_correct.clicked.connect(lambda: self.on_result("correct"))
        self.btn_wrong.clicked.connect(lambda: self.on_result("wrong"))
        self.btn_slash.clicked.connect(lambda: self.on_result("slash"))
        self.show_buttons("waiting")

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:  # type: ignore
        super().resizeEvent(event)
        self.adjust_fonts()
        if self.splash_overlay and not self.splash_overlay.isHidden():
            self.splash_overlay.setGeometry(self.rect())
            self.splash_overlay.update_pixmap()

    def showEvent(self, event: QtGui.QShowEvent) -> None:  # type: ignore
        super().showEvent(event)
        if not self._splash_shown:
            self._splash_shown = True
            self.start_splash()

    def adjust_fonts(self) -> None:
        h = self.height()
        base = max(28, int(h * 0.04))
        meaning = max(20, int(h * 0.032))
        self.word_label.setStyleSheet(f"font-size: {base}px; font-weight: 800; padding: 22px;")
        self.meaning_label.setStyleSheet(f"font-size: {meaning}px; font-weight: 700; padding: 16px;")
        btn_height = max(52, int(h * 0.08))
        for b in (self.btn_known, self.btn_unknown, self.btn_continue, self.btn_correct, self.btn_wrong, self.btn_slash):
            b.setMinimumHeight(btn_height)
        for bar in (self.word_bar, self.global_bar):
            bar.setStyleSheet(
                f"QProgressBar {{border-radius: 16px; height: {int(h*0.024)}px;}}"
                "QProgressBar::chunk {border-radius: 16px; background-color: #7ad7f0;}"
            )

    def toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()
        self.adjust_fonts()

    def fade_in(self, widget: QtWidgets.QWidget, duration: int = 200) -> None:
        effect = QtWidgets.QGraphicsOpacityEffect(widget)
        widget.setGraphicsEffect(effect)
        anim = QtCore.QPropertyAnimation(effect, b"opacity", self)
        anim.setDuration(duration)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QtCore.QEasingCurve.InOutCubic)
        anim.finished.connect(lambda: widget.setGraphicsEffect(None))
        anim.start()
        widget._fade_anim = anim  # type: ignore
        self.adjust_fonts()

    def start_splash(self) -> None:
        if not os.path.exists(self.splash_image):
            return
        if self.splash_overlay:
            self.splash_overlay.deleteLater()
        self.splash_overlay = SplashScreen(self.splash_image, parent=self)
        self.splash_overlay.setGeometry(self.rect())
        self.splash_overlay.raise_()

        def cleanup():
            if self.splash_overlay:
                self.splash_overlay.deleteLater()
                self.splash_overlay = None

        self.splash_overlay.start_sequence(
            fade_in_ms=0, hold_ms=400, fade_out_ms=400, on_finished=cleanup, cover_immediately=True
        )

    def begin_session(self) -> None:
        should_resume = self.chk_resume.isEnabled() and self.chk_resume.isChecked()
        if should_resume and self.saved_state:
            self.state = self._init_state(self.saved_state, allow_fallback=True)
        else:
            self.state = self._init_state(None, allow_fallback=should_resume)
        self.reverse = self.chk_reverse.isChecked()
        self.last_word = None
        self.current_word = None
        self._persist_state()
        self.controls_container.hide()
        self.choose_next()

    def show_buttons(self, mode: str) -> None:
        for i in reversed(range(self.button_area.count())):
            item = self.button_area.takeAt(i)
            if item.widget():
                item.widget().setParent(None)
        if mode == "waiting":
            return
        if mode == "question":
            self.button_area.addWidget(self.btn_unknown)
            self.button_area.addWidget(self.btn_known)
        elif mode == "unknown_reveal":
            self.button_area.addWidget(self.btn_continue)
        elif mode == "known_reveal":
            self.button_area.addWidget(self.btn_wrong)
            self.button_area.addWidget(self.btn_correct)
            self.button_area.addWidget(self.btn_slash)

    def choose_next(self) -> None:
        if not self.state:
            return
        if self.algorithm == "ebbinghaus":
            self._choose_next_ebbinghaus()
            return
        if self.algorithm == "scientific":
            self._choose_next_scientific()
            return
        words_state = self.state["words"]
        candidates = [k for k, v in words_state.items() if v["remaining"] > 0]
        if not candidates:
            self.finish(True)
            return
        total_words = len(candidates)
        top_limit = 0
        if total_words >= 60:
            top_limit = 30
        elif total_words >= 30:
            top_limit = 20
        zero_progress_exists = any(self.word_progress_percent(key) == 0 for key in candidates)
        pool_candidates = candidates
        if zero_progress_exists:
            pool_candidates = [key for key in candidates if words_state[key]["remaining"] != 1]
        pool_source = pool_candidates
        if top_limit:
            positive_progress = [key for key in pool_candidates if self.word_progress_percent(key) > 0]
            zero_progress = [key for key in pool_candidates if self.word_progress_percent(key) == 0]
            pool_source = sorted(
                positive_progress,
                key=lambda key: (self.word_progress_percent(key), key),
            )[:top_limit]
            if len(pool_source) < top_limit:
                pool_source.extend(random.sample(zero_progress, min(len(zero_progress), top_limit - len(pool_source))))
            extra_zero_progress = [key for key in zero_progress if key not in pool_source]
            if extra_zero_progress:
                pool_source.extend(random.sample(extra_zero_progress, min(len(extra_zero_progress), 5)))
        filtered = [c for c in pool_source if c != self.last_word]
        pool = filtered or pool_source
        self.current_word = random.choice(pool)
        self.last_word = self.current_word
        entry = next((e for e in self.book.entries if e.word.lower() == self.current_word), None)
        if entry:
            prompt, _ = self.prompt_answer(entry)
            self.word_label.setText(prompt)
            self.meaning_label.setText("")
            self.fade_in(self.word_label)
        self.show_buttons("question")
        self.update_progress()

    def _ebbinghaus_priority(self, key: str) -> float:
        session_state = self.state["words"][key]
        memory_state = self.datastore.ensure_memory_state(key)
        now_ts = current_timestamp()
        session_mastery = self.word_progress_percent(key) / 100.0
        stability = max(memory_state["stability"], 30 * 60)

        if memory_state["seen_count"] == 0:
            due_component = 1.15
        elif memory_state["due_at"] <= now_ts:
            due_component = 1.0 + max(0, now_ts - memory_state["due_at"]) / stability
        else:
            time_to_due = max(1, memory_state["due_at"] - now_ts)
            due_component = max(0.1, 0.45 - min(0.35, time_to_due / stability * 0.2))

        difficulty_component = max(0.0, (memory_state["difficulty"] - 400) / 250.0)
        lapse_component = min(memory_state["lapses"] * 0.12, 0.6)
        wrong_component = 0.95 if session_state.get("last_result") in ("wrong", "unknown") else 0.0
        remaining_component = session_state["remaining"] * 0.28
        novelty_component = 0.35 if session_state.get("seen", 0) == 0 else 0.0
        return (
            due_component
            + difficulty_component
            + lapse_component
            + wrong_component
            + remaining_component
            + (1.0 - session_mastery)
            + novelty_component
            + random.uniform(0.0, 0.05)
        )

    def _pick_ebbinghaus_candidate(self, candidates: List[str]) -> str:
        scored = [(key, self._ebbinghaus_priority(key)) for key in candidates]
        scored.sort(key=lambda item: item[1], reverse=True)
        shortlist = scored[: min(len(scored), 6)]
        keys = [key for key, _ in shortlist]
        weights = [max(score, 0.1) for _, score in shortlist]
        return random.choices(keys, weights=weights, k=1)[0]

    def _scientific_min_gap(self, key: str) -> int:
        st = self.state["words"][key]
        seen = int(st.get("seen", 0))
        if seen == 0 or int(st.get("last_step", -1)) < 0:
            return 0
        last_result = st.get("last_result", "")
        if last_result in ("wrong", "unknown"):
            return 2
        if last_result == "correct":
            return min(10, 3 + int(st["correct"]) + max(0, 3 - int(st["remaining"])))
        return 2

    def _scientific_ideal_gap(self, key: str) -> int:
        st = self.state["words"][key]
        seen = int(st.get("seen", 0))
        if seen == 0:
            return 0
        last_result = st.get("last_result", "")
        if last_result in ("wrong", "unknown"):
            return 2 + min(2, int(st["wrong"]))
        memory_state = self.datastore.ensure_memory_state(key)
        stability_steps = min(5, max(0, memory_state["stability"] // (18 * 60 * 60)))
        mastery_bonus = max(0, int(self.word_progress_percent(key) / 25))
        return min(16, 4 + int(st["correct"]) * 2 + mastery_bonus + stability_steps)

    def _scientific_priority(self, key: str, step: int) -> float:
        st = self.state["words"][key]
        memory_state = self.datastore.ensure_memory_state(key)
        seen = int(st.get("seen", 0))
        last_step = int(st.get("last_step", -1))
        gap = step - last_step if last_step >= 0 else step + 1
        mastery = self.word_progress_percent(key) / 100.0
        shared_mastery = self._shared_memory_mastery(key, current_timestamp())
        ideal_gap = self._scientific_ideal_gap(key)
        min_gap = self._scientific_min_gap(key)

        if seen == 0:
            due_component = 1.15
        elif ideal_gap <= 0:
            due_component = 0.4
        else:
            due_component = min(2.5, 0.25 + gap / max(1, ideal_gap))
            if gap >= ideal_gap:
                due_component += min(1.0, (gap - ideal_gap) / max(1, ideal_gap))
            elif gap < min_gap:
                due_component *= 0.35

        wrong_component = 0.95 if st.get("last_result") in ("wrong", "unknown") else 0.0
        remaining_component = st["remaining"] * 0.34
        mastery_component = 1.0 - mastery
        starvation_component = 0.0 if last_step < 0 else min(0.9, gap / 18.0)
        memory_component = (1.0 - shared_mastery) * 0.55
        lapse_component = min(memory_state["lapses"] * 0.08, 0.4)
        novelty_component = 0.22 if seen == 0 else 0.0
        return (
            due_component
            + wrong_component
            + remaining_component
            + mastery_component
            + starvation_component
            + memory_component
            + lapse_component
            + novelty_component
            + random.uniform(0.0, 0.05)
        )

    def _pick_scientific_candidate(self, candidates: List[str]) -> str:
        step = int(self.state.get("step", 0))
        scored = [(key, self._scientific_priority(key, step)) for key in candidates]
        scored.sort(key=lambda item: item[1], reverse=True)
        shortlist = scored[: min(len(scored), max(6, min(12, len(scored) // 6 + 4)))]
        keys = [key for key, _ in shortlist]
        weights = [max(score, 0.1) for _, score in shortlist]
        return random.choices(keys, weights=weights, k=1)[0]

    def _choose_scientific_pool(self) -> List[str]:
        words_state = self.state["words"]
        step = int(self.state.get("step", 0))
        candidates = [key for key, value in words_state.items() if value["remaining"] > 0]
        if not candidates:
            return []
        ready = []
        for key in candidates:
            last_step = int(words_state[key].get("last_step", -1))
            if last_step < 0 or int(words_state[key].get("seen", 0)) == 0:
                ready.append(key)
                continue
            if step - last_step >= self._scientific_min_gap(key):
                ready.append(key)
        pool = ready or candidates
        filtered = [key for key in pool if key != self.last_word]
        return filtered or pool

    def _choose_next_scientific(self) -> None:
        pool = self._choose_scientific_pool()
        if not pool:
            self.finish(True)
            return
        self.current_word = self._pick_scientific_candidate(pool)
        self.last_word = self.current_word
        entry = next((e for e in self.book.entries if e.word.lower() == self.current_word), None)
        if entry:
            prompt, _ = self.prompt_answer(entry)
            self.word_label.setText(prompt)
            self.meaning_label.setText("")
            self.fade_in(self.word_label)
        self.show_buttons("question")
        self.update_progress()

    def _choose_ebbinghaus_pool(self) -> List[str]:
        words_state = self.state["words"]
        step = int(self.state.get("step", 0))
        candidates = [key for key, value in words_state.items() if value["remaining"] > 0]
        if not candidates:
            return []
        ready = [key for key in candidates if words_state[key].get("next_step", 0) <= step]
        pool = ready or candidates
        filtered = [key for key in pool if key != self.last_word]
        return filtered or pool

    def _choose_next_ebbinghaus(self) -> None:
        pool = self._choose_ebbinghaus_pool()
        if not pool:
            self.finish(True)
            return
        self.current_word = self._pick_ebbinghaus_candidate(pool)
        self.last_word = self.current_word
        entry = next((e for e in self.book.entries if e.word.lower() == self.current_word), None)
        if entry:
            prompt, _ = self.prompt_answer(entry)
            self.word_label.setText(prompt)
            self.meaning_label.setText("")
            self.fade_in(self.word_label)
        self.show_buttons("question")
        self.update_progress()

    def prompt_answer(self, entry: WordEntry) -> Tuple[str, str]:
        if self.reverse:
            return entry.meaning, entry.word
        return entry.word, entry.meaning

    def on_unknown(self) -> None:
        if not self.state:
            return
        if not self.current_word:
            return
        if self.algorithm == "ebbinghaus":
            self._apply_ebbinghaus_result("unknown")
        elif self.algorithm == "scientific":
            self._apply_scientific_result("unknown")
        else:
            self.datastore.review_word(self.current_word, "unknown")
            self.adjust_counts(remaining_delta=1, wrong_delta=1, result="unknown")
        entry = next((e for e in self.book.entries if e.word.lower() == self.current_word), None)
        if entry:
            _, ans = self.prompt_answer(entry)
            self.meaning_label.setText(ans)
            self.fade_in(self.meaning_label)
        self.show_buttons("unknown_reveal")
        self.update_progress()

    def on_known(self) -> None:
        if not self.state:
            return
        if not self.current_word:
            return
        entry = next((e for e in self.book.entries if e.word.lower() == self.current_word), None)
        if entry:
            _, ans = self.prompt_answer(entry)
            self.meaning_label.setText(ans)
            self.fade_in(self.meaning_label)
        self.show_buttons("known_reveal")

    def on_result(self, result: str) -> None:
        if not self.state:
            return
        if not self.current_word:
            return
        if self.algorithm == "ebbinghaus":
            self._apply_ebbinghaus_result(result)
            self.update_progress()
            self.choose_next()
            return
        if self.algorithm == "scientific":
            self._apply_scientific_result(result)
            self.update_progress()
            self.choose_next()
            return
        self.datastore.review_word(self.current_word, result)
        if result == "correct":
            self.adjust_counts(remaining_delta=-1, correct_delta=1, result="correct")
        elif result == "wrong":
            self.adjust_counts(remaining_delta=1, wrong_delta=1, result="wrong")
        elif result == "slash":
            self._record_full_session_metadata("slash")
            st = self.state["words"][self.current_word]
            st["remaining"] = 0
            st["correct"] += 1
            self._persist_state()
        self.update_progress()
        self.choose_next()

    def _record_full_session_metadata(self, result: str) -> None:
        st = self.state["words"][self.current_word]
        step = int(self.state.get("step", 0)) + 1
        self.state["step"] = step
        st["seen"] = int(st.get("seen", 0)) + 1
        st["last_result"] = result
        st["last_step"] = step

    def adjust_counts(self, remaining_delta=0, correct_delta=0, wrong_delta=0, result: str = "") -> None:
        st = self.state["words"][self.current_word]
        if result:
            self._record_full_session_metadata(result)
        st["remaining"] = max(0, min(5, st["remaining"] + remaining_delta))
        st["correct"] += correct_delta
        st["wrong"] += wrong_delta
        if wrong_delta:
            self._record_error_delta(self.current_word, wrong_delta)
        self._persist_state()

    def _record_error_delta(self, key: str, delta: int) -> None:
        self.datastore.global_errors[key] = self.datastore.global_errors.get(key, 0) + delta
        for book in self.datastore.books:
            for entry in book.entries:
                if entry.word.lower() == key:
                    entry.errors += delta

    def _apply_ebbinghaus_result(self, result: str) -> None:
        st = self.state["words"][self.current_word]
        step = int(self.state.get("step", 0)) + 1
        self.state["step"] = step
        st["seen"] = int(st.get("seen", 0)) + 1
        st["last_result"] = result

        if result == "correct":
            st["correct"] += 1
            st["remaining"] = max(0, st["remaining"] - 1)
            if st["remaining"] > 0:
                st["next_step"] = step + random.randint(3, 6)
        elif result == "slash":
            st["correct"] += 1
            st["remaining"] = 0
            st["next_step"] = step
        else:
            st["wrong"] += 1
            st["remaining"] = min(4, st["remaining"] + 1)
            st["next_step"] = step + random.randint(2, 4)
            self._record_error_delta(self.current_word, 1)

        self.datastore.review_word(self.current_word, result)
        self._persist_state()

    def _apply_scientific_result(self, result: str) -> None:
        st = self.state["words"][self.current_word]
        step = int(self.state.get("step", 0)) + 1
        self.state["step"] = step
        st["seen"] = int(st.get("seen", 0)) + 1
        st["last_result"] = result
        st["last_step"] = step

        if result == "correct":
            st["correct"] += 1
            st["remaining"] = max(0, st["remaining"] - 1)
        elif result == "slash":
            st["correct"] += 1
            st["remaining"] = 0
        else:
            st["wrong"] += 1
            st["remaining"] = min(5, st["remaining"] + 1)
            self._record_error_delta(self.current_word, 1)

        self.datastore.review_word(self.current_word, result)
        self._persist_state()

    def animate_bar(self, bar: QtWidgets.QProgressBar, value: int) -> None:
        value = max(0, min(100, value))
        anim = QtCore.QPropertyAnimation(bar, b"value", self)
        anim.setDuration(320)
        anim.setStartValue(bar.value())
        anim.setEndValue(value)
        anim.setEasingCurve(QtCore.QEasingCurve.InOutCubic)
        anim.start()
        bar._anim = anim

    def word_progress_percent(self, key: str) -> int:
        st = self.state["words"][key]
        attempts = st["correct"] + st["wrong"]
        denom = attempts + st["remaining"]
        return int(attempts / denom * 100) if denom else 100

    def update_progress(self) -> None:
        if not self.current_word or not self.state:
            self.animate_bar(self.word_bar, 0)
            self.animate_bar(self.global_bar, 0)
            return
        st = self.state["words"][self.current_word]
        word_percent = self.word_progress_percent(self.current_word)
        self.animate_bar(self.word_bar, word_percent)
        words_state = self.state["words"]
        total_cap = 5 * len(words_state)
        total_init = min(self.state.get("initial", DEFAULT_REMAIN * len(words_state)), total_cap) if words_state else 0
        remaining_total = sum(v["remaining"] for v in self.state["words"].values())
        remaining_cap = min(remaining_total, total_init) if total_init else remaining_total
        done = total_init - remaining_cap
        global_percent = int(done / total_init * 100) if total_init else 0
        self.animate_bar(self.global_bar, global_percent)
        self.word_bar.setFormat(f"掌握程度 {word_percent}% 剩余 {st['remaining']}")
        self.global_bar.setFormat(f"全局进度 {global_percent}% 剩余 {remaining_total}/{total_init}")

    def finish(self, completed: bool) -> None:
        self.apply_session_errors()
        if completed:
            self.datastore.clear_book_study_states(self.book.id, self.algorithm)
        else:
            self.datastore.set_study_state(self.book.id, self.state, self.algorithm)
        self.datastore.save(push_history=False)
        show_toast(self, "考察已结束" if completed else "进度已保存")
        self.accept()

    def apply_session_errors(self) -> None:
        return

    def _persist_state(self) -> None:
        if self.state is None:
            return
        self.datastore.set_study_state(self.book.id, self.state, self.algorithm)
        self.datastore.save(push_history=False)  # 进度保存不计入撤销历史


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, datastore: DataStore):
        super().__init__()
        self.datastore = datastore
        self.selected_global_words: Set[str] = set()
        self.global_book_filter: Set[str] = set()
        self.global_type_filter = "全部类型"
        self.global_tag_filter = "全部标签"
        self.global_sort_key = "word"
        self.global_sort_order = QtCore.Qt.AscendingOrder
        self.global_search_mode = "word"
        self._applying_global_selection = False
        self.splash_overlay: Optional[SplashScreen] = None
        self.setWindowTitle("Word Master - PyQt5")
        icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "word_master.ico")
        if os.path.exists(icon_path):
            self.setWindowIcon(QtGui.QIcon(icon_path))
        self.resize(1100, 780)
        self.init_ui()
        self.apply_theme()
        self.restore_geometry()

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:  # type: ignore
        self.datastore.window_geo["geometry"] = self.saveGeometry().toBase64().data().decode()
        self.datastore.window_geo["state"] = self.saveState().toBase64().data().decode()
        self.datastore.save(push_history=False)
        event.accept()

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:  # type: ignore
        super().resizeEvent(event)
        if self.splash_overlay and not self.splash_overlay.isHidden():
            self.splash_overlay.setGeometry(self.rect())
            self.splash_overlay.update_pixmap()

    def restore_geometry(self) -> None:
        geo = self.datastore.window_geo.get("geometry")
        state = self.datastore.window_geo.get("state")
        if geo:
            try:
                self.restoreGeometry(QtCore.QByteArray.fromBase64(geo.encode()))
            except Exception:
                pass
        if state:
            try:
                self.restoreState(QtCore.QByteArray.fromBase64(state.encode()))
            except Exception:
                pass

    def start_splash(self, image_path: str) -> None:
        if not os.path.exists(image_path):
            return
        if self.splash_overlay:
            self.splash_overlay.deleteLater()
        self.splash_overlay = SplashScreen(image_path, parent=self)
        self.splash_overlay.setGeometry(self.rect())
        self.splash_overlay.raise_()

        def cleanup():
            if self.splash_overlay:
                self.splash_overlay.deleteLater()
                self.splash_overlay = None

        self.splash_overlay.start_sequence(
            fade_in_ms=0, hold_ms=400, fade_out_ms=400, on_finished=cleanup, cover_immediately=True
        )

    def init_ui(self) -> None:
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        main_layout = QtWidgets.QVBoxLayout(central)

        hero = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("Word Master")
        title.setStyleSheet("font-size: 34px; font-weight: 900; letter-spacing: 1px;")
        hero.addWidget(title)
        hero.addStretch()
        self.history_button = QtWidgets.QToolButton()
        self.history_button.setText("误操作  ")
        self.history_button.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.history_menu = QtWidgets.QMenu(self.history_button)
        self.action_undo = self.history_menu.addAction("撤销")
        self.action_redo = self.history_menu.addAction("恢复")
        self.action_undo.triggered.connect(self.undo_action)
        self.action_redo.triggered.connect(self.redo_action)
        self.history_button.setMenu(self.history_menu)
        hero.addWidget(self.history_button, alignment=QtCore.Qt.AlignRight)
        main_layout.addLayout(hero)

        self.tabs = QtWidgets.QTabWidget()
        self.tabs.currentChanged.connect(self.on_tab_changed)
        main_layout.addWidget(self.tabs, 1)

        self.tab_books = QtWidgets.QWidget()
        self.tab_global = QtWidgets.QWidget()
        self.tabs.addTab(self.tab_books, "单词本")
        self.tabs.addTab(self.tab_global, "全局词库")

        self.build_books_tab()
        self.build_global_tab()
        fade_widget(self.tab_books)
        self.refresh_undo_buttons()

    def build_books_tab(self) -> None:
        layout = QtWidgets.QVBoxLayout(self.tab_books)
        filter_row = QtWidgets.QHBoxLayout()
        filter_row.addWidget(QtWidgets.QLabel("按标签筛选:"))
        self.tag_filter = QtWidgets.QComboBox()
        self.tag_filter.currentIndexChanged.connect(self.refresh_book_table)
        filter_row.addWidget(self.tag_filter)
        filter_row.addStretch()
        layout.addLayout(filter_row)

        btn_row = QtWidgets.QHBoxLayout()
        add_btn = accent_button("添加单词本", "#7cf29c")
        add_btn.clicked.connect(self.add_book)
        edit_btn = accent_button("编辑", "#7ad7f0")
        edit_btn.clicked.connect(self.edit_book)
        del_btn = accent_button("删除", "#ff6b6b")
        del_btn.clicked.connect(self.delete_book)
        study_btn = accent_button("开始考察", "#ffb347")
        study_btn.clicked.connect(self.start_study)
        self.algorithm_combo = QtWidgets.QComboBox()
        self.algorithm_combo.addItem("一般算法", "normal")
        self.algorithm_combo.addItem("艾宾浩斯记忆", "ebbinghaus")
        self.algorithm_combo.addItem("科学间隔记忆", "scientific")
        self.algorithm_combo.setCurrentIndex(
            max(0, self.algorithm_combo.findData(self.datastore.settings.get("algorithm", DEFAULT_ALGORITHM)))
        )
        self.algorithm_combo.currentIndexChanged.connect(self.on_algorithm_changed)
        btn_row.addWidget(add_btn)
        btn_row.addWidget(edit_btn)
        btn_row.addWidget(del_btn)
        btn_row.addStretch()
        btn_row.addWidget(QtWidgets.QLabel("记忆框架"))
        btn_row.addWidget(self.algorithm_combo)
        btn_row.addWidget(study_btn)
        layout.addLayout(btn_row)

        self.book_table = QtWidgets.QTableWidget()
        self.book_table.setColumnCount(5)
        self.book_table.setHorizontalHeaderLabels(["名称", "类型", "标签", "单词数", "累计错误"])
        self.book_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.book_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        header = self.book_table.horizontalHeader()
        header.setSectionResizeMode(QtWidgets.QHeaderView.Stretch)
        header.setSectionsMovable(False)
        header.setSectionsClickable(False)
        vheader = self.book_table.verticalHeader()
        vheader.setSectionResizeMode(QtWidgets.QHeaderView.Fixed)
        vheader.setDefaultSectionSize(32)
        vheader.setVisible(False)
        self.book_table.doubleClicked.connect(self.open_book_preview)
        layout.addWidget(self.book_table, 1)
        self.refresh_book_table()

    def on_tab_changed(self, idx: int) -> None:
        widget = self.tabs.widget(idx)
        fade_widget(widget)

    def refresh_book_table(self) -> None:
        # update tag filter values
        tags = {"全部"}
        for b in self.datastore.books:
            for t in b.tags:
                tags.add(t)
        current_tag = self.tag_filter.currentText() if hasattr(self, "tag_filter") else "全部"
        self.tag_filter.blockSignals(True)
        self.tag_filter.clear()
        for t in sorted(tags):
            self.tag_filter.addItem(t)
        if current_tag in tags:
            idx = sorted(tags).index(current_tag)
            self.tag_filter.setCurrentIndex(idx)
        self.tag_filter.blockSignals(False)

        selected_tag = self.tag_filter.currentText() if hasattr(self, "tag_filter") else "全部"
        books = [b for b in self.datastore.books if selected_tag == "全部" or selected_tag in b.tags]
        self.book_table.setRowCount(len(books))
        for row, b in enumerate(books):
            item_name = QtWidgets.QTableWidgetItem(b.name)
            item_name.setData(QtCore.Qt.UserRole, b.id)
            self.book_table.setItem(row, 0, item_name)
            self.book_table.setItem(row, 1, QtWidgets.QTableWidgetItem(b.book_type))
            self.book_table.setItem(row, 2, QtWidgets.QTableWidgetItem(",".join(b.tags)))
            self.book_table.setItem(row, 3, QtWidgets.QTableWidgetItem(str(len(b.entries))))
            total_err = sum(e.errors for e in b.entries)
            self.book_table.setItem(row, 4, QtWidgets.QTableWidgetItem(str(total_err)))
        self.refresh_undo_buttons()

    def current_book(self) -> Optional[WordBook]:
        rows = self.book_table.selectionModel().selectedRows()
        if not rows:
            show_toast(self, "请选择单词本")
            return None
        row = rows[0].row()
        bid = self.book_table.item(row, 0).data(QtCore.Qt.UserRole)
        for b in self.datastore.books:
            if b.id == bid:
                return b
        show_toast(self, "未找到单词本")
        return None

    def open_book_preview(self) -> None:
        book = self.current_book()
        if not book:
            return
        dlg = BookPreviewDialog(self.datastore, book, self)
        dlg.exec_()
        self.refresh_book_table()

    def add_book(self) -> None:
        dlg = BookEditorDialog(self.datastore, None, self)
        if dlg.exec_():
            self.refresh_book_table()

    def edit_book(self) -> None:
        book = self.current_book()
        if not book:
            return
        dlg = BookEditorDialog(self.datastore, book, self)
        if dlg.exec_():
            self.refresh_book_table()

    def delete_book(self) -> None:
        book = self.current_book()
        if not book:
            return
        if not confirm_dialog(self, f"删除单词本“{book.name}”？"):
            return
        self.datastore.books = [b for b in self.datastore.books if b.id != book.id]
        self.datastore.clear_book_study_states(book.id)
        self.datastore.save(label="删除单词本")
        self.refresh_book_table()

    def start_study(self) -> None:
        book = self.current_book()
        if not book:
            return
        algorithm = self.algorithm_combo.currentData() or DEFAULT_ALGORITHM
        state = self.datastore.get_study_state(book.id, algorithm)
        fallback_state = self.datastore.get_alternate_study_state(book.id, algorithm)
        win = StudyWindow(self.datastore, book, state, fallback_state, self)
        win.exec_()
        self.refresh_book_table()

    def on_algorithm_changed(self) -> None:
        self.datastore.set_algorithm(self.algorithm_combo.currentData() or DEFAULT_ALGORITHM)

    def build_global_tab(self) -> None:
        layout = QtWidgets.QVBoxLayout(self.tab_global)
        top = QtWidgets.QHBoxLayout()
        self.search_mode_combo = QtWidgets.QComboBox()
        self.search_mode_combo.addItem("按单词", "word")
        self.search_mode_combo.addItem("按释义", "meaning")
        self.search_mode_combo.addItem("单词+释义", "both")
        self.search_mode_combo.currentIndexChanged.connect(self.on_global_search_mode_changed)
        self.search_global = QtWidgets.QLineEdit()
        self.search_global.setPlaceholderText("输入单词或释义搜索")
        self.search_global.textChanged.connect(self.refresh_global_table)
        top.addWidget(self.search_mode_combo)
        top.addWidget(self.search_global)
        self.global_type_combo = QtWidgets.QComboBox()
        self.global_tag_combo = QtWidgets.QComboBox()
        self.global_type_combo.currentIndexChanged.connect(self.on_global_type_changed)
        self.global_tag_combo.currentIndexChanged.connect(self.on_global_tag_changed)
        top.addWidget(QtWidgets.QLabel("类型"))
        top.addWidget(self.global_type_combo)
        top.addWidget(QtWidgets.QLabel("标签"))
        top.addWidget(self.global_tag_combo)
        top.addStretch()
        layout.addLayout(top)

        self.global_table = QtWidgets.QTableWidget()
        self.global_table.setColumnCount(4)
        self.global_table.setHorizontalHeaderLabels(["单词", "释义", "错误次数", "所在单词本"])
        self.global_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.global_table.setSelectionMode(QtWidgets.QAbstractItemView.MultiSelection)
        self.global_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.global_table.cellDoubleClicked.connect(self.edit_global_cell)
        header = self.global_table.horizontalHeader()
        header.setSectionResizeMode(QtWidgets.QHeaderView.Stretch)
        header.setSectionsMovable(False)
        header.setSectionsClickable(True)
        header.sectionClicked.connect(self.on_global_header_clicked)
        header.setSortIndicatorShown(True)
        vheader = self.global_table.verticalHeader()
        vheader.setSectionResizeMode(QtWidgets.QHeaderView.Fixed)
        vheader.setDefaultSectionSize(32)
        vheader.setVisible(False)
        self.global_table.itemSelectionChanged.connect(self.sync_global_selection)
        layout.addWidget(self.global_table, 1)

        btn_row = QtWidgets.QHBoxLayout()
        add_btn = accent_button("添加所选到单词本/新建", "#7cf29c")
        add_btn.clicked.connect(self.add_selected_global)
        del_btn = accent_button("删除选中", "#ff6b6b")
        del_btn.clicked.connect(self.delete_selected_global)
        refresh_btn = accent_button("刷新", "#7ad7f0")
        refresh_btn.clicked.connect(self.refresh_global_table)
        btn_row.addWidget(add_btn)
        btn_row.addWidget(del_btn)
        btn_row.addWidget(refresh_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self.refresh_global_table()

    def on_global_search_mode_changed(self) -> None:
        self.global_search_mode = self.search_mode_combo.currentData() or "word"
        self.refresh_global_table()

    def on_global_type_changed(self) -> None:
        self.global_type_filter = self.global_type_combo.currentText() or "全部类型"
        self.refresh_global_table()

    def on_global_tag_changed(self) -> None:
        self.global_tag_filter = self.global_tag_combo.currentText() or "全部标签"
        self.refresh_global_table()

    def on_global_header_clicked(self, section: int) -> None:
        if section == 0:
            self.toggle_global_sort("word")
        elif section == 2:
            self.toggle_global_sort("errors")
        elif section == 3:
            self.show_book_filter_menu()

    def toggle_global_sort(self, key: str) -> None:
        if self.global_sort_key == key:
            self.global_sort_order = (
                QtCore.Qt.DescendingOrder
                if self.global_sort_order == QtCore.Qt.AscendingOrder
                else QtCore.Qt.AscendingOrder
            )
        else:
            self.global_sort_key = key
            self.global_sort_order = QtCore.Qt.DescendingOrder if key == "errors" else QtCore.Qt.AscendingOrder
        self.refresh_global_table()

    def show_book_filter_menu(self) -> None:
        menu = QtWidgets.QMenu(self)
        for b in self.datastore.books:
            act = QtWidgets.QAction(f"{b.name} ({b.book_type})", menu)
            act.setCheckable(True)
            act.setData(b.id)
            act.setChecked(b.id in self.global_book_filter)
            act.toggled.connect(lambda checked, bid=b.id: self._toggle_book_filter(bid, checked))
            menu.addAction(act)
        menu.addSeparator()
        clear_act = QtWidgets.QAction("清空筛选", menu)
        clear_act.triggered.connect(lambda: self.global_book_filter.clear())
        menu.addAction(clear_act)
        header = self.global_table.horizontalHeader()
        x = header.sectionViewportPosition(3) + header.sectionSize(3) // 2
        pos = header.mapToGlobal(QtCore.QPoint(x, header.height()))
        menu.aboutToHide.connect(self.refresh_global_table)
        menu.exec_(pos)

    def _toggle_book_filter(self, bid: str, checked: bool) -> None:
        if checked:
            self.global_book_filter.add(bid)
        else:
            self.global_book_filter.discard(bid)

    def refresh_global_table(self) -> None:
        self._update_global_filter_options()
        keyword = self.search_global.text().strip().lower()
        mode = self.search_mode_combo.currentData() or "word"
        self.global_search_mode = mode
        records = self._collect_global_records()
        filtered: List[dict] = []
        for rec in records:
            meaning_lower = rec["meaning"].lower() if rec["meaning"] else ""
            if keyword:
                if mode == "word" and keyword not in rec["word"]:
                    continue
                if mode == "meaning" and keyword not in meaning_lower:
                    continue
                if mode == "both" and (keyword not in rec["word"] and keyword not in meaning_lower):
                    continue
            if self.global_book_filter and not (rec["book_ids"] & self.global_book_filter):
                continue
            if self.global_type_filter != "全部类型" and self.global_type_filter not in rec["book_types"]:
                continue
            if self.global_tag_filter != "全部标签" and self.global_tag_filter not in rec["tags"]:
                continue
            filtered.append(rec)
        filtered = self._sort_global_records(filtered)
        self._applying_global_selection = True
        try:
            self.global_table.setRowCount(len(filtered))
            for row, rec in enumerate(filtered):
                self.global_table.setItem(row, 0, QtWidgets.QTableWidgetItem(rec["word"]))
                self.global_table.setItem(row, 1, QtWidgets.QTableWidgetItem(rec["meaning"]))
                err_item = QtWidgets.QTableWidgetItem(str(rec["errors"]))
                err_item.setData(QtCore.Qt.UserRole, rec["word"])
                self.global_table.setItem(row, 2, err_item)
                self.global_table.setItem(row, 3, QtWidgets.QTableWidgetItem(", ".join(rec["book_names"])))
                self.global_table.setRowHeight(row, 32)
        finally:
            self._applying_global_selection = False
        self._apply_global_sort_indicator()
        self._apply_global_selection()
        self.refresh_undo_buttons()

    def _update_global_filter_options(self) -> None:
        types = {"全部类型"}
        tags = {"全部标签"}
        valid_book_ids = {b.id for b in self.datastore.books}
        self.global_book_filter &= valid_book_ids
        for b in self.datastore.books:
            types.add(b.book_type)
            for t in b.tags:
                tags.add(t)
        cur_type = self.global_type_filter
        cur_tag = self.global_tag_filter
        self.global_type_combo.blockSignals(True)
        self.global_tag_combo.blockSignals(True)
        self.global_type_combo.clear()
        self.global_tag_combo.clear()
        for t in sorted(types):
            self.global_type_combo.addItem(t)
        for t in sorted(tags):
            self.global_tag_combo.addItem(t)
        if cur_type in types:
            self.global_type_combo.setCurrentText(cur_type)
        else:
            self.global_type_combo.setCurrentText("全部类型")
            self.global_type_filter = "全部类型"
        if cur_tag in tags:
            self.global_tag_combo.setCurrentText(cur_tag)
        else:
            self.global_tag_combo.setCurrentText("全部标签")
            self.global_tag_filter = "全部标签"
        self.global_type_combo.blockSignals(False)
        self.global_tag_combo.blockSignals(False)

    def _collect_global_records(self) -> List[dict]:
        records: List[dict] = []
        for word, err_val in self.datastore.global_errors.items():
            books: List[WordBook] = []
            tags: Set[str] = set()
            types: Set[str] = set()
            meaning = self.datastore.global_words.get(word, "")
            for b in self.datastore.books:
                entry = next((e for e in b.entries if e.word.lower() == word), None)
                if entry:
                    books.append(b)
                    tags.update(b.tags)
                    types.add(b.book_type)
                    if not meaning:
                        meaning = entry.meaning
            records.append(
                {
                    "word": word,
                    "meaning": meaning,
                    "errors": err_val,
                    "books": books,
                    "book_ids": {b.id for b in books},
                    "book_names": [b.name for b in books],
                    "book_types": types,
                    "tags": tags,
                }
            )
        return records

    def _sort_global_records(self, records: List[dict]) -> List[dict]:
        reverse = self.global_sort_order == QtCore.Qt.DescendingOrder
        key = self.global_sort_key
        if key == "errors":
            return sorted(records, key=lambda r: (r["errors"], r["word"]), reverse=reverse)
        return sorted(records, key=lambda r: r["word"], reverse=reverse)

    def _apply_global_sort_indicator(self) -> None:
        header = self.global_table.horizontalHeader()
        labels = ["单词", "释义", "错误次数", "所在单词本"]
        if self.global_book_filter:
            labels[3] = f"所在单词本（已筛选{len(self.global_book_filter)}）"
        self.global_table.setHorizontalHeaderLabels(labels)
        if self.global_sort_key == "word":
            header.setSortIndicator(0, self.global_sort_order)
            header.setSortIndicatorShown(True)
        elif self.global_sort_key == "errors":
            header.setSortIndicator(2, self.global_sort_order)
            header.setSortIndicatorShown(True)
        else:
            header.setSortIndicatorShown(False)

    def _apply_global_selection(self) -> None:
        self._applying_global_selection = True
        self.global_table.clearSelection()
        model = self.global_table.selectionModel()
        if model:
            for row in range(self.global_table.rowCount()):
                item = self.global_table.item(row, 0)
                if not item:
                    continue
                if item.text().lower() in self.selected_global_words:
                    self.global_table.selectRow(row)
        self._applying_global_selection = False

    def sync_global_selection(self) -> None:
        if self._applying_global_selection:
            return
        visible_words: Set[str] = set()
        selected_visible: Set[str] = set()
        model = self.global_table.selectionModel()
        if not model:
            return
        for row in range(self.global_table.rowCount()):
            item = self.global_table.item(row, 0)
            if not item:
                continue
            word = item.text().lower()
            visible_words.add(word)
            if model.isRowSelected(row, QtCore.QModelIndex()):
                selected_visible.add(word)
        self.selected_global_words = (self.selected_global_words - visible_words) | selected_visible

    def edit_global_cell(self, row: int, column: int) -> None:
        if row < 0:
            return
        word_item = self.global_table.item(row, 0)
        meaning_item = self.global_table.item(row, 1)
        if not word_item:
            return
        old_word = word_item.text()
        old_meaning = meaning_item.text() if meaning_item else ""
        key = old_word.lower()

        if column == 3:
            books = [b for b in self.datastore.books if any(e.word.lower() == key for e in b.entries)]
            dlg = QtWidgets.QDialog(self)
            dlg.setWindowTitle("所在单词本")
            lay = QtWidgets.QVBoxLayout(dlg)
            list_widget = QtWidgets.QListWidget()
            for b in books:
                item = QtWidgets.QListWidgetItem(f"{b.name} ({b.book_type})")
                item.setData(QtCore.Qt.UserRole, b.id)
                list_widget.addItem(item)
            lay.addWidget(list_widget)
            style_popup(dlg)

            def open_selected(item: QtWidgets.QListWidgetItem) -> None:
                bid = item.data(QtCore.Qt.UserRole)
                book = next((bk for bk in self.datastore.books if bk.id == bid), None)
                if book:
                    dlg.accept()
                    prev = BookPreviewDialog(self.datastore, book, self)
                    prev.exec_()

            list_widget.itemDoubleClicked.connect(open_selected)
            dlg.exec_()
            return

        if column == 2:
            old_err = self.datastore.global_errors.get(key, 0)
            spin = QtWidgets.QSpinBox(self.global_table)
            spin.setRange(0, 999)
            spin.setValue(old_err)
            spin.setFrame(False)
            self.global_table.setCellWidget(row, 2, spin)
            spin.setFocus()

            def finish_err():
                self.datastore.global_errors[key] = spin.value()
                for b in self.datastore.books:
                    for e in b.entries:
                        if e.word.lower() == key:
                            e.errors = spin.value()
                self.datastore.save(label="修改错误次数")
                self.global_table.removeCellWidget(row, 2)
                self.refresh_book_table()
                self.refresh_global_table()

            spin.editingFinished.connect(finish_err)
            return

        if column in (0, 1):
            edit = QtWidgets.QDialog(self)
            edit.setWindowTitle("编辑单词/释义")
            container = QtWidgets.QVBoxLayout(edit)
            form = QtWidgets.QFormLayout()
            container.addLayout(form)
            word_edit = QtWidgets.QLineEdit(old_word)
            meaning_edit = QtWidgets.QLineEdit(old_meaning)
            form.addRow("单词", word_edit)
            form.addRow("释义", meaning_edit)
            btns = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
            form.addRow(btns)
            style_popup(edit)

            def apply_changes():
                new_word = word_edit.text().strip()
                new_meaning = meaning_edit.text().strip()
                if not new_word or not new_meaning:
                    show_toast(self, "单词与释义不能为空")
                    return
                self.datastore.rename_or_update_word(old_word, new_word, new_meaning)
                self.datastore.ensure_global_word(
                    new_word, new_meaning, self.datastore.global_errors.get(old_word.lower(), 0)
                )
                if key in self.selected_global_words:
                    self.selected_global_words.discard(key)
                    self.selected_global_words.add(new_word.lower())
                self.datastore.save(label="编辑单词/释义")
                edit.accept()
                self.refresh_book_table()
                self.refresh_global_table()

            btns.accepted.connect(apply_changes)
            btns.rejected.connect(edit.reject)
            edit.exec_()

    def add_selected_global(self) -> None:
        words = sorted(self.selected_global_words)
        if not words:
            show_toast(self, "请选择单词（支持单击或拖拽多选）。")
            return
        word_lookup = {}
        for b in self.datastore.books:
            for e in b.entries:
                key = e.word.lower()
                if key not in word_lookup:
                    word_lookup[key] = e
        for key, meaning in self.datastore.global_words.items():
            if key not in word_lookup:
                word_lookup[key] = WordEntry(key, meaning, self.datastore.global_errors.get(key, 0))
        dialog = AddToBookDialog(self.datastore, words, word_lookup, self)
        dialog.exec_()
        self.refresh_book_table()
        self.refresh_global_table()

    def delete_selected_global(self) -> None:
        words = sorted(self.selected_global_words)
        if not words:
            show_toast(self, "请选择要删除的单词（支持单击或拖拽多选）。")
            return
        if not confirm_dialog(self, f"确认删除选中的 {len(words)} 个单词？"):
            return
        self.datastore.delete_words(words, label="删除单词")
        for w in words:
            self.selected_global_words.discard(w.lower())
        self.refresh_book_table()
        self.refresh_global_table()

    def undo_action(self) -> None:
        label = self.datastore.undo()
        if not label:
            show_toast(self, "没有可撤销的操作")
            self.refresh_undo_buttons()
            return
        self.refresh_book_table()
        self.refresh_global_table()
        show_toast(self, f"已撤销：{label}")

    def redo_action(self) -> None:
        label = self.datastore.redo()
        if not label:
            show_toast(self, "没有可恢复的操作")
            self.refresh_undo_buttons()
            return
        self.refresh_book_table()
        self.refresh_global_table()
        show_toast(self, f"已恢复：{label}")

    def refresh_undo_buttons(self) -> None:
        if hasattr(self, "action_undo"):
            self.action_undo.setEnabled(self.datastore.can_undo())
        if hasattr(self, "action_redo"):
            self.action_redo.setEnabled(self.datastore.can_redo())

    def apply_theme(self) -> None:
        QtWidgets.QApplication.instance().setPalette(themed_palette())
        corner_radius = 12
        header_bg = "#e8ecf3"
        header_fg = "#1f2937"
        menu_bg = "#ffffff"
        menu_fg = "#1f2937"
        sheet = f"""
        QWidget#card {{
            border-radius: {corner_radius}px;
        }}
        QTabWidget::pane {{
            border: 1px solid #d7dce6;
            border-radius: {corner_radius}px;
            padding: 6px;
            background: palette(base);
        }}
        QTabBar::tab {{
            padding: 10px 16px;
            border-radius: 10px;
            margin: 4px;
            background: #e9edf4;
            color: #1f2937;
        }}
        QTabBar::tab:selected {{
            background: #d5deed;
            color: #0b1224;
            font-weight: 700;
        }}
        QTableWidget {{
            border-radius: {corner_radius}px;
            padding: 6px;
            gridline-color: #d7dce6;
        }}
        QHeaderView::section {{
            background: {header_bg};
            color: {header_fg};
            padding: 6px;
            border: none;
        }}
        QTableCornerButton::section {{
            background: {header_bg};
            border: none;
        }}
        QMenu, QToolTip {{
            background: {menu_bg};
            color: {menu_fg};
            border: 1px solid #d7dce6;
            padding: 6px;
        }}
        """
        self.setStyleSheet(sheet)


def main() -> None:
    app = QtWidgets.QApplication(sys.argv)
    app.setFont(QtGui.QFont("Calibri"))
    base_dir = APP_DIR
    icon_path = os.path.join(base_dir, "word_master.ico")
    splash_path = SPLASH_IMAGE
    if os.path.exists(icon_path):
        app.setWindowIcon(QtGui.QIcon(icon_path))
    datastore = DataStore(os.path.join(base_dir, DATA_FILE))
    window = MainWindow(datastore)
    window.start_splash(splash_path)
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
